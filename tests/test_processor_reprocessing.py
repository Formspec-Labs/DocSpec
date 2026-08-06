from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters.source_catalog import LocalFileContentFetcher, LocalJsonlSourceCatalog
from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.application.execution import StoreExecutionService
from docspec.domain.content import DerivedRecord, ProcessorDisposition, SourceItem
from docspec.domain.identity import canonical_json_bytes, identity_digest
from docspec.domain.jobs import ChangeKind, EntryExecutionMode
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import (
    AcceptedFailurePolicy,
    DataUsePolicy,
    ProcessorExecutionScope,
    RetentionPolicy,
    RetryPolicy,
)
from docspec.domain.processors import (
    ProcessorCacheMode,
    ProcessorCachePolicy,
    ProcessorDescription,
    ProcessorInput,
    ProcessorItemLimits,
    ProcessorPayload,
    ProcessorRequest,
    ProcessorResourceIdentity,
    ProcessorResourceKind,
    ProcessorResourceUse,
    ProcessorResult,
    ProcessorSet,
)
from docspec.domain.storage import PartitionPolicy
from docspec.errors import IntegrityError
from docspec.processing.extraction import DefaultExtractorRegistry, TextExtractor
from docspec.processing.segmentation import DefaultSegmenterRegistry, ParagraphSegmenter
from tests.helpers import local_profile_set, processor_payload, segment_processor_request
from tests.test_application_pipeline import _run, _write_source
from tests.test_processing_pipeline import _captured


class _CountingFetcher:
    def __init__(self, delegate: LocalFileContentFetcher) -> None:
        self.delegate = delegate
        self.calls: list[str] = []

    def fetch(self, candidate, *, max_bytes: int, task_id: str, attempt_id: str):
        self.calls.append(candidate.candidate_id)
        return self.delegate.fetch(
            candidate,
            max_bytes=max_bytes,
            task_id=task_id,
            attempt_id=attempt_id,
        )


class _CountingExtractor:
    extractor_id = DefaultExtractorRegistry.extractor_id

    def __init__(self) -> None:
        self.delegate = DefaultExtractorRegistry()
        self.calls = 0

    def extract(self, source, content):
        self.calls += 1
        return self.delegate.extract(source, content)


class _CountingSegmenter:
    segmenter_id = DefaultSegmenterRegistry.segmenter_id

    def __init__(self) -> None:
        self.delegate = DefaultSegmenterRegistry()
        self.calls = 0

    def segment(self, representation):
        self.calls += 1
        return self.delegate.segment(representation)


class _CountingProcessor:
    def __init__(self, description: ProcessorDescription) -> None:
        self.description = description
        self.calls: list[str] = []
        self.representation_ranges: list[tuple[int, int]] = []

    def process(
        self,
        request: ProcessorRequest,
        payload: ProcessorPayload,
        prerequisite_results: tuple[ProcessorResult, ...],
    ) -> ProcessorResult:
        payload.require("content")
        payload.require("representationCoordinates")
        content = payload.content
        representation_coordinates = payload.representation_coordinates
        if content is None or representation_coordinates is None:
            raise IntegrityError("test processor received an incomplete projected payload")
        input_record = payload.input_record
        self.calls.append(input_record.record_id)
        self.representation_ranges.append(representation_coordinates)
        value: dict[str, Any] = {
            "processorName": self.description.name,
            "processorVersion": self.description.version,
            "segmentId": input_record.record_id,
            "contentDigest": input_record.record_digest,
        }
        input_ids = (
            input_record.record_id,
            *(
                record.derived_id
                for prerequisite in prerequisite_results
                for record in prerequisite.derived_records
            ),
        )
        receipt: dict[str, Any] = {
            "executionKind": "test-deterministic",
            "requestId": request.request_id,
            "reuseKey": request.reuse_key,
            "processorId": self.description.processor_id,
            "processorDescriptionDigest": identity_digest(self.description.to_dict()),
            "inputIds": list(input_ids),
            "outputDigest": identity_digest(value),
            "outputSchemaId": self.description.output_schema_id,
            "outputMediaType": self.description.output_media_types[0],
            "configurationDigest": self.description.configuration_digest,
            "dataUsePolicyDigest": self.description.data_use_policy_digest,
            "retryPolicyDigest": self.description.retry_policy_digest,
        }
        record = DerivedRecord.create(
            source_item_id=request.source_item_id,
            processor_id=self.description.processor_id,
            input_ids=input_ids,
            schema_id=self.description.output_schema_id,
            value=value,
            provider_receipt_digest=identity_digest(receipt),
            disposition=ProcessorDisposition.PRODUCED,
        )
        return ProcessorResult(
            request.request_id,
            request.reuse_key,
            ProcessorDisposition.PRODUCED,
            self.description.output_media_types[0],
            self.description.external_resources,
            (record,),
            ProcessorResourceUse(
                len(content)
                + sum(len(canonical_json_bytes(result.to_dict())) for result in prerequisite_results),
                len(canonical_json_bytes(value)),
                0,
            ),
            (),
            receipt,
        )


def _description(
    name: str,
    version: str,
    retry: RetryPolicy,
    *,
    dependencies: tuple[str, ...] = (),
    external_resources: tuple[ProcessorResourceIdentity, ...] = (),
    output_media_types: tuple[str, ...] = ("application/json",),
) -> ProcessorDescription:
    return ProcessorDescription.create(
        name=name,
        version=version,
        implementation_id=f"tests.{name}/{version}",
        accepted_inputs=(ProcessorInput("segment", ("docspec-segment/1",), ("*/*",)),),
        output_schema_id=f"tests-{name}-output/1",
        output_media_types=output_media_types,
        execution_scope=ProcessorExecutionScope.LOCAL_ONLY,
        external_resources=external_resources,
        dependencies=dependencies,
        deterministic=True,
        cache_policy=ProcessorCachePolicy(ProcessorCacheMode.EXACT_INPUTS, "tests-exact-inputs/1"),
        configuration_digest=identity_digest({"name": name, "version": version}),
        data_use_policy_digest=DataUsePolicy.local_content().digest,
        item_limits=ProcessorItemLimits(10, 1024 * 1024, 1, 1024 * 1024, 60),
        retry_policy_digest=retry.digest,
        capabilities=("test-output",),
    )


def test_processor_result_must_report_declared_media_and_resources() -> None:
    model = ProcessorResourceIdentity(
        "tests.model",
        ProcessorResourceKind.MODEL,
        "1",
        identity_digest({"model": "tests.model", "revision": "1"}),
    )
    retry = RetryPolicy(base_delay_milliseconds=0)
    processor = _CountingProcessor(
        _description(
            "resource-aware",
            "1",
            retry,
            external_resources=(model,),
            output_media_types=("application/vnd.tests.result+json",),
        )
    )
    source = b"processor declaration fixture"
    extraction = TextExtractor().extract(_captured(source, "text/plain"), source)
    segment = ParagraphSegmenter().segment(extraction.payload)[0]
    request = segment_processor_request(processor, segment)
    result = processor.process(request, processor_payload(segment), ())

    StoreExecutionService._validate_processor_result(
        result,
        request,
        processor.description,
        segment.segment,
        len(segment.content),
        (),
        data_use_policy=DataUsePolicy.local_content(),
        require_current_request=True,
    )
    with pytest.raises(IntegrityError, match="media type"):
        StoreExecutionService._validate_processor_result(
            replace(result, output_media_type="application/json"),
            request,
            processor.description,
            segment.segment,
            len(segment.content),
            (),
            data_use_policy=DataUsePolicy.local_content(),
            require_current_request=True,
        )
    with pytest.raises(IntegrityError, match="resources"):
        StoreExecutionService._validate_processor_result(
            replace(result, resource_identities=()),
            request,
            processor.description,
            segment.segment,
            len(segment.content),
            (),
            data_use_policy=DataUsePolicy.local_content(),
            require_current_request=True,
        )


def _plan(
    source,
    base,
    processors: tuple[_CountingProcessor, ...],
    retry: RetryPolicy,
    accepted: AcceptedFailurePolicy,
) -> ProcessingPlan:
    declared = ProcessorSet(tuple(item.description for item in processors))
    processor_set = ProcessorSet(declared.execution_order)
    stages = StagePolicy(
        (DefaultExtractorRegistry.extractor_id,),
        DefaultSegmenterRegistry.segmenter_id,
        tuple(item.processor_id for item in processor_set.execution_order),
    )
    return ProcessingPlan.create(
        source_catalog=source,
        base_release=base,
        profiles=local_profile_set(),
        limits=WorkLimits(2, 1024 * 1024, 100, 100, 1000, 1024 * 1024, 60, retry.max_attempts),
        stages=stages,
        processors=processor_set,
        partition_count=8,
        selection={},
        retention_policy=RetentionPolicy.retain_all(),
        data_use_policy=DataUsePolicy.local_content(),
        retry_policy_digest=retry.digest,
        accepted_failure_policy_digest=accepted.digest,
    )


def _payloads(catalog, release, layer_kind: str) -> list[dict[str, Any]]:
    return [row["payload"] for row in catalog.scan(release, layer_kind=layer_kind)]


def test_changed_processor_reuses_content_and_runs_only_it_and_dependents(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    candidate = _write_source(sources / "document.txt", "First paragraph.\n\nSecond paragraph.")
    item = SourceItem("document-a", "v1", (candidate,), metadata={"expectedSegments": 2})

    source_catalog = LocalJsonlSourceCatalog(tmp_path / "source-catalogs")
    source_ref = source_catalog.write((item,))
    controls = LocalJsonControlRepository(tmp_path / "controls")
    stores = LocalDocumentStoreRepository(tmp_path / "stores")
    blobs = LocalContentAddressedBlobStore(tmp_path / "blobs")
    records = LocalJsonlRecordStorage(tmp_path / "records")
    partition_policy = PartitionPolicy("source-item-sha256-v1", 8)
    catalog = LocalManifestDocumentCatalog(
        tmp_path / "document-catalog",
        records=records,
        stores=stores,
        controls=controls,
        blobs=blobs,
    )
    fetcher = _CountingFetcher(LocalFileContentFetcher(sources))
    extractor = _CountingExtractor()
    segmenter = _CountingSegmenter()
    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()

    first_root = _CountingProcessor(_description("root", "1", retry))
    unaffected = _CountingProcessor(_description("unaffected", "1", retry))
    first_dependent = _CountingProcessor(
        _description("dependent", "1", retry, dependencies=(first_root.description.processor_id,))
    )
    first_processors = (first_root, unaffected, first_dependent)
    first_plan = _plan(source_ref, None, first_processors, retry, accepted)
    _, _, _, _, first_release = _run(
        plan=first_plan,
        source_catalog=source_catalog,
        controls=controls,
        stores=stores,
        blobs=blobs,
        records=records,
        catalog=catalog,
        fetcher=fetcher,
        processors=first_processors,
        extractor=extractor,
        segmenter=segmenter,
        partition_policy=partition_policy,
    )

    base_payloads = {
        kind: _payloads(catalog, first_release, kind)
        for kind in ("files", "representations", "segments")
    }
    initial_counts = (len(fetcher.calls), extractor.calls, segmenter.calls, len(unaffected.calls))
    assert initial_counts == (1, 1, 1, 2)

    changed_root = _CountingProcessor(_description("root", "2", retry))
    changed_dependent = _CountingProcessor(
        _description("dependent", "2", retry, dependencies=(changed_root.description.processor_id,))
    )
    second_processors = (changed_root, unaffected, changed_dependent)
    second_plan = _plan(source_ref, first_release, second_processors, retry, accepted)
    planned, _, _, _, second_release = _run(
        plan=second_plan,
        source_catalog=source_catalog,
        controls=controls,
        stores=stores,
        blobs=blobs,
        records=records,
        catalog=catalog,
        fetcher=fetcher,
        processors=second_processors,
        extractor=extractor,
        segmenter=segmenter,
        partition_policy=partition_policy,
    )

    entry = stores.load(planned[0]).entries[0]
    assert entry.change == ChangeKind.REPAIR
    assert entry.execution_mode == EntryExecutionMode.PROCESSORS_ONLY
    assert set(entry.requested_stages.processor_ids) == {
        changed_root.description.processor_id,
        changed_dependent.description.processor_id,
    }
    assert (len(fetcher.calls), extractor.calls, segmenter.calls, len(unaffected.calls)) == initial_counts
    assert len(changed_root.calls) == len(changed_dependent.calls) == 2
    expected_ranges = [
        (payload["representationStart"], payload["representationEnd"])
        for payload in base_payloads["segments"]
    ]
    assert changed_root.representation_ranges == expected_ranges
    assert changed_dependent.representation_ranges == expected_ranges
    assert any(start > 0 for start, _ in expected_ranges)
    assert all(_payloads(catalog, second_release, kind) == base_payloads[kind] for kind in base_payloads)

    release = catalog.open(second_release)
    layer_kinds = {layer.layer_kind for layer in release.active_layers}
    assert f"derived:{first_root.description.processor_id}" not in layer_kinds
    assert f"derived:{first_dependent.description.processor_id}" not in layer_kinds
    assert f"derived:{changed_root.description.processor_id}" in layer_kinds
    assert f"derived:{changed_dependent.description.processor_id}" in layer_kinds
    assert _payloads(catalog, second_release, f"derived:{unaffected.description.processor_id}") == _payloads(
        catalog,
        first_release,
        f"derived:{unaffected.description.processor_id}",
    )

    processor_call_counts = {
        item.description.processor_id: len(item.calls)
        for item in (*first_processors, changed_root, changed_dependent)
    }
    removal_plan = _plan(source_ref, second_release, (), retry, accepted)
    removal_planned, _, _, _, removal_release = _run(
        plan=removal_plan,
        source_catalog=source_catalog,
        controls=controls,
        stores=stores,
        blobs=blobs,
        records=records,
        catalog=catalog,
        fetcher=fetcher,
        processors=(),
        extractor=extractor,
        segmenter=segmenter,
        partition_policy=partition_policy,
    )
    removal_entry = stores.load(removal_planned[0]).entries[0]
    assert removal_entry.execution_mode == EntryExecutionMode.PROCESSORS_ONLY
    assert removal_entry.requested_stages.processor_ids == ()
    assert not any(layer.layer_kind.startswith("derived:") for layer in catalog.open(removal_release).active_layers)
    assert (len(fetcher.calls), extractor.calls, segmenter.calls) == initial_counts[:3]
    assert {
        item.description.processor_id: len(item.calls)
        for item in (*first_processors, changed_root, changed_dependent)
    } == processor_call_counts

    added = _CountingProcessor(_description("added", "1", retry))
    addition_plan = _plan(source_ref, removal_release, (added,), retry, accepted)
    addition_planned, _, _, _, addition_release = _run(
        plan=addition_plan,
        source_catalog=source_catalog,
        controls=controls,
        stores=stores,
        blobs=blobs,
        records=records,
        catalog=catalog,
        fetcher=fetcher,
        processors=(added,),
        extractor=extractor,
        segmenter=segmenter,
        partition_policy=partition_policy,
    )
    addition_entry = stores.load(addition_planned[0]).entries[0]
    assert addition_entry.requested_stages.processor_ids == (added.description.processor_id,)
    assert len(added.calls) == 2
    assert (len(fetcher.calls), extractor.calls, segmenter.calls) == initial_counts[:3]

    renamed = _CountingProcessor(_description("renamed", "1", retry))
    rename_plan = _plan(source_ref, addition_release, (renamed,), retry, accepted)
    rename_planned, _, _, _, rename_release = _run(
        plan=rename_plan,
        source_catalog=source_catalog,
        controls=controls,
        stores=stores,
        blobs=blobs,
        records=records,
        catalog=catalog,
        fetcher=fetcher,
        processors=(renamed,),
        extractor=extractor,
        segmenter=segmenter,
        partition_policy=partition_policy,
    )
    rename_entry = stores.load(rename_planned[0]).entries[0]
    assert rename_entry.requested_stages.processor_ids == (renamed.description.processor_id,)
    rename_layers = {layer.layer_kind for layer in catalog.open(rename_release).active_layers}
    assert f"derived:{added.description.processor_id}" not in rename_layers
    assert f"derived:{renamed.description.processor_id}" in rename_layers
    assert len(added.calls) == 2
    assert len(renamed.calls) == 2
    assert (len(fetcher.calls), extractor.calls, segmenter.calls) == initial_counts[:3]
