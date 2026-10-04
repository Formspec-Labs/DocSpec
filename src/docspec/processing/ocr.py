"""Retain shared page extraction through the ordinary document lifecycle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import nullcontext, closing
from dataclasses import fields, is_dataclass
from typing import Any
import json
import math

from docspec.domain.content import CapturedFile, EvidenceCoordinate, EvidenceMapping, Representation
from docspec.domain.identity import freeze_json, identity_digest, sha256_digest, thaw_json
from docspec.domain.references import BlobRef
from docspec.processing.artifacts import OCR_PAGE_TRANSFORM, RepresentationPayload, content_blob_ref, verify_blob_bytes
from docspec.errors import IntegrityError
from docspec.processing.extraction import ExtractionEvidence, ExtractionResult, _receipt, _verify_captured_bytes


def _observation_bytes(value):
    """Ordinary JSON retains measured floats; Core metadata keeps canonical rules."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _media(captured_file: CapturedFile) -> str:
    return captured_file.media_type.partition(";")[0].strip().lower()


class OcrExtractor:
    """Adapt an injected SpicyDocs DocumentExtractor under caller-attested settings.

    ``processing_identity`` must describe the reader, strategy, backend, model and
    resolved settings. The caller owns this attestation for arbitrary injected
    objects; DocSpec cannot infer whether a remote model changed behind its name.
    Provider output must contain JSON-compatible values or bytes, never objects
    converted to strings. Bytes are retained independently with digest references.
    """

    extractor_id = "docspec.spicy-docs-ocr/v1"

    def __init__(
        self,
        document_extractor,
        *,
        processing_identity: Mapping[str, Any],
        pages: Sequence[int] | None = None,
        page_separator: str = "\n\f\n",
    ) -> None:
        if not processing_identity or not page_separator:
            raise ValueError("OCR requires processing identity and a nonempty page separator")
        self.document_extractor = document_extractor
        self._identity = freeze_json(processing_identity, label="OCR processing identity")
        self.pages = None if pages is None else tuple(pages)
        if self.pages is not None and (
            not self.pages
            or len(set(self.pages)) != len(self.pages)
            or any(type(page) is not int or page < 1 for page in self.pages)
        ):
            raise ValueError("select distinct positive pages")
        self.page_separator = page_separator

    @property
    def configuration_digest(self) -> str:
        return identity_digest(
            {
                "processing": thaw_json(self._identity),
                "pages": self.pages,
                "pageSeparator": self.page_separator,
                "mapping": OCR_PAGE_TRANSFORM,
            }
        )

    def selected_identity(self, captured_file: CapturedFile) -> tuple[str, str]:
        media = _media(captured_file)
        if media != "application/pdf" and not media.startswith("image/"):
            raise ValueError("OCR requires PDF or image input")
        return self.extractor_id, self.configuration_digest

    def evidence_resolver(self, result: ExtractionResult):
        """Verify source-page text against retained observations without calling OCR."""
        manifest = next((item for item in result.evidence if item.label == "ocr-observations"), None)
        if manifest is None:
            raise IntegrityError("OCR result has no retained observations")
        receipt = thaw_json(result.receipt.metadata)
        expected = next(item["blob"] for item in receipt["evidence"] if item["label"] == manifest.label)
        verify_blob_bytes(BlobRef.from_dict(expected), manifest.content, label="OCR observations")
        record = json.loads(manifest.content)
        pages = {
            page["metadata"]["page"]: "\n".join(
                block["text"] for block in page["content"]["blocks"] if block["text"]
            ).encode("utf-8")
            for page in record["pages"]
        }

        def resolve(mapping, source_bytes):
            if (
                mapping.transformation != OCR_PAGE_TRANSFORM
                or mapping.evidence.page not in pages
                or sha256_digest(source_bytes) != result.receipt.input_digest
            ):
                raise IntegrityError("OCR mapping differs from the retained source-page observation")
            return pages[mapping.evidence.page]

        return resolve

    def extract(self, captured: CapturedFile, source_bytes: bytes) -> ExtractionResult:
        _verify_captured_bytes(captured, source_bytes)
        extractor_id, digest = self.selected_identity(captured)
        evidence: list[ExtractionEvidence] = []

        def encode(value):
            if isinstance(value, bytes):
                item = ExtractionEvidence(f"ocr-bytes-{len(evidence):06d}", value, "application/octet-stream")
                evidence.append(item)
                return {"retainedBytes": item.to_dict()}
            if is_dataclass(value):
                return {field.name: encode(getattr(value, field.name)) for field in fields(value)}
            if isinstance(value, Mapping):
                if any(not isinstance(key, str) for key in value):
                    raise ValueError("provider evidence object keys must be strings")
                return {key: encode(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [encode(item) for item in value]
            if value is None or isinstance(value, (str, int, bool)):
                return value
            if isinstance(value, float) and math.isfinite(value):
                return value
            raise ValueError("provider evidence must contain JSON values and finite floats")

        page_records, payloads, mappings, seen = [], [], [], set()
        position = 0
        separator = self.page_separator.encode("utf-8")
        coordinate_system = "pdf-page" if _media(captured) == "application/pdf" else "image-page"
        try:
            iterator = self.document_extractor.extract(source_bytes, media_type=captured.media_type, pages=self.pages)
            with closing(iterator) if hasattr(iterator, "close") else nullcontext(iterator):
                for result in iterator:
                    page = result.metadata["page"]
                    page_count = result.metadata["page_count"]
                    if type(page) is not int or page < 1 or page in seen:
                        raise ValueError("page extractor returned invalid or duplicate pages")
                    if type(page_count) is not int or page_count < 1 or page > page_count:
                        raise ValueError("page extractor returned invalid page count")
                    seen.add(page)
                    if result.metadata["source_sha256"] != captured.blob.digest.removeprefix("sha256:"):
                        raise ValueError("page extractor returned a different source digest")
                    record = encode(result)
                    page_records.append(record)
                    content = result.text.encode("utf-8")
                    payloads.append(content)
                    if len(payloads) > 1:
                        position += len(separator)
                    mappings.append(
                        EvidenceMapping(
                            position,
                            position + len(content),
                            EvidenceCoordinate(
                                coordinate_system,
                                captured.blob.digest,
                                page=page,
                                region={"kind": "inspected-page", "page": page},
                            ),
                            OCR_PAGE_TRANSFORM,
                        )
                    )
                    position += len(content)
            if not page_records:
                raise ValueError("page extraction yielded no pages")
            actual = tuple(record["metadata"]["page"] for record in page_records)
            expected = self.pages or tuple(range(1, page_records[0]["metadata"]["page_count"] + 1))
            if actual != expected or any(
                record["metadata"]["page_count"] != page_records[0]["metadata"]["page_count"] for record in page_records
            ):
                raise ValueError("page extraction ended before all requested pages were returned")
        except Exception as error:
            try:
                details = encode(getattr(error, "details", None))
            except (ValueError, TypeError):
                details = {"unsupportedDiagnosticType": type(getattr(error, "details", None)).__name__}
            evidence.append(
                ExtractionEvidence(
                    "ocr-failure",
                    _observation_bytes(
                        {
                            "complete": False,
                            "sourceDigest": captured.blob.digest,
                            "processingIdentity": thaw_json(self._identity),
                            "requestedPages": self.pages,
                            "pages": page_records,
                            "errorType": type(error).__name__,
                            "message": str(error),
                            "providerDetails": details,
                            "evidence": [item.to_dict() for item in evidence],
                        }
                    ),
                    "application/json",
                )
            )
            error.extraction_evidence = tuple(evidence)
            raise
        evidence.append(
            ExtractionEvidence(
                "ocr-observations",
                _observation_bytes(
                    {
                        "complete": True,
                        "selectedPages": actual,
                        "processingIdentity": thaw_json(self._identity),
                        "pages": page_records,
                    }
                ),
                "application/json",
            )
        )
        content = separator.join(payloads)
        representation = Representation.create(
            source_item_id=captured.source_item_id,
            file_id=captured.file_id,
            file_digest=captured.blob.digest,
            kind="pdf-text",
            blob=content_blob_ref(content, "text/plain; charset=utf-8"),
            extractor_id=extractor_id,
            configuration_digest=digest,
            evidence_mappings=tuple(mappings),
            warnings=tuple(
                f"page {page} has no recognized text" for page, value in zip(actual, payloads, strict=True) if not value
            ),
        )
        payload = RepresentationPayload(representation, content)
        return ExtractionResult(
            payload,
            _receipt(
                captured,
                payload,
                metadata={
                    "complete": True,
                    "selectedPages": actual,
                    "processingIdentity": thaw_json(self._identity),
                    "evidence": [item.to_dict() for item in evidence],
                },
            ),
            tuple(evidence),
        )
