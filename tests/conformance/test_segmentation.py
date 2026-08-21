from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from docspec.domain.identity import sha256_digest
from docspec.processing import DefaultExtractorRegistry, DefaultSegmenterRegistry

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_representation = importlib.import_module("tests.conformance.test_representation")
_pipeline_helpers = importlib.import_module("tests.test_processing_pipeline")
FIXTURES = _representation.FIXTURES
FAKE_PDF_PAGES = _representation.FAKE_PDF_PAGES
install_fake_pypdf = _representation.install_fake_pypdf
_captured = _pipeline_helpers._captured


def _segments(media_type: str, source: bytes):
    payload = DefaultExtractorRegistry().extract(_captured(source, media_type), source).payload
    return payload, DefaultSegmenterRegistry().segment(payload)


def test_segments_are_deterministic_for_every_supported_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pypdf(monkeypatch)
    for media_type, (source, _) in FIXTURES.items():
        payload, first = _segments(media_type, source)
        _, second = _segments(media_type, source)
        assert [item.segment.to_dict() for item in first] == [
            item.segment.to_dict() for item in second
        ], media_type
        assert first, f"{media_type} must produce at least one segment"
        assert [item.segment.ordinal for item in first] == list(range(len(first))), media_type

        previous_end = 0
        for item in first:
            segment = item.segment
            assert segment.representation_id == payload.representation.representation_id
            assert 0 <= segment.representation_start <= segment.representation_end <= len(payload.content)
            assert segment.representation_start >= previous_end, "segments must not overlap"
            previous_end = segment.representation_end
            assert item.content == payload.content[segment.representation_start : segment.representation_end]
            assert segment.content.digest == sha256_digest(item.content)
            assert segment.content.byte_size == len(item.content)
            assert segment.segmenter_id


def test_segment_coverage_matches_each_supported_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pypdf(monkeypatch)
    for media_type in ("text/plain", "text/html", "application/xml"):
        source, _ = FIXTURES[media_type]
        payload, segments = _segments(media_type, source)
        assert len(segments) == 2, f"{media_type} fixture holds two paragraphs"
        uncovered = bytearray(payload.content)
        for item in reversed(segments):
            del uncovered[item.segment.representation_start : item.segment.representation_end]
        assert bytes(uncovered).decode("utf-8").strip() == "", (
            f"{media_type} paragraph coverage may exclude only whitespace"
        )

    json_source, _ = FIXTURES["application/json"]
    _, json_segments = _segments("application/json", json_source)
    records = json.loads(json_source)
    assert len(json_segments) == len(records)
    assert [json.loads(item.content) for item in json_segments] == records

    image_source, _ = FIXTURES["image/png"]
    _, image_segments = _segments("image/png", image_source)
    assert len(image_segments) == 1
    assert image_segments[0].content == image_source, "the whole image is one exact segment"

    pdf_source, _ = FIXTURES["application/pdf"]
    _, pdf_segments = _segments("application/pdf", pdf_source)
    assert [item.content.decode("utf-8") for item in pdf_segments] == list(FAKE_PDF_PAGES)
    assert [item.segment.evidence.page for item in pdf_segments] == [1, 2, 3], (
        "every page, including an empty one, must keep its coverage ordinal"
    )
