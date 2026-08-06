"""Deterministic standard-library extractors and one lazy PDF profile."""

from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from importlib import import_module
from io import BytesIO
from typing import Any
from xml.etree import ElementTree

from docspec.domain.content import CapturedFile, EvidenceCoordinate, EvidenceMapping, Representation
from docspec.domain.identity import (
    freeze_json,
    identity_digest,
    require_sha256,
    require_text,
    thaw_json,
)
from docspec.errors import IntegrityError
from docspec.processing.artifacts import (
    IDENTITY_TRANSFORM,
    PDF_PAGE_TEXT_TRANSFORM,
    DerivedEvidenceResolver,
    RepresentationPayload,
    content_blob_ref,
    decode_utf8,
    verify_blob_bytes,
    verify_representation_evidence,
)
from docspec.processing.json_tools import strict_json_value

TEXT_EXTRACTOR_ID = "docspec.text-source/v1"
HTML_EXTRACTOR_ID = "docspec.html-source/v1"
XML_EXTRACTOR_ID = "docspec.xml-source/v1"
JSON_EXTRACTOR_ID = "docspec.json-source/v1"
IMAGE_EXTRACTOR_ID = "docspec.image-passthrough/v1"
DEFAULT_EXTRACTOR_REGISTRY_ID = "docspec.default-extractors/v1"
EXTRACTION_RECEIPT_FORMAT = "docspec-extraction-receipt"
EXTRACTION_RECEIPT_FORMAT_VERSION = "1.0"


class ExtractionError(IntegrityError):
    """A file cannot produce a verified representation under this profile."""


@dataclass(frozen=True, slots=True)
class ExtractionReceipt:
    """Recomputable evidence for one extractor invocation."""

    extractor_id: str
    configuration_digest: str
    file_id: str
    input_digest: str
    representation_id: str
    output_digest: str
    output_byte_size: int
    kind: str
    metadata: Mapping[str, Any]
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (
            ("extractor_id", self.extractor_id),
            ("file_id", self.file_id),
            ("representation_id", self.representation_id),
            ("kind", self.kind),
        ):
            require_text(value, f"extraction receipt {label}")
        require_sha256(self.configuration_digest, "extraction receipt configuration_digest")
        require_sha256(self.input_digest, "extraction receipt input_digest")
        require_sha256(self.output_digest, "extraction receipt output_digest")
        if type(self.output_byte_size) is not int or self.output_byte_size < 0:
            raise ValueError("extraction output byte size must be non-negative")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("extraction receipt metadata must be an object")
        object.__setattr__(self, "metadata", freeze_json(self.metadata, label="extraction receipt metadata"))
        if not isinstance(self.warnings, tuple):
            raise ValueError("extraction receipt warnings must be an immutable tuple")
        for warning in self.warnings:
            require_text(warning, "extraction receipt warning")

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": EXTRACTION_RECEIPT_FORMAT,
            "formatVersion": EXTRACTION_RECEIPT_FORMAT_VERSION,
            "extractorId": self.extractor_id,
            "configurationDigest": self.configuration_digest,
            "fileId": self.file_id,
            "inputDigest": self.input_digest,
            "representationId": self.representation_id,
            "outputDigest": self.output_digest,
            "outputByteSize": self.output_byte_size,
            "kind": self.kind,
            "metadata": thaw_json(self.metadata),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExtractionReceipt:
        expected = {
            "format",
            "formatVersion",
            "extractorId",
            "configurationDigest",
            "fileId",
            "inputDigest",
            "representationId",
            "outputDigest",
            "outputByteSize",
            "kind",
            "metadata",
            "warnings",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("extraction receipt has an invalid closed shape")
        if value["format"] != EXTRACTION_RECEIPT_FORMAT:
            raise ValueError("extraction receipt format is not supported")
        if value["formatVersion"] != EXTRACTION_RECEIPT_FORMAT_VERSION:
            raise ValueError("extraction receipt format version is not supported")
        metadata = value["metadata"]
        warnings = value["warnings"]
        if not isinstance(metadata, Mapping):
            raise ValueError("extraction receipt metadata must be an object")
        if not isinstance(warnings, (list, tuple)):
            raise ValueError("extraction receipt warnings must be an array")
        return cls(
            extractor_id=value["extractorId"],
            configuration_digest=value["configurationDigest"],
            file_id=value["fileId"],
            input_digest=value["inputDigest"],
            representation_id=value["representationId"],
            output_digest=value["outputDigest"],
            output_byte_size=value["outputByteSize"],
            kind=value["kind"],
            metadata=metadata,
            warnings=tuple(warnings),
        )

    @property
    def receipt_digest(self) -> str:
        return identity_digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """A representation payload and the receipt that proves how it was made."""

    payload: RepresentationPayload
    receipt: ExtractionReceipt

    def __post_init__(self) -> None:
        representation = self.payload.representation
        if self.receipt.extractor_id != representation.extractor_id:
            raise IntegrityError("extraction receipt names a different extractor")
        if self.receipt.configuration_digest != representation.configuration_digest:
            raise IntegrityError("extraction receipt configuration digest differs")
        if self.receipt.file_id != representation.file_id:
            raise IntegrityError("extraction receipt names a different captured file")
        if self.receipt.representation_id != representation.representation_id:
            raise IntegrityError("extraction receipt names a different representation")
        if self.receipt.input_digest != representation.file_digest:
            raise IntegrityError("extraction receipt input digest differs")
        if self.receipt.output_digest != representation.blob.digest:
            raise IntegrityError("extraction receipt output digest differs")
        if self.receipt.output_byte_size != representation.blob.byte_size:
            raise IntegrityError("extraction receipt output byte size differs")
        if self.receipt.kind != representation.kind:
            raise IntegrityError("extraction receipt representation kind differs")
        if self.receipt.warnings != representation.warnings:
            raise IntegrityError("extraction receipt warnings differ")


class TextExtractor:
    """Validate UTF-8 text and retain its exact source bytes."""

    def extract(self, captured: CapturedFile, source_bytes: bytes) -> ExtractionResult:
        text = decode_utf8(source_bytes, label="captured text")
        metadata = {
            "unicodeCodepointCount": len(text),
            "lineCount": len(text.splitlines()) if text else 0,
        }
        return _passthrough_result(captured, source_bytes, TEXT_EXTRACTOR_ID, "text", metadata)


class _HtmlFacts(HTMLParser):
    _SUPPRESSED = frozenset({"script", "style", "template", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.element_count = 0
        self.visible_parts: list[str] = []
        self._suppressed_depth = 0
        self._stack: list[tuple[str, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.casefold()
        suppressed = normalized in self._SUPPRESSED
        self.element_count += 1
        self._stack.append((normalized, suppressed))
        self._suppressed_depth += int(suppressed)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag, attrs
        self.element_count += 1

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == normalized:
                removed = self._stack[index:]
                self._suppressed_depth -= sum(int(suppressed) for _, suppressed in removed)
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._suppressed_depth == 0:
            self.visible_parts.append(data)


class HtmlExtractor:
    """Validate HTML with the standard parser and retain source-native markup."""

    def extract(self, captured: CapturedFile, source_bytes: bytes) -> ExtractionResult:
        text = decode_utf8(source_bytes, label="captured HTML")
        parser = _HtmlFacts()
        try:
            parser.feed(text)
            parser.close()
        except (AssertionError, ValueError) as error:
            raise ExtractionError(f"captured HTML cannot be parsed: {error}") from error
        visible = "".join(parser.visible_parts)
        metadata = {
            "elementCount": parser.element_count,
            "visibleUnicodeCodepointCount": len(visible),
        }
        return _passthrough_result(captured, source_bytes, HTML_EXTRACTOR_ID, "html", metadata)


class XmlExtractor:
    """Validate XML with ElementTree and retain exact source-native XML."""

    def extract(self, captured: CapturedFile, source_bytes: bytes) -> ExtractionResult:
        text = decode_utf8(source_bytes, label="captured XML")
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as error:
            raise ExtractionError(f"captured XML cannot be parsed: {error}") from error
        metadata = {
            "rootTag": root.tag,
            "elementCount": sum(1 for _ in root.iter()),
        }
        return _passthrough_result(captured, source_bytes, XML_EXTRACTOR_ID, "xml", metadata)


class JsonExtractor:
    """Validate closed JSON and retain its exact UTF-8 source bytes."""

    def extract(self, captured: CapturedFile, source_bytes: bytes) -> ExtractionResult:
        text = decode_utf8(source_bytes, label="captured JSON")
        value = strict_json_value(text, label="captured JSON")
        root_kind = "array" if isinstance(value, list) else "object" if isinstance(value, dict) else "scalar"
        metadata = {
            "rootKind": root_kind,
            "recordCount": len(value) if isinstance(value, list) else 1,
        }
        return _passthrough_result(captured, source_bytes, JSON_EXTRACTOR_ID, "json", metadata)


class ImageExtractor:
    """Retain an exact image and report header-derived metadata when available."""

    def extract(self, captured: CapturedFile, source_bytes: bytes) -> ExtractionResult:
        _verify_media_prefix(captured, "image/")
        image_format, width, height = _image_dimensions(source_bytes)
        metadata: dict[str, Any] = {"imageFormat": image_format}
        region: dict[str, Any] = {"kind": "whole-image"}
        if width is not None and height is not None:
            metadata.update({"widthPixels": width, "heightPixels": height})
            region.update({"x": 0, "y": 0, "width": width, "height": height, "unit": "pixel"})
        return _passthrough_result(
            captured,
            source_bytes,
            IMAGE_EXTRACTOR_ID,
            "image",
            metadata,
            coordinate_system="source-byte-range",
            region=region,
        )


class LazyPypdfExtractor:
    """Extract one text representation per PDF page through the optional profile.

    Importing DocSpec or this module never imports ``pypdf``. The dependency is
    resolved only when a worker actually selects this extractor.
    """

    def __init__(self, *, page_separator: str = "\n\f\n", strip_page_whitespace: bool = False) -> None:
        if not page_separator:
            raise ValueError("PDF page separator must be non-empty")
        self.page_separator = page_separator
        self.strip_page_whitespace = strip_page_whitespace

    def extract(self, captured: CapturedFile, source_bytes: bytes) -> ExtractionResult:
        _verify_captured_bytes(captured, source_bytes)
        _verify_media(captured, "application/pdf")
        pages, provider_version = self._read_pages(source_bytes)
        page_bytes = tuple(page.encode("utf-8") for page in pages)
        separator = self.page_separator.encode("utf-8")
        content = separator.join(page_bytes)
        configuration_digest = identity_digest(
            {
                "pageSeparator": self.page_separator,
                "stripPageWhitespace": self.strip_page_whitespace,
            }
        )
        extractor_id = f"docspec.pypdf/{provider_version}"
        mappings: list[EvidenceMapping] = []
        position = 0
        for page, payload in enumerate(page_bytes, start=1):
            evidence = EvidenceCoordinate(
                coordinate_system="pdf-page",
                source_digest=captured.blob.digest,
                page=page,
                region={"kind": "whole-page", "page": page},
            )
            mappings.append(EvidenceMapping(position, position + len(payload), evidence, PDF_PAGE_TEXT_TRANSFORM))
            position += len(payload) + (len(separator) if page < len(page_bytes) else 0)
        warnings = tuple(f"page {page} has no embedded text" for page, value in enumerate(pages, start=1) if not value)
        blob = content_blob_ref(content, "text/plain; charset=utf-8")
        representation = Representation.create(
            source_item_id=captured.source_item_id,
            file_id=captured.file_id,
            file_digest=captured.blob.digest,
            kind="pdf-text",
            blob=blob,
            extractor_id=extractor_id,
            configuration_digest=configuration_digest,
            evidence_mappings=tuple(mappings),
            warnings=warnings,
        )
        payload = RepresentationPayload(representation, content)
        receipt = _receipt(
            captured,
            payload,
            metadata={"pageCount": len(pages), "emptyPageCount": sum(not page for page in pages)},
        )
        result = ExtractionResult(payload, receipt)
        self.verify(result, source_bytes)
        return result

    def verify(self, result: ExtractionResult, source_bytes: bytes) -> None:
        """Re-run the named parser and prove every page mapping."""

        pages, provider_version = self._read_pages(source_bytes)
        expected_id = f"docspec.pypdf/{provider_version}"
        if result.payload.representation.extractor_id != expected_id:
            raise IntegrityError("PDF representation names a different parser version")
        verify_representation_evidence(
            result.payload,
            source_bytes,
            derived_resolver=self.evidence_resolver(source_bytes, pages=pages),
        )

    def evidence_resolver(
        self,
        source_bytes: bytes,
        *,
        pages: tuple[str, ...] | None = None,
    ) -> DerivedEvidenceResolver:
        """Return a resolver suitable for the shared representation/segment verifier."""

        resolved_pages = pages if pages is not None else self._read_pages(source_bytes)[0]
        encoded_pages = tuple(page.encode("utf-8") for page in resolved_pages)

        def resolve(mapping: EvidenceMapping, _: bytes) -> bytes:
            page = mapping.evidence.page
            if page is None or page > len(encoded_pages):
                raise IntegrityError("PDF evidence names a page outside the captured file")
            return encoded_pages[page - 1]

        return resolve

    def _read_pages(self, source_bytes: bytes) -> tuple[tuple[str, ...], str]:
        try:
            provider = import_module("pypdf")
        except (ImportError, ModuleNotFoundError) as error:
            raise ExtractionError("the pypdf extraction profile requires the docspec[pdf] extra") from error
        provider_version = str(getattr(provider, "__version__", "unknown"))
        try:
            reader = provider.PdfReader(BytesIO(source_bytes), strict=False)
            if bool(getattr(reader, "is_encrypted", False)):
                raise ExtractionError("encrypted PDF requires an explicit decryption profile")
            extracted = tuple((page.extract_text() or "") for page in reader.pages)
        except ExtractionError:
            raise
        except Exception as error:  # optional provider failures are normalized at this boundary
            raise ExtractionError(f"pypdf cannot extract the captured PDF: {error}") from error
        if self.strip_page_whitespace:
            extracted = tuple(page.strip() for page in extracted)
        return extracted, provider_version


class DefaultExtractorRegistry:
    """Dispatch common media types without exposing a provider type."""

    extractor_id = DEFAULT_EXTRACTOR_REGISTRY_ID

    def __init__(self, *, pdf: Any | None = None) -> None:
        self._text = TextExtractor()
        self._html = HtmlExtractor()
        self._xml = XmlExtractor()
        self._json = JsonExtractor()
        self._image = ImageExtractor()
        self._pdf = pdf or LazyPypdfExtractor()

    def extract(self, captured: CapturedFile, source_bytes: bytes) -> ExtractionResult:
        media_type = _base_media_type(captured.media_type)
        if media_type == "text/html":
            extractor = self._html
        elif media_type in {"application/xml", "text/xml"} or media_type.endswith("+xml"):
            extractor = self._xml
        elif media_type == "application/json" or media_type.endswith("+json"):
            extractor = self._json
        elif media_type == "application/pdf":
            extractor = self._pdf
        elif media_type.startswith("image/"):
            extractor = self._image
        elif media_type.startswith("text/"):
            extractor = self._text
        else:
            raise ExtractionError(f"no extractor is registered for media type {captured.media_type!r}")
        return extractor.extract(captured, source_bytes)


def _passthrough_result(
    captured: CapturedFile,
    source_bytes: bytes,
    extractor_id: str,
    kind: str,
    metadata: Mapping[str, Any],
    *,
    coordinate_system: str = "utf8-byte-range",
    region: Mapping[str, Any] | None = None,
) -> ExtractionResult:
    _verify_captured_bytes(captured, source_bytes)
    configuration_digest = identity_digest({"mode": "source-native-passthrough"})
    evidence = EvidenceCoordinate(
        coordinate_system=coordinate_system,
        source_digest=captured.blob.digest,
        start=0,
        end=len(source_bytes),
        region=dict(region) if region is not None else None,
    )
    representation = Representation.create(
        source_item_id=captured.source_item_id,
        file_id=captured.file_id,
        file_digest=captured.blob.digest,
        kind=kind,
        blob=captured.blob,
        extractor_id=extractor_id,
        configuration_digest=configuration_digest,
        evidence_mappings=(EvidenceMapping(0, len(source_bytes), evidence, IDENTITY_TRANSFORM),),
    )
    payload = RepresentationPayload(representation, source_bytes)
    receipt = _receipt(captured, payload, metadata=metadata)
    result = ExtractionResult(payload, receipt)
    verify_representation_evidence(result.payload, source_bytes)
    return result


def _receipt(
    captured: CapturedFile,
    payload: RepresentationPayload,
    *,
    metadata: Mapping[str, Any],
) -> ExtractionReceipt:
    representation = payload.representation
    return ExtractionReceipt(
        extractor_id=representation.extractor_id,
        configuration_digest=representation.configuration_digest,
        file_id=captured.file_id,
        input_digest=captured.blob.digest,
        representation_id=representation.representation_id,
        output_digest=representation.blob.digest,
        output_byte_size=representation.blob.byte_size,
        kind=representation.kind,
        metadata=metadata,
        warnings=representation.warnings,
    )


def _verify_captured_bytes(captured: CapturedFile, source_bytes: bytes) -> None:
    verify_blob_bytes(captured.blob, source_bytes, label="captured file")
    if _base_media_type(captured.media_type) != _base_media_type(captured.blob.media_type):
        raise IntegrityError("captured file media type differs from its blob reference")


def _verify_media(captured: CapturedFile, expected: str) -> None:
    if _base_media_type(captured.media_type) != expected:
        raise ExtractionError(f"extractor requires {expected}, received {captured.media_type}")


def _verify_media_prefix(captured: CapturedFile, expected_prefix: str) -> None:
    if not _base_media_type(captured.media_type).startswith(expected_prefix):
        raise ExtractionError(f"extractor requires {expected_prefix}*, received {captured.media_type}")


def _base_media_type(value: str) -> str:
    return value.partition(";")[0].strip().casefold()


def _image_dimensions(content: bytes) -> tuple[str, int | None, int | None]:
    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 24:
        return "png", int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big")
    if content[:6] in {b"GIF87a", b"GIF89a"} and len(content) >= 10:
        return "gif", int.from_bytes(content[6:8], "little"), int.from_bytes(content[8:10], "little")
    if content.startswith(b"\xff\xd8"):
        dimensions = _jpeg_dimensions(content)
        return ("jpeg", *dimensions) if dimensions is not None else ("jpeg", None, None)
    return "unknown", None, None


def _jpeg_dimensions(content: bytes) -> tuple[int, int] | None:
    position = 2
    start_of_frame = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
    while position + 4 <= len(content):
        if content[position] != 0xFF:
            position += 1
            continue
        marker = content[position + 1]
        position += 2
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        length = struct.unpack(">H", content[position : position + 2])[0]
        if length < 2 or position + length > len(content):
            return None
        if marker in start_of_frame and length >= 7:
            height, width = struct.unpack(">HH", content[position + 3 : position + 7])
            return width, height
        position += length
    return None
