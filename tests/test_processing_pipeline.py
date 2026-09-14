from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

import pytest

from docspec.domain.content import CapturedFile, Segment
from docspec.errors import IntegrityError
from docspec.processing import (
    DefaultExtractorRegistry,
    DefaultSegmenterRegistry,
    HtmlExtractor,
    ImageExtractor,
    JsonExtractor,
    LazyPypdfExtractor,
    ParagraphSegmenter,
    RecordSegmenter,
    RepresentationPayload,
    SegmentPayload,
    TextExtractor,
    WholeImageSegmenter,
    XmlExtractor,
    verify_representation_evidence,
    verify_segment_evidence,
)
from docspec.processing.extraction import ExtractionError
from tests.support.processing import (
    _captured,
)


@pytest.mark.parametrize(
    ("extractor", "media_type", "content", "kind", "metadata_key"),
    [
        (TextExtractor(), "text/plain", "Alpha §\nBeta".encode(), "text", "unicodeCodepointCount"),
        (
            HtmlExtractor(),
            "text/html",
            b"<html><body><p>Visible</p><script>held</script></body></html>",
            "html",
            "visibleUnicodeCodepointCount",
        ),
        (XmlExtractor(), "application/xml", b"<root><record>one</record></root>", "xml", "rootTag"),
        (JsonExtractor(), "application/json", b'[{"record":1},{"record":2}]', "json", "recordCount"),
    ],
)
def test_stdlib_extractors_preserve_exact_source_and_are_retry_stable(
    extractor: object,
    media_type: str,
    content: bytes,
    kind: str,
    metadata_key: str,
) -> None:
    captured = _captured(content, media_type)

    first = extractor.extract(captured, content)  # type: ignore[attr-defined]
    second = extractor.extract(captured, content)  # type: ignore[attr-defined]

    assert first.payload.content == content
    assert first.payload.representation.kind == kind
    assert metadata_key in first.receipt.metadata
    assert first.payload.representation.representation_id == second.payload.representation.representation_id
    assert first.receipt.receipt_digest == second.receipt.receipt_digest
    assert extractor.selected_identity(captured) == (  # type: ignore[attr-defined]
        first.receipt.extractor_id, first.receipt.configuration_digest
    )
    verify_representation_evidence(first.payload, content)


def test_closed_json_xml_and_captured_digests_fail_closed() -> None:
    with pytest.raises(IntegrityError, match="duplicate object key"):
        JsonExtractor().extract(_captured(b'{"a":1,"a":2}', "application/json"), b'{"a":1,"a":2}')
    with pytest.raises(ExtractionError, match="cannot be parsed"):
        XmlExtractor().extract(_captured(b"<root>", "application/xml"), b"<root>")

    captured = _captured(b"original", "text/plain")
    with pytest.raises(IntegrityError, match="digest differs"):
        TextExtractor().extract(captured, b"changed!")


def test_image_passthrough_reports_header_metadata_and_exact_extent() -> None:
    png_header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (640).to_bytes(4, "big") + (480).to_bytes(4, "big")
    captured = _captured(png_header, "image/png")

    result = ImageExtractor().extract(captured, png_header)
    segment = WholeImageSegmenter().segment(result.payload)[0]

    assert result.receipt.metadata["imageFormat"] == "png"
    assert result.receipt.metadata["widthPixels"] == 640
    assert result.receipt.metadata["heightPixels"] == 480
    assert segment.content == png_header
    assert segment.segment.evidence.region["width"] == 640
    verify_segment_evidence(segment, result.payload, png_header)


def test_default_media_dispatch_uses_source_specific_extractors() -> None:
    registry = DefaultExtractorRegistry(pdf=TextExtractor())
    cases = [
        (b"prose", "text/plain", "text"),
        (b"<p>prose</p>", "text/html", "html"),
        (b"<root/>", "application/atom+xml", "xml"),
        (b'{"value":1}', "application/ld+json", "json"),
    ]
    for content, media_type, kind in cases:
        captured = _captured(content, media_type)
        representation = registry.extract(captured, content).payload.representation
        assert representation.kind == kind
        assert registry.selected_identity(captured) == (
            representation.extractor_id, representation.configuration_digest
        )

    with pytest.raises(ExtractionError, match="no extractor"):
        registry.extract(_captured(b"bytes", "application/octet-stream"), b"bytes")


def test_pypdf_is_loaded_only_when_selected_and_pages_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    parsed_sources: list[bytes] = []

    class FakePage:
        def __init__(self, text: str | None) -> None:
            self._text = text

        def extract_text(self) -> str | None:
            return self._text

    class FakeReader:
        is_encrypted = False

        def __init__(self, stream: BytesIO, *, strict: bool) -> None:
            parsed_sources.append(stream.read())
            assert strict is False
            self.pages = [FakePage("Page one §"), FakePage(""), FakePage("Page three 🧪")]

    provider = SimpleNamespace(__version__="6.1.0-fixture", PdfReader=FakeReader)
    imports: list[str] = []

    def import_provider(name: str) -> object:
        imports.append(name)
        return provider

    monkeypatch.setattr("docspec.processing.extraction.import_module", import_provider)
    monkeypatch.setattr("docspec.processing.extraction.distribution_version", lambda _: provider.__version__)
    extractor = LazyPypdfExtractor()
    assert imports == []
    source = b"%PDF-fixture-bytes"
    result = extractor.extract(_captured(source, "application/pdf"), source)
    assert parsed_sources == [source]
    assert imports == ["pypdf"]
    assert result.receipt.extractor_id == "docspec.pypdf/6.1.0-fixture"
    assert result.receipt.configuration_digest == extractor.configuration_digest
    assert extractor.extractor_id != result.receipt.extractor_id
    persisted_representation = type(result.payload.representation).from_dict(
        result.payload.representation.to_dict()
    )
    persisted_payload = RepresentationPayload(persisted_representation, result.payload.content)
    segments = tuple(
        SegmentPayload(type(segment.segment).from_dict(segment.segment.to_dict()), segment.content)
        for segment in DefaultSegmenterRegistry().segment(persisted_payload)
    )

    assert [segment.content.decode() for segment in segments] == ["Page one §", "", "Page three 🧪"]
    assert [segment.segment.evidence.page for segment in segments] == [1, 2, 3]
    assert [mapping.transformation for mapping in persisted_representation.evidence_mappings] == [
        "pypdf-page-text",
        "pypdf-page-text",
        "pypdf-page-text",
    ]
    resolver = extractor.evidence_resolver(source)
    for segment in segments:
        verify_segment_evidence(segment, persisted_payload, source, derived_resolver=resolver)
    assert [segment.segment.segment_id for segment in segments] == [
        segment.segment.segment_id for segment in DefaultSegmenterRegistry().segment(persisted_payload)
    ]

    assert parsed_sources == [source, source]  # The standalone resolver reparses too.
    extractor.verify(result, source)
    assert parsed_sources == [source] * 3

    first, *remaining = persisted_representation.evidence_mappings
    wrong_mapping = replace(first, evidence=replace(
        first.evidence, page=3, region={"kind": "whole-page", "page": 3},
    ))
    wrong_representation = type(persisted_representation).create(
        source_item_id=persisted_representation.source_item_id,
        file_id=persisted_representation.file_id,
        file_digest=persisted_representation.file_digest,
        kind=persisted_representation.kind,
        blob=persisted_representation.blob,
        extractor_id=persisted_representation.extractor_id,
        configuration_digest=persisted_representation.configuration_digest,
        evidence_mappings=(wrong_mapping, *remaining),
        warnings=persisted_representation.warnings,
    )
    wrong_result = replace(
        result,
        payload=RepresentationPayload(wrong_representation, persisted_payload.content),
        receipt=replace(result.receipt, representation_id=wrong_representation.representation_id),
    )
    with pytest.raises(IntegrityError, match="round-trip"):
        extractor.verify(wrong_result, source)
    assert parsed_sources == [source] * 4


def test_missing_optional_pdf_profile_has_one_actionable_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_: str) -> object:
        raise ModuleNotFoundError("pypdf")

    monkeypatch.setattr("docspec.processing.extraction.import_module", missing)
    monkeypatch.setattr("docspec.processing.extraction.distribution_version", lambda _: "6.1.0-fixture")
    extractor = LazyPypdfExtractor()
    source = b"%PDF-fixture-bytes"
    with pytest.raises(ExtractionError, match=r"docspec\[pdf\]"):
        extractor.extract(_captured(source, "application/pdf"), source)


def test_unicode_paragraphs_use_exact_utf8_byte_coordinates() -> None:
    source = "  Alpha §.  \n\n  🧪 beta\r\n\r\nThird  ".encode()
    result = TextExtractor().extract(_captured(source, "text/plain"), source)

    first = ParagraphSegmenter().segment(result.payload)
    retry = ParagraphSegmenter().segment(result.payload)

    assert [segment.content.decode() for segment in first] == ["Alpha §.", "🧪 beta", "Third"]
    assert [segment.segment.segment_id for segment in first] == [segment.segment.segment_id for segment in retry]
    for segment in first:
        coordinate = segment.segment.evidence
        assert coordinate.start is not None and coordinate.end is not None
        assert source[coordinate.start : coordinate.end] == segment.content
        verify_segment_evidence(segment, result.payload, source)
    assert first[1].segment.evidence.start == source.index("🧪".encode())


def test_persisted_segment_reloads_with_exact_representation_and_source_round_trip() -> None:
    source = "First § paragraph.\n\nSecond 🧪 paragraph.".encode()
    extraction = TextExtractor().extract(_captured(source, "text/plain"), source)
    original = ParagraphSegmenter().segment(extraction.payload)[1]

    representation = type(extraction.payload.representation).from_dict(
        extraction.payload.representation.to_dict()
    )
    segment = type(original.segment).from_dict(original.segment.to_dict())
    representation_payload = RepresentationPayload(representation, extraction.payload.content)
    segment_payload = SegmentPayload(segment, original.content)

    assert segment.representation_start == source.index("Second".encode())
    assert segment.representation_start > 0
    assert representation.evidence_mappings[0].transformation == "identity-byte-slice"
    verify_segment_evidence(segment_payload, representation_payload, source)


def test_identical_bytes_share_a_blob_without_sharing_logical_file_lineage() -> None:
    source = b"Repeated publisher bytes."
    first_file = _captured(source, "text/plain")
    second_file = CapturedFile.create(
        source_item_id="source:item-2",
        source_version=first_file.source_version,
        candidate_id=first_file.candidate_id,
        blob=first_file.blob,
        media_type=first_file.media_type,
        acquired_at=first_file.acquired_at,
        downloader_id=first_file.downloader_id,
        transport_version=first_file.transport_version,
    )

    first = TextExtractor().extract(first_file, source).payload
    second = TextExtractor().extract(second_file, source).payload
    first_segment = ParagraphSegmenter().segment(first)[0].segment
    second_segment = ParagraphSegmenter().segment(second)[0].segment

    assert first.representation.blob.digest == second.representation.blob.digest
    assert first.representation.representation_id != second.representation.representation_id
    assert first_segment.segment_id != second_segment.segment_id


def test_json_array_records_are_exact_reversible_source_slices() -> None:
    source = '[ {"name":"§"},\n  {"name":"🧪","values":[1,2]} ]'.encode()
    result = JsonExtractor().extract(_captured(source, "application/json"), source)

    segments = RecordSegmenter().segment(result.payload)

    assert [segment.content.decode() for segment in segments] == [
        '{"name":"§"}',
        '{"name":"🧪","values":[1,2]}',
    ]
    for segment in segments:
        coordinate = segment.segment.evidence
        assert coordinate.start is not None and coordinate.end is not None
        assert source[coordinate.start : coordinate.end] == segment.content
        verify_segment_evidence(segment, result.payload, source)


def test_payload_checks_reject_tampering_before_processing() -> None:
    source = b"one paragraph"
    result = TextExtractor().extract(_captured(source, "text/plain"), source)
    segment = ParagraphSegmenter().segment(result.payload)[0]

    with pytest.raises(IntegrityError, match="segment digest differs"):
        SegmentPayload(segment.segment, b"tampered!!!!!")
    with pytest.raises(IntegrityError, match="representation digest differs"):
        RepresentationPayload(result.payload.representation, b"tampered!!!!!")

    wrong_evidence = segment.segment.evidence.__class__(
        coordinate_system=segment.segment.evidence.coordinate_system,
        source_digest=segment.segment.evidence.source_digest,
        start=1,
        end=len(source),
    )
    with pytest.raises(ValueError, match="segment identity differs"):
        Segment(
            segment.segment.segment_id,
            segment.segment.source_item_id,
            segment.segment.file_id,
            segment.segment.representation_id,
            segment.segment.representation_start,
            segment.segment.representation_end,
            segment.segment.ordinal,
            segment.segment.kind,
            segment.segment.content,
            wrong_evidence,
            segment.segment.segmenter_id,
            segment.segment.policy_digest,
            segment.segment.derivation,
        )


def test_content_records_recompute_identity_and_output_digests_when_read() -> None:
    source = b"identity-bearing content"
    captured = _captured(source, "text/plain")
    extraction = TextExtractor().extract(captured, source)
    segment = ParagraphSegmenter().segment(extraction.payload)[0]
    captured_value = captured.to_dict()
    captured_value["candidateId"] = "alternate"
    with pytest.raises(ValueError, match="captured file identity differs"):
        type(captured).from_dict(captured_value)

    representation_value = extraction.payload.representation.to_dict()
    representation_value["extractorId"] = "alternate"
    with pytest.raises(ValueError, match="representation identity differs"):
        type(extraction.payload.representation).from_dict(representation_value)
    representation_value = extraction.payload.representation.to_dict()
    representation_value["fileId"] = "alternate-file"
    with pytest.raises(ValueError, match="representation identity differs"):
        type(extraction.payload.representation).from_dict(representation_value)

    segment_value = segment.segment.to_dict()
    segment_value["ordinal"] = 12
    with pytest.raises(ValueError, match="segment identity differs"):
        type(segment.segment).from_dict(segment_value)
