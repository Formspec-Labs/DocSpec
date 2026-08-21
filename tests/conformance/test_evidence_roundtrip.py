from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from docspec.errors import IntegrityError
from docspec.processing import (
    DefaultExtractorRegistry,
    DefaultSegmenterRegistry,
    RepresentationPayload,
    SegmentPayload,
    verify_segment_evidence,
)

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_representation = importlib.import_module("tests.conformance.test_representation")
_pipeline_helpers = importlib.import_module("tests.test_processing_pipeline")
FIXTURES = _representation.FIXTURES
install_fake_pypdf = _representation.install_fake_pypdf
resolver_for = _representation.resolver_for
_captured = _pipeline_helpers._captured


def _persisted(media_type: str, source: bytes):
    """Extract and segment, then rebuild both from their persisted records so
    only the durable mappings carry the round trip."""

    result = DefaultExtractorRegistry().extract(_captured(source, media_type), source)
    representation = type(result.payload.representation).from_dict(result.payload.representation.to_dict())
    payload = RepresentationPayload(representation, result.payload.content)
    segments = tuple(
        SegmentPayload(type(item.segment).from_dict(item.segment.to_dict()), item.content)
        for item in DefaultSegmenterRegistry().segment(payload)
    )
    return payload, segments


def test_every_fixture_segment_resolves_to_its_representation_and_exact_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pypdf(monkeypatch)
    verified = 0
    for media_type, (source, _) in FIXTURES.items():
        payload, segments = _persisted(media_type, source)
        assert segments, f"{media_type} must contribute at least one fixture segment"
        resolver = resolver_for(media_type, source)
        for item in segments:
            verify_segment_evidence(item, payload, source, derived_resolver=resolver)
            verified += 1
    assert verified >= len(FIXTURES)


def test_roundtrip_fails_closed_when_source_or_coordinates_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_pypdf(monkeypatch)
    source, _ = FIXTURES["text/plain"]
    payload, segments = _persisted("text/plain", source)

    with pytest.raises(IntegrityError, match="differ from the representation's file digest"):
        verify_segment_evidence(segments[0], payload, source + b" drifted")

    # The payload constructor itself refuses drifted bytes, so a truncated
    # segment cannot even be assembled for verification.
    with pytest.raises(IntegrityError, match="byte size differs"):
        SegmentPayload(segments[0].segment, segments[0].content[:-1])
