"""Retain source files and map shared reader observations into representations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as distribution_version
from typing import Any

from docspec.domain.content import CapturedFile, EvidenceCoordinate, EvidenceMapping, Representation
from docspec.domain.identity import (
    freeze_json,
    identity_digest,
    require_sha256,
    require_text,
    thaw_json,
)
from docspec.errors import IntegrityError
from docspec.ports.extractor import Extractor
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
from docspec.processing.reader_identity import (
    IMAGE_MODULES,
    JSON_MODULES,
    MARKUP_MODULES,
    PDF_MODULES,
    installed_reader_identity,
    reader_configuration,
    require_reader_identity,
)
from docspec.processing.source_profiles import JSON_SOURCE_PROFILE

TEXT_EXTRACTOR_ID = "docspec.text-source/v1"
HTML_EXTRACTOR_ID = "docspec.html-source/v2"
XML_EXTRACTOR_ID = "docspec.xml-source/v2"
JSON_EXTRACTOR_ID = "docspec.json-source/v2"
IMAGE_EXTRACTOR_ID = "docspec.image-passthrough/v2"
DEFAULT_EXTRACTOR_REGISTRY_ID = "docspec.default-extractors/v1"
PYPDF_EXTRACTOR_ID = "docspec.pypdf-adapter/v2"
EXTRACTION_RECEIPT_FORMAT = "docspec-extraction-receipt"
EXTRACTION_RECEIPT_FORMAT_VERSION = "1.0"
_SOURCE_NATIVE_CONFIGURATION_DIGEST = identity_digest({"mode": "source-native-passthrough"})


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

    extractor_id = TEXT_EXTRACTOR_ID
    configuration_digest = _SOURCE_NATIVE_CONFIGURATION_DIGEST

    def selected_identity(self, captured_file: CapturedFile) -> tuple[str, str]:
        return self.extractor_id, self.configuration_digest

    def extract(self, captured: CapturedFile, source_bytes: bytes) -> ExtractionResult:
        text = decode_utf8(source_bytes, label="captured text")
        metadata = {
            "unicodeCodepointCount": len(text),
            "lineCount": len(text.splitlines()) if text else 0,
        }
        return _passthrough_result(
            captured, source_bytes, self.extractor_id, self.configuration_digest, "text", metadata
        )


def _html_visible_count(events) -> int:
    suppressed_tags = frozenset({"script", "style", "template", "noscript"})
    stack: list[tuple[str, bool]] = []
    positions: dict[str, list[int]] = {}
    suppressed_depth = 0
    count = 0
    for event in events:
        if event.kind == "start":
            suppressed = event.name in suppressed_tags
            positions.setdefault(event.name, []).append(len(stack))
            stack.append((event.name, suppressed))
            suppressed_depth += int(suppressed)
        elif event.kind == "end":
            matches = positions.get(event.name)
            if matches:
                # Each entry is pushed/popped once; unmatched end tags stay O(1)
                # even when malformed HTML leaves many void starts on the stack.
                index = matches[-1]
                while len(stack) > index:
                    name, flag = stack.pop()
                    positions[name].pop()
                    suppressed_depth -= int(flag)
        elif event.kind == "text" and suppressed_depth == 0:
            count += len(event.text)
    return count


class HtmlExtractor:
    """Read HTML observations through SpicyDocs and retain source-native markup."""

    extractor_id = HTML_EXTRACTOR_ID

    def __init__(self) -> None:
        self._reader_identity = installed_reader_identity(MARKUP_MODULES)
        self.configuration_digest = identity_digest(
            {
                "mode": "source-native-passthrough",
                **reader_configuration(self._reader_identity),
            }
        )

    def selected_identity(self, captured_file: CapturedFile) -> tuple[str, str]:
        require_reader_identity(self._reader_identity, MARKUP_MODULES)
        return self.extractor_id, self.configuration_digest

    def extract(self, captured: CapturedFile, source_bytes: bytes) -> ExtractionResult:
        self.selected_identity(captured)
        from spicy_docs.reading.markup import read_html_events

        try:
            observed = read_html_events(source_bytes)
        except ValueError as error:
            raise ExtractionError(f"captured HTML cannot be parsed: {error}") from error
        metadata = {
            "elementCount": observed.element_count,
            "visibleUnicodeCodepointCount": _html_visible_count(observed.events),
        }
        return _passthrough_result(
            captured, source_bytes, self.extractor_id, self.configuration_digest, "html", metadata
        )


class XmlExtractor:
    """Read XML observations through SpicyDocs and retain exact source-native XML."""

    extractor_id = XML_EXTRACTOR_ID

    def __init__(self) -> None:
        self._reader_identity = installed_reader_identity(MARKUP_MODULES)
        self.configuration_digest = identity_digest(
            {
                "mode": "source-native-passthrough",
                "allowExternalDoctype": True,
                **reader_configuration(self._reader_identity),
            }
        )

    def selected_identity(self, captured_file: CapturedFile) -> tuple[str, str]:
        require_reader_identity(self._reader_identity, MARKUP_MODULES)
        return self.extractor_id, self.configuration_digest

    def extract(self, captured: CapturedFile, source_bytes: bytes) -> ExtractionResult:
        self.selected_identity(captured)
        from spicy_docs.reading.markup import read_xml_events

        decode_utf8(source_bytes, label="captured XML")
        try:
            observed = read_xml_events(source_bytes, allow_external_doctype=True)
        except ValueError as error:
            raise ExtractionError(f"captured XML cannot be parsed: {error}") from error
        metadata = {
            "rootTag": observed.root_expanded_name,
            "elementCount": observed.element_count,
        }
        return _passthrough_result(
            captured, source_bytes, self.extractor_id, self.configuration_digest, "xml", metadata
        )


class JsonExtractor:
    """Validate closed JSON and retain its exact UTF-8 source bytes."""

    extractor_id = JSON_EXTRACTOR_ID

    def __init__(self) -> None:
        self._reader_identity = installed_reader_identity(JSON_MODULES)
        self._configuration_digest = identity_digest(
            {
                "mode": "source-native-passthrough",
                "sourceProfile": dict(JSON_SOURCE_PROFILE),
                **reader_configuration(self._reader_identity),
            }
        )

    @property
    def configuration_digest(self) -> str:
        require_reader_identity(self._reader_identity, JSON_MODULES)
        return self._configuration_digest

    def selected_identity(self, captured_file: CapturedFile) -> tuple[str, str]:
        return self.extractor_id, self.configuration_digest

    def extract(self, captured: CapturedFile, source_bytes: bytes) -> ExtractionResult:
        self.selected_identity(captured)
        from spicy_docs.reading.json_input import load_bounded_json

        try:
            value = load_bounded_json(source_bytes, source="captured", error_type=ValueError, **JSON_SOURCE_PROFILE)
        except ValueError as error:
            raise IntegrityError(str(error)) from error
        root_kind = "array" if isinstance(value, list) else "object" if isinstance(value, dict) else "scalar"
        metadata = {
            "rootKind": root_kind,
            "recordCount": len(value) if isinstance(value, list) else 1,
        }
        return _passthrough_result(
            captured, source_bytes, self.extractor_id, self.configuration_digest, "json", metadata
        )


class ImageExtractor:
    """Retain an exact image and report header-derived metadata when available."""

    extractor_id = IMAGE_EXTRACTOR_ID

    def __init__(self) -> None:
        self._reader_identity = installed_reader_identity(IMAGE_MODULES)
        self.configuration_digest = identity_digest(
            {
                "mode": "source-native-passthrough",
                **reader_configuration(self._reader_identity),
            }
        )

    def selected_identity(self, captured_file: CapturedFile) -> tuple[str, str]:
        require_reader_identity(self._reader_identity, IMAGE_MODULES)
        return self.extractor_id, self.configuration_digest

    def extract(self, captured: CapturedFile, source_bytes: bytes) -> ExtractionResult:
        self.selected_identity(captured)
        _verify_media_prefix(captured, "image/")
        from spicy_docs.reading.image_header import read_image_header

        observed = read_image_header(source_bytes)
        image_format, width, height = observed.format, observed.width, observed.height
        metadata: dict[str, Any] = {"imageFormat": image_format}
        region: dict[str, Any] = {"kind": "whole-image"}
        if width is not None and height is not None:
            metadata.update({"widthPixels": width, "heightPixels": height})
            region.update({"x": 0, "y": 0, "width": width, "height": height, "unit": "pixel"})
        return _passthrough_result(
            captured,
            source_bytes,
            self.extractor_id,
            self.configuration_digest,
            "image",
            metadata,
            coordinate_system="source-byte-range",
            region=region,
        )


def _pypdf_reader_identity() -> tuple[str, str] | None:
    """Identify the installed reader module without importing the PDF backend."""
    identity = installed_reader_identity(PDF_MODULES)
    return (identity[0], identity[1][0][1]) if identity is not None else None


class LazyPypdfExtractor:
    """Extract one text representation per PDF page through the optional profile.

    Importing DocSpec or this module never imports ``pypdf``. The dependency is
    imported only when a worker actually selects this extractor. Distribution
    metadata pins its availability and version when the stage is configured.
    """

    extractor_id = PYPDF_EXTRACTOR_ID

    def __init__(self, *, page_separator: str = "\n\f\n", strip_page_whitespace: bool = False) -> None:
        if not page_separator:
            raise ValueError("PDF page separator must be non-empty")
        self.page_separator = page_separator
        self.strip_page_whitespace = strip_page_whitespace
        self._reader_identity = _pypdf_reader_identity()
        try:
            self._provider_version: str | None = distribution_version("pypdf")
        except PackageNotFoundError:
            self._provider_version = None
        if self._provider_version is not None:
            require_text(self._provider_version, "pypdf distribution version")

    @property
    def configuration_digest(self) -> str:
        return identity_digest(
            {
                "provider": "pypdf",
                "providerVersion": self._provider_version,
                "reader": "spicy-docs.extraction.pypdf",
                "readerVersion": self._reader_identity[0] if self._reader_identity else None,
                "readerModuleSha256": self._reader_identity[1] if self._reader_identity else None,
                "available": self._provider_version is not None and self._reader_identity is not None,
                "pageSeparator": self.page_separator,
                "stripPageWhitespace": self.strip_page_whitespace,
            }
        )

    def selected_identity(self, captured_file: CapturedFile) -> tuple[str, str]:
        _verify_media(captured_file, "application/pdf")
        return f"docspec.pypdf/{self._require_provider_version()}", self.configuration_digest

    def _require_provider_version(self) -> str:
        if self._provider_version is None or self._reader_identity is None:
            raise ExtractionError("the pypdf extraction profile requires the docspec[pdf] extra")
        if _pypdf_reader_identity() != self._reader_identity:
            raise ExtractionError("installed PDF reader differs from the configured reader version or module bytes")
        return self._provider_version

    def extract(self, captured: CapturedFile, source_bytes: bytes) -> ExtractionResult:
        _verify_captured_bytes(captured, source_bytes)
        extractor_id, configuration_digest = self.selected_identity(captured)
        pages, _ = self._read_pages(source_bytes)
        page_bytes = tuple(page.encode("utf-8") for page in pages)
        separator = self.page_separator.encode("utf-8")
        content = separator.join(page_bytes)
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
        verify_representation_evidence(
            result.payload,
            source_bytes,
            derived_resolver=self.evidence_resolver(source_bytes, pages=pages),
        )
        return result

    def verify(self, result: ExtractionResult, source_bytes: bytes) -> None:
        """Re-run the named parser and prove every page mapping."""

        pages, provider_version = self._read_pages(source_bytes)
        expected_id = f"docspec.pypdf/{provider_version}"
        if result.payload.representation.extractor_id != expected_id:
            raise IntegrityError("PDF representation names a different parser version")
        if result.payload.representation.configuration_digest != self.configuration_digest:
            raise IntegrityError("PDF representation names different extraction settings")
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
        expected_version = self._require_provider_version()
        try:
            from spicy_docs.extraction.pypdf import PdfEncryptedError, PdfReadError, PypdfReader
        except (ImportError, ModuleNotFoundError) as error:
            raise ExtractionError("the pypdf extraction profile requires the docspec[pdf] extra") from error
        try:
            with PypdfReader(expected_backend_version=expected_version).open(source_bytes, password=None) as document:
                extracted = tuple(document.read_page(page) or "" for page in range(1, document.page_count + 1))
                provider_version = document.backend_version
        except PdfEncryptedError as error:
            raise ExtractionError("encrypted PDF requires an explicit decryption profile") from error
        except (PdfReadError, ValueError) as error:
            if isinstance(error.__cause__, (ImportError, PackageNotFoundError)):
                raise ExtractionError("the pypdf extraction profile requires the docspec[pdf] extra") from error
            raise ExtractionError(f"pypdf cannot extract the captured PDF: {error}") from error
        if self.strip_page_whitespace:
            extracted = tuple(page.strip() for page in extracted)
        return extracted, provider_version


class DefaultExtractorRegistry:
    """Dispatch common media types without exposing a provider type.

    The dispatcher ID versions the media-matching rules in ``_select``; its
    configuration digest binds the implementations assigned to those routes.
    """

    extractor_id = DEFAULT_EXTRACTOR_REGISTRY_ID

    def __init__(self, *, pdf: Extractor[ExtractionResult] | None = None) -> None:
        self._text = TextExtractor()
        self._html = HtmlExtractor()
        self._xml = XmlExtractor()
        self._json = JsonExtractor()
        self._image = ImageExtractor()
        self._pdf = pdf if pdf is not None else LazyPypdfExtractor()

    @property
    def configuration_digest(self) -> str:
        children = {
            "text": self._text,
            "html": self._html,
            "xml": self._xml,
            "json": self._json,
            "image": self._image,
            "pdf": self._pdf,
        }
        return identity_digest(
            {
                "dispatcher": self.extractor_id,
                "children": {
                    route: {"extractorId": child.extractor_id, "configurationDigest": child.configuration_digest}
                    for route, child in children.items()
                },
            }
        )

    def selected_identity(self, captured_file: CapturedFile) -> tuple[str, str]:
        return self._select(captured_file).selected_identity(captured_file)

    def extract(self, captured: CapturedFile, source_bytes: bytes) -> ExtractionResult:
        return self._select(captured).extract(captured, source_bytes)

    def _select(self, captured: CapturedFile) -> Extractor[ExtractionResult]:
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
        return extractor


def _passthrough_result(
    captured: CapturedFile,
    source_bytes: bytes,
    extractor_id: str,
    configuration_digest: str,
    kind: str,
    metadata: Mapping[str, Any],
    *,
    coordinate_system: str = "utf8-byte-range",
    region: Mapping[str, Any] | None = None,
) -> ExtractionResult:
    _verify_captured_bytes(captured, source_bytes)
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
