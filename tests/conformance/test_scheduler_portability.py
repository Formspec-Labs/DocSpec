from __future__ import annotations

import importlib
import json
import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

import pytest

from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.adapters.execution import ExternalExecutionBackend
from docspec.cli import main
from docspec.domain.execution import ExecutionHandoff, ExecutionProfile, StoreTask, StoreTaskResult, iter_store_tasks
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
_qualification_job = importlib.import_module("tests.dagster_qualification_job")
_active_document_state = _equivalence._active_document_state
_portable_local_profiles = _cli_helpers._portable_local_profiles
_write_local_run_request = _cli_helpers._write_local_run_request
shared_source_item = _platform_helpers.shared_source_item
write_shared_source_catalog = _platform_helpers.write_shared_source_catalog
docspec_qualified_celery_job = _qualification_job.docspec_qualified_celery_job

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


@dataclass(frozen=True)
class _DockerWorker:
    container_id: str
    name: str
    hostname: str
    image_id: str
    restart_policy: str


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


def _prepare_dagster_deployment(
    scheduled: _Arm,
    *,
    wait_for_worker_loss: bool = False,
    worker_executable: str = sys.executable,
):
    from docspec.adapters.dagster import (
        DagsterDeploymentConfig,
        adapter_profile_file_digest,
        build_task_membership_index,
        seal_task_membership_verification,
    )

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
    worker_loss = (
        "loss_marker = scratch / 'worker-loss-ready.json'\n"
        f"if task.task_id == {tasks[0].task_id!r} and not loss_marker.exists():\n"
        "    loss_marker.write_bytes(canonical_json_file_bytes({\n"
        "        'hostname': socket.gethostname(),\n"
        "        'taskId': task.task_id,\n"
        "    }))\n"
        "    time.sleep(300)\n"
        if wait_for_worker_loss
        else ""
    )
    worker.write_text(
        """
import argparse
import json
import socket
import sys
import time
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
{worker_loss}task_request = scratch / ('cli-request-' + label + '.json')
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
            worker_loss=worker_loss,
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
        (worker_executable, str(worker)),
        2,
        2,
        0,
        60,
        (75,),
    )
    deployment_path = scheduled.root / "deployment.json"
    deployment_path.write_bytes(deployment.to_bytes())
    return deployment, deployment_path, handoff_reference, tasks


def _docker_workers(value: str) -> tuple[str, tuple[_DockerWorker, ...]]:
    docker = shutil.which("docker")
    assert docker is not None, "the worker-loss qualification requires the Docker CLI"
    requested = tuple(item.strip() for item in value.split(",") if item.strip())
    assert len(requested) == 2 and len(set(requested)) == len(requested), (
        "qualification requires exactly two distinct Docker worker containers"
    )
    workers = []
    for requested_name in requested:
        inspected = subprocess.run(
            [
                docker,
                "inspect",
                "--format",
                (
                    "{{.Id}}\t{{.Name}}\t{{.Config.Hostname}}\t{{.Image}}\t"
                    "{{.HostConfig.RestartPolicy.Name}}\t{{.State.Running}}"
                ),
                requested_name,
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        assert inspected.returncode == 0, inspected.stderr
        container_id, name, hostname, image_id, restart_policy, running = inspected.stdout.strip().split("\t")
        worker = _DockerWorker(container_id, name.removeprefix("/"), hostname, image_id, restart_policy)
        assert worker.name == requested_name and running == "true", (
            f"Docker worker container {requested_name!r} must be running"
        )
        assert worker.restart_policy in {"", "no"}, (
            f"Docker worker container {requested_name!r} must disable automatic restart for qualification"
        )
        workers.append(worker)
    assert len({worker.container_id for worker in workers}) == len(workers)
    assert len({worker.hostname for worker in workers}) == len(workers)
    return docker, tuple(workers)


def _verified_worker_configurations(
    *,
    broker: str,
    backend: str,
    workers: tuple[_DockerWorker, ...],
) -> dict[str, dict[str, object]]:
    from celery import Celery

    app = Celery(broker=broker, backend=backend)
    try:
        configurations = app.control.inspect(timeout=5).conf()
    finally:
        app.close()
    assert isinstance(configurations, dict), "qualification could not inspect the declared Celery workers"
    by_hostname = {name.rsplit("@", 1)[-1]: configuration for name, configuration in configurations.items()}
    required = {
        "task_acks_late": True,
        "task_reject_on_worker_lost": True,
        "worker_prefetch_multiplier": 1,
    }
    observed = {}
    for worker in workers:
        configuration = by_hostname.get(worker.hostname)
        assert isinstance(configuration, dict), (
            f"Docker container {worker.name!r} is not running one broker-visible Celery worker"
        )
        assert all(configuration.get(name) == expected for name, expected in required.items()), (
            f"Celery worker in {worker.name!r} does not enable the required late-ack redelivery configuration"
        )
        observed[worker.hostname] = required
    return observed


def _terminate_marked_worker(
    *,
    docker: str,
    workers: tuple[_DockerWorker, ...],
    marker: Path,
    expected_task_id: str,
    execution: mp.Process,
) -> tuple[_DockerWorker, int]:
    deadline = time.monotonic() + 120
    marker_payload = None
    while time.monotonic() < deadline:
        if marker.is_file():
            try:
                marker_payload = json.loads(marker.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
            else:
                break
        if not execution.is_alive():
            raise AssertionError("Dagster execution ended before a mapped task reached the worker-loss marker")
        time.sleep(0.1)
    assert marker_payload is not None, "no mapped task reached the Docker worker-loss marker"
    assert marker_payload.get("taskId") == expected_task_id
    hostname = marker_payload.get("hostname")
    target = next((worker for worker in workers if worker.hostname == hostname), None)
    assert target is not None, "the marked task did not run in a declared Docker worker container"
    terminated = subprocess.run(
        [docker, "kill", "--signal", "KILL", target.name],
        capture_output=True,
        check=False,
        text=True,
    )
    assert terminated.returncode == 0, terminated.stderr
    state = subprocess.run(
        [docker, "inspect", "--format", "{{.State.Running}}\t{{.State.ExitCode}}", target.name],
        capture_output=True,
        check=False,
        text=True,
    )
    assert state.returncode == 0, state.stderr
    running, exit_code = state.stdout.strip().split("\t")
    assert running == "false", f"Docker worker container {target.name!r} remained running"
    return target, int(exit_code)


def _restart_docker_worker(docker: str, worker: _DockerWorker) -> None:
    restarted = subprocess.run(
        [docker, "start", worker.name],
        capture_output=True,
        check=False,
        text=True,
    )
    assert restarted.returncode == 0, restarted.stderr


def _execute_qualified_celery_job(
    deployment_path: str,
    broker: str,
    backend: str,
    run_ids,
) -> None:
    from dagster import DagsterInstance, execute_job, reconstructable

    with DagsterInstance.get() as instance:
        with execute_job(
            reconstructable(docspec_qualified_celery_job),
            instance=instance,
            run_config={
                "resources": {"docspec_deployment": {"config": {"path": deployment_path}}},
                "execution": {
                    "config": {
                        "broker": broker,
                        "backend": backend,
                        "config_source": {
                            "task_acks_late": True,
                            "task_reject_on_worker_lost": True,
                            "worker_prefetch_multiplier": 1,
                        },
                    }
                },
            },
        ) as execution:
            if not execution.success:
                raise AssertionError(f"qualified Dagster run {execution.run_id} failed")
            run_ids.put(execution.run_id)


def test_serialized_tasks_cross_a_real_process_boundary_and_match_the_local_backend(tmp_path: Path) -> None:
    """The same fixture graph runs through the local backend and through
    brand-new OS processes that rebuild their worker from the pinned run
    request and sealed handoff; replaying one task returns identical bytes,
    the results reconcile out of order with a duplicate, and both paths
    publish equivalent sealed stores, run receipts, and logical state."""

    local = _seed(tmp_path / "local")
    external = _seed(tmp_path / "external")
    local_run_ref, local_release = _run_local_backend(local)

    _, handoff_reference, handoff, tasks = _prepare_handoff(external)
    assert len(tasks) == 2, "the shared fixture graph must plan into two serialized tasks"
    profile = ExecutionProfile.from_dict(external.controls.load(handoff.execution_profile))
    backend = ExternalExecutionBackend(
        profile,
        _SubprocessDispatcher(external, handoff_reference),
        profile_reference=handoff.execution_profile,
        controls=external.controls,
    )
    lines = [result.to_bytes() for result in backend.execute(handoff, tasks)]
    replayed = _execute_task_in_subprocess(external, handoff_reference, tasks[0], "replay")
    assert replayed == next(
        line for line in lines if StoreTaskResult.from_bytes(line).task == tasks[0]
    ), "a fresh process replay returns the identical serialized result"

    results_path = external.root / "results.jsonl"
    results_path.write_bytes(b"".join((*lines, replayed)))
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
    from docspec.adapters.dagster import build_dagster_definitions, iter_persisted_task_results

    local = _seed(tmp_path / "local")
    scheduled = _seed(tmp_path / "scheduled")
    local_run_ref, local_release = _run_local_backend(local)

    deployment, deployment_path, handoff_reference, tasks = _prepare_dagster_deployment(scheduled)

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


@pytest.mark.integration
def test_qualified_celery_workers_publish_the_local_backend_state(tmp_path: Path) -> None:
    """Kill a Docker worker mid-task and require broker redelivery on another worker."""

    broker = os.environ.get("DOCSPEC_DAGSTER_CELERY_BROKER_URL")
    worker_container_value = os.environ.get("DOCSPEC_DAGSTER_CELERY_WORKER_CONTAINERS")
    if broker is None or worker_container_value is None:
        pytest.skip(
            "set DOCSPEC_DAGSTER_CELERY_BROKER_URL and DOCSPEC_DAGSTER_CELERY_WORKER_CONTAINERS "
            "for the Docker worker-loss qualification"
        )
    backend = os.environ.get("DOCSPEC_DAGSTER_CELERY_BACKEND_URL", "rpc://")
    assert broker.startswith(("amqp://", "pyamqp://")), (
        "the worker-loss qualification requires RabbitMQ redelivery semantics"
    )
    docker, docker_workers = _docker_workers(worker_container_value)
    dagster = pytest.importorskip("dagster", reason="install the 'dagster' extra for qualification")
    pytest.importorskip("dagster_celery", reason="install dagster-celery for qualification")
    from dagster import DagsterInstance

    from docspec.adapters.dagster import iter_persisted_task_results

    worker_configurations = _verified_worker_configurations(
        broker=broker,
        backend=backend,
        workers=docker_workers,
    )

    local = _seed(tmp_path / "local")
    scheduled = _seed(tmp_path / "scheduled")
    local_run_ref, local_release = _run_local_backend(local)
    deployment, deployment_path, handoff_reference, tasks = _prepare_dagster_deployment(
        scheduled,
        wait_for_worker_loss=True,
        worker_executable="python",
    )
    loss_marker = scheduled.root / "worker-scratch" / "worker-loss-ready.json"
    process_context = mp.get_context("spawn")
    run_ids = process_context.Queue(maxsize=1)
    execution_process = process_context.Process(
        target=_execute_qualified_celery_job,
        args=(str(deployment_path), broker, backend, run_ids),
        name="docspec-dagster-qualified-run",
    )
    execution_process.start()
    terminated_worker = None
    terminated_exit_code = None
    try:
        terminated_worker, terminated_exit_code = _terminate_marked_worker(
            docker=docker,
            workers=docker_workers,
            marker=loss_marker,
            expected_task_id=tasks[0].task_id,
            execution=execution_process,
        )
        execution_process.join(180)
        if execution_process.is_alive():
            execution_process.terminate()
            execution_process.join(10)
            pytest.fail("Dagster did not complete the broker-redelivered mapped task within 180 seconds")
        assert execution_process.exitcode == 0
        run_id = run_ids.get(timeout=5)
        with DagsterInstance.get() as instance:
            event_log = tuple(instance.all_logs(run_id))
    finally:
        if execution_process.is_alive():
            execution_process.terminate()
            execution_process.join(10)
        for worker in docker_workers:
            _restart_docker_worker(docker, worker)

    assert terminated_worker is not None and terminated_exit_code is not None

    task_workers: dict[str, set[str]] = {}
    retry_count = 0
    for entry in event_log:
        event = entry.dagster_event
        if event is None:
            continue
        if event.event_type_value == "STEP_UP_FOR_RETRY":
            retry_count += 1
        step_key = event.step_key
        if (
            not event.is_engine_event
            or event.engine_event_data is None
            or not isinstance(step_key, str)
            or not step_key.startswith("execute_store_task[")
        ):
            continue
        worker = event.engine_event_data.metadata.get("Celery worker")
        value = None if worker is None else getattr(worker, "value", None)
        if isinstance(value, str):
            task_workers.setdefault(step_key, set()).add(value)

    expected_step_keys = {f"execute_store_task[{task.task_id.rsplit(':', 1)[-1]}]" for task in tasks}
    assert set(task_workers) == expected_step_keys, "every sealed handoff task must report its executing Celery worker"
    observed_task_workers = {worker for workers in task_workers.values() for worker in workers}
    observed_hostnames = {worker.rsplit("@", 1)[-1] for worker in observed_task_workers}
    assert observed_hostnames == {worker.hostname for worker in docker_workers}, (
        "every claimed Docker worker must execute at least one sealed handoff task"
    )
    assert terminated_worker.hostname in observed_hostnames
    loss_task_step = f"execute_store_task[{tasks[0].task_id.rsplit(':', 1)[-1]}]"
    loss_task_hostnames = {worker.rsplit("@", 1)[-1] for worker in task_workers[loss_task_step]}
    assert terminated_worker.hostname in loss_task_hostnames
    assert loss_task_hostnames - {terminated_worker.hostname}, (
        "the broker must redeliver the interrupted mapped task to a different Docker worker"
    )
    assert retry_count == 0, "broker redelivery must not be mislabeled as a Dagster task retry"
    results = tuple(iter_persisted_task_results(deployment))
    assert {item.task.task_id for item in results} == {task.task_id for task in tasks}
    results_path = scheduled.root / "results.jsonl"
    results_path.write_bytes(b"".join((*[result.to_bytes() for result in reversed(results)], results[0].to_bytes())))
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

    evidence_path_value = os.environ.get("DOCSPEC_DAGSTER_QUALIFICATION_EVIDENCE")
    if evidence_path_value is not None:
        evidence_path = Path(evidence_path_value)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_bytes(
            canonical_json_file_bytes(
                {
                    "deployment": {
                        "executor": "dagster_celery.celery_executor",
                        "executorVersion": version("dagster-celery"),
                        "nodeBoundary": "distinct Docker container IDs observed through one Docker Engine",
                        "schedulerVersion": dagster.__version__,
                        "workerContainers": [
                            {
                                "containerId": worker.container_id,
                                "containerName": worker.name,
                                "hostname": worker.hostname,
                                "imageId": worker.image_id,
                                "restartPolicy": worker.restart_policy or "no",
                            }
                            for worker in docker_workers
                        ],
                        "workerConfiguration": worker_configurations,
                    },
                    "execution": {
                        "handoffId": deployment.handoff.artifact_id,
                        "retryEventCount": retry_count,
                        "runId": run_id,
                        "taskIds": sorted(task.task_id for task in tasks),
                        "taskWorkers": {
                            step_key: sorted(workers) for step_key, workers in sorted(task_workers.items())
                        },
                        "terminatedContainerId": terminated_worker.container_id,
                        "terminatedContainerExitCode": terminated_exit_code,
                        "workerLossStep": loss_task_step,
                    },
                    "format": "docspec-dagster-multinode-qualification",
                    "formatVersion": "1.0",
                    "result": {
                        "localArtifactDigest": local_release.digest,
                        "logicalId": local_release.release_id,
                        "scheduledArtifactDigest": scheduled_release.digest,
                    },
                    "testId": "SCHEDULER-PORTABILITY",
                    "verdict": "passed",
                }
            )
        )
