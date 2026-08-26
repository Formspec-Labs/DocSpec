from __future__ import annotations

import importlib
import json
import os
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

import pytest

from docspec.adapters.dagster import DagsterRuntime, build_dagster_definitions
from docspec.domain.execution import (
    EXECUTE_AND_DELIVER_OPERATION_ID,
    ExecutionHandoff,
    StoreTask,
    StoreTaskResult,
    summarize_store_tasks,
)
from docspec.domain.identity import canonical_json_file_bytes, sha256_digest
from docspec.domain.references import ArtifactRef, LayerRef, StoreRef

dagster = pytest.importorskip("dagster", reason="install the 'dagster' extra to test the optional adapter")
dagster_process_fixture = importlib.import_module("tests.dagster_process_fixture")


def _artifact(name: str) -> ArtifactRef:
    payload = canonical_json_file_bytes({"name": name})
    return ArtifactRef(
        f"urn:docspec:test:{name}",
        f"memory://{name}",
        sha256_digest(payload),
        "application/json",
        len(payload),
    )


def _tasks(count: int = 3) -> tuple[StoreTask, ...]:
    return tuple(
        StoreTask(
            "urn:docspec:test:plan",
            EXECUTE_AND_DELIVER_OPERATION_ID,
            StoreRef(
                f"store-{index}",
                0,
                f"memory://store-{index}",
                sha256_digest(str(index).encode()),
            ),
        )
        for index in range(count)
    )


def _handoff(tasks: tuple[StoreTask, ...]) -> ExecutionHandoff:
    count, digest = summarize_store_tasks(tasks)
    return ExecutionHandoff(
        ArtifactRef(
            "urn:docspec:test:plan",
            "memory://plan",
            sha256_digest(b"plan"),
            "application/json",
            4,
        ),
        _artifact("execution-profile"),
        _artifact("worker-composition"),
        LayerRef(
            "urn:docspec:test:planned-store-ledger",
            "planned-document-stores",
            "docspec-planned-store-reference/1.0",
            "urn:docspec:test:document-store-profile",
            "memory://planned-store-ledger",
            sha256_digest(b"planned-store-ledger"),
            count,
        ),
        EXECUTE_AND_DELIVER_OPERATION_ID,
        count,
        digest,
        _artifact("result-sink"),
    )


def _success(handoff: ExecutionHandoff, task: StoreTask) -> StoreTaskResult:
    return StoreTaskResult.succeeded(
        handoff_id=handoff.handoff_id,
        task=task,
        output_store=StoreRef(
            task.input_store.store_id,
            task.input_store.revision + 1,
            task.input_store.locator,
            task.input_store.digest,
        ),
    )


def _resource(runtime: DagsterRuntime):  # type: ignore[no-untyped-def]
    return dagster.ResourceDefinition.hardcoded_resource(runtime)


def _result_payloads(result) -> tuple[bytes, ...]:  # type: ignore[no-untyped-def]
    outputs = result.output_for_node("execute_store_task")
    assert isinstance(outputs, dict)
    return tuple(outputs[key] for key in sorted(outputs))


def test_reconstructable_job_crosses_dagsters_multiprocess_worker_boundary(tmp_path: Path) -> None:
    tasks = _tasks(2)
    handoff = _handoff(tasks)
    handoff_path = tmp_path / "handoff.json"
    task_ledger_path = tmp_path / "tasks.jsonl"
    worker_evidence_root = tmp_path / "worker-evidence"
    instance_root = tmp_path / "dagster-instance"
    worker_evidence_root.mkdir()
    instance_root.mkdir()
    handoff_path.write_bytes(handoff.to_bytes())
    task_ledger_path.write_bytes(b"".join(task.to_bytes() for task in tasks))

    job = dagster.reconstructable(dagster_process_fixture.reconstructable_job)
    run_config = {
        "execution": {"config": {"max_concurrent": 2}},
        "resources": {
            "docspec_runtime": {
                "config": {
                    "handoff_path": str(handoff_path),
                    "task_ledger_path": str(task_ledger_path),
                    "worker_evidence_root": str(worker_evidence_root),
                }
            }
        },
    }
    with dagster.DagsterInstance.local_temp(tempdir=str(instance_root)) as instance:
        with dagster.execute_job(job, instance=instance, run_config=run_config) as result:
            restored = tuple(StoreTaskResult.from_bytes(payload) for payload in _result_payloads(result))
            run_id = result.run_id
            assert result.success
        stored_events = instance.all_logs(run_id)

    evidence = tuple(json.loads(path.read_text()) for path in sorted(worker_evidence_root.glob("*.json")))
    assert {item["taskId"] for item in evidence} == {task.task_id for task in tasks}
    assert all(item["pid"] != os.getpid() for item in evidence)
    assert {item.task.task_id for item in restored} == {task.task_id for task in tasks}
    assert any(
        event.dagster_event and event.dagster_event.event_type is dagster.DagsterEventType.STEP_WORKER_STARTED
        for event in stored_events
    )


def test_real_dagster_job_executes_reference_only_tasks_and_records_events() -> None:
    tasks = _tasks()
    handoff = _handoff(tasks)
    handled: list[str] = []

    def task_source(current_handoff: ExecutionHandoff) -> Iterable[StoreTask]:
        assert ExecutionHandoff.from_bytes(current_handoff.to_bytes()) == handoff
        return iter(tasks)

    def handler(current_handoff: ExecutionHandoff, task: StoreTask) -> StoreTaskResult:
        handled.append(task.task_id)
        return _success(current_handoff, StoreTask.from_bytes(task.to_bytes()))

    runtime = DagsterRuntime(handoff, task_source, handler)
    job = build_dagster_definitions(_resource(runtime)).get_job_def("docspec_store_tasks")

    assert job.executor_def.name == "multiprocess"
    with dagster.DagsterInstance.ephemeral() as instance:
        result = job.execute_in_process(instance=instance)
        stored_events = instance.all_logs(result.run_id)

    restored = tuple(StoreTaskResult.from_bytes(payload) for payload in _result_payloads(result))
    assert result.success
    assert result.get_run_success_event() is not None
    assert stored_events
    assert set(handled) == {task.task_id for task in tasks}
    assert {item.task.task_id for item in restored} == {task.task_id for task in tasks}
    assert all(item.output_store is not None and item.output_store.revision == 1 for item in restored)


def test_dagster_retry_policy_recovers_one_mapped_task_without_rewriting_siblings() -> None:
    tasks = _tasks(2)
    handoff = _handoff(tasks)
    attempts: defaultdict[str, int] = defaultdict(int)

    def handler(current_handoff: ExecutionHandoff, task: StoreTask) -> StoreTaskResult:
        attempts[task.task_id] += 1
        if task == tasks[0] and attempts[task.task_id] == 1:
            raise RuntimeError("transient fixture failure")
        return _success(current_handoff, task)

    runtime = DagsterRuntime(handoff, lambda _handoff: iter(tasks), handler)
    job = build_dagster_definitions(
        _resource(runtime),
        retry_policy=dagster.RetryPolicy(max_retries=1, delay=0),
    ).get_job_def("docspec_store_tasks")

    result = job.execute_in_process()

    failed_key = tasks[0].task_id.rsplit(":", 1)[-1]
    sibling_key = tasks[1].task_id.rsplit(":", 1)[-1]
    retry_steps = {
        event.step_key
        for event in result.all_events
        if event.event_type is dagster.DagsterEventType.STEP_RESTARTED
    }
    assert result.success
    assert retry_steps == {f"execute_store_task[{failed_key}]"}
    assert f"execute_store_task[{sibling_key}]" not in retry_steps
    assert attempts == {tasks[0].task_id: 2, tasks[1].task_id: 1}


def test_rerunning_the_job_reuses_idempotent_handler_results() -> None:
    tasks = _tasks(2)
    handoff = _handoff(tasks)
    saved: dict[str, StoreTaskResult] = {}
    writes = 0

    def handler(current_handoff: ExecutionHandoff, task: StoreTask) -> StoreTaskResult:
        nonlocal writes
        result = saved.get(task.idempotency_key)
        if result is None:
            writes += 1
            result = _success(current_handoff, task)
            saved[task.idempotency_key] = result
        return result

    runtime = DagsterRuntime(handoff, lambda _handoff: iter(tasks), handler)
    job = build_dagster_definitions(_resource(runtime)).get_job_def("docspec_store_tasks")

    first = job.execute_in_process()
    second = job.execute_in_process()

    assert first.success and second.success
    assert writes == len(tasks)
    assert _result_payloads(first) == _result_payloads(second)


@pytest.mark.parametrize(
    "task_source",
    (
        lambda tasks: iter(tasks[:-1]),
        lambda tasks: iter(reversed(tasks)),
        lambda tasks: iter((*tasks, tasks[-1])),
        lambda tasks: iter((tasks[0], object())),
    ),
    ids=("incomplete", "wrong-order", "extra", "malformed"),
)
def test_malformed_or_incomplete_task_stream_fails_the_run(task_source) -> None:  # type: ignore[no-untyped-def]
    tasks = _tasks(2)
    handoff = _handoff(tasks)
    handled: list[StoreTask] = []

    def handler(current_handoff: ExecutionHandoff, task: StoreTask) -> StoreTaskResult:
        handled.append(task)
        return _success(current_handoff, task)

    runtime = DagsterRuntime(handoff, lambda _handoff: task_source(tasks), handler)
    job = build_dagster_definitions(_resource(runtime)).get_job_def("docspec_store_tasks")

    result = job.execute_in_process(raise_on_error=False)

    assert not result.success
    assert result.get_run_failure_event() is not None
    assert handled == []


def test_missing_mapped_result_cannot_produce_a_successful_run() -> None:
    tasks = _tasks(2)
    handoff = _handoff(tasks)

    def incomplete_handler(current_handoff: ExecutionHandoff, task: StoreTask) -> StoreTaskResult:
        if task == tasks[0]:
            return _success(current_handoff, task)
        return None  # type: ignore[return-value]

    runtime = DagsterRuntime(handoff, lambda _handoff: iter(tasks), incomplete_handler)
    job = build_dagster_definitions(_resource(runtime)).get_job_def("docspec_store_tasks")

    result = job.execute_in_process(raise_on_error=False)

    assert not result.success
    assert result.get_run_failure_event() is not None
    assert len(result.get_step_success_events()) == 2  # emitter plus the one complete mapped task


def test_executor_and_retry_policy_are_injected_at_the_composition_root() -> None:
    tasks = _tasks(1)
    handoff = _handoff(tasks)
    runtime = DagsterRuntime(handoff, lambda _handoff: iter(tasks), _success)
    job = build_dagster_definitions(
        _resource(runtime),
        executor_def=dagster.in_process_executor,
        retry_policy=dagster.RetryPolicy(max_retries=3),
    ).get_job_def("docspec_store_tasks")

    assert job.executor_def.name == "in_process"
    execute_op = job.graph.node_named("execute_store_task")
    assert execute_op.definition.retry_policy == dagster.RetryPolicy(max_retries=3)


def test_importing_the_adapter_does_not_eagerly_load_dagster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "docspec.adapters.dagster", raising=False)
    monkeypatch.delitem(sys.modules, "dagster", raising=False)

    imported = __import__("docspec.adapters.dagster", fromlist=["DagsterRuntime"])

    assert imported.DagsterRuntime.__name__ == "DagsterRuntime"
    assert "dagster" not in sys.modules
