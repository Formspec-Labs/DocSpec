"""Concrete extraction challenges separate byte evidence from content suitability."""

from dataclasses import replace

import pytest

from docspec.domain.content import Representation
from docspec.errors import IntegrityError
from docspec.processing.artifacts import (
    RepresentationPayload, content_blob_ref, verify_representation_evidence,
)
from docspec.processing.extraction import LazyPypdfExtractor
from docspec.processing.visible_text_runtime import VisibleTextExtractor
from tests.support.processing import _captured
from tests.support.representation import install_fake_pypdf


def _edited(payload, content, mappings):
    original = payload.representation
    return RepresentationPayload(Representation.create(
        source_item_id=original.source_item_id, file_id=original.file_id,
        file_digest=original.file_digest, kind=original.kind,
        blob=content_blob_ref(content, original.blob.media_type), extractor_id=original.extractor_id,
        configuration_digest=original.configuration_digest, evidence_mappings=tuple(mappings),
        warnings=original.warnings,
    ), content)


def test_complete_visible_text_can_be_a_tiny_fraction_of_its_markup():
    source = (b"<script>" + b"hidden application code " * 1000 + b"</script>"
        + "<h2>Café &amp; policy</h2><p>Apply by Friday.</p>".encode())
    captured = _captured(source, "text/html")
    extractor = VisibleTextExtractor()
    result = extractor.extract(captured, source)
    assert result.payload.content == "## Café & policy\n\nApply by Friday.".encode()
    verify_representation_evidence(result.payload, source,
        derived_resolver=extractor.evidence_resolver(captured, source))
    # The exact known visible content is complete, despite a fraction below 1%.
    assert 100 * len(b" ".join(result.payload.content.split())) < len(b" ".join(source.split()))


def test_large_nonempty_boilerplate_and_valid_declared_mappings_do_not_prove_completeness():
    boilerplate = b"General boilerplate. " * 1000
    source = b"<p>" + boilerplate + b"</p><p>Important: apply by Friday.</p>"
    captured = _captured(source, "text/html")
    extractor = VisibleTextExtractor()
    full = extractor.extract(captured, source).payload
    mapping = full.representation.evidence_mappings[0]
    dropped = _edited(full, full.content[:mapping.representation_end], (mapping,))
    assert b"apply by Friday" in full.content and b"apply by Friday" not in dropped.content
    # Mapping verification has always checked declared evidence, not whether a
    # producer supplied every meaningful block. A high ratio misses this defect.
    verify_representation_evidence(dropped, source,
        derived_resolver=extractor.evidence_resolver(captured, source))
    assert 100 * len(b" ".join(dropped.content.split())) > 90 * len(b" ".join(source.split()))


def test_named_visible_block_evidence_rejects_invented_duplicate_text():
    source = b"<p>Specific instruction.</p>"
    captured = _captured(source, "text/html")
    extractor = VisibleTextExtractor()
    original = extractor.extract(captured, source).payload
    doubled = original.content + original.content
    mapping = replace(original.representation.evidence_mappings[0], representation_end=len(doubled))
    misleading = _edited(original, doubled, (mapping,))
    with pytest.raises(IntegrityError):
        verify_representation_evidence(misleading, source,
            derived_resolver=extractor.evidence_resolver(captured, source))


@pytest.mark.parametrize("media_type,source", [
    ("text/html", b"<script>application code only</script>"),
    ("application/xml", b"<document/>"),
])
def test_empty_visible_content_is_a_recordable_input_failure(media_type, source):
    with pytest.raises(ValueError, match="visible"):
        VisibleTextExtractor().extract(_captured(source, media_type), source)


def test_pdf_empty_pages_are_observed_and_truncated_page_claims_refuse(monkeypatch):
    install_fake_pypdf(monkeypatch)
    source = b"%PDF-quality-fixture"
    extractor = LazyPypdfExtractor()
    result = extractor.extract(_captured(source, "application/pdf"), source)
    assert result.receipt.metadata["pageCount"] == 3
    assert result.receipt.metadata["emptyPageCount"] == 1
    assert result.payload.representation.warnings == ("page 2 has no embedded text",)
    assert result.payload.content.strip()
    first = result.payload.representation.evidence_mappings[0]
    truncated = _edited(result.payload, b"Page", (replace(first, representation_end=4),))
    with pytest.raises(IntegrityError, match="round-trip"):
        verify_representation_evidence(truncated, source, derived_resolver=extractor.evidence_resolver(source))


def test_source_pin_detects_truncation_but_a_new_unpinned_capture_cannot_infer_missing_upstream_text():
    complete = b"<p>First paragraph.</p><p>Important deadline.</p>"
    partial = b"<p>First paragraph.</p>"
    extractor = VisibleTextExtractor()
    with pytest.raises(IntegrityError, match="reference"):
        extractor.extract(_captured(complete, "text/html"), partial)
    # Treating the shorter upstream response as a new capture gives no evidence
    # of the absent passage. Admission must keep semantic completeness unknown.
    captured_partial = _captured(partial, "text/html")
    result = extractor.extract(captured_partial, partial)
    assert result.payload.content == b"First paragraph."
    verify_representation_evidence(result.payload, partial,
        derived_resolver=extractor.evidence_resolver(captured_partial, partial))
