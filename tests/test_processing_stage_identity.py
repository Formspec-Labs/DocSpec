"""Configured stage pins remain distinct from selected output identities."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

import pytest

from docspec.domain.identity import identity_digest
from docspec.processing import (
    BoundedSegmenter,
    BoundedSegmentSettings,
    BoundedSegmentationError,
    DefaultExtractorRegistry,
    DefaultSegmenterRegistry,
    LazyPypdfExtractor,
    TextExtractor,
)
from docspec.processing.extraction import ExtractionError
from docspec.processing.segmentation import ParagraphSegmenter, SegmentationReceipt
from tests.support.processing import _captured
from tests.support.representation import FIXTURES, install_fake_pypdf


def test_selected_children_match_outputs_for_every_default_route(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_pypdf(monkeypatch)
    extractor = DefaultExtractorRegistry()
    segmenter = DefaultSegmenterRegistry()
    for media_type, (source, _) in FIXTURES.items():
        captured = _captured(source, media_type)
        result = extractor.extract(captured, source)
        representation = result.payload.representation
        extraction_identity = extractor.selected_identity(captured)
        assert extraction_identity == (representation.extractor_id, representation.configuration_digest)
        assert extraction_identity == (result.receipt.extractor_id, result.receipt.configuration_digest)
        assert extraction_identity[0] != extractor.extractor_id

        selected_segmenter = segmenter.selected_identity(representation)
        segments = segmenter.segment(result.payload)
        assert segments
        assert {(item.segment.segmenter_id, item.segment.policy_digest) for item in segments} == {selected_segmenter}
        assert selected_segmenter[0] != segmenter.segmenter_id


def test_configured_passthrough_subclass_emits_its_declared_identity() -> None:
    class ConfiguredText(TextExtractor):
        extractor_id = "test.configured-text/v1"
        configuration_digest = identity_digest({"normalization": "none", "variant": "experiment"})

    extractor = ConfiguredText()
    source = b"same source bytes"
    captured = _captured(source, "text/plain")
    result = extractor.extract(captured, source)
    assert extractor.selected_identity(captured) == (result.receipt.extractor_id, result.receipt.configuration_digest)
    assert result.payload.representation.configuration_digest == extractor.configuration_digest


def test_pdf_configuration_and_availability_are_pinned_without_importing_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_import(name: str) -> object:
        pytest.fail(f"configuration must not import {name}")

    monkeypatch.setattr("spicy_docs.extraction.pypdf.import_module", unexpected_import)
    monkeypatch.setattr("docspec.processing.extraction.distribution_version", lambda _: "6.1.0")
    default = LazyPypdfExtractor()
    changed_separator = LazyPypdfExtractor(page_separator="\n")
    changed_whitespace = LazyPypdfExtractor(strip_page_whitespace=True)
    assert DefaultExtractorRegistry(pdf=default).configuration_digest == DefaultExtractorRegistry(
        pdf=LazyPypdfExtractor()
    ).configuration_digest

    monkeypatch.setattr("docspec.processing.extraction.distribution_version", lambda _: "6.2.0")
    upgraded = LazyPypdfExtractor()

    def unavailable(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr("docspec.processing.extraction.distribution_version", unavailable)
    absent = LazyPypdfExtractor()
    variants = (default, changed_separator, changed_whitespace, upgraded, absent)
    assert len({stage.configuration_digest for stage in variants}) == len(variants)
    assert len({DefaultExtractorRegistry(pdf=stage).configuration_digest for stage in variants}) == len(variants)
    assert len({stage.extractor_id for stage in variants}) == 1

    captured = _captured(b"%PDF", "application/pdf")
    assert default.selected_identity(captured) == ("docspec.pypdf/6.1.0", default.configuration_digest)
    assert upgraded.selected_identity(captured) == ("docspec.pypdf/6.2.0", upgraded.configuration_digest)
    with pytest.raises(ExtractionError, match=r"docspec\[pdf\]"):
        absent.selected_identity(captured)
    with pytest.raises(ExtractionError, match=r"docspec\[pdf\]"):
        absent.extract(captured, b"%PDF")
    registry = DefaultExtractorRegistry(pdf=absent)
    assert registry.extract(_captured(b"text", "text/plain"), b"text").payload.content == b"text"


@pytest.mark.parametrize("loaded_version", [None, "unknown", "6.2.0"])
def test_changed_or_unidentified_pdf_provider_refuses_before_parsing(
    monkeypatch: pytest.MonkeyPatch, loaded_version: str | None,
) -> None:
    def unexpected_parse(*args: object, **kwargs: object) -> object:
        pytest.fail("a different provider must not parse source bytes")

    provider = SimpleNamespace(__version__=loaded_version, PdfReader=unexpected_parse)
    monkeypatch.setattr("spicy_docs.extraction.pypdf.version", lambda _: "6.1.0")
    monkeypatch.setattr("docspec.processing.extraction.distribution_version", lambda _: "6.1.0")
    monkeypatch.setattr("spicy_docs.extraction.pypdf.import_module", lambda _: provider)
    extractor = LazyPypdfExtractor()
    with pytest.raises(ExtractionError, match="version differs"):
        extractor.extract(_captured(b"%PDF", "application/pdf"), b"%PDF")


class _CharacterCounter:
    name = "characters"
    version = "1"

    def count(self, text: str) -> int:
        return len(text)


def test_segment_registry_pins_bounded_settings_and_selected_child() -> None:
    counter = _CharacterCounter()
    first = BoundedSegmenter(counter)
    changed = BoundedSegmenter(counter, settings=BoundedSegmentSettings.for_counter(counter, max_tokens=2000))
    default = DefaultSegmenterRegistry()
    bounded = DefaultSegmenterRegistry(bounded=first)
    changed_bounded = DefaultSegmenterRegistry(bounded=changed)
    assert len({stage.policy_digest for stage in (default, bounded, changed_bounded)}) == 3
    assert bounded.policy_digest == DefaultSegmenterRegistry(bounded=BoundedSegmenter(counter)).policy_digest
    source = b"A paragraph."
    payload = TextExtractor().extract(_captured(source, "text/plain"), source).payload
    expected = (first.segmenter_id, first.policy_digest)
    assert first.selected_identity(payload.representation) == expected
    assert bounded.selected_identity(payload.representation) == expected
    assert {(item.segment.segmenter_id, item.segment.policy_digest) for item in bounded.segment(payload)} == {expected}
    assert default.selected_identity(payload.representation) == (
        ParagraphSegmenter.segmenter_id, ParagraphSegmenter.policy_digest
    )


def test_changed_live_counter_refuses_configuration_read_before_any_segmentation() -> None:
    counter = _CharacterCounter()
    bounded = BoundedSegmenter(counter)
    registry = DefaultSegmenterRegistry(bounded=bounded)
    assert registry.policy_digest
    counter.version = "2"
    with pytest.raises(BoundedSegmentationError, match="tokenizer"):
        _ = bounded.policy_digest
    with pytest.raises(BoundedSegmentationError, match="tokenizer"):
        _ = registry.policy_digest


def test_empty_segmentation_retains_selected_policy_in_closed_receipt() -> None:
    source = b" \n\n "
    payload = TextExtractor().extract(_captured(source, "text/plain"), source).payload
    registry = DefaultSegmenterRegistry()
    segmenter_id, policy_digest = registry.selected_identity(payload.representation)
    assert registry.segment(payload) == ()
    receipt = SegmentationReceipt(payload.representation.representation_id, segmenter_id, policy_digest, ())
    value = receipt.to_dict()
    assert value["formatVersion"] == "2.0"
    assert value["segmenterId"] == ParagraphSegmenter.segmenter_id
    assert value["policyDigest"] == ParagraphSegmenter.policy_digest
    assert SegmentationReceipt.from_dict(value) == receipt
    with pytest.raises(ValueError, match="version"):
        SegmentationReceipt.from_dict({**value, "formatVersion": "1.0"})
    with pytest.raises(ValueError, match="closed shape"):
        SegmentationReceipt.from_dict({key: item for key, item in value.items() if key != "policyDigest"})
    with pytest.raises(ValueError, match="policy_digest"):
        SegmentationReceipt.from_dict({**value, "policyDigest": "unidentified"})
