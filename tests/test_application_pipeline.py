from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from docspec.adapters.execution import LocalExecutionBackend
from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.adapters.sinks import DurableDatasetSink
from tests.legacy_source_catalog import (
    LocalJsonlSourceCatalog,
    LocalSourceReleaseReader,
    SourceReleaseCatalogView,
)
from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.application.commit import ReleaseCommitService
from docspec.application.delivery import StoreDeliveryService
from docspec.application.execution import StoreExecutionService
from docspec.application.planner import RunPlanner
from docspec.application.reconcile import RunReconciler
from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.delivery import core_delivery_schemas
from docspec.domain.execution import (
    EXECUTE_AND_DELIVER_OPERATION_ID,
    ExecutionHandoff,
    ExecutionLimits,
    ExecutionProfile,
    StoreTask,
    StoreTaskResult,
    iter_store_tasks,
    summarize_store_tasks,
)
from docspec.domain.identity import sha256_digest
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, DataUsePolicy, RetentionPolicy, RetryPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.receipts import RunReceipt
from docspec.domain.references import BlobRef, StoreRef
from docspec.domain.storage import PartitionPolicy, partition_bucket
from docspec.errors import IntegrityError
from docspec.processing.extraction import DefaultExtractorRegistry
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.processing.segmentation import DefaultSegmenterRegistry
from tests.helpers import local_profile_set


def _clock() -> str:
    return "2026-08-05T12:00:00Z"


def _write_source(path: Path, text: str) -> CandidateFile:
    path.write_text(text, encoding="utf-8")
    payload = path.read_bytes()
    return CandidateFile(
        "primary",
        path.name,
        "text/plain",
        expected_digest=sha256_digest(payload),
        expected_size=len(payload),
        transport_version=f"fixture:{sha256_digest(payload)}",
    )


def _plan(
    source,
    base,
    processor,
    retry,
    accepted,
    *,
    buckets: int,
    max_entries: int = 2,
) -> ProcessingPlan:
    return ProcessingPlan.create(
        source_catalog=source,
        base_release=base,
        profiles=local_profile_set(),
        limits=WorkLimits(max_entries, 1024 * 1024, 100, 100, 1000, 1024 * 1024, 60, retry.max_attempts),
        stages=StagePolicy(
            (DefaultExtractorRegistry.extractor_id,),
            DefaultSegmenterRegistry.segmenter_id,
            (processor.description.processor_id,),
        ),
        processors=ProcessorSet((processor.description,)),
        partition_count=buckets,
        selection={},
        retention_policy=RetentionPolicy.retain_all(),
        data_use_policy=DataUsePolicy.local_content(),
        retry_policy_digest=retry.digest,
        accepted_failure_policy_digest=accepted.digest,
    )


def _run(
    *,
    plan: ProcessingPlan,
    source_catalog,
    controls,
    stores,
    blobs,
    records,
    catalog,
    fetcher,
    processor=None,
    processors=None,
    extractor=None,
    segmenter=None,
    processor_cache=None,
    partition_policy,
    accepted_failure_policy=None,
):
    configured_processors = processors
    if configured_processors is None:
        if processor is None:
            raise ValueError("the test run requires at least one processor")
        configured_processors = (processor,)
    processor_registry = {item.description.processor_id: item for item in configured_processors}
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    planned = tuple(
        RunPlanner(
            source_catalog=source_catalog,
            document_catalog=catalog,
            stores=stores,
            controls=controls,
            workspace_factory=LocalSqliteReconciliationWorkspaceFactory(
                records.root / ".planning",
                read_batch_size=1,
            ),
        ).plan_run(plan.source_catalog, plan.base_release, plan_ref)
    )
    retry = RetryPolicy(max_attempts=plan.limits.max_attempts, base_delay_milliseconds=0)
    executor = StoreExecutionService(
        plan_ref=plan_ref,
        controls=controls,
        stores=stores,
        document_catalog=catalog,
        blobs=blobs,
        fetcher=fetcher,
        extractor=extractor or DefaultExtractorRegistry(),
        segmenter=segmenter or DefaultSegmenterRegistry(),
        processors=processor_registry,
        retry_policy=retry,
        accepted_failure_policy=accepted_failure_policy or AcceptedFailurePolicy(),
        processor_cache=processor_cache,
        clock=_clock,
        sleep=lambda _: None,
    )
    blob_root = controls.put(
        kind="profile-state",
        artifact_id="urn:docspec:test:blob-root",
        value={
            "profileId": "urn:docspec:profile:blob-storage:local-content-addressed:1",
            "profileVersion": "1.0.0",
            "storageRoot": blobs.root.as_posix(),
        },
    )
    sink = DurableDatasetSink(
        sink_id="urn:docspec:test:sink:durable",
        profile_id="urn:docspec:profile:result-delivery:durable-dataset:1",
        storage=records,
        partition_policy=partition_policy,
        blob_roots=(blob_root,),
        clock=_clock,
    )
    sink_ref = controls.put(
        kind="sinks",
        artifact_id=sink.sink_id,
        value={"sinkId": sink.sink_id, "profileId": sink.profile_id},
    )
    delivery = StoreDeliveryService(stores=stores, controls=controls, sinks={sink.sink_id: sink})
    worker_composition = controls.put(
        kind="worker-compositions",
        artifact_id="urn:docspec:test:worker-composition",
        value={"implementationId": "tests.local-worker/v1", "planId": plan.plan_id},
    )
    scheduler_configuration = controls.put(
        kind="scheduler-configurations",
        artifact_id="urn:docspec:test:scheduler-configuration",
        value={"adapterId": "docspec.local-threaded", "maxWorkers": 2, "maxInFlight": 2},
    )
    execution_profile = ExecutionProfile(
        "docspec.local-threaded",
        "1.0.0",
        worker_composition,
        scheduler_configuration,
        ExecutionLimits(2, 1, 2, 4 * 1024**3, 8 * 1024**3, 100, 2, 1, 0, 0),
        2_000_000_000,
    )
    execution_profile_ref = controls.put(
        kind="execution-profiles",
        artifact_id=execution_profile.profile_id,
        value=execution_profile.to_dict(),
    )
    tasks = tuple(iter_store_tasks(plan.plan_id, EXECUTE_AND_DELIVER_OPERATION_ID, planned))
    task_count, task_set_digest = summarize_store_tasks(tasks)
    handoff = ExecutionHandoff(
        processing_plan=plan_ref,
        execution_profile=execution_profile_ref,
        worker_composition=worker_composition,
        planned_store_ledger=stores.planned_store_ledger(plan.plan_id),
        operation_id=EXECUTE_AND_DELIVER_OPERATION_ID,
        expected_task_count=task_count,
        task_set_digest=task_set_digest,
        result_sink=sink_ref,
        base_release=plan.base_release,
    )
    handoff_ref = controls.put(
        kind="execution-handoffs",
        artifact_id=handoff.handoff_id,
        value=handoff.to_dict(),
    )
    processed_references: list[StoreRef] = []

    def execute_and_deliver(current_handoff, task):
        processed_reference = executor.execute_store(task.input_store)
        processed_references.append(processed_reference)
        sealed_reference = delivery.deliver_store(processed_reference, current_handoff.result_sink)
        return StoreTaskResult.succeeded(
            handoff_id=current_handoff.handoff_id,
            task=task,
            output_store=sealed_reference,
        )

    results = tuple(
        LocalExecutionBackend(
            execution_profile,
            execute_and_deliver,
            profile_reference=execution_profile_ref,
            controls=controls,
        ).execute(handoff, tasks)
    )
    processed = tuple(processed_references)
    sealed = tuple(result.output_store for result in results if result.output_store is not None)
    reconciler = RunReconciler(
        plan_ref=plan_ref,
        execution_profile_ref=execution_profile_ref,
        execution_handoff_ref=handoff_ref,
        source_catalog_ref=plan.source_catalog,
        base_release_ref=plan.base_release,
        controls=controls,
        stores=stores,
        records=records,
        document_catalog=catalog,
        source_catalog=source_catalog,
        workspace_factory=LocalSqliteReconciliationWorkspaceFactory(records.root / ".reconciliation"),
        partition_policy=partition_policy,
        clock=_clock,
    )
    run_ref = reconciler.reconcile_run(results)
    release_ref = ReleaseCommitService(
        plan_ref=plan_ref,
        controls=controls,
        records=records,
        document_catalog=catalog,
    ).commit_release(plan.base_release, run_ref)
    return planned, processed, sealed, run_ref, release_ref


def test_full_and_incremental_runs_use_bounded_jobs_and_immutable_releases(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    first_id = "document-a"
    second_id = "document-b"
    while partition_bucket(first_id, 16) == partition_bucket(second_id, 16):
        second_id += "x"
    first_candidate = _write_source(sources / "first.txt", "Alpha paragraph.\n\nSecond paragraph.")
    second_candidate = _write_source(sources / "second.txt", "Unchanged document.")

    source_catalog = LocalJsonlSourceCatalog(tmp_path / "source-catalogs")
    pipeline_source_catalog = SourceReleaseCatalogView(LocalSourceReleaseReader(source_catalog))
    controls = LocalJsonControlRepository(tmp_path / "controls")
    stores = LocalDocumentStoreRepository(tmp_path / "stores")
    blobs = LocalContentAddressedBlobStore(tmp_path / "blobs")
    records = LocalJsonlRecordStorage(tmp_path / "records")
    partition_policy = PartitionPolicy("source-item-sha256-v1", 16)
    catalog = LocalManifestDocumentCatalog(
        tmp_path / "document-catalog",
        records=records,
        stores=stores,
        controls=controls,
        blobs=blobs,
    )
    fetcher = LocalFileContentFetcher(sources)
    retry = RetryPolicy(base_delay_milliseconds=0)
    processor = ContentStatisticsProcessor(retry_policy=retry)
    accepted = AcceptedFailurePolicy()

    source_v1 = source_catalog.write(
        tuple(
            sorted(
                (
                    SourceItem(first_id, "v1", (first_candidate,), metadata={"expectedSegments": 2}),
                    SourceItem(second_id, "v1", (second_candidate,), metadata={"expectedSegments": 1}),
                ),
                key=lambda item: item.item_id,
            )
        )
    )
    first_plan = _plan(source_v1, None, processor, retry, accepted, buckets=16)
    planned, processed, sealed, run_v1_ref, release_v1_ref = _run(
        plan=first_plan,
        source_catalog=pipeline_source_catalog,
        controls=controls,
        stores=stores,
        blobs=blobs,
        records=records,
        catalog=catalog,
        fetcher=fetcher,
        processor=processor,
        partition_policy=partition_policy,
    )

    assert planned and len(planned) <= 2
    assert all(stores.load(reference).state.value == "running" for reference in processed)
    assert all(stores.load(reference).state.value == "sealed" for reference in sealed)
    run_v1 = RunReceipt.from_dict(controls.load(run_v1_ref))
    assert run_v1.store_count == len(sealed)
    assert run_v1.selected_item_count == 2
    release_v1 = catalog.open(release_v1_ref)
    assert release_v1.source_catalog == source_v1
    assert release_v1.previous_release is None
    assert catalog.current() == release_v1_ref
    assert len(list(catalog.scan(release_v1_ref, layer_kind="segments"))) == 3
    v1_segments = next(layer for layer in release_v1.active_layers if layer.layer_kind == "segments")
    v1_root = json.loads((records.root / v1_segments.state_ref).read_text())

    first_candidate_v2 = _write_source(sources / "first.txt", "Alpha changed.\n\nSecond paragraph.")
    source_v2 = source_catalog.write(
        tuple(
            sorted(
                (
                    SourceItem(first_id, "v2", (first_candidate_v2,), metadata={"expectedSegments": 2}),
                    SourceItem(second_id, "v1", (second_candidate,), metadata={"expectedSegments": 1}),
                ),
                key=lambda item: item.item_id,
            )
        )
    )
    second_plan = _plan(source_v2, release_v1_ref, processor, retry, accepted, buckets=16)
    planned_v2, _, sealed_v2, _, release_v2_ref = _run(
        plan=second_plan,
        source_catalog=pipeline_source_catalog,
        controls=controls,
        stores=stores,
        blobs=blobs,
        records=records,
        catalog=catalog,
        fetcher=fetcher,
        processor=processor,
        partition_policy=partition_policy,
    )

    assert sum(len(stores.load(reference).entries) for reference in planned_v2) == 1
    assert sealed_v2
    release_v2 = catalog.open(release_v2_ref)
    assert release_v2.previous_release == release_v1_ref
    assert catalog.lookup(release_v2_ref, layer_kind="source-items", record_id=first_id)["payload"]["version"] == "v2"
    assert catalog.lookup(release_v2_ref, layer_kind="source-items", record_id=second_id)["payload"]["version"] == "v1"
    assert len(list(catalog.scan(release_v2_ref, layer_kind="segments"))) == 3

    v2_segments = next(layer for layer in release_v2.active_layers if layer.layer_kind == "segments")
    v2_root = json.loads((records.root / v2_segments.state_ref).read_text())
    unchanged_bucket = partition_bucket(second_id, 16)
    v1_members = {item["partition"]: item["path"] for item in v1_root["members"]}
    v2_members = {item["partition"]: item["path"] for item in v2_root["members"]}
    assert v2_members[unchanged_bucket] == v1_members[unchanged_bucket]

    source_v3 = source_catalog.write(
        (SourceItem(first_id, "v2", (first_candidate_v2,), metadata={"expectedSegments": 2}),)
    )
    third_plan = _plan(source_v3, release_v2_ref, processor, retry, accepted, buckets=16)
    planned_v3, _, _, _, release_v3_ref = _run(
        plan=third_plan,
        source_catalog=pipeline_source_catalog,
        controls=controls,
        stores=stores,
        blobs=blobs,
        records=records,
        catalog=catalog,
        fetcher=fetcher,
        processor=processor,
        partition_policy=partition_policy,
    )

    deletion_entries = [entry for reference in planned_v3 for entry in stores.load(reference).entries]
    assert [(entry.source_item.item_id, entry.change.value) for entry in deletion_entries] == [
        (second_id, "deleted")
    ]
    release_v3 = catalog.open(release_v3_ref)
    second_tombstone = catalog.lookup(release_v3_ref, layer_kind="source-items", record_id=second_id)
    assert second_tombstone["deleted"] is True
    assert second_tombstone["payload"]["state"] == "deleted"
    assert len(list(catalog.scan(release_v3_ref, layer_kind="segments"))) == 2

    v3_segments = next(layer for layer in release_v3.active_layers if layer.layer_kind == "segments")
    v3_root = json.loads((records.root / v3_segments.state_ref).read_text())
    first_bucket = partition_bucket(first_id, 16)
    v3_members = {item["partition"]: item["path"] for item in v3_root["members"]}
    assert v3_members[first_bucket] == v2_members[first_bucket]

    retained_file = next(catalog.scan(release_v3_ref, layer_kind="files"))
    retained_blob = BlobRef.from_dict(retained_file["payload"]["blob"])
    retained_path = blobs.root / retained_blob.locator
    original_bytes = retained_path.read_bytes()
    retained_path.write_bytes(bytes([original_bytes[0] ^ 1]) + original_bytes[1:])
    with pytest.raises(IntegrityError, match="retained blob.*failed verification"):
        catalog.open(release_v3_ref)


def test_reconciler_matches_the_exact_planned_terminal_store_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    source_catalog = LocalJsonlSourceCatalog(tmp_path / "source-catalogs")
    controls = LocalJsonControlRepository(tmp_path / "controls")
    stores = LocalDocumentStoreRepository(tmp_path / "stores")
    blobs = LocalContentAddressedBlobStore(tmp_path / "blobs")
    records = LocalJsonlRecordStorage(tmp_path / "records")
    partition_policy = PartitionPolicy("source-item-sha256-v1", 1)
    catalog = LocalManifestDocumentCatalog(
        tmp_path / "document-catalog",
        records=records,
        stores=stores,
        controls=controls,
        blobs=blobs,
    )
    retry = RetryPolicy(base_delay_milliseconds=0)
    processor = ContentStatisticsProcessor(retry_policy=retry)
    accepted = AcceptedFailurePolicy()
    source_ref = source_catalog.write(
        (
            SourceItem("document-a", "v1", (_write_source(sources / "a.txt", "Alpha."),)),
            SourceItem("document-b", "v1", (_write_source(sources / "b.txt", "Bravo."),)),
        )
    )
    plan = _plan(source_ref, None, processor, retry, accepted, buckets=1, max_entries=1)
    planned, _, sealed, run_ref, _ = _run(
        plan=plan,
        source_catalog=source_catalog,
        controls=controls,
        stores=stores,
        blobs=blobs,
        records=records,
        catalog=catalog,
        fetcher=LocalFileContentFetcher(sources),
        processor=processor,
        partition_policy=partition_policy,
    )
    assert len(planned) == len(sealed) == 2
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    run = RunReceipt.from_dict(controls.load(run_ref))
    handoff = ExecutionHandoff.from_dict(controls.load(run.execution_handoff))
    results = tuple(
        StoreTaskResult.from_dict(row["result"])
        for row in records.stream(run.task_result_ledger)
    )

    def reconciler() -> RunReconciler:
        return RunReconciler(
            plan_ref=plan_ref,
            execution_profile_ref=run.execution_profile,
            execution_handoff_ref=run.execution_handoff,
            source_catalog_ref=plan.source_catalog,
            base_release_ref=None,
            controls=controls,
            stores=stores,
            records=records,
            document_catalog=catalog,
            source_catalog=source_catalog,
            workspace_factory=LocalSqliteReconciliationWorkspaceFactory(records.root / ".reconciliation"),
            partition_policy=partition_policy,
            clock=_clock,
        )

    write_calls: list[str] = []
    original_write_layer = records.write_layer

    def counted_write_layer(*args, **kwargs):
        write_calls.append(kwargs["layer_kind"])
        return original_write_layer(*args, **kwargs)

    monkeypatch.setattr(records, "write_layer", counted_write_layer)
    reconciler().reconcile_run(results)
    expected_layer_kinds = {
        *core_delivery_schemas(),
        f"derived:{processor.description.processor_id}",
        "run-selection",
        "run-store-receipts",
        "execution-task-results",
    }
    assert Counter(write_calls) == Counter({kind: 1 for kind in expected_layer_kinds})

    for replay in (tuple(reversed(results)), (*results, results[-1])):
        reconciler().reconcile_run(replay)

    unknown = StoreRef("unknown-store", 0, "memory://unknown-store", sealed[0].digest)
    conflicting = StoreRef(
        sealed[0].store_id,
        sealed[0].revision + 1,
        sealed[0].locator,
        sealed[0].digest,
    )
    unknown_task = StoreTask(plan.plan_id, handoff.operation_id, unknown)
    unknown_result = StoreTaskResult.succeeded(
        handoff_id=handoff.handoff_id,
        task=unknown_task,
        output_store=unknown,
    )
    original = next(result for result in results if result.output_store == sealed[0])
    conflicting_result = StoreTaskResult.succeeded(
        handoff_id=handoff.handoff_id,
        task=original.task,
        output_store=conflicting,
    )
    nonterminal_original = next(result for result in results if result.task.input_store == planned[0])
    nonterminal_result = StoreTaskResult.succeeded(
        handoff_id=handoff.handoff_id,
        task=nonterminal_original.task,
        output_store=planned[0],
    )
    other_result = next(result for result in results if result.task.input_store != planned[0])
    cases = (
        ("missing", results[:-1], "missing planned store"),
        ("unknown", (*results, unknown_result), "unknown or extra store"),
        ("conflicting", (*results, conflicting_result), "conflicts for repeated store"),
        ("duplicate-with-missing", (results[0], results[0]), "missing planned store"),
        ("nonterminal", (nonterminal_result, other_result), "unsealed store"),
    )
    for _, task_results, message in cases:
        receipt_directory = controls.root / "control" / "run-receipts"
        receipts_before = tuple(sorted(receipt_directory.rglob("*.json")))
        with pytest.raises(IntegrityError, match=message):
            reconciler().reconcile_run(task_results)
        assert tuple(sorted(receipt_directory.rglob("*.json"))) == receipts_before
