"""Visible-text choices preserve source evidence through the public lifecycle."""

from dataclasses import FrozenInstanceError, replace

import pytest

from docspec.application.execution_evidence import failure_record
from docspec.domain.content import Representation
from docspec.domain.jobs import FailureClass
from docspec.errors import IntegrityError
from docspec.processing.artifacts import (
    PDF_PAGE_TEXT_TRANSFORM,
    RepresentationPayload,
    verify_representation_evidence,
    verify_segment_evidence,
)
from docspec.processing.visible_text_runtime import VisibleTextBlockSegmenter, VisibleTextExtractor
from docspec.runtime import stage_policy
from examples import representation_choices
from tests.support.processing import _captured


@pytest.mark.parametrize("media_type,source,expected", [
    (
        "text/html; charset=utf-8",
        '<head><title>Hidden</title></head><h2>Café &amp; policy</h2>'
        '<p>A <em>bold</em> &amp; B.</p><pre>First\n\nSecond</pre>'.encode(),
        ("## Café & policy", "A bold & B.", "First\n\nSecond"),
    ),
    (
        "application/example+xml",
        '<doc><TITLE> Café &amp; policy </TITLE><p> Alpha\n beta &amp; gamma </p></doc>'.encode(),
        ("# Café & policy", "Alpha beta & gamma"),
    ),
])
def test_visible_blocks_round_trip_to_enclosing_source_spans(media_type, source, expected):
    captured = _captured(source, media_type)
    extractor = VisibleTextExtractor(xml_heading_levels={"TITLE": 1})
    result = extractor.extract(captured, source)
    segments = VisibleTextBlockSegmenter().segment(result.payload)
    assert tuple(item.content.decode() for item in segments) == expected
    assert b"Hidden" not in result.payload.content
    assert extractor.selected_identity(captured) == (
        result.receipt.extractor_id, result.receipt.configuration_digest,
    )
    assert result.receipt.extractor_id != extractor.extractor_id
    first_source = source[segments[0].segment.evidence.start:segments[0].segment.evidence.end]
    assert "Café &amp; policy".encode() in first_source
    assert b"#" not in first_source
    assert segments[0].segment.evidence.source_digest == captured.blob.digest
    resolver = extractor.evidence_resolver(captured, source)
    verify_representation_evidence(result.payload, source, derived_resolver=resolver)
    for segment in segments:
        verify_segment_evidence(segment, result.payload, source, derived_resolver=resolver)
    with pytest.raises(IntegrityError, match="declared boundary"):
        result.payload.evidence_for_range(1, len(expected[0].encode()))


def test_heading_settings_are_snapshotted_and_changes_have_distinct_pins():
    headings = {"TITLE": 1}
    extractor = VisibleTextExtractor(xml_heading_levels=headings)
    segmenter = VisibleTextBlockSegmenter()
    original_policy = stage_policy(extractor=extractor, segmenter=segmenter)
    source = b"<doc><TITLE>Heading</TITLE></doc>"
    captured = _captured(source, "application/xml")
    original = extractor.extract(captured, source)
    headings["TITLE"] = 2
    assert extractor.extract(captured, source) == original
    assert stage_policy(extractor=extractor, segmenter=segmenter) == original_policy
    changed = VisibleTextExtractor(xml_heading_levels=headings)
    assert changed.extract(captured, source).payload.content == b"## Heading"
    assert changed.selected_identity(captured) != extractor.selected_identity(captured)
    assert stage_policy(extractor=changed, segmenter=segmenter) != original_policy
    with pytest.raises(FrozenInstanceError):
        extractor._xml_headings = (("TITLE", 3),)


def test_block_segmenter_and_resolver_refuse_foreign_or_changed_mappings():
    source = b"<p>A &amp; B</p>"
    captured = _captured(source, "text/html")
    extractor = VisibleTextExtractor()
    payload = extractor.extract(captured, source).payload
    mapping = payload.representation.evidence_mappings[0]
    resolver = extractor.evidence_resolver(captured, source)
    with pytest.raises(IntegrityError, match="regenerated block"):
        resolver(replace(mapping, evidence=replace(mapping.evidence, start=0)), source)
    with pytest.raises(IntegrityError, match="different captured bytes"):
        resolver(mapping, b"other source")
    segmenter = VisibleTextBlockSegmenter()
    # The segmenter checks the semantic mapping before trying to build a segment.
    foreign = replace(mapping, transformation=PDF_PAGE_TEXT_TRANSFORM)
    inputs = {
        "source_item_id": captured.source_item_id,
        "file_id": captured.file_id,
        "file_digest": captured.blob.digest,
        "blob": payload.representation.blob,
        "extractor_id": payload.representation.extractor_id,
        "configuration_digest": payload.representation.configuration_digest,
        "evidence_mappings": (foreign,),
    }
    with pytest.raises(IntegrityError, match="visible-text representation"):
        segmenter.selected_identity(Representation.create(**inputs, kind="pdf-text"))
    representation = Representation.create(**inputs, kind="visible-text")
    with pytest.raises(IntegrityError, match="non-block evidence"):
        segmenter.segment(RepresentationPayload(representation, payload.content))


@pytest.mark.parametrize("media_type,source", [
    ("text/html", b"<script>hidden</script>"),
    ("application/xml", b"<doc>unclosed"),
    ("application/pdf", b"%PDF"),
    ("text/html", b"<p>\xff</p>"),
])
def test_source_input_refusals_are_deterministic(media_type, source):
    with pytest.raises(ValueError) as failure:
        VisibleTextExtractor().extract(_captured(source, media_type), source)
    assert failure_record("processing", failure.value, 1).failure_class is FailureClass.DETERMINISTIC_INPUT


def test_captured_byte_pin_failure_remains_artifact_integrity():
    with pytest.raises(IntegrityError) as failure:
        VisibleTextExtractor().extract(_captured(b"<p>Pinned</p>", "text/html"), b"<p>Changed</p>")
    assert failure_record("processing", failure.value, 1).failure_class is FailureClass.ARTIFACT_INTEGRITY


def test_captured_media_type_must_agree_with_its_blob():
    source = b"<p>Content</p>"
    captured = _captured(source, "text/html")
    captured = replace(captured, blob=replace(captured.blob, media_type="application/xml"))
    with pytest.raises(IntegrityError, match="media type differs"):
        VisibleTextExtractor().extract(captured, source)


def test_public_experiment_retains_exact_capture_and_inspects_either_representation(tmp_path):
    markup = representation_choices.run_example(tmp_path / "markup", "markup")
    visible = representation_choices.run_example(tmp_path / "visible", "visible-text")
    assert markup["capturedText"] == visible["capturedText"]
    assert markup["capturedDigest"] == visible["capturedDigest"] == markup["representationDigest"]
    assert visible["representationDigest"] != visible["capturedDigest"]
    assert markup["representationKind"] == "html"
    assert visible["representationKind"] == "visible-text"
    assert "<html" not in visible["representationText"]
    assert "café" in visible["representationText"]
    assert visible["counts"]["files"] == visible["counts"]["representations"] == 1
    assert visible["counts"]["segments"] == len(visible["segments"]) == 3
    assert visible["counts"]["failures"] == 0
    for segment in visible["segments"]:
        evidence = segment["evidence"]
        assert evidence["sourceDigest"] == visible["capturedDigest"]
        assert 0 <= evidence["start"] < evidence["end"] <= len(visible["capturedText"].encode())


def test_visible_experiment_recovery_uses_saved_stage_pins_without_refetch(tmp_path, monkeypatch):
    actual_prepare = representation_choices.prepare_local_experiment
    prepared_call = {}
    fetches = []
    actual_fetch = representation_choices.ExampleFetcher.fetch

    def observe_prepare(*args, **kwargs):
        prepared = actual_prepare(*args, **kwargs)
        prepared_call.update(args=args, kwargs=kwargs, prepared=prepared)
        return prepared

    def observe_fetch(self, candidate, **kwargs):
        fetches.append(candidate.candidate_id)
        return actual_fetch(self, candidate, **kwargs)

    monkeypatch.setattr(representation_choices, "prepare_local_experiment", observe_prepare)
    monkeypatch.setattr(representation_choices.ExampleFetcher, "fetch", observe_fetch)
    representation_choices.run_example(tmp_path / "visible")
    initial = prepared_call["prepared"]
    settings = {**prepared_call["kwargs"], "handoff_ref": initial.handoff_ref}
    with actual_prepare(*prepared_call["args"], **settings) as recovered:
        assert recovered.run() == initial.run()
    assert len(fetches) == 1
    settings["extractor"] = VisibleTextExtractor(html_heading_tags={"h1": 2})
    with pytest.raises(IntegrityError, match="saved|different|differs"):
        actual_prepare(*prepared_call["args"], **settings)
    assert len(fetches) == 1
