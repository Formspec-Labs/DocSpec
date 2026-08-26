from __future__ import annotations

import importlib
import json
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

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


def _seed(root: Path) -> _Arm:
    source_content = root / "source-content"
    source_content.mkdir(parents=True)
    items = tuple(
        SourceItem(item_id, "2026-08-24", (), state=SourceItemState.DELETED)
        for item_id in _FIXTURE_IDENTITIES
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

    _, handoff_reference, handoff, tasks = _prepare_handoff(external)
    assert len(tasks) == 2
    profile = ExecutionProfile.from_dict(external.controls.load(handoff.execution_profile))
    backend = ExternalExecutionBackend(
        profile,
        _SubprocessDispatcher(external, handoff_reference),
        profile_reference=handoff.execution_profile,
        controls=external.controls,
    )
    lines = [result.to_bytes() for result in backend.execute(handoff, tasks)]
    replayed = _execute_task_in_subprocess(external, handoff_reference, tasks[0], "replay")
    assert replayed == next(line for line in lines if StoreTaskResult.from_bytes(line).task == tasks[0])

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
