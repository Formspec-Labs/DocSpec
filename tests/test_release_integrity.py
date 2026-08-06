from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import replace

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
from docspec.domain.plans import StagePolicy, WorkLimits
from docspec.domain.references import BlobRef
from docspec.errors import IntegrityError
from docspec.processing import ContentStatisticsProcessor, ParagraphSegmenter, TextExtractor
from tests.helpers import segment_processor_request


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
        segment,
        (),
    ).derived_records[0]
    entry = replace(
        DocumentEntry.create(source, ChangeKind.ADDED, StagePolicy((captured.downloader_id,), segment.segment.segmenter_id)),
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
