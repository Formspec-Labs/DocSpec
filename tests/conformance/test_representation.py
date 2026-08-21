from __future__ import annotations

from types import SimpleNamespace

import pytest

from docspec.domain.content import Representation
from docspec.domain.identity import sha256_digest
from docspec.processing import DefaultExtractorRegistry, LazyPypdfExtractor
from docspec.processing.extraction import ExtractionError

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_pipeline_helpers = importlib.import_module("tests.test_processing_pipeline")
_captured = _pipeline_helpers._captured

# One exact source fixture per media-type family the default registry
# dispatches. PDF extraction crosses the lazy optional-provider boundary, so
# its provider is pinned to a deterministic fake exactly as the regular suite
# does -- CI installs no pypdf, and the adapter under test is DocSpec's.
_PNG_HEADER = (
    b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (640).to_bytes(4, "big") + (480).to_bytes(4, "big")
)
FIXTURES: dict[str, tuple[bytes, str]] = {
    "text/plain": ("Alpha paragraph \U0001f9ea one.\n\nBeta paragraph two.".encode(), "text"),
    "text/html": (b"<p>Alpha &amp; one.</p>\n\n<p>Beta two.</p>", "html"),
    "application/xml": (b"<doc>\n  <p>Alpha one.</p>\n\n  <p>Beta two.</p>\n</doc>", "xml"),
    "application/json": (b'[{"id": "a", "value": 1}, {"id": "b", "value": 2}]', "json"),
    "image/png": (_PNG_HEADER, "image"),
    "application/pdf": (b"%PDF-conformance-fixture", "pdf-text"),
}
FAKE_PDF_PAGES = ("Page one §", "", "Page three \U0001f9ea")


def install_fake_pypdf(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakePage:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class FakeReader:
        is_encrypted = False

        def __init__(self, stream: object, *, strict: bool) -> None:
            assert stream is not None
            assert strict is False
            self.pages = [FakePage(text) for text in FAKE_PDF_PAGES]

    provider = SimpleNamespace(__version__="conformance-fixture", PdfReader=FakeReader)
    monkeypatch.setattr(
        "docspec.processing.extraction.import_module",
        lambda name: provider if name == "pypdf" else importlib.import_module(name),
    )


def resolver_for(media_type: str, source: bytes):
    if media_type != "application/pdf":
        return None
    return LazyPypdfExtractor().evidence_resolver(source)


def test_supported_content_produces_identified_receipted_representations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pypdf(monkeypatch)
    registry = DefaultExtractorRegistry()
    for media_type, (source, expected_kind) in FIXTURES.items():
        captured = _captured(source, media_type)
        first = registry.extract(captured, source)
        second = registry.extract(captured, source)

        representation = first.payload.representation
        assert representation.to_dict() == second.payload.representation.to_dict(), media_type
        assert first.receipt.to_dict() == second.receipt.to_dict(), media_type

        assert representation.kind == expected_kind
        assert representation.representation_id
        assert Representation.from_dict(representation.to_dict()) == representation
        assert representation.blob.digest == sha256_digest(first.payload.content)
        assert representation.blob.byte_size == len(first.payload.content)
        assert "/" in representation.extractor_id, "an extractor identity must carry its version"
        assert representation.evidence_mappings

        receipt = first.receipt
        assert receipt.extractor_id == representation.extractor_id
        assert receipt.representation_id == representation.representation_id
        assert receipt.file_id == captured.file_id
        assert receipt.input_digest == sha256_digest(source)
        assert receipt.output_digest == sha256_digest(first.payload.content)
        assert receipt.output_byte_size == len(first.payload.content)
        assert receipt.kind == expected_kind


def test_unregistered_media_types_fail_closed() -> None:
    registry = DefaultExtractorRegistry()
    source = b"PK\x03\x04 archive bytes"
    with pytest.raises(ExtractionError, match="no extractor is registered"):
        registry.extract(_captured(source, "application/zip"), source)
