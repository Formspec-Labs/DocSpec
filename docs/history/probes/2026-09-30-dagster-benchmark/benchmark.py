"""Bounded comparison of direct Core calls and native Dagster on the existing example."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import time
from importlib.metadata import version

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))


def measured_resolver(root, resolver):
    def resolve(definition):
        producer = resolver(definition)

        def produce(context):
            started = time.perf_counter()
            try:
                return producer(context)
            finally:
                evidence = {"pid": os.getpid(), "seconds": time.perf_counter() - started}
                path = root / "calls" / (hashlib.sha256(context.execution.execution_id.encode()).hexdigest() + ".json")
                path.write_text(json.dumps(evidence))
        return produce
    return resolve


def job_definition():
    import dagster
    from docspec.adapters.dagster import DAGSTER_JOB_NAME, DagsterRuntime, build_dagster_definitions
    from docspec.runtime.core import CoreWorkspace
    from examples import dagster_experiment as example

    @dagster.resource(config_schema={"root": str})
    def runtime(context):
        root = Path(context.resource_config["root"])
        with CoreWorkspace(root / "workspace") as workspace:
            yield DagsterRuntime(workspace.operations, lambda: example.task_source(root / "operations.jsonl"),
                                 measured_resolver(root, example.producer_resolver))

    return build_dagster_definitions({"docspec_runtime": runtime}).get_job_def(DAGSTER_JOB_NAME)


def run_case():
    import dagster
    from docspec.domain.identity import canonical_value_bytes
    from docspec.runtime.core import CoreWorkspace
    from examples import dagster_experiment as example

    arm, root_text = sys.argv[1:]
    root = Path(root_text)
    (root / "calls").mkdir()
    calls = tuple(example.task_source(root / "operations.jsonl"))
    started = time.perf_counter()
    instance = None
    if arm != "direct":
        (root / "dagster").mkdir()
        instance = dagster.DagsterInstance.local_temp(str(root / "dagster"))
        job = job_definition() if arm == "dagster_in_process" else dagster.reconstructable(job_definition)
    result = {"arm": arm, "parent_pid": os.getpid(), "setup_seconds": time.perf_counter() - started,
              "tasks_sha256": hashlib.sha256((root / "operations.jsonl").read_bytes()).hexdigest(), "phases": {}}
    first_ids = None
    try:
        for phase in ("fresh", "recovery"):
            before = set((root / "calls").glob("*.json"))
            started = time.perf_counter()
            if arm == "direct":
                with CoreWorkspace(root / "workspace") as workspace:
                    for call in calls:
                        workspace.operations.resolve(call.definition, call.request,
                            measured_resolver(root, example.producer_resolver)(call.definition),
                            selection_id=call.selection_id, target=call.target, fresh=call.fresh,
                            reuse_policy=lambda saved: saved.outcome.status == "success")
            else:
                config = {"resources": {"docspec_runtime": {"config": {"root": str(root)}}}}
                if arm == "dagster_in_process":
                    execution = job.execute_in_process(instance=instance, run_config=config)
                    assert execution.success
                else:
                    config["execution"] = {"config": {"max_concurrent": 2}}
                    with dagster.execute_job(job, instance=instance, run_config=config) as execution:
                        assert execution.success
            elapsed = time.perf_counter() - started
            measurements = [json.loads(p.read_text()) for p in set((root / "calls").glob("*.json")) - before]
            with CoreWorkspace(root / "workspace") as workspace:
                selections = [next(workspace.ledger.read_records([("selection", call.selection_id)]))[0].value for call in calls]
                values = example.output_values(workspace, selections)
            ids = [selection.selected_result_id for selection in selections]
            if phase == "fresh":
                assert len(measurements) == len(calls)
                first_ids = ids
                if arm == "dagster_multiprocess":
                    assert all(item["pid"] != os.getpid() for item in measurements)
            else:
                assert not measurements and ids == first_ids
            result["phases"][phase] = {"wall_seconds": elapsed, "producer_calls": measurements,
                "producer_seconds_sum": sum(item["seconds"] for item in measurements),
                "selected_result_ids": ids, "output_sha256": hashlib.sha256(canonical_value_bytes(values)).hexdigest(),
                "output_values": values}
    finally:
        if instance is not None:
            instance.dispose()
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    arms = ["direct", "dagster_in_process", "dagster_multiprocess"]
    receipt = {"revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "platform": platform.platform(), "python": sys.version, "cpu_count": os.cpu_count(),
        "versions": {name: version(name) for name in ("docspec", "spicy-docs", "dagster", "duckdb", "pyarrow")},
        "cases": [], "failures": []}
    from examples import dagster_experiment as example
    template = output / "prepared-inputs"
    example.prepare(template)
    started = time.monotonic()
    for repetition in range(3):
        for arm in arms[repetition:] + arms[:repetition]:
            root = output / f"{repetition + 1}-{arm}"
            shutil.copytree(template, root)
            log = output / f"{root.name}.log"
            with log.open("w") as stream:
                command = [sys.executable, "-c", "import benchmark; benchmark.run_case()", arm, str(root)]
                try:
                    completed = subprocess.run(command, cwd=Path(__file__).parent, stdout=stream, stderr=subprocess.STDOUT,
                                               timeout=max(1, min(120, 600 - (time.monotonic() - started))))
                    assert completed.returncode == 0, f"exit {completed.returncode}"
                    receipt["cases"].append(json.loads((root / "result.json").read_text()))
                    assert len({case["tasks_sha256"] for case in receipt["cases"]}) == 1
                    assert len({phase["output_sha256"] for case in receipt["cases"] for phase in case["phases"].values()}) == 1
                except (AssertionError, subprocess.TimeoutExpired) as error:
                    receipt["failures"].append({"case": root.name, "error": str(error), "log": str(log)})
                    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
                    raise
            print(root.name, receipt["cases"][-1]["phases"]["fresh"]["wall_seconds"], flush=True)
            (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    receipt["summary"] = {}
    for arm in arms:
        cases = [case for case in receipt["cases"] if case["arm"] == arm]
        fields = {"setup_seconds": [case["setup_seconds"] for case in cases]}
        fields.update({phase: [case["phases"][phase]["wall_seconds"] for case in cases] for phase in ("fresh", "recovery")})
        receipt["summary"][arm] = {name: {"median": statistics.median(values), "min": min(values), "max": max(values)}
                                   for name, values in fields.items()}
    receipt["elapsed_seconds"] = time.monotonic() - started
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt["summary"], indent=2))


if __name__ == "__main__":
    main()
