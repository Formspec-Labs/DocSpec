"""Shared representation fixtures, extracted from tests.conformance.test_representation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from docspec.processing import LazyPypdfExtractor

# One exact source fixture per media-type family the default registry
# dispatches. PDF extraction crosses the lazy optional-provider boundary, so
# its provider is pinned to a deterministic fake exactly as the regular suite
# does; DocSpec still owns representation policy and evidence coordinates.
_PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (640).to_bytes(4, "big") + (480).to_bytes(4, "big")

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

        def close(self) -> None:
            pass

    provider = SimpleNamespace(__version__="conformance-fixture", PdfReader=FakeReader)
    monkeypatch.setattr("docspec.processing.extraction.distribution_version", lambda _name: provider.__version__)
    monkeypatch.setattr("spicy_docs.extraction.pypdf.version", lambda _name: provider.__version__)
    monkeypatch.setattr(
        "spicy_docs.extraction.pypdf.import_module",
        lambda _: provider,
    )


def resolver_for(media_type: str, source: bytes):
    if media_type != "application/pdf":
        return None
    return LazyPypdfExtractor().evidence_resolver(source)
