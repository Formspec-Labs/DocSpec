"""Shared pipeline fixtures, extracted from tests.test_application_pipeline."""

from __future__ import annotations

from pathlib import Path

from docspec.adapters.execution import LocalExecutionBackend
from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.adapters.sinks import DurableDatasetSink
from docspec.application.commit import ReleaseCommitService
from docspec.application.delivery import StoreDeliveryService
from docspec.application.execution import StoreExecutionService
from docspec.application.planner import RunPlanner
from docspec.application.reconcile import RunReconciler
from docspec.domain.content import CandidateFile
from docspec.domain.execution import (
    EXECUTE_AND_DELIVER_OPERATION_ID,
    ExecutionHandoff,
    ExecutionLimits,
    ExecutionProfile,
    StoreTaskResult,
    iter_store_tasks,
    summarize_store_tasks,
)
from docspec.domain.identity import sha256_digest
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, DataUsePolicy, RetentionPolicy, RetryPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.references import StoreRef
from docspec.processing.extraction import DefaultExtractorRegistry
from docspec.processing.segmentation import DefaultSegmenterRegistry
from tests.helpers import (
    local_profile_set,
)


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
    select_current: bool = True,
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
    service = ReleaseCommitService(
        plan_ref=plan_ref,
        controls=controls,
        records=records,
        document_catalog=catalog,
    )
    save = service.commit_release if select_current else service.retain_release
    release_ref = save(plan.base_release, run_ref)
    return planned, processed, sealed, run_ref, release_ref
