from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from docspec.adapters.platform_artifact import LocalPlatformSourceCatalog
from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.application.execution import StoreExecutionService
from docspec.domain.content import AcquisitionDisposition, SourceItem
from docspec.domain.jobs import ChangeKind, DocumentEntry, DocumentStore, EntryExecutionMode, StoreState
from docspec.domain.plans import ProcessingPlan, StagePolicy
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.storage import PartitionPolicy
from docspec.errors import IntegrityError
from tests.test_application_pipeline import _run, _write_source
from tests.helpers import SharedFixtureContentFetcher, write_shared_source_catalog
from tests.test_processor_reprocessing import (
    _CountingExtractor,
    _CountingFetcher,
    _CountingProcessor,
    _CountingSegmenter,
    _description,
    _plan,
)
from tests.test_stage_checkpoint_recovery import (
    _InterruptAfterStageRepository,
    _WorkerInterrupted,
)


NOW = "2026-08-05T12:00:00Z"


def _with_processor_cost(plan: ProcessingPlan, maximum: int) -> ProcessingPlan:
    return ProcessingPlan.create(
        source_catalog=plan.source_catalog,
        base_release=plan.base_release,
        profiles=plan.profiles,
        limits=replace(plan.limits, max_processor_cost=maximum),
        stages=plan.stages,
        processors=plan.processors,
        partition_count=plan.partition_count,
        selection=plan.selection,
        retention_policy=plan.retention_policy,
        data_use_policy=plan.data_use_policy,
        retry_policy_digest=plan.retry_policy_digest,
        accepted_failure_policy_digest=plan.accepted_failure_policy_digest,
    )


def test_processor_only_restart_reuses_base_and_checkpoints_changed_layers(
    tmp_path: Path,
) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    candidate = _write_source(sources / "document.txt", "First paragraph.\n\nSecond paragraph.")
    item = SourceItem("document-a", "v1", (candidate,), metadata={"expectedSegments": 2})
    source_catalog_root = tmp_path / "source-catalogs"
    source_catalog_root.mkdir()
    source_catalog = LocalPlatformSourceCatalog(source_catalog_root)
    source_ref = write_shared_source_catalog(source_catalog_root, (item,))
    mapped_item = next(source_catalog.stream(source_ref))
    controls = LocalJsonControlRepository(tmp_path / "controls")
    stores = LocalDocumentStoreRepository(tmp_path / "stores")
    blobs = LocalContentAddressedBlobStore(tmp_path / "blobs")
    records = LocalJsonlRecordStorage(tmp_path / "records")
    catalog = LocalManifestDocumentCatalog(
        tmp_path / "document-catalog",
        records=records,
        stores=stores,
        controls=controls,
        blobs=blobs,
    )
    partition_policy = PartitionPolicy("source-item-sha256-v1", 8)
    fetcher = _CountingFetcher(SharedFixtureContentFetcher(sources))
    extractor = _CountingExtractor()
    segmenter = _CountingSegmenter()
    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()

    original_root = _CountingProcessor(_description("root", "1", retry))
    unaffected = _CountingProcessor(_description("unaffected", "1", retry))
    original_dependent = _CountingProcessor(
        _description("dependent", "1", retry, dependencies=(original_root.description.processor_id,))
    )
    original_processors = (original_root, unaffected, original_dependent)
    base_plan = _plan(source_ref, None, original_processors, retry, accepted)
    _, _, _, _, base_release = _run(
        plan=base_plan,
        source_catalog=source_catalog,
        controls=controls,
        stores=stores,
        blobs=blobs,
        records=records,
        catalog=catalog,
        fetcher=fetcher,
        processors=original_processors,
        extractor=extractor,
        segmenter=segmenter,
        partition_policy=partition_policy,
    )
    base_counts = (len(fetcher.calls), extractor.calls, segmenter.calls, len(unaffected.calls))
    assert base_counts == (1, 1, 1, 2)

    changed_root = _CountingProcessor(_description("root", "2", retry))
    changed_dependent = _CountingProcessor(
        _description("dependent", "2", retry, dependencies=(changed_root.description.processor_id,))
    )
    processors = (changed_root, unaffected, changed_dependent)
    broad_plan = _plan(source_ref, base_release, processors, retry, accepted)
    plan = _with_processor_cost(broad_plan, 4)
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    requested = StagePolicy(
        plan.stages.extractor_ids,
        plan.stages.segmenter_id,
        (changed_root.description.processor_id, changed_dependent.description.processor_id),
    )
    planned = DocumentStore.planned(
        plan_id=plan.plan_id,
        logical_partition="bucket-00000/store-00000000",
        entries=(
            DocumentEntry.create(
                    mapped_item,
                ChangeKind.REPAIR,
                requested,
                execution_mode=EntryExecutionMode.PROCESSORS_ONLY,
            ),
        ),
        limits=plan.limits,
    )
    planned_ref = stores.save(planned)

    def service(repository) -> StoreExecutionService:
        return StoreExecutionService(
            plan_ref=plan_ref,
            controls=controls,
            stores=repository,
            document_catalog=catalog,
            blobs=blobs,
            fetcher=fetcher,
            extractor=extractor,
            segmenter=segmenter,
            processors={processor.description.processor_id: processor for processor in processors},
            retry_policy=retry,
            accepted_failure_policy=accepted,
            processor_cache=None,
            clock=lambda: NOW,
            sleep=lambda _: None,
        )

    def changed_root_frontier(store: DocumentStore) -> bool:
        entry = store.entries[0]
        processor_ids = {record.processor_id for record in entry.derived_records}
        return (
            store.state is StoreState.RUNNING
            and not entry.terminal
            and entry.execution_mode is EntryExecutionMode.PROCESSORS_ONLY
            and changed_root.description.processor_id in processor_ids
            and unaffected.description.processor_id in processor_ids
            and changed_dependent.description.processor_id not in processor_ids
        )

    interrupting = _InterruptAfterStageRepository(stores, changed_root_frontier)
    with pytest.raises(_WorkerInterrupted, match="durable stage checkpoint"):
        service(interrupting).execute_store(planned_ref)

    checkpoint_ref = stores.latest(planned.store_id)
    assert checkpoint_ref is not None
    checkpoint = stores.load(checkpoint_ref)
    assert changed_root_frontier(checkpoint)
    assert (len(fetcher.calls), extractor.calls, segmenter.calls, len(unaffected.calls)) == base_counts
    assert len(changed_root.calls) == 2
    assert not changed_dependent.calls

    resumed_ref = service(stores).execute_store(planned_ref)
    resumed = stores.load(resumed_ref)
    entry = resumed.entries[0]

    assert entry.disposition is AcquisitionDisposition.CAPTURED
    assert (len(fetcher.calls), extractor.calls, segmenter.calls, len(unaffected.calls)) == base_counts
    assert len(changed_root.calls) == len(changed_dependent.calls) == 2
    assert len(resumed.attempts) == 2
    assert len(stores.revisions(planned.store_id)) == 6

    changed_root_receipts = [
        reference
        for reference in entry.stage_receipts
        if (value := controls.load(reference)).get("format")
        == "docspec-processor-invocation-receipt"
        and value.get("processorId") == changed_root.description.processor_id
    ]
    assert len(changed_root_receipts) == 2
    receipt_path = controls.root / changed_root_receipts[0].locator
    receipt_path.write_bytes(receipt_path.read_bytes() + b"tampered")
    calls_before_tamper_check = (
        len(fetcher.calls),
        extractor.calls,
        segmenter.calls,
        len(unaffected.calls),
        len(changed_root.calls),
        len(changed_dependent.calls),
    )

    with pytest.raises(IntegrityError, match="artifact|bytes|digest|canonical"):
        service(stores).execute_store(planned_ref)

    assert (
        len(fetcher.calls),
        extractor.calls,
        segmenter.calls,
        len(unaffected.calls),
        len(changed_root.calls),
        len(changed_dependent.calls),
    ) == calls_before_tamper_check
