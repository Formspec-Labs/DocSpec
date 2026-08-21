from __future__ import annotations

import importlib
import sys
from pathlib import Path

from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.adapters.sinks import DurableDatasetSink
from docspec.adapters.source_catalog import LocalFileContentFetcher
from docspec.application.commit import ReleaseCommitService
from docspec.application.delivery import StoreDeliveryService
from docspec.application.execution import StoreExecutionService
from docspec.application.planner import RunPlanner
from docspec.application.reconcile import RunReconciler
from docspec.domain.content import SourceItem
from docspec.domain.execution import (
    EXECUTE_AND_DELIVER_OPERATION_ID,
    ExecutionHandoff,
    ExecutionLimits,
    ExecutionProfile,
    StoreTaskResult,
    iter_store_tasks,
    summarize_store_tasks,
)
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.receipts import RunReceipt

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_equivalence = importlib.import_module("tests.conformance.test_incremental_equivalence")
_document_store = importlib.import_module("tests.conformance.test_document_store")
_pipeline_helpers = importlib.import_module("tests.test_application_pipeline")
_processor_helpers = importlib.import_module("tests.test_processor_reprocessing")
_platform = _equivalence._platform
_active_document_state = _equivalence._active_document_state
_reconciled_counts = _document_store._reconciled_counts
_clock = _pipeline_helpers._clock
_write_source = _pipeline_helpers._write_source
_CountingExtractor = _processor_helpers._CountingExtractor
_CountingFetcher = _processor_helpers._CountingFetcher
_CountingProcessor = _processor_helpers._CountingProcessor
_description = _processor_helpers._description
_plan = _processor_helpers._plan


class _Composition:
    """The five scheduler-neutral functions, held open between phases so a
    scenario can crash a coordinator or replay a worker between them."""

    def __init__(self, platform, plan, processors, fetcher, *, extractor=None) -> None:
        self.platform = platform
        self.plan = plan
        self.fetcher = fetcher
        self.extractor = extractor
        controls = platform.controls
        self.plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
        self.planned = tuple(
            RunPlanner(
                source_catalog=platform.source_catalog,
                document_catalog=platform.catalog,
                stores=platform.stores,
                controls=controls,
                workspace_factory=LocalSqliteReconciliationWorkspaceFactory(
                    platform.records.root / ".planning",
                    read_batch_size=1,
                ),
            ).plan_run(plan.source_catalog, plan.base_release, self.plan_ref)
        )
        retry = RetryPolicy(max_attempts=plan.limits.max_attempts, base_delay_milliseconds=0)
        self.executor = StoreExecutionService(
            plan_ref=self.plan_ref,
            controls=controls,
            stores=platform.stores,
            document_catalog=platform.catalog,
            blobs=platform.blobs,
            fetcher=fetcher,
            extractor=extractor or _CountingExtractor(),
            segmenter=_processor_helpers._CountingSegmenter(),
            processors={item.description.processor_id: item for item in processors},
            retry_policy=retry,
            accepted_failure_policy=AcceptedFailurePolicy(),
            clock=_clock,
            sleep=lambda _: None,
        )
        blob_root = controls.put(
            kind="profile-state",
            artifact_id="urn:docspec:test:blob-root",
            value={
                "profileId": "urn:docspec:profile:blob-storage:local-content-addressed:1",
                "profileVersion": "1.0.0",
                "storageRoot": platform.blobs.root.as_posix(),
            },
        )
        sink = DurableDatasetSink(
            sink_id="urn:docspec:test:sink:durable",
            profile_id="urn:docspec:profile:result-delivery:durable-dataset:1",
            storage=platform.records,
            partition_policy=platform.partition_policy,
            blob_roots=(blob_root,),
            clock=_clock,
        )
        self.sink_ref = controls.put(
            kind="sinks",
            artifact_id=sink.sink_id,
            value={"sinkId": sink.sink_id, "profileId": sink.profile_id},
        )
        self.delivery = StoreDeliveryService(stores=platform.stores, controls=controls, sinks={sink.sink_id: sink})
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
        self.execution_profile_ref = controls.put(
            kind="execution-profiles",
            artifact_id=execution_profile.profile_id,
            value=execution_profile.to_dict(),
        )
        self.tasks = tuple(iter_store_tasks(plan.plan_id, EXECUTE_AND_DELIVER_OPERATION_ID, self.planned))
        task_count, task_set_digest = summarize_store_tasks(self.tasks)
        self.handoff = ExecutionHandoff(
            processing_plan=self.plan_ref,
            execution_profile=self.execution_profile_ref,
            worker_composition=worker_composition,
            planned_store_ledger=platform.stores.planned_store_ledger(plan.plan_id),
            operation_id=EXECUTE_AND_DELIVER_OPERATION_ID,
            expected_task_count=task_count,
            task_set_digest=task_set_digest,
            result_sink=self.sink_ref,
            base_release=plan.base_release,
        )
        self.handoff_ref = controls.put(
            kind="execution-handoffs",
            artifact_id=self.handoff.handoff_id,
            value=self.handoff.to_dict(),
        )

    def execute_and_deliver(self, task) -> StoreTaskResult:
        processed = self.executor.execute_store(task.input_store)
        sealed = self.delivery.deliver_store(processed, self.sink_ref)
        return StoreTaskResult.succeeded(
            handoff_id=self.handoff.handoff_id,
            task=task,
            output_store=sealed,
        )

    def fresh_reconciler(self) -> RunReconciler:
        """A brand-new coordinator instance, as after a restart."""

        return RunReconciler(
            plan_ref=self.plan_ref,
            execution_profile_ref=self.execution_profile_ref,
            execution_handoff_ref=self.handoff_ref,
            source_catalog_ref=self.plan.source_catalog,
            base_release_ref=self.plan.base_release,
            controls=self.platform.controls,
            stores=self.platform.stores,
            records=self.platform.records,
            document_catalog=self.platform.catalog,
            source_catalog=self.platform.source_catalog,
            workspace_factory=LocalSqliteReconciliationWorkspaceFactory(
                self.platform.records.root / ".reconciliation"
            ),
            partition_policy=self.platform.partition_policy,
            clock=_clock,
        )

    def commit(self, run_ref):
        return ReleaseCommitService(
            plan_ref=self.plan_ref,
            controls=self.platform.controls,
            records=self.platform.records,
            document_catalog=self.platform.catalog,
        ).commit_release(self.plan.base_release, run_ref)


def _seeded_composition(root: Path, *, items: tuple[str, ...]):
    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    processor = _CountingProcessor(_description("recovery", "1", retry))
    platform = _platform(root, member_bytes=1024 * 1024)
    source_items = []
    for item_id in sorted(items):
        candidate = _write_source(platform.sources / f"{item_id}.txt", f"{item_id} holds one paragraph.")
        source_items.append(SourceItem(item_id, "v1", (candidate,), metadata={"expectedSegments": 1}))
    source = platform.source_catalog.write(tuple(source_items))
    fetcher = _CountingFetcher(LocalFileContentFetcher(platform.sources))
    extractor = _CountingExtractor()
    plan = _plan(source, None, (processor,), retry, accepted)
    composition = _Composition(platform, plan, (processor,), fetcher, extractor=extractor)
    return platform, composition, processor


def test_a_restarted_coordinator_reconciles_replayed_results_to_the_same_release(tmp_path: Path) -> None:
    """A coordinator that crashes after every store delivered is replaced by a
    fresh instance receiving the results out of order with one duplicated
    replay; it must publish the same active document state as an uninterrupted
    coordinator without repeating any verified work."""

    items = ("doc-recover-a", "doc-recover-b")

    steady_platform, steady, _ = _seeded_composition(tmp_path / "steady", items=items)
    steady_results = tuple(steady.execute_and_deliver(task) for task in steady.tasks)
    steady_run_ref = steady.fresh_reconciler().reconcile_run(steady_results)
    steady_release = steady.commit(steady_run_ref)

    platform, restarted, processor = _seeded_composition(tmp_path / "restarted", items=items)
    results = tuple(restarted.execute_and_deliver(task) for task in restarted.tasks)
    fetches_before_restart = list(restarted.fetcher.calls)
    extractions_before_restart = restarted.extractor.calls
    invocations_before_restart = list(processor.calls)

    replayed = (*reversed(results), results[0])
    run_ref = restarted.fresh_reconciler().reconcile_run(replayed)
    release_ref = restarted.commit(run_ref)

    assert restarted.fetcher.calls == fetches_before_restart, "reconciliation refetches nothing"
    assert restarted.extractor.calls == extractions_before_restart
    assert processor.calls == invocations_before_restart
    assert len(fetches_before_restart) == len(items), "each candidate was fetched exactly once"

    run = RunReceipt.from_dict(platform.controls.load(run_ref))
    steady_run = RunReceipt.from_dict(steady_platform.controls.load(steady_run_ref))
    assert dict(run.counts) == dict(steady_run.counts), "a duplicated replay never changes the counts"
    assert dict(run.counts) == _reconciled_counts(platform, run)
    assert run.counts["selectedItems"] == len(items)
    assert run.failures["counts"] == {}

    assert _active_document_state(platform.catalog, release_ref) == _active_document_state(
        steady_platform.catalog, steady_release
    ), "restart and replay must not change the published logical state"
    assert platform.catalog.current() == release_ref


def test_a_replayed_store_task_reuses_verified_work_and_delivers_identically(tmp_path: Path) -> None:
    """Re-running one already-delivered store task -- a replaced worker
    replaying its assignment -- reuses every verified checkpoint, returns the
    same sealed revision and delivery receipt, and reconciles into one release
    without duplicated output."""

    platform, composition, processor = _seeded_composition(
        tmp_path, items=("doc-replay-a", "doc-replay-b")
    )
    first_task, second_task = composition.tasks
    first_result = composition.execute_and_deliver(first_task)
    fetches_after_first = list(composition.fetcher.calls)
    invocations_after_first = list(processor.calls)
    sealed_first = first_result.output_store
    assert sealed_first is not None
    delivery_receipt = platform.stores.load(sealed_first).delivery_receipt

    replayed_result = composition.execute_and_deliver(first_task)
    assert replayed_result.output_store == sealed_first, "a replay returns the same sealed revision"
    assert composition.fetcher.calls == fetches_after_first, "a replay refetches nothing"
    assert processor.calls == invocations_after_first, "a replay reruns no processor"
    assert platform.stores.load(sealed_first).delivery_receipt == delivery_receipt, (
        "a replayed delivery keeps the one immutable receipt"
    )

    second_result = composition.execute_and_deliver(second_task)
    run_ref = composition.fresh_reconciler().reconcile_run(
        (first_result, replayed_result, second_result)
    )
    release_ref = composition.commit(run_ref)

    run = RunReceipt.from_dict(platform.controls.load(run_ref))
    assert dict(run.counts) == _reconciled_counts(platform, run)
    assert run.counts["stores"] == 2
    assert run.counts["selectedItems"] == 2

    source_rows = list(platform.catalog.scan(release_ref, layer_kind="source-items"))
    assert len(source_rows) == 2, "a replayed task must not duplicate published records"
    derived_rows = list(
        platform.catalog.scan(release_ref, layer_kind=f"derived:{processor.description.processor_id}")
    )
    assert len(derived_rows) == 2
    assert platform.catalog.current() == release_ref
