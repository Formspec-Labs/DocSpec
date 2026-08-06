from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from docspec.domain.content import CapturedFile, Segment
from docspec.domain.identity import identity_digest, sha256_digest
from docspec.domain.references import BlobRef
from docspec.errors import IntegrityError
from docspec.processing import (
    ContentStatisticsProcessor,
    DefaultExtractorRegistry,
    DefaultSegmenterRegistry,
    HtmlExtractor,
    ImageExtractor,
    JsonExtractor,
    LazyPypdfExtractor,
    ParagraphSegmenter,
    ProcessorCacheMode,
    ProcessorItemLimits,
    ProcessorResult,
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
from tests.helpers import processor_payload, segment_processor_request


def _captured(content: bytes, media_type: str) -> CapturedFile:
    blob = BlobRef(
        locator=f"fixture://{sha256_digest(content).removeprefix('sha256:')}",
        digest=sha256_digest(content),
        byte_size=len(content),
        media_type=media_type,
    )
    return CapturedFile.create(
        source_item_id="source:item-1",
        source_version="2026-08-05",
        candidate_id="primary",
        blob=blob,
        media_type=media_type,
        acquired_at="2026-08-05T12:01:00Z",
        downloader_id="fixture-downloader/v1",
        transport_version="fixture-v1",
        acquisition_started_at="2026-08-05T12:00:00Z",
        downloader_configuration_digest=sha256_digest(b"fixture-downloader-config"),
        task_id="fixture-task",
        attempt_id="fixture-attempt",
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
        assert registry.extract(_captured(content, media_type), content).payload.representation.kind == kind

    with pytest.raises(ExtractionError, match="no extractor"):
        registry.extract(_captured(b"bytes", "application/octet-stream"), b"bytes")


def test_pypdf_is_loaded_only_when_selected_and_pages_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakePage:
        def __init__(self, text: str | None) -> None:
            self._text = text

        def extract_text(self) -> str | None:
            return self._text

    class FakeReader:
        is_encrypted = False

        def __init__(self, stream: object, *, strict: bool) -> None:
            assert stream is not None
            assert strict is False
            self.pages = [FakePage("Page one §"), FakePage(""), FakePage("Page three 🧪")]

    provider = SimpleNamespace(__version__="6.1.0-fixture", PdfReader=FakeReader)
    imports: list[str] = []

    def import_provider(name: str) -> object:
        imports.append(name)
        return provider

    monkeypatch.setattr("docspec.processing.extraction.import_module", import_provider)
    extractor = LazyPypdfExtractor()
    assert imports == []
    source = b"%PDF-fixture-bytes"
    result = extractor.extract(_captured(source, "application/pdf"), source)
    persisted_representation = type(result.payload.representation).from_dict(
        result.payload.representation.to_dict()
    )
    persisted_payload = RepresentationPayload(persisted_representation, result.payload.content)
    segments = tuple(
        SegmentPayload(type(segment.segment).from_dict(segment.segment.to_dict()), segment.content)
        for segment in DefaultSegmenterRegistry().segment(persisted_payload)
    )

    assert imports
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


def test_missing_optional_pdf_profile_has_one_actionable_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_: str) -> object:
        raise ModuleNotFoundError("pypdf")

    monkeypatch.setattr("docspec.processing.extraction.import_module", missing)
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
    processor = ContentStatisticsProcessor()
    derived = processor.process(
        segment_processor_request(processor, segment),
        processor_payload(segment),
        (),
    ).derived_records[0]

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

    derived_value = derived.to_dict()
    derived_value["value"]["byteCount"] += 1
    with pytest.raises(ValueError, match="derived output digest differs"):
        type(derived).from_dict(derived_value)


def test_injected_statistics_processor_is_deterministic_and_evidence_linked() -> None:
    source = "Alpha §\nBeta 🧪".encode()
    extraction = TextExtractor().extract(_captured(source, "text/plain"), source)
    segment = ParagraphSegmenter().segment(extraction.payload)[0]
    processor = ContentStatisticsProcessor()

    request = segment_processor_request(processor, segment)
    first = processor.process(request, processor_payload(segment), ())
    retry = processor.process(request, processor_payload(segment), ())

    first_record = first.derived_records[0]
    assert first_record == retry.derived_records[0]
    assert isinstance(first, ProcessorResult)
    assert first.provider_receipt == retry.provider_receipt
    assert first_record.value["byteCount"] == len(source)
    assert first_record.value["utf8CodepointCount"] == len(source.decode())
    assert first_record.value["wordCount"] == 4
    assert first_record.value["evidence"] == segment.segment.evidence.to_dict()
    assert first_record.input_ids == (segment.segment.segment_id,)
    assert processor.description.name == "content-statistics"
    assert processor.description.input_kinds == ("segment",)
    assert processor.description.accepted_inputs[0].schema_ids == ("docspec-segment/1",)
    assert processor.description.output_media_types == ("application/vnd.docspec.content-statistics+json",)
    assert processor.description.external_resources == ()
    assert processor.description.cache_policy.mode is ProcessorCacheMode.EXACT_INPUTS
    assert first.provider_receipt["processorDescriptionDigest"] == identity_digest(processor.description.to_dict())

    limited = ContentStatisticsProcessor(
        item_limits=ProcessorItemLimits(
            max_input_records=1,
            max_input_bytes=len(source) - 1,
            max_output_records=1,
            max_output_bytes=1024,
            max_duration_seconds=30,
        )
    )
    assert limited.description.processor_id != processor.description.processor_id
    with pytest.raises(IntegrityError, match="input exceeds"):
        limited.process(
            segment_processor_request(limited, segment),
            processor_payload(segment),
            (),
        )

    class ProviderSdkResponse:
        pass

    with pytest.raises(ValueError, match="unsupported type"):
        replace(first, provider_receipt={"rawResponse": ProviderSdkResponse()})
