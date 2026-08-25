from __future__ import annotations

import importlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.cli import main
from docspec.domain.execution import ExecutionHandoff, iter_store_tasks
from docspec.domain.identity import canonical_json_file_bytes, sha256_digest
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
_platform_helpers = importlib.import_module("tests.test_platform_artifact")
_active_document_state = _equivalence._active_document_state
_portable_local_profiles = _cli_helpers._portable_local_profiles
_write_local_run_request = _cli_helpers._write_local_run_request
shared_source_item = _platform_helpers.shared_source_item
write_shared_source_catalog = _platform_helpers.write_shared_source_catalog

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


def _seed(root: Path) -> _Arm:
    source_content = root / "source-content"
    source_content.mkdir(parents=True)
    items = []
    for item_id in _FIXTURE_IDENTITIES:
        item = shared_source_item()
        item.update(
            {
                "sourceItemId": item_id,
                "documentId": item_id,
                "normalizedMetadata": None,
                "candidateRenditions": [],
                "selection": {
                    "disposition": "deleted",
                    "reasonCode": "source.deleted",
                    "reason": "Deleted distributed-execution fixture",
                },
            }
        )
        items.append(item)
    source_ref = write_shared_source_catalog(root / "source-catalog", tuple(items))
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


def _prepare_handoff(arm: _Arm):
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


def _execute_task_in_subprocess(arm: _Arm, handoff_reference: ArtifactRef, task, label: str) -> bytes:
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


def _assert_equivalent(local: _Arm, local_run: RunReceipt, local_release, other: _Arm, other_run: RunReceipt, other_release) -> None:
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
    """The same fixture graph runs through the local backend and through
    brand-new OS processes that rebuild their worker from the pinned run
    request and sealed handoff; replaying one task returns identical bytes,
    the results reconcile out of order with a duplicate, and both paths
    publish equivalent sealed stores, run receipts, and logical state."""

    local = _seed(tmp_path / "local")
    external = _seed(tmp_path / "external")
    local_run_ref, local_release = _run_local_backend(local)

    _, handoff_reference, _, tasks = _prepare_handoff(external)
    assert len(tasks) == 2, "the shared fixture graph must plan into two serialized tasks"
    lines = [
        _execute_task_in_subprocess(external, handoff_reference, task, f"task-{index}")
        for index, task in enumerate(tasks)
    ]
    replayed = _execute_task_in_subprocess(external, handoff_reference, tasks[0], "replay")
    assert replayed == lines[0], "a fresh process replay returns the identical serialized result"

    results_path = external.root / "results.jsonl"
    results_path.write_bytes(b"".join((*reversed(lines), replayed)))
    run_reference, run_reference_path = _reconcile(external, handoff_reference, results_path)
    external_release = _commit(external, run_reference_path)

    _assert_equivalent(
        local,
        local.run_receipt(local_run_ref),
        local_release,
        external,
        external.run_receipt(run_reference),
        external_release,
    )


def test_the_native_dagster_job_publishes_the_local_backend_state(tmp_path: Path) -> None:
    """The same sealed handoff drives Dagster's native dynamic job; each
    mapped task crosses a real process boundary into a worker rebuilt from
    pinned profiles, and the reconciled Dagster run publishes the same
    logical state as the local backend."""

    pytest.importorskip("dagster", reason="install the 'dagster' extra to run the maintained-scheduler leg")
    from docspec.adapters.dagster import (
        DagsterDeploymentConfig,
        adapter_profile_file_digest,
        build_dagster_definitions,
        build_task_membership_index,
        iter_persisted_task_results,
        seal_task_membership_verification,
    )

    local = _seed(tmp_path / "local")
    scheduled = _seed(tmp_path / "scheduled")
    local_run_ref, local_release = _run_local_backend(local)

    handoff_reference_path, handoff_reference, handoff, tasks = _prepare_handoff(scheduled)
    handoff_payload = handoff.to_bytes()
    handoff_path = scheduled.root / "handoff.json"
    handoff_path.write_bytes(handoff_payload)
    task_payload = b"".join(task.to_bytes() for task in tasks)
    task_path = scheduled.root / "tasks.jsonl"
    task_path.write_bytes(task_payload)
    profile_payload = (ROOT / "profiles" / "schedulers" / "dagster-dynamic-process-v1.json").read_bytes()
    profile_path = scheduled.root / "dagster-profile.json"
    profile_path.write_bytes(profile_payload)
    profile_id = json.loads(profile_payload)["profileId"]
    result_root = scheduled.root / "dagster-results"
    result_root.mkdir()
    scratch_root = scheduled.root / "worker-scratch"
    scratch_root.mkdir()
    worker = scheduled.root / "worker.py"
    worker.write_text(
        """
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, {source_root!r})

from docspec.adapters.dagster import parse_worker_request
from docspec.cli import main
from docspec.domain.identity import canonical_json_file_bytes, sha256_digest

parser = argparse.ArgumentParser()
parser.add_argument('--request', type=Path, required=True)
parser.add_argument('--result', type=Path, required=True)
args = parser.parse_args()
handoff, task = parse_worker_request(args.request.read_bytes())
scratch = Path({scratch_root!r})
label = sha256_digest(task.task_id.encode('utf-8'))[:16]
task_request = scratch / ('cli-request-' + label + '.json')
task_request.write_bytes(canonical_json_file_bytes({{
    'format': 'docspec-local-task-execution-request',
    'formatVersion': '1.0',
    'runRequest': {run_request!r},
    'handoff': json.loads(Path({handoff_reference!r}).read_text(encoding='utf-8')),
    'task': task.to_dict(),
}}))
destination = scratch / ('cli-result-' + label + '.jsonl')
code = main([
    'task', 'execute',
    '--request', str(task_request),
    '--destination', str(destination),
    '--receipt', str(scratch / ('cli-receipt-' + label + '.json')),
])
if code != 0:
    raise SystemExit(code)
args.result.write_bytes(destination.read_bytes())
""".format(
            source_root=str(ROOT / "src"),
            scratch_root=str(scratch_root),
            run_request=scheduled.run_request.as_posix(),
            handoff_reference=str(handoff_reference_path),
        ),
        encoding="utf-8",
    )

    profile_ref = ArtifactRef(
        profile_id,
        str(profile_path),
        adapter_profile_file_digest(profile_payload),
        "application/json",
        len(profile_payload),
    )
    handoff_file_ref = ArtifactRef(
        handoff.handoff_id,
        str(handoff_path),
        sha256_digest(handoff_payload),
        "application/json",
        len(handoff_payload),
    )
    task_ref = ArtifactRef(
        handoff.planned_store_ledger.layer_id,
        str(task_path),
        sha256_digest(task_payload),
        "application/x-ndjson",
        len(task_payload),
    )
    membership_ref = build_task_membership_index(
        handoff=handoff_file_ref,
        task_ledger=task_ref,
        destination=scheduled.root / "task-membership.sqlite3",
    )
    membership_verification_ref = seal_task_membership_verification(
        handoff=handoff_file_ref,
        task_ledger=task_ref,
        task_membership=membership_ref,
        destination=scheduled.root / "task-membership-verification.json",
    )
    deployment = DagsterDeploymentConfig(
        profile_ref,
        handoff_file_ref,
        task_ref,
        membership_ref,
        membership_verification_ref,
        str(result_root),
        (sys.executable, str(worker)),
        2,
        2,
        0,
        60,
        (75,),
    )
    deployment_path = scheduled.root / "deployment.json"
    deployment_path.write_bytes(deployment.to_bytes())

    job = build_dagster_definitions().get_job_def("docspec_store_tasks")
    execution = job.execute_in_process(
        run_config={
            "resources": {"docspec_deployment": {"config": {"path": str(deployment_path)}}},
        }
    )
    assert execution.success

    results = tuple(iter_persisted_task_results(deployment))
    assert {item.task.task_id for item in results} == {task.task_id for task in tasks}
    results_path = scheduled.root / "results.jsonl"
    results_path.write_bytes(b"".join(result.to_bytes() for result in results))
    run_reference, run_reference_path = _reconcile(scheduled, handoff_reference, results_path)
    scheduled_release = _commit(scheduled, run_reference_path)

    _assert_equivalent(
        local,
        local.run_receipt(local_run_ref),
        local_release,
        scheduled,
        scheduled.run_receipt(run_reference),
        scheduled_release,
    )
