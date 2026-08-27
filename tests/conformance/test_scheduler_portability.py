from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

from docspec.adapters.execution import ExternalExecutionBackend
from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.cli import main
from docspec.domain.content import SourceItem, SourceItemState
from docspec.domain.execution import ExecutionHandoff, ExecutionProfile, StoreTask, StoreTaskResult, iter_store_tasks
from docspec.domain.identity import canonical_json_file_bytes
from docspec.domain.jobs import StoreState
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, DataUsePolicy, RetentionPolicy, RetryPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.receipts import RunReceipt
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, StoreRef
from docspec.processing.extraction import DefaultExtractorRegistry
from docspec.processing.segmentation import DefaultSegmenterRegistry

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_equivalence = importlib.import_module("tests.conformance.test_incremental_equivalence")
_cli_helpers = importlib.import_module("tests.test_cli")
_helpers = importlib.import_module("tests.helpers")
_active_document_state = _equivalence._active_document_state
_portable_local_profiles = _cli_helpers._portable_local_profiles
_write_local_run_request = _cli_helpers._write_local_run_request
document_release_producer = _helpers.document_release_producer
write_shared_source_catalog = _helpers.write_shared_source_catalog

_FIXTURE_IDENTITIES = ("document-a", "document-b")


@dataclass(frozen=True)
class _Arm:
    """One complete platform root sharing the exact fixture bytes."""

    root: Path
    run_request: Path
    plan: ProcessingPlan
    roots: dict[str, str]

    @property
    def controls(self) -> LocalJsonControlRepository:
        return LocalJsonControlRepository(Path(self.roots["controlRepository"]))

    @property
    def stores(self) -> LocalDocumentStoreRepository:
        return LocalDocumentStoreRepository(Path(self.roots["documentStores"]))

    @property
    def records(self) -> LocalJsonlRecordStorage:
        return LocalJsonlRecordStorage(Path(self.roots["recordStorage"]))

    @property
    def catalog(self) -> LocalManifestDocumentCatalog:
        return LocalManifestDocumentCatalog(
            Path(self.roots["documentCatalog"]),
            records=self.records,
            stores=self.stores,
            controls=self.controls,
            producer=document_release_producer(),
            blobs=LocalContentAddressedBlobStore(Path(self.roots["blobStorage"])),
        )

    def run_receipt(self, reference: ArtifactRef) -> RunReceipt:
        return RunReceipt.from_dict(self.controls.load(reference))

    def store_verdicts(self, run: RunReceipt) -> dict[str, str]:
        verdicts = {}
        for row in self.records.stream(run.store_ledger):
            store = self.stores.load(StoreRef.from_dict(row["store"]))
            assert store.verdict is not None
            verdicts[store.store_id] = store.verdict.value
        return verdicts

    def store_references(self, run: RunReceipt) -> tuple[StoreRef, ...]:
        return tuple(StoreRef.from_dict(row["store"]) for row in self.records.stream(run.store_ledger))

    def task_results(self, run: RunReceipt) -> tuple[StoreTaskResult, ...]:
        return tuple(StoreTaskResult.from_dict(row["result"]) for row in self.records.stream(run.task_result_ledger))


@dataclass(frozen=True)
class _RunOutcome:
    receipt: RunReceipt
    release: DocumentReleaseRef
    store_references: tuple[StoreRef, ...]
    task_results: tuple[StoreTaskResult, ...]
    active_state: dict[str, tuple[dict[str, object], ...]]


def _seed(root: Path) -> _Arm:
    source_content = root / "source-content"
    source_content.mkdir(parents=True)
    items = tuple(
        SourceItem(item_id, "2026-08-24", (), state=SourceItemState.DELETED) for item_id in _FIXTURE_IDENTITIES
    )
    source_ref = write_shared_source_catalog(root / "source-catalog", items)
    retry = RetryPolicy()
    accepted = AcceptedFailurePolicy()
    plan = ProcessingPlan.create(
        source_catalog=source_ref,
        base_release=None,
        profiles=_portable_local_profiles(),
        limits=WorkLimits(1, 1024 * 1024, 10, 10, 100, 1024 * 1024, 60, retry.max_attempts),
        stages=StagePolicy(
            (DefaultExtractorRegistry.extractor_id,),
            DefaultSegmenterRegistry.segmenter_id,
        ),
        processors=ProcessorSet(()),
        partition_count=4,
        selection={},
        retention_policy=RetentionPolicy.retain_all(),
        data_use_policy=DataUsePolicy.local_content(),
        retry_policy_digest=retry.digest,
        accepted_failure_policy_digest=accepted.digest,
    )
    plan_path = root / "plan.json"
    plan_path.write_bytes(canonical_json_file_bytes(plan.to_dict()))
    roots = {
        "blobStorage": (root / "blobs").as_posix(),
        "controlRepository": (root / "controls").as_posix(),
        "documentCatalog": (root / "catalog").as_posix(),
        "documentStores": (root / "stores").as_posix(),
        "reconciliation": (root / "reconciliation").as_posix(),
        "recordStorage": (root / "records").as_posix(),
        "sourceCatalog": (root / "source-catalog").as_posix(),
        "sourceContent": source_content.as_posix(),
    }
    run_request = _write_local_run_request(
        root / "run-request.json",
        plan_path=plan_path,
        roots=roots,
        result_sink_id="urn:docspec:test:sink:local-durable",
        retry=retry,
        accepted=accepted,
        completed_at="2026-08-05T12:00:00Z",
        max_workers=2,
        max_in_flight=2,
    )
    return _Arm(root, run_request, plan, roots)


def _run_local_backend(arm: _Arm) -> tuple[ArtifactRef, DocumentReleaseRef]:
    run_reference_path = arm.root / "run-reference.json"
    assert (
        main(
            [
                "run",
                "start",
                "--request",
                str(arm.run_request),
                "--destination",
                str(run_reference_path),
                "--receipt",
                str(arm.root / "run-operation.json"),
            ]
        )
        == 0
    )
    run_reference = ArtifactRef.from_dict(json.loads(run_reference_path.read_text()))
    return run_reference, _commit(arm, run_reference_path)


def _commit(arm: _Arm, run_reference_path: Path) -> DocumentReleaseRef:
    commit_request = arm.root / "commit-request.json"
    commit_request.write_bytes(
        canonical_json_file_bytes(
            {
                "format": "docspec-local-release-commit-request",
                "formatVersion": "1.0",
                "runRequest": arm.run_request.as_posix(),
                "runReceipt": run_reference_path.as_posix(),
                "baseRelease": None,
            }
        )
    )
    release_reference_path = arm.root / "release-reference.json"
    assert (
        main(
            [
                "document-release",
                "commit",
                "--request",
                str(commit_request),
                "--destination",
                str(release_reference_path),
                "--receipt",
                str(arm.root / "commit-operation.json"),
            ]
        )
        == 0
    )
    return DocumentReleaseRef.from_dict(json.loads(release_reference_path.read_text()))


def _prepare_handoff(arm: _Arm) -> tuple[Path, ArtifactRef, ExecutionHandoff, tuple[StoreTask, ...]]:
    handoff_reference_path = arm.root / "handoff-reference.json"
    assert (
        main(
            [
                "run",
                "prepare",
                "--request",
                str(arm.run_request),
                "--destination",
                str(handoff_reference_path),
                "--receipt",
                str(arm.root / "prepare-operation.json"),
            ]
        )
        == 0
    )
    handoff_reference = ArtifactRef.from_dict(json.loads(handoff_reference_path.read_text()))
    handoff = ExecutionHandoff.from_dict(arm.controls.load(handoff_reference))
    tasks = tuple(
        iter_store_tasks(
            arm.plan.plan_id,
            handoff.operation_id,
            arm.stores.stream_planned_stores(handoff.planned_store_ledger),
        )
    )
    return handoff_reference_path, handoff_reference, handoff, tasks


def _execute_task_in_subprocess(arm: _Arm, handoff_reference: ArtifactRef, task: StoreTask, label: str) -> bytes:
    """Run one serialized task through a brand-new OS process with no closure."""

    task_request = arm.root / f"task-request-{label}.json"
    task_request.write_bytes(
        canonical_json_file_bytes(
            {
                "format": "docspec-local-task-execution-request",
                "formatVersion": "1.0",
                "runRequest": arm.run_request.as_posix(),
                "handoff": handoff_reference.to_dict(),
                "task": task.to_dict(),
            }
        )
    )
    destination = arm.root / f"task-result-{label}.jsonl"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "docspec.cli",
            "task",
            "execute",
            "--request",
            str(task_request),
            "--destination",
            str(destination),
            "--receipt",
            str(arm.root / f"task-operation-{label}.json"),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    return destination.read_bytes()


class _SubprocessDispatcher:
    """Deployment-owned dispatcher over the public serialized task seam."""

    def __init__(self, arm: _Arm, handoff_reference: ArtifactRef) -> None:
        self._arm = arm
        self._handoff_reference = handoff_reference

    def dispatch(self, *, handoff: bytes, tasks: Iterable[bytes]) -> tuple[bytes, ...]:
        restored_handoff = ExecutionHandoff.from_bytes(handoff)
        restored_tasks = tuple(StoreTask.from_bytes(payload) for payload in tasks)
        assert restored_handoff.handoff_id == self._handoff_reference.artifact_id
        return tuple(
            _execute_task_in_subprocess(self._arm, self._handoff_reference, task, f"external-{index}")
            for index, task in reversed(tuple(enumerate(restored_tasks)))
        )


def _reconcile(arm: _Arm, handoff_reference: ArtifactRef, results_path: Path) -> tuple[ArtifactRef, Path]:
    reconcile_request = arm.root / "reconcile-request.json"
    reconcile_request.write_bytes(
        canonical_json_file_bytes(
            {
                "format": "docspec-local-run-reconcile-request",
                "formatVersion": "1.0",
                "runRequest": arm.run_request.as_posix(),
                "handoff": handoff_reference.to_dict(),
                "results": results_path.as_posix(),
            }
        )
    )
    reconciled_reference_path = arm.root / "reconciled-reference.json"
    assert (
        main(
            [
                "run",
                "reconcile",
                "--request",
                str(reconcile_request),
                "--destination",
                str(reconciled_reference_path),
                "--receipt",
                str(arm.root / "reconcile-operation.json"),
            ]
        )
        == 0
    )
    return ArtifactRef.from_dict(json.loads(reconciled_reference_path.read_text())), reconciled_reference_path


def _capture_outcome(
    arm: _Arm,
    run_reference: ArtifactRef,
    release: DocumentReleaseRef,
) -> _RunOutcome:
    receipt = arm.run_receipt(run_reference)
    return _RunOutcome(
        receipt,
        release,
        arm.store_references(receipt),
        arm.task_results(receipt),
        _active_document_state(arm.catalog, release),
    )


def _run_external_backend(
    arm: _Arm,
) -> tuple[ArtifactRef, DocumentReleaseRef, tuple[StoreTaskResult, ...]]:
    _, handoff_reference, handoff, tasks = _prepare_handoff(arm)
    profile = ExecutionProfile.from_dict(arm.controls.load(handoff.execution_profile))
    backend = ExternalExecutionBackend(
        profile,
        _SubprocessDispatcher(arm, handoff_reference),
        profile_reference=handoff.execution_profile,
        controls=arm.controls,
    )
    results = tuple(backend.execute(handoff, tasks))
    replayed = StoreTaskResult.from_bytes(_execute_task_in_subprocess(arm, handoff_reference, tasks[0], "replay"))
    assert replayed == next(result for result in results if result.task == tasks[0])

    results_path = arm.root / "results.jsonl"
    results_path.write_bytes(b"".join(result.to_bytes() for result in (*reversed(results), replayed)))
    run_reference, run_reference_path = _reconcile(arm, handoff_reference, results_path)
    return run_reference, _commit(arm, run_reference_path), results


def _dagster_output_payloads(result: object, node_name: str) -> tuple[bytes, ...]:
    outputs = result.output_for_node(node_name)  # type: ignore[attr-defined]
    assert isinstance(outputs, dict)
    return tuple(outputs[key] for key in sorted(outputs))


def _run_dagster_tasks(
    arm: _Arm,
    handoff_reference_path: Path,
    *,
    fail_task_id: str | None = None,
) -> tuple[bool, tuple[StoreTask, ...], tuple[StoreTaskResult, ...], tuple[dict[str, object], ...]]:
    dagster = pytest.importorskip("dagster", reason="install the 'dagster' extra for scheduler portability")
    fixture = importlib.import_module("tests.dagster_process_fixture")
    job = dagster.reconstructable(fixture.reconstructable_application_job)
    instance_root = arm.root / "dagster-instance"
    evidence_root = arm.root / "dagster-worker-evidence"
    instance_root.mkdir()
    evidence_root.mkdir()
    resource_config: dict[str, object] = {
        "run_request_path": arm.run_request.as_posix(),
        "handoff_reference_path": handoff_reference_path.as_posix(),
        "worker_evidence_root": evidence_root.as_posix(),
    }
    if fail_task_id is not None:
        resource_config["fail_task_id"] = fail_task_id
    run_config = {
        "execution": {"config": {"max_concurrent": 2}},
        "resources": {"docspec_runtime": {"config": resource_config}},
    }

    with dagster.DagsterInstance.local_temp(tempdir=str(instance_root)) as instance:
        with dagster.execute_job(job, instance=instance, run_config=run_config) as result:
            success = result.success
            emitted_payloads = _dagster_output_payloads(result, "emit_store_tasks")
            result_payloads = _dagster_output_payloads(result, "execute_store_task") if success else ()
            run_id = result.run_id
        events = instance.all_logs(run_id)

    tasks = tuple(StoreTask.from_bytes(payload) for payload in emitted_payloads)
    results = tuple(StoreTaskResult.from_bytes(payload) for payload in result_payloads)
    evidence = tuple(json.loads(path.read_text(encoding="utf-8")) for path in sorted(evidence_root.glob("*.json")))
    assert tasks
    assert all(item["pid"] != os.getpid() for item in evidence)
    assert any(
        event.dagster_event and event.dagster_event.event_type is dagster.DagsterEventType.STEP_WORKER_STARTED
        for event in events
    )
    return success, tasks, results, evidence


def _run_dagster_backend(
    arm: _Arm,
) -> tuple[ArtifactRef, DocumentReleaseRef, tuple[StoreTaskResult, ...], tuple[dict[str, object], ...]]:
    handoff_reference_path, handoff_reference, handoff, expected_tasks = _prepare_handoff(arm)
    success, tasks, results, evidence = _run_dagster_tasks(arm, handoff_reference_path)
    assert success
    assert len(tasks) == len(expected_tasks)
    assert set(tasks) == set(expected_tasks)
    assert {result.task for result in results} == set(tasks)
    assert all(result.handoff_id == handoff.handoff_id for result in results)
    assert all(
        set(item) == {"pid", "result", "status", "task"}
        and item["status"] == "succeeded"
        and StoreTask.from_dict(item["task"]) in tasks
        and StoreTaskResult.from_dict(item["result"]) in results
        for item in evidence
    )

    results_path = arm.root / "dagster-results.jsonl"
    results_path.write_bytes(b"".join(result.to_bytes() for result in reversed(results)))
    run_reference, run_reference_path = _reconcile(arm, handoff_reference, results_path)
    return run_reference, _commit(arm, run_reference_path), results, evidence


def _assert_equivalent(
    local: _Arm,
    local_run: RunReceipt,
    local_release: DocumentReleaseRef,
    other: _Arm,
    other_run: RunReceipt,
    other_release: DocumentReleaseRef,
) -> None:
    assert other_release.release_id == local_release.release_id, (
        "scheduler handoffs and task evidence must not change derivation logical identity"
    )
    assert dict(other_run.counts) == dict(local_run.counts), "backend selection must not change the counts"
    assert other_run.failures["counts"] == local_run.failures["counts"] == {}
    assert other.store_verdicts(other_run) == local.store_verdicts(local_run), (
        "both backends must seal the same stores with the same verdicts"
    )
    assert _active_document_state(other.catalog, other_release) == _active_document_state(
        local.catalog, local_release
    ), "backend selection must not change the published logical state"


def test_serialized_tasks_cross_a_real_process_boundary_and_match_the_local_backend(tmp_path: Path) -> None:
    """A fresh process reconstructs each task and preserves the local result."""

    local = _seed(tmp_path / "local")
    external = _seed(tmp_path / "external")
    local_run_ref, local_release = _run_local_backend(local)
    run_reference, external_release, results = _run_external_backend(external)
    assert len(results) == 2

    _assert_equivalent(
        local,
        local.run_receipt(local_run_ref),
        local_release,
        external,
        external.run_receipt(run_reference),
        external_release,
    )


def test_native_dagster_executes_the_real_graph_and_matches_local_and_external_runs(
    tmp_path: Path,
) -> None:
    """All three backends seal byte-identical task, store, run, and release evidence."""

    root = tmp_path / "portable-application"

    local = _seed(root)
    local_run_reference, local_release = _run_local_backend(local)
    expected = _capture_outcome(local, local_run_reference, local_release)
    shutil.rmtree(root)

    external = _seed(root)
    external_run_reference, external_release, external_results = _run_external_backend(external)
    assert len(external_results) == 2
    assert _capture_outcome(external, external_run_reference, external_release) == expected
    shutil.rmtree(root)

    dagster_arm = _seed(root)
    dagster_run_reference, dagster_release, dagster_results, evidence = _run_dagster_backend(dagster_arm)
    actual = _capture_outcome(dagster_arm, dagster_run_reference, dagster_release)

    assert actual == expected
    assert {result.output_store for result in dagster_results} == set(actual.store_references)
    assert all(len(result.task.to_bytes()) < 4096 and len(result.to_bytes()) < 4096 for result in dagster_results)
    assert len(evidence) == len(dagster_results) == 2
    for store_reference in actual.store_references:
        store = dagster_arm.stores.load(store_reference)
        assert store.state is StoreState.SEALED
        assert store.delivery_receipt is not None
        delivery = dagster_arm.controls.load(store.delivery_receipt)
        assert delivery["blobRoots"]
        assert delivery["layers"]


def test_native_dagster_worker_failure_publishes_no_partial_run_or_release(
    tmp_path: Path,
) -> None:
    arm = _seed(tmp_path / "dagster-failure")
    handoff_reference_path, _, _, tasks = _prepare_handoff(arm)
    failed_task, sibling_task = tasks

    success, emitted_tasks, results, evidence = _run_dagster_tasks(
        arm,
        handoff_reference_path,
        fail_task_id=failed_task.task_id,
    )

    assert not success
    assert len(emitted_tasks) == len(tasks)
    assert set(emitted_tasks) == set(tasks)
    assert results == ()
    assert {item["status"] for item in evidence} == {"injected-failure", "succeeded"}
    failed_reference = arm.stores.latest(failed_task.input_store.store_id)
    sibling_reference = arm.stores.latest(sibling_task.input_store.store_id)
    assert failed_reference is not None and sibling_reference is not None
    assert arm.stores.load(failed_reference).state is StoreState.SEALED
    assert arm.stores.load(sibling_reference).state is StoreState.SEALED
    assert arm.catalog.current() is None
    run_receipt_root = Path(arm.roots["controlRepository"]) / "control" / "run-receipts"
    assert not run_receipt_root.exists()
