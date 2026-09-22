"""PDF reading matches the frozen page oracle for text and evidence while DocSpec keeps its refusal policy.

Encryption needs an explicit profile, unreadable input and cap overruns refuse before backend construction,
and a failed page aborts with its number and cause. A changed reader identity changes configuration without
changing content and refuses to verify an older extraction.
"""

from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest

from docspec.domain.identity import identity_digest
from docspec.errors import IntegrityError
from docspec.processing.extraction import ExtractionError, LazyPypdfExtractor
from tests.support.pdf_fixtures import make_pdf, make_textless_pdf
from tests.support.pdf_oracle import FrozenPypdfExtractor
from tests.support.processing import _captured

pypdf = pytest.importorskip("pypdf", reason="real PDF qualification requires docspec[pdf]")
SOURCE = Path(__file__).parent / "fixtures/pdf/FAA-2016-6907-0001-content.pdf"


def _rewrite(source: bytes, *, password: str | None = None, broken_page: int | None = None) -> bytes:
    """Rewrite ``source`` with pypdf, optionally encrypting it or replacing one page's resources with a number."""
    from pypdf.generic import NameObject, NumberObject

    with pypdf.PdfReader(BytesIO(source)) as reader, pypdf.PdfWriter() as writer:
        writer.append_pages_from_reader(reader)
        if broken_page is not None:
            writer.pages[broken_page - 1][NameObject("/Resources")] = NumberObject(9)
        if password is not None:
            writer.encrypt(user_password=password, owner_password="owner", algorithm="RC4-128")
        output = BytesIO()
        writer.write(output)
        return output.getvalue()


@pytest.mark.parametrize("strip", [False, True])
@pytest.mark.parametrize("separator", ["\n\f\n", " | "])
@pytest.mark.parametrize("sample", ["publisher", "mixed-pages", "blank"])
def test_raw_page_oracle_and_representation_evidence_remain_equal(strip, separator, sample):
    source = {
        "publisher": SOURCE.read_bytes(),
        "mixed-pages": make_pdf(["  First page  ", "", "   ", "Last page"]),
        "blank": make_textless_pdf(),
    }[sample]
    pages, backend = FrozenPypdfExtractor(strip_page_whitespace=strip)._read_pages(source)
    extractor = LazyPypdfExtractor(page_separator=separator, strip_page_whitespace=strip)
    assert extractor._read_pages(source) == (pages, backend)
    result = extractor.extract(_captured(source, "application/pdf"), source)
    assert result.payload.content == separator.join(pages).encode()
    assert dict(result.receipt.metadata) == {"pageCount": len(pages), "emptyPageCount": sum(not p for p in pages)}
    assert result.payload.representation.warnings == tuple(
        f"page {i} has no embedded text" for i, page in enumerate(pages, 1) if not page
    )
    position = 0
    for i, (page, mapping) in enumerate(zip(pages, result.payload.representation.evidence_mappings, strict=True), 1):
        assert mapping.representation_start == position
        assert mapping.representation_end == position + len(page.encode())
        assert mapping.evidence.page == i
        assert mapping.evidence.source_digest == f"sha256:{sha256(source).hexdigest()}"
        position += len(page.encode()) + len(separator.encode())
    extractor.verify(result, source)


def test_retained_pdf_pin_and_known_text():
    source = SOURCE.read_bytes()
    assert sha256(source).hexdigest() == "f4494ea77d0f8a0ec0b6e7f64e20c6ffe6c53d3be47cd59245f42f74036a7fc0"
    result = LazyPypdfExtractor().extract(_captured(source, "application/pdf"), source)
    assert result.payload.content == (
        b"Comment Info: =================\n"
        b"General Comment:Rank Investigation and Protection, Inc. - Exemption/Rulemaking"
    )


@pytest.mark.parametrize("password", ["", "secret"])
def test_all_encryption_still_requires_an_explicit_docspec_profile(password):
    source = _rewrite(make_pdf(["Private"]), password=password)
    with pytest.raises(ExtractionError, match="explicit decryption"):
        FrozenPypdfExtractor()._read_pages(source)
    with pytest.raises(ExtractionError, match="explicit decryption"):
        LazyPypdfExtractor().extract(_captured(source, "application/pdf"), source)


@pytest.mark.parametrize("source", [b"not a PDF", b"%PDF-1.4 truncated", b""])
def test_unreadable_input_still_refuses_without_a_representation(source):
    with pytest.raises(ExtractionError):
        FrozenPypdfExtractor()._read_pages(source)
    with pytest.raises(ExtractionError):
        LazyPypdfExtractor().extract(_captured(source, "application/pdf"), source)


def test_actual_failed_page_aborts_and_preserves_number_and_cause():
    from spicy_docs.extraction.pypdf import PdfPageError

    source = _rewrite(make_pdf(["First", "Bad", "Third"]), broken_page=2)
    with pytest.raises(ExtractionError):
        FrozenPypdfExtractor()._read_pages(source)
    with pytest.raises(ExtractionError) as failure:
        LazyPypdfExtractor().extract(_captured(source, "application/pdf"), source)
    assert isinstance(failure.value.__cause__, PdfPageError)
    assert failure.value.__cause__.page == 2
    assert isinstance(failure.value.__cause__.__cause__, TypeError)


@pytest.mark.parametrize("raw", [None, "", "  \n"])
def test_backend_no_text_and_whitespace_keep_docspec_policy(monkeypatch, raw):
    monkeypatch.setattr(pypdf.PageObject, "extract_text", lambda _: raw)
    source = make_textless_pdf()
    for strip in (False, True):
        assert LazyPypdfExtractor(strip_page_whitespace=strip)._read_pages(source) == (
            FrozenPypdfExtractor(strip_page_whitespace=strip)._read_pages(source)
        )


def test_new_reader_identity_changes_configuration_without_changing_content(monkeypatch):
    from docspec.processing.extraction import _pypdf_reader_identity

    original = _pypdf_reader_identity()
    assert original is not None
    base = LazyPypdfExtractor()
    source = make_pdf(["Same text"])
    captured = _captured(source, "application/pdf")
    first = base.extract(captured, source)
    old_digest = identity_digest({
        "provider": "pypdf", "providerVersion": pypdf.__version__, "available": True,
        "pageSeparator": "\n\f\n", "stripPageWhitespace": False,
    })
    assert base.extractor_id == "docspec.pypdf-adapter/v2"
    assert base.configuration_digest != old_digest
    monkeypatch.setattr("docspec.processing.extraction._pypdf_reader_identity", lambda: (original[0], "a" * 64))
    changed = LazyPypdfExtractor()
    second = changed.extract(captured, source)
    assert first.payload.content == second.payload.content
    assert first.payload.representation.evidence_mappings == second.payload.representation.evidence_mappings
    assert first.payload.representation.representation_id != second.payload.representation.representation_id
    with pytest.raises(IntegrityError, match="different extraction settings"):
        changed.verify(first, source)


@pytest.mark.parametrize("change", ["owner-version", "module-bytes", "missing-reader"])
def test_reader_identity_drift_refuses_before_backend_construction(monkeypatch, change):
    from docspec.processing.extraction import _pypdf_reader_identity

    original = _pypdf_reader_identity()
    assert original is not None
    extractor = LazyPypdfExtractor()
    mutation = {"owner-version": ("different", original[1]), "module-bytes": (original[0], "a" * 64), "missing-reader": None}[change]
    monkeypatch.setattr("docspec.processing.extraction._pypdf_reader_identity", lambda: mutation)
    monkeypatch.setattr(pypdf, "PdfReader", lambda *a, **kw: pytest.fail("parsed after reader identity changed"))
    with pytest.raises(ExtractionError, match="configured reader"):
        extractor.extract(_captured(b"%PDF", "application/pdf"), b"%PDF")


def test_shared_input_cap_refuses_before_backend_construction(monkeypatch):
    source = b"%PDF" + b" " * (64 * 1024**2)
    monkeypatch.setattr(pypdf, "PdfReader", lambda *a, **kw: pytest.fail("parsed oversized input"))
    with pytest.raises(ExtractionError, match="max_input_bytes"):
        LazyPypdfExtractor().extract(_captured(source, "application/pdf"), source)
