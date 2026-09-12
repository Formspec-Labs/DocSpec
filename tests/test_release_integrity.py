from __future__ import annotations

from docspec.runtime import stage_policy

from collections import defaultdict
from copy import deepcopy
from dataclasses import fields, replace

import pytest

from docspec.domain.content import (
    AcquisitionDisposition,
    CandidateFile,
    CapturedFile,
    DerivedRecord,
    Representation,
    Segment,
    SourceItem,
)
from docspec.domain.delivery import iter_delivery_records, verify_logical_release_layers
from docspec.domain.identity import sha256_digest
from docspec.domain.jobs import ChangeKind, DocumentEntry, DocumentStore
from docspec.domain.plans import WorkLimits
from docspec.domain.references import BlobRef
from docspec.errors import IntegrityError
from docspec.processing import ContentStatisticsProcessor, ParagraphSegmenter, TextExtractor
from tests.helpers import processor_payload, segment_processor_request


def _release_layers() -> dict[str, list[dict]]:
    content = b"Exact source paragraph."
    blob = BlobRef(
        "fixture/source.txt",
        sha256_digest(content),
        len(content),
        "text/plain",
    )
    candidate = CandidateFile(
        "primary",
        "source.txt",
        "text/plain",
        expected_digest=blob.digest,
        expected_size=blob.byte_size,
        transport_version="fixture-v1",
    )
    source = SourceItem("source-1", "v1", (candidate,))
    captured = CapturedFile.create(
        source_item_id=source.item_id,
        source_version=source.version,
        candidate_id=candidate.candidate_id,
        blob=blob,
        media_type=candidate.media_type,
        acquired_at="2026-08-05T12:00:01Z",
        downloader_id="fixture-downloader/v1",
        transport_version=candidate.transport_version,
    )
    extraction = TextExtractor().extract(captured, content)
    segment = ParagraphSegmenter().segment(extraction.payload)[0]
    processor = ContentStatisticsProcessor()
    derived = processor.process(
        segment_processor_request(processor, segment),
        processor_payload(segment),
        (),
    ).derived_records[0]
    entry = replace(
        DocumentEntry.create(source, ChangeKind.ADDED, stage_policy(extractor=TextExtractor(), segmenter=ParagraphSegmenter(), processor_ids=(processor.description.processor_id,))),
        captured_files=(captured,),
        representations=(extraction.payload.representation,),
        segments=(segment.segment,),
        derived_records=(derived,),
        disposition=AcquisitionDisposition.CAPTURED,
    )
    store = DocumentStore.planned(
        plan_id="plan-1",
        logical_partition="bucket-00000/store-00000000",
        entries=(entry,),
        limits=WorkLimits(2, 10_000, 10, 10, 10, 10_000, 60, 2),
    )
    layers: defaultdict[str, list[dict]] = defaultdict(list)
    for record in iter_delivery_records(store):
        layers[record.layer_kind].append(record.to_record())
    return dict(layers)


def test_logical_release_verifier_accepts_complete_source_lineage() -> None:
    verify_logical_release_layers(_release_layers())


@pytest.mark.parametrize("observed", [None, "different-version"])
def test_required_transport_version_refuses_missing_or_different_capture_evidence(observed):
    layers = _release_layers()
    original = CapturedFile.from_dict(layers["files"][0]["payload"])
    values = {field.name: getattr(original, field.name) for field in fields(original)
              if field.name not in {"file_id", "disposition"}}
    changed = CapturedFile.create(**(values | {"transport_version": observed}))
    row = layers["files"][0] | {"recordId": changed.file_id, "payload": changed.to_dict()}
    with pytest.raises(IntegrityError, match="file has no matching active source candidate"):
        verify_logical_release_layers({"source-items": layers["source-items"], "files": [row]})


def test_dispositions_preserve_requested_stages_and_refuse_missing_stage_evidence() -> None:
    layers = _release_layers()
    disposition = layers["dispositions"][0]["payload"]
    assert disposition["requestedStages"] == stage_policy(
        extractor=TextExtractor(), segmenter=ParagraphSegmenter(),
        processor_ids=(ContentStatisticsProcessor().description.processor_id,),
    ).to_dict()
    disposition.pop("requestedStages")
    with pytest.raises(IntegrityError, match="invalid closed payload"):
        verify_logical_release_layers(layers)


def test_logical_release_verifier_visits_every_retained_blob_reference() -> None:
    layers = _release_layers()
    visited: list[BlobRef] = []

    verify_logical_release_layers(layers, verify_blob=visited.append)

    assert visited == [
        CapturedFile.from_dict(layers["files"][0]["payload"]).blob,
        Representation.from_dict(layers["representations"][0]["payload"]).blob,
        Segment.from_dict(layers["segments"][0]["payload"]).content,
    ]


def test_logical_release_verifier_rejects_individually_valid_but_unlinked_records() -> None:
    layers = _release_layers()
    broken_representation = deepcopy(layers)
    original = Representation.from_dict(broken_representation["representations"][0]["payload"])
    replacement = Representation.create(
        source_item_id=original.source_item_id,
        file_id="missing-file",
        file_digest=original.file_digest,
        kind=original.kind,
        blob=original.blob,
        extractor_id=original.extractor_id,
        configuration_digest=original.configuration_digest,
        evidence_mappings=original.evidence_mappings,
        warnings=original.warnings,
    )
    broken_representation["representations"][0]["recordId"] = replacement.representation_id
    broken_representation["representations"][0]["payload"] = replacement.to_dict()

    with pytest.raises(IntegrityError, match="representation has broken exact-file lineage"):
        verify_logical_release_layers(broken_representation)

    broken_input = deepcopy(layers)
    derived_kind = "derived:" + ContentStatisticsProcessor().description.processor_id
    original_derived = DerivedRecord.from_dict(broken_input[derived_kind][0]["payload"])
    replacement_derived = DerivedRecord.create(
        source_item_id=original_derived.source_item_id,
        processor_id=original_derived.processor_id,
        input_ids=("missing-segment",),
        schema_id=original_derived.schema_id,
        value=original_derived.value,
        provider_receipt_digest=original_derived.provider_receipt_digest,
        disposition=original_derived.disposition,
    )
    broken_input[derived_kind][0]["recordId"] = replacement_derived.derived_id
    broken_input[derived_kind][0]["payload"] = replacement_derived.to_dict()
    with pytest.raises(IntegrityError, match="derived record names an unavailable"):
        verify_logical_release_layers(broken_input)


def test_logical_release_verifier_rejects_segment_without_persisted_mapping() -> None:
    layers = _release_layers()
    original = Segment.from_dict(layers["segments"][0]["payload"])
    shifted = Segment.create(
        source_item_id=original.source_item_id,
        file_id=original.file_id,
        representation_id=original.representation_id,
        representation_start=original.representation_start + 1,
        representation_end=original.representation_end + 1,
        ordinal=original.ordinal,
        kind=original.kind,
        content=original.content,
        evidence=original.evidence,
        segmenter_id=original.segmenter_id,
        policy_digest=original.policy_digest,
        derivation=original.derivation,
    )
    layers["segments"][0]["recordId"] = shifted.segment_id
    layers["segments"][0]["payload"] = shifted.to_dict()

    with pytest.raises(IntegrityError, match="no persisted reversible representation mapping"):
        verify_logical_release_layers(layers)


def test_derived_output_requires_its_own_documents_requested_processor() -> None:
    layers = _release_layers()
    layers["dispositions"][0]["payload"]["requestedStages"]["processorIds"] = []
    with pytest.raises(IntegrityError, match="not requested by its source item"):
        verify_logical_release_layers(layers)
