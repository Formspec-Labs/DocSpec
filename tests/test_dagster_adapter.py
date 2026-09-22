"""Dagster drives Core operations without re-implementing meaning: retries, failures and workers land on Core.

Covers native job execution and identity recording, retry-only-failed semantics after failures or lost
scheduler responses, producer-free reexecution, bounded wire validation, and that importing the adapter
never imports dagster.
"""

import importlib
import json
import os
import subprocess
import sys

import msgspec
import pytest

from docspec.adapters.dagster import (
    DAGSTER_JOB_NAME, DagsterAdapterError, DagsterRuntime,
    _task_payloads, build_dagster_definitions, decode_operation, encode_operation,
)
from docspec.domain.core_admission import admit_record
from docspec.domain.identity import sha256_digest
from docspec.errors import IntegrityError, LimitExceededError
from docspec.runtime.core import CoreWorkspace
from tests.support.core_scheduling import operations, seed, resolver, result_values


dagster = pytest.importorskip("dagster", reason="install the 'dagster' extra to test the optional adapter")



def job(runtime, **kwargs):
    """Build the definitions with ``runtime`` as the ``docspec_runtime`` resource and return the named job."""
    return build_dagster_definitions({"docspec_runtime": dagster.ResourceDefinition.hardcoded_resource(runtime)}, **kwargs).get_job_def(DAGSTER_JOB_NAME)


def selections(result):
    """Admit the ``execute_operation`` node outputs back into selected-value records."""
    return tuple(admit_record(payload) for payload in result.output_for_node("execute_operation").values())



def test_native_job_executes_core_operations_and_records_original_identities(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        seed(workspace)
        runtime = DagsterRuntime(workspace.operations, lambda: operations(), resolver)
        result = job(runtime).execute_in_process()
        assert result.success and result_values(workspace, selections(result)) == [2, 4]
        assert {item.selection_id for item in selections(result)} == {"job:selection:0", "job:selection:1"}
        outputs = [event.step_output_data for event in result.all_events if event.event_type is dagster.DagsterEventType.STEP_OUTPUT and event.step_key.startswith("execute_operation[")]
        assert len(outputs) == 2
        assert all({"request_id", "selection_id", "result_id", "execution_id", "status"} <= output.metadata.keys() for output in outputs)
    with CoreWorkspace(tmp_path) as workspace:
        assert result_values(workspace, selections(result)) == [2, 4]


def test_native_retry_preserves_failure_and_starts_only_failed_work_again(tmp_path):
    calls = {}
    def flaky(definition):
        def produce(context):
            key = definition.configuration["input"]
            calls[key] = calls.get(key, 0) + 1
            if key == "input:0" and calls[key] == 1:
                raise OSError("temporary producer failure")
            return resolver(definition)(context)
        return produce
    with CoreWorkspace(tmp_path) as workspace:
        seed(workspace)
        result = job(DagsterRuntime(workspace.operations, lambda: operations(), flaky), retry_policy=dagster.RetryPolicy(max_retries=1, delay=0)).execute_in_process()
        assert result.success and calls == {"input:0": 2, "input:1": 1}
        assert sum(event.event_type is dagster.DagsterEventType.STEP_RESTARTED for event in result.all_events) == 1
        with workspace.ledger._transaction() as connection:
            assert dict(connection.execute("SELECT outcome,count(*) FROM records WHERE kind='result' GROUP BY outcome")) == {"failed": 1, "success": 2}


def test_retry_after_lost_scheduler_response_keeps_the_original_core_result(tmp_path, monkeypatch):
    calls, interrupted = [], []
    def observed(definition):
        def produce(context):
            calls.append(context.execution.execution_id)
            return resolver(definition)(context)
        return produce
    with CoreWorkspace(tmp_path) as workspace:
        seed(workspace)
        resolve = workspace.operations.resolve
        def lost(*args, **kwargs):
            selected = resolve(*args, **kwargs)
            if kwargs["selection_id"] == "job:selection:0" and not interrupted:
                interrupted.append(selected)
                raise OSError("scheduler output was lost after Core commit")
            return selected
        monkeypatch.setattr(workspace.operations, "resolve", lost)
        result = job(DagsterRuntime(workspace.operations, lambda: operations(), observed), retry_policy=dagster.RetryPolicy(max_retries=1, delay=0)).execute_in_process()
        assert result.success and len(calls) == 2
        assert interrupted[0].selection in selections(result)
        with workspace.ledger._transaction() as connection:
            assert connection.execute("SELECT count(*) FROM records WHERE kind='execution'").fetchone() == (2,)


def test_reexecution_of_same_selections_needs_no_producer_but_new_fresh_selection_executes(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        seed(workspace)
        first = job(DagsterRuntime(workspace.operations, lambda: operations(fresh=True), resolver)).execute_in_process()
        def absent(_):
            pytest.fail("producer must not be resolved when recovering the exact selected result")
        second = job(DagsterRuntime(workspace.operations, lambda: operations(fresh=True), absent)).execute_in_process()
        assert {item.selected_result_id for item in selections(second)} == {item.selected_result_id for item in selections(first)}
        third = job(DagsterRuntime(workspace.operations, lambda: operations(prefix="another", fresh=True), resolver)).execute_in_process()
        assert {item.selected_result_id for item in selections(third)}.isdisjoint(item.selected_result_id for item in selections(first))


def test_failed_native_job_retains_completed_sibling_and_authoritative_failure(tmp_path):
    def failing(definition):
        if definition.configuration["input"] == "input:1":
            def fail(context):
                raise ValueError("permanent failure")
            return fail
        return resolver(definition)
    with CoreWorkspace(tmp_path) as workspace:
        seed(workspace)
        result = job(DagsterRuntime(workspace.operations, lambda: operations(), failing)).execute_in_process(raise_on_error=False)
        assert not result.success
        sibling, failed = next(workspace.ledger.read_records([("selection", "job:selection:0"), ("selection", "job:selection:1")]))
        assert sibling is not None and sibling.retained and failed is None
        with workspace.ledger._transaction() as connection:
            assert dict(connection.execute("SELECT outcome,count(*) FROM records WHERE kind='result' GROUP BY outcome")) == {"failed": 1, "success": 1}


def test_native_generator_resources_close_and_source_failures_close_streams(tmp_path):
    closed = []
    @dagster.resource
    def resource():
        with CoreWorkspace(tmp_path) as workspace:
            seed(workspace)
            try:
                yield DagsterRuntime(workspace.operations, lambda: operations(), resolver)
            finally:
                closed.append("resource")
    result = build_dagster_definitions({"docspec_runtime": resource}).get_job_def(DAGSTER_JOB_NAME).execute_in_process()
    assert result.success and closed == ["resource"]
    def source():
        try:
            yield next(operations())
            raise ValueError("bad source")
        finally:
            closed.append("source")
    with CoreWorkspace(tmp_path) as workspace:
        with pytest.raises(ValueError, match="bad source"):
            list(_task_payloads(DagsterRuntime(workspace.operations, source, resolver)))
    assert closed == ["resource", "source"]


def test_operation_wire_validation_is_bounded_and_preserves_exact_definitions():
    operation = next(operations())
    payload = encode_operation(operation)
    assert decode_operation(payload) == operation and len(payload) < 4096
    assert next(_task_payloads(DagsterRuntime(None, lambda: [operation], resolver)))[0] == sha256_digest(operation.selection_id.encode()).split(":")[1]
    with pytest.raises(IntegrityError):
        decode_operation(payload.replace(b'"version":1', b'"version":1,"version":1'))
    with pytest.raises(IntegrityError):
        encode_operation(msgspec.structs.replace(operation, request=msgspec.structs.replace(operation.request, definition_id="different")))
    with pytest.raises(LimitExceededError):
        decode_operation(b" " * (8 * 1024**2 + 1))


def test_native_executor_and_retry_settings_remain_owned_by_dagster(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        selected = job(DagsterRuntime(workspace.operations, lambda: (), resolver), executor_def=dagster.in_process_executor,
                       retry_policy=dagster.RetryPolicy(max_retries=3))
        assert selected.executor_def is dagster.in_process_executor
        assert selected.graph.node_named("execute_operation").definition.retry_policy == dagster.RetryPolicy(max_retries=3)
    with pytest.raises(DagsterAdapterError, match="docspec_runtime"):
        build_dagster_definitions({})


def test_adapter_import_keeps_dagster_optional():
    result = subprocess.run([sys.executable, "-c", "import sys; import docspec.adapters.dagster; assert 'dagster' not in sys.modules"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_reconstructed_native_workers_use_core_and_record_process_identity(tmp_path):
    root = tmp_path / "workspace"
    with CoreWorkspace(root) as workspace:
        seed(workspace)
    task_file = tmp_path / "operations.jsonl"
    task_file.write_bytes(b"".join(encode_operation(operation) + b"\n" for operation in operations()))
    fixture = importlib.import_module("tests.dagster_process_fixture")
    configuration = {"resources": {"docspec_runtime": {"config": {"workspace": str(root), "operations_path": str(task_file),
        "evidence_root": str(tmp_path / "evidence")}}}, "execution": {"config": {"max_concurrent": 2}}}
    instance_root = tmp_path / "dagster"
    instance_root.mkdir()
    with dagster.DagsterInstance.local_temp(str(instance_root)) as instance:
        with dagster.execute_job(dagster.reconstructable(fixture.reconstructable_job), instance=instance, run_config=configuration) as result:
            assert result.success
            selected = selections(result)
            events = instance.all_logs(result.run_id)
    evidence = [json.loads(path.read_bytes()) for path in (tmp_path / "evidence").glob("*.json")]
    assert len(evidence) == 2 and all(item["pid"] != os.getpid() for item in evidence)
    assert {item["request_id"] for item in evidence} == {"job:request:0", "job:request:1"}
    assert any(event.dagster_event and event.dagster_event.event_type is dagster.DagsterEventType.STEP_WORKER_STARTED for event in events)
    with CoreWorkspace(root) as workspace:
        assert result_values(workspace, selected) == [2, 4]
