"""Schedulers preserve Core values and actual attempt identities across processes."""

import importlib
import json
import os
import subprocess
import sys

import pytest

from docspec.adapters.dagster import encode_operation
from docspec.domain.core_admission import admit_record
from docspec.runtime.core import CoreWorkspace
from tests.support.core_scheduling import operations, resolver, result_values, seed


def test_serialized_tasks_cross_a_real_process_boundary_and_match_the_local_backend(tmp_path):
    with CoreWorkspace(tmp_path / "local") as workspace:
        seed(workspace)
        choices = [workspace.operations.resolve(call.definition, call.request, resolver(call.definition),
            selection_id=call.selection_id, target=call.target, reuse_policy=lambda _: True).selection for call in operations()]
        expected = result_values(workspace, choices)
    with CoreWorkspace(tmp_path / "external") as workspace:
        seed(workspace)
    script = '''
import sys
from docspec.adapters.dagster import DagsterRuntime, decode_operation, _execute_operation
from docspec.domain.core_admission import encode_record
from docspec.runtime.core import CoreWorkspace
from tests.support.core_scheduling import resolver
with CoreWorkspace(sys.argv[1]) as workspace:
    operation = decode_operation(sys.stdin.buffer.read())
    result = _execute_operation(DagsterRuntime(workspace.operations, lambda: (), resolver), operation)
    sys.stdout.buffer.write(encode_record(result.selection))
'''
    choices = []
    for call in reversed(tuple(operations())):
        completed = subprocess.run([sys.executable, "-c", script, str(tmp_path / "external")], input=encode_operation(call), capture_output=True)
        assert completed.returncode == 0, completed.stderr.decode()
        choices.append(admit_record(completed.stdout))
    repeated = subprocess.run([sys.executable, "-c", script, str(tmp_path / "external")], input=encode_operation(next(operations())), capture_output=True)
    assert repeated.returncode == 0 and admit_record(repeated.stdout) == choices[-1]
    with CoreWorkspace(tmp_path / "external") as workspace:
        assert result_values(workspace, choices) == expected == [2, 4]
        with workspace.ledger._transaction() as connection:
            assert connection.execute("SELECT count(*) FROM records WHERE kind='execution'").fetchone() == (2,)


def test_native_dagster_worker_failure_retains_sibling_without_a_failed_selection(tmp_path):
    dagster = pytest.importorskip("dagster")
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        seed(workspace)
    path = tmp_path / "operations.jsonl"
    path.write_bytes(b"".join(encode_operation(call) + b"\n" for call in operations()))
    fixture = importlib.import_module("tests.dagster_process_fixture")
    config = {"resources": {"docspec_runtime": {"config": {"workspace": str(tmp_path / "workspace"),
        "operations_path": str(path), "evidence_root": str(tmp_path / "evidence"), "fail_input": "input:1"}}},
        "execution": {"config": {"max_concurrent": 2}}}
    instance_root = tmp_path / "dagster"
    instance_root.mkdir()
    with dagster.DagsterInstance.local_temp(str(instance_root)) as instance:
        with dagster.execute_job(dagster.reconstructable(fixture.reconstructable_job), instance=instance, run_config=config) as result:
            assert not result.success
    evidence = [json.loads(item.read_bytes()) for item in (tmp_path / "evidence").glob("*.json")]
    assert len(evidence) == 2 and all(item["pid"] != os.getpid() for item in evidence)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        sibling, failed = next(workspace.ledger.read_records([("selection", "job:selection:0"), ("selection", "job:selection:1")]))
        assert sibling is not None and sibling.retained and failed is None
        assert result_values(workspace, [sibling.value]) == [2]
        with workspace.ledger._transaction() as connection:
            assert dict(connection.execute("SELECT outcome,count(*) FROM records WHERE kind='result' GROUP BY outcome")) == {"failed": 1, "success": 1}
