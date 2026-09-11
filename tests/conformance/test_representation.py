
from __future__ import annotations

from pathlib import Path

import pytest

from docspec.domain.content import Representation
from docspec.domain.identity import sha256_digest
from docspec.processing import DefaultExtractorRegistry
from docspec.processing.extraction import ExtractionError
from tests.support import processing as _pipeline_helpers
from tests.support.representation import (
    FIXTURES,
    install_fake_pypdf,
)

ROOT = Path(__file__).resolve().parents[2]


_captured = _pipeline_helpers._captured


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
