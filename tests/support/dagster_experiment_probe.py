"""Installed native Dagster qualification; fault controls exist only in this probe."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

import dagster

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.adapters.dagster import DAGSTER_JOB_NAME, build_dagster_definitions
from docspec.domain.execution import StoreTaskResult
from docspec.domain.identity import canonical_json_file_bytes
from docspec.domain.jobs import FailureClass
from docspec.domain.policies import AcceptedFailurePolicy
from docspec.errors import IntegrityError
from docspec.processing import TextExtractor
from docspec.runtime import open_local_inspection
from docspec.workspace import LocalWorkspace
from examples import dagster_experiment as example
from examples.offline_demo import run_example as reference_example


def _write(path, value):
    path.write_bytes(canonical_json_file_bytes(value))


@dagster.resource(required_resource_keys={"workspace"})
def observed_fetcher(context):
    root = context.resources.workspace.root.parent / "evidence"
    root.mkdir(parents=True, exist_ok=True)

    class ObservedFetcher(LocalFileContentFetcher):
        def fetch(self, candidate, **kwargs):
            _write(root / f"fetch-{uuid4().hex}.json", {"locator": candidate.locator, "pid": os.getpid()})
            return super().fetch(candidate, **kwargs)

    return ObservedFetcher(context.resources.workspace.roots["sourceContent"])


@dagster.resource(required_resource_keys={"workspace"})
def blocking_extractor(context):
    root = context.resources.workspace.root.parent / "evidence"
    root.mkdir(parents=True, exist_ok=True)

    class ObservedExtractor(TextExtractor):
        def extract(self, captured, source_bytes):
            _write(root / f"extract-{uuid4().hex}.json", {"fileId": captured.file_id, "pid": os.getpid()})
            if source_bytes.startswith(b"Personal data") and not (root / "released").exists():
                _write(root / "active.json", {"pid": os.getpid(), "capture": captured.to_dict()})
                try:
                    # The parent run receives SIGINT. Dagster must forward its
                    # native interruption to this active child and unwind it.
                    while not (root / "released").exists():
                        time.sleep(0.05)
                finally:
                    (root / "active-extractor-closed").touch()
            return super().extract(captured, source_bytes)

    return ObservedExtractor()


@dagster.resource(config_schema=example.REFERENCE_CONFIG, required_resource_keys={
    "reference_root", "workspace", "fetcher", "extractor", "segmenter", "processor",
})
def observed_runtime(context):
    root = context.resources.workspace.root.parent / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    live = root / f"resource-live-{uuid4().hex}"
    live.touch()
    try:
        with example._prepare(context, extractor=context.resources.extractor, segmenter=context.resources.segmenter,
                              processors=(context.resources.processor,)) as prepared:
            yield prepared
    finally:
        live.unlink()


TEST_RESOURCES = example.PROCESSING_RESOURCES | {
    "fetcher": observed_fetcher, "extractor": blocking_extractor, "docspec_runtime": observed_runtime,
}


def interruption_job():
    return build_dagster_definitions(
        resource_defs=TEST_RESOURCES, retry_policy=dagster.RetryPolicy(max_retries=1),
    ).get_job_def(DAGSTER_JOB_NAME)


@dagster.resource(config_schema=example.REFERENCE_CONFIG, required_resource_keys={"reference_root", "workspace", "fetcher"})
def refusal_runtime(context):
    with example._prepare(context, stop_after="capture", accepted_failure_policy=AcceptedFailurePolicy(
        accepted_classes=(FailureClass.TRANSIENT_EXTERNAL,),
    )) as prepared:
        yield prepared


REFUSAL_RESOURCES = example.CAPTURE_RESOURCES | {"docspec_runtime": refusal_runtime, "fetcher": observed_fetcher}


def refusal_job():
    return build_dagster_definitions(
        resource_defs=REFUSAL_RESOURCES, retry_policy=dagster.RetryPolicy(max_retries=1),
    ).get_job_def(DAGSTER_JOB_NAME)


def _configuration(reference, root):
    return {
        "resources": {
            "reference_root": {"config": {"path": str(reference)}},
            "workspace": {"config": {"path": str(root / "dataset")}},
            "docspec_runtime": {"config": {}},
        },
        "execution": {"config": {"max_concurrent": 2}},
    }


def _prepare_config(definitions, config, instance):
    with dagster.build_resources(definitions, resource_config=config["resources"], instance=instance) as resources:
        prepared = resources.docspec_runtime
        assert prepared.handoff.expected_task_count == 2
        config["resources"]["docspec_runtime"]["config"]["handoff"] = prepared.handoff_ref.to_dict()
        return prepared.handoff_ref


def execute_cancelled_run(config_path, instance_root):
    with dagster.DagsterInstance.local_temp(instance_root) as instance:
        config = json.loads(Path(config_path).read_bytes())
        with dagster.execute_job(dagster.reconstructable(interruption_job), instance=instance, run_config=config) as result:
            assert not result.success


def _events(instance, run_id):
    return [entry.dagster_event for entry in instance.all_logs(run_id) if entry.dagster_event is not None]


def _outputs(instance, run_id):
    # Load only successful native outputs through Dagster's own IO manager.
    # This is a two-task probe, not a DocSpec result store or replay ledger.
    values = {}
    with dagster.build_resources({"io_manager": dagster.FilesystemIOManager()}, instance=instance) as resources:
        for event in _events(instance, run_id):
            if not event.is_successful_output or not event.step_key.startswith("execute_store_task["):
                continue
            output = event.step_output_data
            with dagster.build_output_context(step_key=event.step_key, name=output.output_name, run_id=run_id) as upstream:
                with dagster.build_input_context(upstream_output=upstream) as context:
                    raw = resources.io_manager.load_input(context)
            values[event.step_key] = StoreTaskResult.from_bytes(raw)
    return values


def _retain(definitions, config, instance, results):
    with dagster.build_resources(definitions, resource_config=config["resources"], instance=instance) as resources:
        prepared = resources.docspec_runtime
        release = prepared.retain(prepared.reconcile(results))
        return open_local_inspection(prepared.plan, resources.workspace,
            document_release_producer=example._producers(resources.reference_root)[1], release_ref=release)


def interrupted_reexecution(reference, root):
    root.mkdir()
    instance_root = root / "dagster"
    instance_root.mkdir()
    with dagster.DagsterInstance.local_temp(str(instance_root)) as instance:
        config = _configuration(reference, root)
        handoff = _prepare_config(TEST_RESOURCES, config, instance)
        _write(root / "native-config.json", config)
        with (root / "interruption.log").open("w") as log:
            child = subprocess.Popen([
                sys.executable, "-c", "from dagster_experiment_probe import execute_cancelled_run; "
                "import sys; execute_cancelled_run(sys.argv[1], sys.argv[2])",
                str(root / "native-config.json"), str(instance_root),
            ], stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 60
                while True:
                    runs = instance.get_runs()
                    events = [] if not runs else _events(instance, runs[0].run_id)
                    completed = [event for event in events if event.is_step_success and event.step_key.startswith("execute_store_task[")]
                    if (root / "evidence/active.json").exists() and len(completed) == 1:
                        break
                    assert child.poll() is None, (root / "interruption.log").read_text()
                    assert time.monotonic() < deadline, "native worker did not reach the checkpoint barrier"
                    time.sleep(0.05)
                run_id = runs[0].run_id
                active = json.loads((root / "evidence/active.json").read_bytes())
                assert active["pid"] != child.pid != os.getpid()
                # Register the native requested transition, as Dagster's run
                # launcher does. The executor must produce CANCELED only after
                # interruption reaches its workers and cleanup completes.
                instance.report_run_canceling(runs[0])
                os.kill(child.pid, signal.SIGINT)
                assert child.wait(timeout=30) == 0, (root / "interruption.log").read_text()
            finally:
                if child.poll() is None:
                    child.send_signal(signal.SIGINT)
                    child.wait(timeout=30)
        first = instance.get_run_by_id(run_id)
        assert first is not None and first.status is dagster.DagsterRunStatus.CANCELED
        assert (root / "evidence/active-extractor-closed").exists()
        assert not list((root / "evidence").glob("resource-live-*"))
        original = _outputs(instance, run_id)
        assert len(original) == 1
        roots = LocalWorkspace(root / "dataset").roots
        assert not (roots["controlRepository"] / "control/run-receipts").exists()
        assert not list((roots["reconciliation"] / "task-membership").glob("*"))
        (root / "evidence/released").touch()
        options = dagster.ReexecutionOptions.from_failure(run_id, instance)
        with dagster.execute_job(dagster.reconstructable(interruption_job), instance=instance,
                                run_config=config, reexecution_options=options) as retried:
            assert retried.success
            repeated = _outputs(instance, retried.run_id)
            assert len(repeated) == 1 and original.keys().isdisjoint(repeated)
            assert retried.dagster_run.parent_run_id == run_id
        results = original | repeated
        view = _retain(TEST_RESOURCES, config, instance, results.values())
        assert len(tuple(view.records("files"))) == 2
        assert len(tuple(view.records("failures"))) == 0
        files = [row["payload"] for row in view.records("files")]
        assert active["capture"] in files
        counts = view.summary()["work"]["counts"]
        assert counts["newCapturedFiles"] == counts["capturedFiles"] == 2
        assert counts["newCapturedBytes"] == counts["capturedBytes"] == sum(row["blob"]["byteSize"] for row in files)
        fetches = [json.loads(path.read_bytes()) for path in (root / "evidence").glob("fetch-*.json")]
        assert Counter(row["locator"] for row in fetches) == {"privacy.txt": 1, "security.txt": 1}
        extractions = [json.loads(path.read_bytes()) for path in (root / "evidence").glob("extract-*.json")]
        assert Counter(row["fileId"] for row in extractions)[active["capture"]["fileId"]] == 2
        assert len(extractions) == 3  # one interrupted call, its retry, and the untouched sibling
        with dagster.build_resources(TEST_RESOURCES, resource_config=config["resources"], instance=instance) as resources:
            assert resources.docspec_runtime.handoff_ref == handoff
            digests = {row["blob"]["digest"] for row in files}
            expected = [value for value in json.loads((reference / "matches.json").read_bytes())["resource-v2"]
                        if value["enclosingSourceEvidence"]["sourceDigest"] in digests]
            assert example._phrase_values(view, resources.processor) == expected
        assert not list((root / "evidence").glob("resource-live-*"))
        return {"canceledRunId": run_id, "completedSiblingReexecuted": False, "capturesRepeated": 0}


def source_refusal(reference, root):
    root.mkdir()
    (root / "dagster").mkdir()
    path = LocalWorkspace(reference).roots["sourceContent"] / "privacy.txt"
    content = path.read_bytes()
    path.unlink()
    try:
        with dagster.DagsterInstance.local_temp(str(root / "dagster")) as instance:
            config = _configuration(reference, root)
            _prepare_config(REFUSAL_RESOURCES, config, instance)
            with dagster.execute_job(dagster.reconstructable(refusal_job), instance=instance, run_config=config) as result:
                assert result.success
                events = _events(instance, result.run_id)
                assert not any(event.event_type_value == "STEP_UP_FOR_RETRY" for event in events)
                view = _retain(REFUSAL_RESOURCES, config, instance, _outputs(instance, result.run_id).values())
            assert len(tuple(view.records("files"))) == len(tuple(view.records("failures"))) == 1
    finally:
        path.write_bytes(content)


def main():
    root = Path.cwd() / "native-qualification"
    root.mkdir()
    reference = root / "reference"
    reference_example(reference)
    normal = example.run_example(reference, root / "native")
    assert normal["cleanOutputValuesAgree"]
    # The saved reference pin is independent of the subsequently loaded bytes.
    path = reference / "reference-inputs/vocabulary-v2.json"
    raw = path.read_bytes()
    path.write_bytes(raw + b" ")
    try:
        with dagster.build_resources({"reference_root": example.reference_root, "processor": example.processor},
                                    resource_config={"reference_root": {"config": {"path": str(reference)}}}):
            raise AssertionError("changed vocabulary was admitted")
    except dagster.DagsterResourceFunctionError as error:
        assert isinstance(error.__cause__, IntegrityError)
    finally:
        path.write_bytes(raw)
    cancellation = interrupted_reexecution(reference, root / "interrupted")
    source_refusal(reference, root / "refusal")
    print(json.dumps({"installedNativeExperiment": "pass", "sourceRefusalPreserved": True, **cancellation}, sort_keys=True))


if __name__ == "__main__":
    main()
