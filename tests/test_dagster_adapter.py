from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import docspec.adapters.dagster as dagster_adapter
from docspec.adapters.dagster import (
    DagsterAdapterError,
    DagsterAdapterProfile,
    DagsterDeploymentConfig,
    DagsterTransientWorkerError,
    adapter_profile_file_digest,
    build_dagster_definitions,
    build_task_membership_index,
    dagster_run_config,
    execute_or_reuse_task,
    invoke_worker_process,
    iter_persisted_task_results,
    iter_verified_store_tasks,
    load_dagster_adapter_profile,
    parse_worker_request,
    persist_task_result,
    registered_dagster_adapter_profile,
    seal_task_membership_verification,
)
from docspec.domain.execution import (
    EXECUTE_AND_DELIVER_OPERATION_ID,
    MAX_RESULT_BYTES,
    ExecutionHandoff,
    StoreTask,
    StoreTaskResult,
    summarize_store_tasks,
)
from docspec.domain.identity import canonical_json_file_bytes, sha256_digest
from docspec.domain.references import ArtifactRef, LayerRef, StoreRef
from docspec.errors import IntegrityError, LimitExceededError, ProfileError

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "profiles" / "schedulers" / "dagster-dynamic-process-v1.json"


def _artifact(identifier: str) -> ArtifactRef:
    payload = canonical_json_file_bytes({"id": identifier})
    return ArtifactRef(identifier, f"memory://{identifier}", sha256_digest(payload), "application/json", len(payload))


def _deployment(
    tmp_path: Path,
    *,
    worker_command: tuple[str, ...] = ("docspec-worker",),
) -> tuple[DagsterDeploymentConfig, ExecutionHandoff, tuple[StoreTask, ...]]:
    plan = _artifact("urn:docspec:test:plan")
    tasks = tuple(
        StoreTask(
            plan.artifact_id,
            EXECUTE_AND_DELIVER_OPERATION_ID,
            StoreRef(
                f"store-{index}",
                0,
                f"memory://store-{index}",
                sha256_digest(str(index).encode()),
            ),
        )
        for index in range(3)
    )
    count, task_set_digest = summarize_store_tasks(tasks)
    task_payload = b"".join(task.to_bytes() for task in tasks)
    task_path = tmp_path / "tasks.jsonl"
    task_path.write_bytes(task_payload)
    ledger = LayerRef(
        "urn:docspec:test:planned-store-ledger",
        "planned-document-stores",
        "docspec-planned-store-reference/1.0",
        "urn:docspec:test:document-store-profile",
        str(task_path),
        sha256_digest(task_payload),
        count,
    )
    handoff = ExecutionHandoff(
        plan,
        _artifact("urn:docspec:test:execution-profile"),
        _artifact("urn:docspec:test:worker-composition"),
        ledger,
        EXECUTE_AND_DELIVER_OPERATION_ID,
        count,
        task_set_digest,
        _artifact("urn:docspec:test:result-sink"),
    )
    handoff_payload = handoff.to_bytes()
    handoff_path = tmp_path / "handoff.json"
    handoff_path.write_bytes(handoff_payload)
    result_root = tmp_path / "results"
    result_root.mkdir()
    profile_payload = PROFILE_PATH.read_bytes()
    profile = DagsterAdapterProfile.from_bytes(profile_payload)
    profile_path = tmp_path / "dagster-profile.json"
    profile_path.write_bytes(profile_payload)
    profile_ref = ArtifactRef(
        profile.profile_id,
        str(profile_path),
        adapter_profile_file_digest(profile_payload),
        "application/json",
        len(profile_payload),
    )
    handoff_ref = ArtifactRef(
        handoff.handoff_id,
        str(handoff_path),
        sha256_digest(handoff_payload),
        "application/json",
        len(handoff_payload),
    )
    task_ref = ArtifactRef(
        ledger.layer_id,
        str(task_path),
        sha256_digest(task_payload),
        "application/x-ndjson",
        len(task_payload),
    )
    membership_ref = build_task_membership_index(
        handoff=handoff_ref,
        task_ledger=task_ref,
        destination=tmp_path / "task-membership.sqlite3",
    )
    membership_verification_ref = seal_task_membership_verification(
        handoff=handoff_ref,
        task_ledger=task_ref,
        task_membership=membership_ref,
        destination=tmp_path / "task-membership-verification.json",
    )
    deployment = DagsterDeploymentConfig(
        profile_ref,
        handoff_ref,
        task_ref,
        membership_ref,
        membership_verification_ref,
        str(result_root),
        worker_command,
        2,
        2,
        0,
        30,
        (75,),
    )
    return deployment, handoff, tasks


def _worker_command(
    tmp_path: Path,
    *,
    counter: Path | None = None,
    exit_code: int | None = None,
) -> tuple[str, ...]:
    worker = tmp_path / "worker.py"
    worker.write_text(
        """
import argparse
import sys
from pathlib import Path

sys.path.insert(0, {source_root!r})

from docspec.adapters.dagster import parse_worker_request
from docspec.domain.execution import StoreTaskResult
from docspec.domain.references import StoreRef

parser = argparse.ArgumentParser()
parser.add_argument('--request', type=Path, required=True)
parser.add_argument('--result', type=Path, required=True)
args = parser.parse_args()
{counter_code}
{exit_code_line}
handoff, task = parse_worker_request(args.request.read_bytes())
output = StoreRef(
    task.input_store.store_id,
    task.input_store.revision + 1,
    task.input_store.locator,
    task.input_store.digest,
)
args.result.write_bytes(StoreTaskResult.succeeded(
    handoff_id=handoff.handoff_id,
    task=task,
    output_store=output,
).to_bytes())
""".format(
            source_root=str(ROOT / "src"),
            counter_code=""
            if counter is None
            else (
                f"counter = Path({str(counter)!r})\n"
                "counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else '1')"
            ),
            exit_code_line="" if exit_code is None else f"raise SystemExit({exit_code})",
        ),
        encoding="utf-8",
    )
    return sys.executable, str(worker)


def test_dagster_adapter_profile_is_closed_and_pins_passed_multinode_qualification() -> None:
    payload = PROFILE_PATH.read_bytes()
    profile = DagsterAdapterProfile.from_bytes(payload)

    assert profile.to_dict() == json.loads(payload)
    assert profile == registered_dagster_adapter_profile()
    assert profile.configuration_digest == profile.to_dict()["configurationDigest"]
    assert profile.implementation_module == "docspec.adapters.dagster:build_dagster_definitions"
    assert profile.qualification_status == "passed"
    assert profile.verifier_status == "implemented"
    assert profile.qualification_evidence is not None
    evidence_path = ROOT / profile.qualification_evidence.locator
    evidence_payload = evidence_path.read_bytes()
    assert len(evidence_payload) == profile.qualification_evidence.byte_size
    assert sha256_digest(evidence_payload) == profile.qualification_evidence.digest
    assert profile.profile_id.startswith("urn:docspec:scheduler-adapter-profile:v1:")

    mutable = profile.to_dict()
    restored = DagsterAdapterProfile.from_dict(mutable)
    mutable["configuration"]["retryClassification"]["transient"] = "changed"
    assert restored.configuration["retryClassification"]["transient"] != "changed"
    with pytest.raises(TypeError):
        restored.configuration["retryClassification"]["transient"] = "changed"

    tampered = profile.to_dict()
    tampered["configuration"]["executor"] = "homegrown"
    with pytest.raises(ProfileError, match="digest differs"):
        DagsterAdapterProfile.from_dict(tampered)

    unsupported = profile.to_dict()
    unsupported["adapterVersion"] = "2.0.0"
    unsupported_profile = DagsterAdapterProfile(
        unsupported["adapterId"],
        unsupported["adapterVersion"],
        unsupported["packageRequirement"],
        unsupported["implementationModule"],
        unsupported["configuration"],
        tuple(unsupported["capabilities"]),
        unsupported["verifier"]["status"],
        unsupported["verifier"]["testId"],
        unsupported["verifier"]["qualificationStatus"],
        profile.qualification_evidence,
    )
    unsupported["profileId"] = unsupported_profile.profile_id
    with pytest.raises(ProfileError, match="not the logical profile"):
        adapter_profile_file_digest(canonical_json_file_bytes(unsupported))

    passed_without_evidence = profile.to_dict()
    passed_without_evidence["verifier"].update(
        {"status": "implemented", "qualificationEvidence": None, "qualificationStatus": "passed"}
    )
    with pytest.raises(ProfileError, match="requires implemented evidence"):
        DagsterAdapterProfile.from_dict(passed_without_evidence)


def test_dagster_deployment_and_task_ledger_are_sealed_and_streamable(tmp_path: Path) -> None:
    deployment, _handoff, tasks = _deployment(tmp_path)

    assert DagsterDeploymentConfig.from_bytes(deployment.to_bytes()) == deployment
    assert load_dagster_adapter_profile(deployment) == registered_dagster_adapter_profile()
    assert tuple(iter_verified_store_tasks(deployment)) == tasks
    assert deployment.deployment_id.startswith("urn:docspec:dagster-deployment:v1:")

    Path(deployment.task_ledger.locator).write_bytes(tasks[0].to_bytes())
    with pytest.raises(IntegrityError, match="differs from its reference"):
        tuple(iter_verified_store_tasks(deployment))


def test_dagster_deployment_rejects_changed_adapter_profile_bytes(tmp_path: Path) -> None:
    deployment, _handoff, _tasks = _deployment(tmp_path)
    profile_path = Path(deployment.adapter_profile.locator)
    payload = bytearray(profile_path.read_bytes())
    payload[payload.index(b"dynamicTaskMapping")] = ord("D")
    profile_path.write_bytes(payload)

    with pytest.raises(IntegrityError, match="digest differs"):
        load_dagster_adapter_profile(deployment)


def test_runtime_profile_requires_the_registered_evidence_pin_without_loading_its_locator(tmp_path: Path) -> None:
    deployment, _handoff, _tasks = _deployment(tmp_path)
    evidence_payload = canonical_json_file_bytes({"campaign": "external-dagster", "verdict": "pass"})
    evidence_path = tmp_path / "qualification-evidence.json"
    evidence_path.write_bytes(evidence_payload)
    evidence = ArtifactRef(
        "urn:docspec:test:dagster-qualification",
        str(evidence_path),
        sha256_digest(evidence_payload),
        "application/json",
        len(evidence_payload),
    )
    registered = registered_dagster_adapter_profile()
    passed = DagsterAdapterProfile(
        registered.adapter_id,
        registered.adapter_version,
        registered.package_requirement,
        registered.implementation_module,
        registered.configuration,
        registered.capabilities,
        "implemented",
        registered.verifier_test_id,
        "passed",
        evidence,
    )
    profile_path = tmp_path / "passed-profile.json"
    profile_path.write_bytes(passed.to_bytes())
    passed_deployment = replace(
        deployment,
        adapter_profile=ArtifactRef(
            passed.profile_id,
            str(profile_path),
            sha256_digest(passed.to_bytes()),
            "application/json",
            len(passed.to_bytes()),
        ),
    )

    with pytest.raises(ProfileError, match="qualification evidence pin differs"):
        load_dagster_adapter_profile(passed_deployment)

    registered_evidence = registered.qualification_evidence
    assert registered_evidence is not None
    relocated_evidence = ArtifactRef(
        registered_evidence.artifact_id,
        str(tmp_path / "history-is-not-a-runtime-input.json"),
        registered_evidence.digest,
        registered_evidence.media_type,
        registered_evidence.byte_size,
    )
    relocated = replace(passed, qualification_evidence=relocated_evidence)
    relocated_path = tmp_path / "relocated-profile.json"
    relocated_path.write_bytes(relocated.to_bytes())
    relocated_deployment = replace(
        deployment,
        adapter_profile=ArtifactRef(
            relocated.profile_id,
            str(relocated_path),
            sha256_digest(relocated.to_bytes()),
            "application/json",
            len(relocated.to_bytes()),
        ),
    )
    assert load_dagster_adapter_profile(relocated_deployment) == relocated


def test_runtime_profile_loading_is_independent_of_the_history_file_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, _handoff, _tasks = _deployment(tmp_path)
    runtime_directory = tmp_path / "runtime"
    runtime_directory.mkdir()
    monkeypatch.chdir(runtime_directory)

    assert load_dagster_adapter_profile(deployment) == registered_dagster_adapter_profile()


def test_dagster_worker_crosses_a_process_boundary_and_persists_idempotent_results(tmp_path: Path) -> None:
    deployment, handoff, tasks = _deployment(tmp_path, worker_command=_worker_command(tmp_path))

    result = invoke_worker_process(deployment, handoff, tasks[0])
    destination = persist_task_result(deployment, result)

    assert destination.is_file()
    assert persist_task_result(deployment, result) == destination
    assert tuple(iter_persisted_task_results(deployment)) == (result,)
    restored_handoff, restored_task = parse_worker_request(
        canonical_json_file_bytes(
            {
                "format": "docspec-scheduler-worker-request",
                "formatVersion": "1.0",
                "handoff": handoff.to_dict(),
                "task": tasks[0].to_dict(),
            }
        )
    )
    assert (restored_handoff, restored_task) == (handoff, tasks[0])

    conflicting = StoreTaskResult.succeeded(
        handoff_id=handoff.handoff_id,
        task=tasks[0],
        output_store=StoreRef(
            tasks[0].input_store.store_id,
            2,
            tasks[0].input_store.locator,
            tasks[0].input_store.digest,
        ),
    )
    with pytest.raises(IntegrityError, match="conflicts"):
        persist_task_result(deployment, conflicting)


def test_saved_results_require_task_membership_filename_and_handoff(tmp_path: Path) -> None:
    deployment, handoff, tasks = _deployment(tmp_path, worker_command=_worker_command(tmp_path))
    result = invoke_worker_process(deployment, handoff, tasks[0])
    destination = persist_task_result(deployment, result)

    unknown_task = StoreTask(
        handoff.processing_plan.artifact_id,
        handoff.operation_id,
        StoreRef("unknown-store", 0, "memory://unknown", sha256_digest(b"unknown")),
    )
    unknown_result = StoreTaskResult.succeeded(
        handoff_id=handoff.handoff_id,
        task=unknown_task,
        output_store=StoreRef("unknown-store", 1, "memory://unknown", sha256_digest(b"unknown")),
    )
    with pytest.raises(IntegrityError, match="not a member"):
        persist_task_result(deployment, unknown_result)

    wrong_handoff = StoreTaskResult.succeeded(
        handoff_id="urn:docspec:test:other-handoff",
        task=tasks[1],
        output_store=StoreRef(
            tasks[1].input_store.store_id,
            1,
            tasks[1].input_store.locator,
            tasks[1].input_store.digest,
        ),
    )
    with pytest.raises(IntegrityError, match="outside its execution handoff"):
        persist_task_result(deployment, wrong_handoff)

    wrong_name = destination.with_name("0" * 64 + ".json")
    destination.rename(wrong_name)
    with pytest.raises(IntegrityError, match="filename differs"):
        tuple(iter_persisted_task_results(deployment))


def test_result_reads_and_replay_comparisons_enforce_the_serialized_bound(tmp_path: Path) -> None:
    deployment, _handoff, tasks = _deployment(tmp_path)
    result_path = Path(deployment.result_root) / f"{tasks[0].task_id.rsplit(':', 1)[-1]}.json"
    result_path.write_bytes(b"x" * (MAX_RESULT_BYTES + 1))

    with pytest.raises(LimitExceededError, match="exceeds"):
        tuple(iter_persisted_task_results(deployment))


def test_identical_verified_result_short_circuits_worker_reexecution(tmp_path: Path) -> None:
    counter = tmp_path / "worker-count.txt"
    deployment, handoff, tasks = _deployment(
        tmp_path,
        worker_command=_worker_command(tmp_path, counter=counter),
    )

    first_result, first_path, first_reused = execute_or_reuse_task(deployment, handoff, tasks[0])
    unavailable_worker = replace(deployment, worker_command=("/definitely/not/a/worker",))
    second_result, second_path, second_reused = execute_or_reuse_task(
        unavailable_worker,
        handoff,
        tasks[0],
    )

    assert not first_reused
    assert second_reused
    assert (second_result, second_path) == (first_result, first_path)
    assert counter.read_text(encoding="utf-8") == "1"


def test_only_sealed_transient_worker_failures_are_retryable(tmp_path: Path) -> None:
    command = _worker_command(tmp_path, exit_code=75)
    deployment, handoff, tasks = _deployment(tmp_path, worker_command=command)

    with pytest.raises(DagsterTransientWorkerError, match="sealed transient status"):
        execute_or_reuse_task(deployment, handoff, tasks[0])

    permanent = replace(deployment, retryable_exit_codes=())
    with pytest.raises(DagsterAdapterError, match="exited with status 75") as raised:
        execute_or_reuse_task(permanent, handoff, tasks[0])
    assert not isinstance(raised.value, DagsterTransientWorkerError)


def test_task_membership_artifact_is_exact_and_tamper_evident(tmp_path: Path) -> None:
    deployment, handoff, tasks = _deployment(tmp_path, worker_command=_worker_command(tmp_path))
    result = invoke_worker_process(deployment, handoff, tasks[0])
    persist_task_result(deployment, result)
    membership_path = Path(deployment.task_membership.locator)
    with membership_path.open("ab") as handle:
        handle.write(b"changed")

    with pytest.raises(IntegrityError, match="verified file snapshot"):
        tuple(iter_persisted_task_results(deployment))


def test_task_membership_open_refuses_a_symlink_at_the_verified_path(tmp_path: Path) -> None:
    deployment, handoff, tasks = _deployment(tmp_path, worker_command=_worker_command(tmp_path))
    membership_path = Path(deployment.task_membership.locator)
    moved = tmp_path / "moved-task-membership.sqlite3"
    membership_path.replace(moved)
    membership_path.symlink_to(moved)

    with pytest.raises(IntegrityError, match="regular, non-symlink file"):
        execute_or_reuse_task(deployment, handoff, tasks[0])


def test_task_membership_reader_refuses_path_replacement_during_no_follow_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, handoff, tasks = _deployment(tmp_path, worker_command=_worker_command(tmp_path))
    membership_path = Path(deployment.task_membership.locator)
    held_path = tmp_path / "held-task-membership.sqlite3"
    forged_path = tmp_path / "forged-task-membership.sqlite3"
    shutil.copyfile(membership_path, forged_path)
    with sqlite3.connect(forged_path) as forged:
        forged.execute("DELETE FROM tasks")

    real_connect = sqlite3.connect
    replaced = False

    def connect_after_replacing_path(database, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal replaced
        if not replaced and isinstance(database, str) and "immutable=1" in database:
            replaced = True
            membership_path.replace(held_path)
            forged_path.replace(membership_path)
            try:
                return real_connect(database, *args, **kwargs)
            finally:
                membership_path.replace(forged_path)
                held_path.replace(membership_path)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect_after_replacing_path)

    with pytest.raises(IntegrityError, match="changed while it was opened"):
        execute_or_reuse_task(deployment, handoff, tasks[0])
    assert replaced


def test_pinned_membership_index_cannot_substitute_an_unknown_task_for_a_ledger_member(tmp_path: Path) -> None:
    deployment, handoff, tasks = _deployment(tmp_path)
    membership_path = Path(deployment.task_membership.locator)
    unknown = StoreTask(
        handoff.processing_plan.artifact_id,
        handoff.operation_id,
        StoreRef("unknown-store", 0, "memory://unknown", sha256_digest(b"unknown")),
    )
    connection = sqlite3.connect(membership_path)
    connection.execute("DELETE FROM tasks WHERE task_id = ?", (tasks[0].task_id,))
    connection.execute(
        "INSERT INTO tasks (task_id, task_digest) VALUES (?, ?)",
        (unknown.task_id, sha256_digest(unknown.to_bytes())),
    )
    connection.commit()
    connection.close()
    changed = membership_path.read_bytes()
    forged_membership = ArtifactRef(
        deployment.task_membership.artifact_id,
        deployment.task_membership.locator,
        sha256_digest(changed),
        deployment.task_membership.media_type,
        len(changed),
    )

    with pytest.raises(IntegrityError, match="not a member"):
        seal_task_membership_verification(
            handoff=deployment.handoff,
            task_ledger=deployment.task_ledger,
            task_membership=forged_membership,
            destination=tmp_path / "forged-membership-verification.json",
        )


def test_dynamic_tasks_do_only_indexed_membership_work_after_one_coordinator_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, handoff, tasks = _deployment(tmp_path, worker_command=_worker_command(tmp_path))
    original = dagster_adapter._iter_verified_store_task_references
    ledger_passes = 0

    def counted_ledger_pass(*args, **kwargs):
        nonlocal ledger_passes
        ledger_passes += 1
        yield from original(*args, **kwargs)

    monkeypatch.setattr(
        dagster_adapter,
        "_iter_verified_store_task_references",
        counted_ledger_pass,
    )

    assert tuple(iter_verified_store_tasks(deployment, handoff=handoff)) == tasks
    for task in tasks:
        execute_or_reuse_task(deployment, handoff, task)
    assert len(tuple(iter_persisted_task_results(deployment))) == len(tasks)
    assert ledger_passes == 1


def test_optional_dagster_package_executes_the_native_dynamic_job_locally(tmp_path: Path) -> None:
    dagster = pytest.importorskip("dagster", reason="install the 'dagster' extra to verify native Dagster composition")
    deployment, _handoff, tasks = _deployment(tmp_path, worker_command=_worker_command(tmp_path))
    deployment_path = tmp_path / "deployment.json"
    deployment_path.write_bytes(deployment.to_bytes())

    definitions = build_dagster_definitions()
    job = definitions.get_job_def("docspec_store_tasks")
    assert dagster.validate_run_config(job, dagster_run_config(deployment_path, deployment))["execution"]
    result = job.execute_in_process(
        run_config={
            "resources": {"docspec_deployment": {"config": {"path": str(deployment_path)}}},
        }
    )

    assert job.name == "docspec_store_tasks"
    assert {node.name for node in job.nodes} == {"emit_store_tasks", "execute_store_task"}
    assert result.success
    assert {item.task.task_id for item in iter_persisted_task_results(deployment)} == {task.task_id for task in tasks}


def test_dagster_executor_and_run_configuration_are_injected_at_the_deployment_edge(tmp_path: Path) -> None:
    dagster = pytest.importorskip("dagster", reason="install the 'dagster' extra to verify executor injection")
    deployment, _handoff, _tasks = _deployment(tmp_path, worker_command=_worker_command(tmp_path))
    deployment_path = tmp_path / "deployment.json"
    deployment_path.write_bytes(deployment.to_bytes())

    job = build_dagster_definitions(executor_def=dagster.in_process_executor).get_job_def("docspec_store_tasks")
    execution_config = {"config": {}}
    run_config = dagster_run_config(deployment_path, deployment, execution=execution_config)
    execution_config["config"]["changedAfterComposition"] = True

    assert job.executor_def.name == "in_process"
    assert dagster.validate_run_config(job, run_config)["execution"]
    assert "changedAfterComposition" not in run_config["execution"]["config"]
    assert run_config["resources"]["docspec_deployment"]["config"]["path"] == str(deployment_path)
