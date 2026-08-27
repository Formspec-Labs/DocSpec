from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import docspec.cli as cli_module
from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
    RootOnlyBlobProfileStateReachability,
)
from docspec.application.maintenance import BlobRetentionSetService
from docspec.cli import main
from docspec.domain.content import SourceItem, SourceItemState
from docspec.domain.execution import ExecutionHandoff, StoreTask, iter_store_tasks
from docspec.domain.identity import canonical_json_file_bytes, sha256_digest
from docspec.domain.maintenance import BlobRetentionSet, ReleaseCompactionReceipt
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, DataUsePolicy, RetentionPolicy, RetryPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.profiles import ProfilePin, ProfileRole, ProfileSet
from docspec.domain.receipts import RunReceipt
from docspec.domain.jobs import StoreState
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, SourceCatalogRef, StoreRef
from docspec.processing.extraction import DefaultExtractorRegistry
from docspec.processing.segmentation import DefaultSegmenterRegistry
from docspec.profile_registry import ProfileRegistry
from tests.test_maintenance import _platform
from tests.helpers import (
    document_release_producer,
    source_catalog_producer,
    write_shared_source_catalog,
)


REPO_ROOT = Path(__file__).parents[1]
ZERO_DIGEST = "sha256:" + "0" * 64


@pytest.mark.parametrize(
    ("group", "commands"),
    [
        ("source-catalog", ("build", "verify")),
        ("profile", ("list", "verify")),
        ("scale-profile", ("seal", "verify")),
        ("document-catalog", ("open", "compare")),
        ("plan", ("create",)),
        ("document-store", ("create", "verify")),
        ("run", ("prepare", "start", "resume", "reconcile", "status")),
        ("task", ("execute",)),
        ("sink", ("verify",)),
        ("document-release", ("commit", "verify", "diff", "compact")),
        ("blob-store", ("verify", "gc")),
        ("conformance", ("run", "report")),
    ],
)
def test_one_cli_exposes_the_complete_lifecycle(
    group: str,
    commands: tuple[str, ...],
    capfd: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main([group, "--help"])
    assert raised.value.code == 0
    output = capfd.readouterr().out
    for command in commands:
        assert command in output


def test_profile_verification_uses_the_machine_description(capfd: pytest.CaptureFixture[str]) -> None:
    profile = REPO_ROOT / "profiles" / "canonical-release-manifest-v1.json"
    assert main(["profile", "verify", str(profile)]) == 0
    result = json.loads(capfd.readouterr().out)
    assert result == {
        "configurationDigest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        "descriptionDigest": result["descriptionDigest"],
        "fileDigest": result["fileDigest"],
        "format": "docspec-profile-verification",
        "formatVersion": "1.0",
        "implementationStatus": "implemented",
        "profileId": "urn:docspec:profile:release-manifest:canonical-json:1",
        "role": "ReleaseManifestProfile",
        "verdict": "pass",
        "version": "1.0.0",
    }
    assert result["fileDigest"].startswith("sha256:")
    assert result["descriptionDigest"].startswith("sha256:")


def _profile_set() -> ProfileSet:
    pins = tuple(
        sorted(
            (
                ProfilePin(
                    role=role,
                    profile_id=f"urn:docspec:test-profile:{role.value}",
                    version="1.0.0",
                    implementation_id=f"docspec.test.{role.name.lower()}.v1",
                    configuration_digest=ZERO_DIGEST,
                    description_digest=ZERO_DIGEST,
                    capabilities=("bounded",),
                )
                for role in ProfileRole
            ),
            key=lambda item: item.role.value,
        )
    )
    return ProfileSet(pins)


def test_plan_create_writes_canonical_artifact_and_receipt_once(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    request = {
        "sourceCatalog": SourceCatalogRef("urn:docspec:test:catalog-1", "catalog.json", ZERO_DIGEST).to_dict(),
        "baseRelease": None,
        "profiles": _profile_set().to_dict(),
        "limits": WorkLimits(10, 1000, 100, 200, 300, 4000, 60, 2).to_dict(),
        "stages": StagePolicy(("text-v1",), "paragraph-v1", ()).to_dict(),
        "processors": ProcessorSet(()).to_dict(),
        "partitionCount": 8,
        "selection": {"kind": "all"},
        "retentionPolicy": RetentionPolicy.retain_all().to_dict(),
        "dataUsePolicy": DataUsePolicy.local_content().to_dict(),
        "retryPolicyDigest": ZERO_DIGEST,
        "acceptedFailurePolicyDigest": ZERO_DIGEST,
    }
    request_path = tmp_path / "request.json"
    destination = tmp_path / "plan.json"
    receipt = tmp_path / "receipt.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    arguments = [
        "plan",
        "create",
        "--request",
        str(request_path),
        "--destination",
        str(destination),
        "--receipt",
        str(receipt),
    ]
    assert main(arguments) == 0
    operation = json.loads(capfd.readouterr().out)
    plan_value = json.loads(destination.read_text())
    plan = ProcessingPlan.from_dict(plan_value)
    assert destination.read_bytes() == canonical_json_file_bytes(plan.to_dict())
    assert operation["artifact"]["artifactId"] == plan.plan_id
    assert json.loads(receipt.read_text()) == operation

    assert main(arguments) == 2
    error = json.loads(capfd.readouterr().err)
    assert "refusing to replace" in error["message"]


def test_scale_profile_seal_and_verify_use_one_canonical_artifact(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    from docspec.domain.scale import ScaleProfile
    from tests.test_scale_profile import scale_profile_content

    request = tmp_path / "scale-content.json"
    destination = tmp_path / "scale-profile.json"
    receipt = tmp_path / "scale-operation.json"
    request.write_bytes(canonical_json_file_bytes(scale_profile_content()))

    assert main(
        [
            "scale-profile",
            "seal",
            "--request",
            str(request),
            "--destination",
            str(destination),
            "--receipt",
            str(receipt),
        ]
    ) == 0
    operation = json.loads(capfd.readouterr().out)
    profile = ScaleProfile.from_bytes(destination.read_bytes())
    assert operation["artifact"]["artifactId"] == profile.profile_id

    assert main(["scale-profile", "verify", str(destination)]) == 0
    verification = json.loads(capfd.readouterr().out)
    assert verification["profileId"] == profile.profile_id
    assert verification["profileDigest"] == profile.digest
    assert verification["workloadKind"] == "document-processing"
    assert verification["unitCount"] == 100_000
    assert verification["verdict"] == "pass"


def test_source_catalog_scale_profile_seal_and_verify_use_the_same_cli(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    from docspec.domain.scale import ScaleProfile
    from tests.test_scale_profile import source_catalog_scale_profile_content

    request = tmp_path / "catalog-scale-content.json"
    destination = tmp_path / "catalog-scale-profile.json"
    receipt = tmp_path / "catalog-scale-operation.json"
    request.write_bytes(canonical_json_file_bytes(source_catalog_scale_profile_content()))

    assert main(
        [
            "scale-profile",
            "seal",
            "--request",
            str(request),
            "--destination",
            str(destination),
            "--receipt",
            str(receipt),
        ]
    ) == 0
    operation = json.loads(capfd.readouterr().out)
    profile = ScaleProfile.from_bytes(destination.read_bytes())
    assert operation["artifact"]["artifactId"] == profile.profile_id

    assert main(["scale-profile", "verify", str(destination)]) == 0
    verification = json.loads(capfd.readouterr().out)
    assert verification == {
        "format": "docspec-scale-profile-verification",
        "formatVersion": "1.0",
        "maxSourceRecordCount": 100_000,
        "profileDigest": profile.digest,
        "profileId": profile.profile_id,
        "sourceNativeInputCount": 2,
        "verdict": "pass",
        "workloadKind": "source-catalog",
    }


def test_mutating_command_failure_writes_a_new_machine_receipt(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    request = tmp_path / "invalid-request.json"
    request.write_bytes(canonical_json_file_bytes({}))
    destination = tmp_path / "plan.json"
    receipt = tmp_path / "failure-receipt.json"

    assert main(
        [
            "plan",
            "create",
            "--request",
            str(request),
            "--destination",
            str(destination),
            "--receipt",
            str(receipt),
        ]
    ) == 2
    error = json.loads(capfd.readouterr().err)
    failure = json.loads(receipt.read_text(encoding="utf-8"))
    assert error["verdict"] == "fail"
    assert failure["format"] == "docspec-operation-failure-receipt"
    assert failure["operation"] == "plan.create"
    assert failure["requestDigest"] == sha256_digest(request.read_bytes())
    assert failure["verdict"] == "failed"
    assert not destination.exists()


def _portable_local_profiles() -> ProfileSet:
    return ProfileRegistry.from_directory(REPO_ROOT / "profiles").select(
        (
            "urn:docspec:profile:release-manifest:canonical-json:1",
            "urn:docspec:profile:document-catalog:local-manifest:1",
            "urn:docspec:profile:record-storage:local-jsonl:1",
            "urn:docspec:profile:blob-storage:local-content-addressed:1",
            "urn:docspec:profile:document-store-persistence:local-json:1",
            "urn:docspec:profile:result-delivery:durable-dataset:1",
        )
    )


def _write_local_run_request(
    path: Path,
    *,
    plan_path: Path,
    roots: dict[str, str],
    result_sink_id: str,
    retry: RetryPolicy,
    accepted: AcceptedFailurePolicy,
    completed_at: str,
    partition_policy_id: str = "source-item-sha256-v1",
    max_workers: int = 1,
    max_in_flight: int = 1,
) -> Path:
    path.write_bytes(
        canonical_json_file_bytes(
            {
                "format": "docspec-local-run-request",
                "formatVersion": "1.0",
                "documentReleaseProducer": document_release_producer().as_dict(),
                "sourceCatalogProducer": source_catalog_producer().as_dict(),
                "plan": plan_path.as_posix(),
                "profileDirectory": (REPO_ROOT / "profiles").as_posix(),
                "roots": roots,
                "resultSinkId": result_sink_id,
                "partitionPolicyId": partition_policy_id,
                "retryPolicy": retry.to_dict(),
                "acceptedFailurePolicy": accepted.to_dict(),
                "execution": {
                    "maxWorkers": max_workers,
                    "maxInFlight": max_in_flight,
                    "deadlineEpochSeconds": 2_000_000_000,
                },
                "completedAt": completed_at,
            }
        )
    )
    return path


@pytest.mark.parametrize("state", (StoreState.RUNNING, StoreState.SEALED))
def test_local_task_recovery_executes_only_an_unfinished_store(state: StoreState) -> None:
    plan_ref = ArtifactRef("plan-1", "plan.json", ZERO_DIGEST, "application/json", 1)
    sink_ref = ArtifactRef("sink-1", "sink.json", ZERO_DIGEST, "application/json", 1)
    planned_ref = StoreRef("store-1", 0, "planned.json", ZERO_DIGEST)
    current_ref = StoreRef("store-1", 2, "current.json", ZERO_DIGEST)
    processed_ref = StoreRef("store-1", 3, "processed.json", ZERO_DIGEST)
    sealed_ref = StoreRef("store-1", 4, "sealed.json", ZERO_DIGEST)
    task = StoreTask("plan-1", "execute-and-deliver", planned_ref)
    current_store = SimpleNamespace(plan_id="plan-1", state=state)
    executor_calls: list[StoreRef] = []
    delivery_calls: list[StoreRef] = []

    class _Stores:
        def load(self, reference: StoreRef) -> object:
            assert reference in (planned_ref, current_ref)
            return current_store

        def latest(self, store_id: str) -> StoreRef:
            assert store_id == "store-1"
            return current_ref

    class _Executor:
        def execute_store(self, reference: StoreRef) -> StoreRef:
            executor_calls.append(reference)
            return processed_ref

    class _Delivery:
        def deliver_store(self, reference: StoreRef, requested_sink: ArtifactRef) -> StoreRef:
            assert requested_sink == sink_ref
            delivery_calls.append(reference)
            return current_ref if state is StoreState.SEALED else sealed_ref

    composition = SimpleNamespace(
        plan=SimpleNamespace(plan_id="plan-1"),
        plan_ref=plan_ref,
        stores=_Stores(),
        executor=_Executor(),
        delivery=_Delivery(),
    )
    prepared = SimpleNamespace(
        handoff=SimpleNamespace(
            operation_id="execute-and-deliver",
            processing_plan=plan_ref,
            result_sink=sink_ref,
            handoff_id="handoff-1",
        )
    )

    result = cli_module._execute_local_task(composition, prepared, task)

    if state is StoreState.SEALED:
        assert executor_calls == []
        assert delivery_calls == [current_ref]
        assert result.output_store == current_ref
    else:
        assert executor_calls == [current_ref]
        assert delivery_calls == [processed_ref]
        assert result.output_store == sealed_ref


def test_local_run_start_resume_and_release_commit_use_real_application_services(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_content = tmp_path / "source-content"
    source_content.mkdir()
    source_catalog_root = tmp_path / "source-catalog"
    item = SourceItem(
        "document-a",
        "2026-08-24",
        (),
        state=SourceItemState.DELETED,
    )
    source_ref = write_shared_source_catalog(source_catalog_root, (item,))
    retry = RetryPolicy()
    accepted = AcceptedFailurePolicy()
    plan = ProcessingPlan.create(
        source_catalog=source_ref,
        base_release=None,
        profiles=_portable_local_profiles(),
        limits=WorkLimits(2, 1024 * 1024, 10, 10, 100, 1024 * 1024, 60, retry.max_attempts),
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
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(canonical_json_file_bytes(plan.to_dict()))
    roots = {
        "blobStorage": (tmp_path / "blobs").as_posix(),
        "controlRepository": (tmp_path / "controls").as_posix(),
        "documentCatalog": (tmp_path / "catalog").as_posix(),
        "documentStores": (tmp_path / "stores").as_posix(),
        "reconciliation": (tmp_path / "reconciliation").as_posix(),
        "recordStorage": (tmp_path / "records").as_posix(),
        "sourceCatalog": source_catalog_root.as_posix(),
        "sourceContent": source_content.as_posix(),
    }
    run_request = _write_local_run_request(
        tmp_path / "run-request.json",
        plan_path=plan_path,
        roots=roots,
        result_sink_id="urn:docspec:test:sink:local-durable",
        retry=retry,
        accepted=accepted,
        completed_at="2026-08-05T12:00:00Z",
        max_workers=2,
        max_in_flight=2,
    )
    run_reference_path = tmp_path / "run-reference.json"
    run_operation_path = tmp_path / "run-operation.json"
    start_arguments = [
        "run",
        "start",
        "--request",
        str(run_request),
        "--destination",
        str(run_reference_path),
        "--receipt",
        str(run_operation_path),
    ]
    assert main(start_arguments) == 0
    start_operation = json.loads(capfd.readouterr().out)
    run_reference = ArtifactRef.from_dict(json.loads(run_reference_path.read_text()))
    controls = LocalJsonControlRepository(Path(roots["controlRepository"]))
    run_receipt = RunReceipt.from_dict(controls.load(run_reference))
    assert start_operation["operation"] == "run.start"
    assert start_operation["verdict"] == "completed"
    assert run_receipt.store_count == 1
    assert run_receipt.selected_item_count == 1
    assert (
        main(
            [
                "run",
                "status",
                "--receipt",
                str(run_reference_path),
                "--control-root",
                roots["controlRepository"],
            ]
        )
        == 0
    )
    run_status = json.loads(capfd.readouterr().out)
    assert run_status["runId"] == run_receipt.run_id
    assert run_status["status"] == "completed"
    assert run_status["failureCount"] == 0
    assert run_status["verificationScope"] == "run-receipt-structure"
    assert run_status["verdict"] == "structurally-valid"

    def unexpected_replanning(*_args: object, **_kwargs: object) -> object:
        pytest.fail("automatic recovery must not rerun planning when a durable planned-store ledger exists")

    def unexpected_execution(*_args: object, **_kwargs: object) -> object:
        pytest.fail("automatic recovery must not deeply re-execute an already sealed store")

    with monkeypatch.context() as recovery_patch:
        recovery_patch.setattr(cli_module.RunPlanner, "plan_run", unexpected_replanning)
        recovery_patch.setattr(cli_module.StoreExecutionService, "execute_store", unexpected_execution)
        automatic_resume = cli_module._execute_local_run(
            cli_module._local_run_request(run_request),
            resume=None,
        )
    assert automatic_resume == run_reference

    resume_reference_path = tmp_path / "resume-reference.json"
    resume_operation_path = tmp_path / "resume-operation.json"
    assert (
        main(
            [
                "run",
                "resume",
                "--request",
                str(run_request),
                "--destination",
                str(resume_reference_path),
                "--receipt",
                str(resume_operation_path),
            ]
        )
        == 0
    )
    resume_operation = json.loads(capfd.readouterr().out)
    assert ArtifactRef.from_dict(json.loads(resume_reference_path.read_text())) == run_reference
    assert resume_operation["operation"] == "run.resume"

    handoff_reference_path = tmp_path / "handoff-reference.json"
    prepare_operation_path = tmp_path / "prepare-operation.json"
    assert (
        main(
            [
                "run",
                "prepare",
                "--request",
                str(run_request),
                "--destination",
                str(handoff_reference_path),
                "--receipt",
                str(prepare_operation_path),
            ]
        )
        == 0
    )
    assert json.loads(capfd.readouterr().out)["operation"] == "run.prepare"
    handoff_reference = ArtifactRef.from_dict(json.loads(handoff_reference_path.read_text()))
    handoff = ExecutionHandoff.from_dict(controls.load(handoff_reference))
    stores_for_task = LocalDocumentStoreRepository(Path(roots["documentStores"]))
    task_to_execute = next(
        iter_store_tasks(
            plan.plan_id,
            handoff.operation_id,
            stores_for_task.stream_planned_stores(handoff.planned_store_ledger),
        )
    )
    task_request = tmp_path / "task-request.json"
    task_request.write_bytes(
        canonical_json_file_bytes(
            {
                "format": "docspec-local-task-execution-request",
                "formatVersion": "1.0",
                "runRequest": run_request.as_posix(),
                "handoff": handoff_reference.to_dict(),
                "task": task_to_execute.to_dict(),
            }
        )
    )
    task_result_path = tmp_path / "task-result.jsonl"
    task_operation_path = tmp_path / "task-operation.json"
    task_process = subprocess.run(
        [
            sys.executable,
            "-m",
            "docspec.cli",
            "task",
            "execute",
            "--request",
            str(task_request),
            "--destination",
            str(task_result_path),
            "--receipt",
            str(task_operation_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert task_process.returncode == 0, task_process.stderr
    assert json.loads(task_process.stdout)["operation"] == "task.execute"
    reconcile_request = tmp_path / "reconcile-request.json"
    reconcile_request.write_bytes(
        canonical_json_file_bytes(
            {
                "format": "docspec-local-run-reconcile-request",
                "formatVersion": "1.0",
                "runRequest": run_request.as_posix(),
                "handoff": handoff_reference.to_dict(),
                "results": task_result_path.as_posix(),
            }
        )
    )
    reconciled_reference_path = tmp_path / "reconciled-reference.json"
    reconcile_operation_path = tmp_path / "reconcile-operation.json"
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
                str(reconcile_operation_path),
            ]
        )
        == 0
    )
    assert json.loads(capfd.readouterr().out)["operation"] == "run.reconcile"
    assert ArtifactRef.from_dict(json.loads(reconciled_reference_path.read_text())) == run_reference

    commit_request = tmp_path / "commit-request.json"
    commit_request.write_bytes(
        canonical_json_file_bytes(
            {
                "format": "docspec-local-release-commit-request",
                "formatVersion": "1.0",
                "runRequest": run_request.as_posix(),
                "runReceipt": run_reference_path.as_posix(),
                "baseRelease": None,
            }
        )
    )
    release_reference_path = tmp_path / "release-reference.json"
    commit_operation_path = tmp_path / "commit-operation.json"
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
                str(commit_operation_path),
            ]
        )
        == 0
    )
    commit_operation = json.loads(capfd.readouterr().out)
    release_reference = DocumentReleaseRef.from_dict(json.loads(release_reference_path.read_text()))
    stores = LocalDocumentStoreRepository(Path(roots["documentStores"]))
    records = LocalJsonlRecordStorage(Path(roots["recordStorage"]))
    catalog = LocalManifestDocumentCatalog(
        Path(roots["documentCatalog"]),
        records=records,
        stores=stores,
        controls=controls,
        producer=document_release_producer(),
        blobs=LocalContentAddressedBlobStore(Path(roots["blobStorage"])),
    )
    assert commit_operation["operation"] == "document-release.commit"
    assert commit_operation["verdict"] == "completed"
    assert catalog.current() == release_reference
    assert catalog.open(release_reference).run_receipt == run_reference

    assert (
        main(
            [
                "document-catalog",
                "open",
                "--catalog-root",
                roots["documentCatalog"],
                "--blob-root",
                roots["blobStorage"],
                "--record-root",
                roots["recordStorage"],
                "--store-root",
                roots["documentStores"],
                "--control-root",
                roots["controlRepository"],
                "--reference",
                str(release_reference_path),
                "--implementation-id",
                document_release_producer().implementation_id,
                "--verifier-implementation-id",
                document_release_producer().verifier_implementation_id,
            ]
        )
        == 0
    )
    opened = json.loads(capfd.readouterr().out)
    assert opened["reference"] == release_reference.to_dict()
    assert opened["release"]["runReceipt"] == run_reference.to_dict()

    assert main(start_arguments) == 2
    error = json.loads(capfd.readouterr().err)
    assert "refusing to replace" in error["message"]


def _maintenance_run_request(
    tmp_path: Path,
    platform,
    *,
    completed_at: str,
) -> tuple[Path, ProcessingPlan]:
    release = platform.catalog.open(platform.release)
    plan = ProcessingPlan.from_dict(platform.controls.load(release.processing_plan))
    plan_path = tmp_path / "maintenance-plan.json"
    plan_path.write_bytes(canonical_json_file_bytes(plan.to_dict()))
    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    request = _write_local_run_request(
        tmp_path / "maintenance-run-request.json",
        plan_path=plan_path,
        roots={
            "blobStorage": platform.blobs.root.as_posix(),
            "controlRepository": platform.controls.root.as_posix(),
            "documentCatalog": platform.catalog.root.as_posix(),
            "documentStores": platform.stores.root.as_posix(),
            "reconciliation": (tmp_path / "maintenance-workspace").as_posix(),
            "recordStorage": platform.records.root.as_posix(),
            "sourceCatalog": (tmp_path / "unused-source-catalog").as_posix(),
            "sourceContent": (tmp_path / "unused-source-content").as_posix(),
        },
        result_sink_id="urn:docspec:test:sink:maintenance",
        retry=retry,
        accepted=accepted,
        completed_at=completed_at,
        partition_policy_id=platform.partition_policy.policy_id,
    )
    return request, plan


def test_document_release_compact_runs_the_local_maintenance_service(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform_root = tmp_path / "platform"
    platform_root.mkdir()
    platform = _platform(platform_root, document_count=12, member_bytes=4 * 1024)
    run_request, plan = _maintenance_run_request(
        tmp_path,
        platform,
        completed_at="2026-08-05T16:00:00Z",
    )
    compacted_records = LocalJsonlRecordStorage(
        platform.records.root,
        max_member_bytes=1024 * 1024,
    )
    compacting_catalog = LocalManifestDocumentCatalog(
        platform.catalog.root,
        records=compacted_records,
        stores=platform.stores,
        controls=platform.controls,
        producer=document_release_producer(),
        blobs=platform.blobs,
    )
    monkeypatch.setattr(cli_module, "_verified_local_plan", lambda _request: (plan, {}, {}))
    monkeypatch.setattr(
        cli_module,
        "_local_storage",
        lambda _roots, _profiles, _producer: (
            platform.controls,
            platform.stores,
            compacted_records,
            platform.blobs,
            compacting_catalog,
        ),
    )
    request = tmp_path / "compaction-request.json"
    request.write_bytes(
        canonical_json_file_bytes(
            {
                "format": "docspec-local-release-compaction-request",
                "formatVersion": "1.0",
                "runRequest": run_request.as_posix(),
                "sourceRelease": platform.release.to_dict(),
            }
        )
    )
    destination = tmp_path / "compaction-reference.json"
    operation_path = tmp_path / "compaction-operation.json"

    assert (
        main(
            [
                "document-release",
                "compact",
                "--request",
                str(request),
                "--destination",
                str(destination),
                "--receipt",
                str(operation_path),
            ]
        )
        == 0
    )
    operation = json.loads(capfd.readouterr().out)
    reference = ArtifactRef.from_dict(json.loads(destination.read_text(encoding="utf-8")))
    compaction = ReleaseCompactionReceipt.from_dict(platform.controls.load(reference))

    assert operation["operation"] == "document-release.compact"
    assert operation["artifact"]["artifactId"] == compaction.receipt_id
    assert compaction.source_release == platform.release
    assert compaction.successor_release == compacting_catalog.current()
    assert compaction.source_logical_state_digest == compaction.successor_logical_state_digest
    assert compaction.rewritten_layer_kinds


def test_blob_gc_streams_a_sealed_retention_layer_through_a_bounded_index(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform_root = tmp_path / "platform"
    platform_root.mkdir()
    platform = _platform(platform_root, document_count=2, member_bytes=64 * 1024)
    release = platform.catalog.open(platform.release)
    orphan_locators = {
        platform.blobs.put_if_absent((payload,), media_type="application/octet-stream").locator
        for payload in (b"orphan-a", b"orphan-b")
    }
    retention_reference = BlobRetentionSetService(
        controls=platform.controls,
        records=platform.records,
        stores=platform.stores,
        blobs=platform.blobs,
        document_catalog=platform.catalog,
        profile_state_reachability=RootOnlyBlobProfileStateReachability(),
        workspace_factory=LocalSqliteReconciliationWorkspaceFactory(
            tmp_path / "retention-workspace"
        ),
        partition_policy=platform.partition_policy,
    ).build(
        blob_profile_state=release.blob_roots[0],
        retained_releases=(platform.release,),
    )
    retention = BlobRetentionSet.from_dict(platform.controls.load(retention_reference))
    retention_path = tmp_path / "retention-reference.json"
    retention_path.write_bytes(canonical_json_file_bytes(retention_reference.to_dict()))
    run_request, plan = _maintenance_run_request(
        tmp_path,
        platform,
        completed_at="2026-08-05T16:00:00Z",
    )
    monkeypatch.setattr(cli_module, "_verified_local_plan", lambda _request: (plan, {}, {}))
    monkeypatch.setattr(
        cli_module,
        "_local_storage",
        lambda _roots, _profiles, _producer: (
            platform.controls,
            platform.stores,
            platform.records,
            platform.blobs,
            platform.catalog,
        ),
    )

    assert (
        main(
            [
                "blob-store",
                "gc",
                "--run-request",
                str(run_request),
                "--retention-set",
                str(retention_path),
                "--minimum-age-seconds",
                "0",
                "--sample-limit",
                "1",
                "--dry-run",
            ]
        )
        == 0
    )
    result = json.loads(capfd.readouterr().out)

    assert result["verdict"] == "pass"
    assert result["retentionSet"] == retention_reference.to_dict()
    assert result["retentionReferenceLayer"] == retention.references.to_dict()
    assert result["retainedReferenceCount"] == retention.references.record_count
    assert result["retainedObjectCount"] == retention.references.record_count
    assert result["candidateCount"] == 2
    assert result["candidateSampleLimit"] == 1
    assert result["candidateSampleTruncated"] is True
    assert len(result["candidateSample"]) == 1
    assert result["candidateSample"][0]["locator"] in orphan_locators
    assert result["boundedMembershipIndex"]["adapterId"] == (
        "docspec.local-sqlite-record-workspace"
    )
    assert not tuple((tmp_path / "maintenance-workspace" / "blob-gc").glob("*.sqlite3"))
