"""Installed native worker cancellation uses ordinary retained Core attempts.

Qualifies the installed-wheel Dagster path: a canceled run must retain one
interrupted attempt, and its reexecution must repeat only the interrupted step,
reuse the completed sibling, and leave no live resource behind.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import dagster

from docspec.adapters.dagster import DAGSTER_JOB_NAME, DagsterRuntime, build_dagster_definitions
from docspec.domain.core_admission import admit_record
from docspec.domain.identity import canonical_value_bytes
from docspec.runtime.core import CoreWorkspace
from examples import dagster_experiment as example


def _write(path, value):
    path.write_bytes(canonical_value_bytes(value))


@dagster.resource(config_schema=example.RUNTIME_CONFIG)
def observed_runtime(context):
    """Yield the example runtime with a resolver that blocks the second producer until released."""
    config = context.resource_config
    root = Path(config["workspace"]).parent / "evidence"
    root.mkdir(exist_ok=True)
    live = root / f"resource-live-{uuid4().hex}"
    live.touch()
    def resolver(definition):
        def produce(operation):
            request_id = operation.execution.request_id
            _write(root / f"call-{uuid4().hex}.json", {"pid": os.getpid(), "request_id": request_id})
            if request_id.endswith(":1") and not (root / "released").exists():
                _write(root / "active.json", {"pid": os.getpid()})
                try:
                    deadline = time.monotonic() + 90
                    while not (root / "released").exists():
                        if time.monotonic() > deadline:
                            raise TimeoutError("native cancellation probe was not released")
                        time.sleep(0.05)
                finally:
                    (root / "active-producer-closed").touch()
            return example.producer_resolver(definition)(operation)
        return produce
    try:
        with CoreWorkspace(config["workspace"]) as workspace:
            yield DagsterRuntime(workspace.operations, lambda: example.task_source(config["operations_path"]), resolver)
    finally:
        live.unlink()


def interruption_job():
    """Return the job definition that runs the example through the observing runtime."""
    return build_dagster_definitions({"docspec_runtime": observed_runtime}).get_job_def(DAGSTER_JOB_NAME)


def execute_cancelled_run(config_path, instance_root):
    """Run the interruption job in a local temp instance and require it not to succeed."""
    with dagster.DagsterInstance.local_temp(instance_root) as instance:
        with dagster.execute_job(dagster.reconstructable(interruption_job), instance=instance,
                                run_config=json.loads(Path(config_path).read_bytes())) as result:
            assert not result.success


def _events(instance, run_id):
    return [entry.dagster_event for entry in instance.all_logs(run_id) if entry.dagster_event is not None]


def _outputs(instance, run_id):
    """Admit every successful ``execute_operation`` step output as a Core record."""
    values = {}
    with dagster.build_resources({"io_manager": dagster.FilesystemIOManager()}, instance=instance) as resources:
        for event in _events(instance, run_id):
            if not event.is_successful_output or not event.step_key.startswith("execute_operation["):
                continue
            output = event.step_output_data
            with dagster.build_output_context(step_key=event.step_key, name=output.output_name, run_id=run_id) as upstream:
                with dagster.build_input_context(upstream_output=upstream) as context:
                    raw = resources.io_manager.load_input(context)
            values[event.step_key] = admit_record(raw)
    return values


def interrupted_reexecution(root):
    """Cancel a native run mid-producer, then reexecute and check attempts, outputs and cleanup.

    Fails unless the canceled run retains exactly one interrupted result, the
    completed sibling is not recomputed, and the reexecuted outputs are disjoint
    from the originals.
    """

    config = example.prepare(root)
    with CoreWorkspace(root / "workspace") as workspace:
        with workspace.ledger._transaction() as connection:
            prepared_executions = {row[0] for row in connection.execute(
                "SELECT record_id FROM records WHERE kind='execution'")}
    instance_root = root / "dagster"
    instance_root.mkdir()
    with dagster.DagsterInstance.local_temp(str(instance_root)) as instance:
        _write(root / "native-config.json", config)
        with (root / "interruption.log").open("w") as log:
            child = subprocess.Popen([sys.executable, "-c",
                "from dagster_experiment_probe import execute_cancelled_run; import sys; execute_cancelled_run(sys.argv[1], sys.argv[2])",
                str(root / "native-config.json"), str(instance_root)], stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 60
                while True:
                    runs = instance.get_runs()
                    events = [] if not runs else _events(instance, runs[0].run_id)
                    completed = [event for event in events if event.is_step_success and event.step_key.startswith("execute_operation[")]
                    if (root / "evidence/active.json").exists() and len(completed) == 1:
                        break
                    assert child.poll() is None, (root / "interruption.log").read_text()
                    assert time.monotonic() < deadline, "native worker did not reach the cancellation barrier"
                    time.sleep(0.05)
                run_id = runs[0].run_id
                active = json.loads((root / "evidence/active.json").read_bytes())
                assert active["pid"] != child.pid != os.getpid()
                instance.report_run_canceling(runs[0])
                os.kill(child.pid, signal.SIGINT)
                assert child.wait(timeout=30) == 0, (root / "interruption.log").read_text()
            finally:
                if child.poll() is None:
                    child.send_signal(signal.SIGINT)
                    child.wait(timeout=30)
        first = instance.get_run_by_id(run_id)
        assert first.status is dagster.DagsterRunStatus.CANCELED
        assert (root / "evidence/active-producer-closed").exists()
        assert not list((root / "evidence").glob("resource-live-*"))
        original = _outputs(instance, run_id)
        assert len(original) == 1
        with CoreWorkspace(root / "workspace") as workspace:
            with workspace.ledger._transaction() as connection:
                assert connection.execute("SELECT count(*) FROM records WHERE kind='result' AND outcome='interrupted'").fetchone() == (1,)
        (root / "evidence/released").touch()
        options = dagster.ReexecutionOptions.from_failure(run_id, instance)
        with dagster.execute_job(dagster.reconstructable(interruption_job), instance=instance,
                                run_config=config, reexecution_options=options) as retried:
            assert retried.success and retried.dagster_run.parent_run_id == run_id
            repeated = _outputs(instance, retried.run_id)
            assert len(repeated) == 1 and original.keys().isdisjoint(repeated)
        calls = [json.loads(path.read_bytes()) for path in (root / "evidence").glob("call-*.json")]
        assert [item["request_id"] for item in calls].count("scheduled:request:0") == 1
        assert [item["request_id"] for item in calls].count("scheduled:request:1") == 2
        with CoreWorkspace(root / "workspace") as workspace:
            assert len(example.output_values(workspace, (original | repeated).values())) == 4
            scheduled_executions = set()
            for request_id, expected_count in (("scheduled:request:0", 1), ("scheduled:request:1", 2)):
                attempts = [identity for batch in workspace.ledger.executions(request_id) for identity in batch]
                assert len(attempts) == expected_count
                scheduled_executions.update(attempts)
            with workspace.ledger._transaction() as connection:
                all_executions = {row[0] for row in connection.execute(
                    "SELECT record_id FROM records WHERE kind='execution'")}
            assert prepared_executions.isdisjoint(scheduled_executions)
            assert all_executions == prepared_executions | scheduled_executions
        assert not list((root / "evidence").glob("resource-live-*"))
        return {"canceledRunId": run_id, "completedSiblingReexecuted": False, "capturesRepeated": 0}


def main():
    root = Path.cwd() / "native-qualification"
    root.mkdir()
    normal = example.run_example(root / "native")
    assert normal["cleanOutputValuesAgree"]
    cancellation = interrupted_reexecution(root / "interrupted")
    print(json.dumps({"installedNativeExperiment": "pass", **cancellation}, sort_keys=True))


if __name__ == "__main__":
    main()
