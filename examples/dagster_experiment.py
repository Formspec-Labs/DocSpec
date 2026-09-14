"""Run document statistics in native Dagster workers over retained Core inputs.

python -m examples.dagster_experiment --output /new/path
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import dagster

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.adapters.dagster import (
    DAGSTER_JOB_NAME, DagsterRuntime, ScheduledOperation, build_dagster_definitions,
    decode_operation, encode_operation,
)
from docspec.application.document_processors import content_statistics_processor
from docspec.domain import core
from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.core_admission import admit_record
from docspec.domain.identity import canonical_value_bytes
from docspec.runtime.core import CoreWorkspace


RUNTIME_CONFIG = {"workspace": str, "operations_path": str}


def task_source(path):
    with Path(path).open("rb") as stream:
        for payload in stream:
            yield decode_operation(payload.removesuffix(b"\n"))


def producer_resolver(definition):
    processor = content_statistics_processor()
    if definition != processor.definition:
        raise ValueError("the requested definition does not match this installed processor")
    def produce(context):
        request = next(context.session.read_records([("request", context.execution.request_id)]))[0].value
        binding, = request.inputs
        processor.process(context, {"segments": core.State(format_version=1, state_id=binding.state_id)})
    return produce


@dagster.resource(config_schema=RUNTIME_CONFIG)
def runtime_resource(context):
    config = context.resource_config
    with CoreWorkspace(config["workspace"]) as workspace:
        yield DagsterRuntime(workspace.operations, lambda: task_source(config["operations_path"]), producer_resolver)


def processing_job():
    return build_dagster_definitions({"docspec_runtime": runtime_resource},
        retry_policy=dagster.RetryPolicy(max_retries=1)).get_job_def(DAGSTER_JOB_NAME)


def prepare(root: Path):
    """Capture and segment once; the scheduler receives only bounded Core calls."""
    root.mkdir(parents=True, exist_ok=True)
    sources = root / "sources"
    sources.mkdir()
    for name, text in {"privacy": "Personal data requires care.\n\nKeep only necessary records.",
                       "security": "Protect every account.\n\nReview access regularly."}.items():
        (sources / f"{name}.txt").write_text(text)
    workspace_path = root / "workspace"
    calls = []
    definition = content_statistics_processor().definition
    with CoreWorkspace(workspace_path) as workspace:
        pipeline = workspace.documents(fetcher=LocalFileContentFetcher(sources))
        catalog = pipeline.import_sources((SourceItem(name, "1", (CandidateFile("text", f"{name}.txt", "text/plain"),))
            for name in ("privacy", "security")), state_id="catalog")
        run = pipeline.run(catalog.state_id, run_id="prepared", dataset="documents")
        for index, (_, _, summary) in enumerate(pipeline.rows(run.state_id)):
            selected = next(workspace.ledger.read_records([("selection", summary["selections"][-1])]))[0].value
            result = next(workspace.ledger.read_records([("result", selected.selected_result_id)]))[0].value
            segments, = (output for output in result.outcome.outputs if output.label == "segments")
            request = core.Request(format_version=1, request_id=f"scheduled:request:{index}", definition_id=definition.definition_id,
                inputs=(core.StateInput(label="segments", state_id=segments.entity_id),),
                dependencies=(core.Dependency(label="segments", binding_label="segments", selection=core.StateMembers(member_selector=core.Whole(), material_keys=True)),))
            calls.append(ScheduledOperation(definition=definition, request=request, selection_id=f"scheduled:selection:{index}",
                target=core.Origin(parent_entity_id=segments.entity_id), fresh=True))
    path = root / "operations.jsonl"
    path.write_bytes(b"".join(encode_operation(call) + b"\n" for call in calls))
    return {"resources": {"docspec_runtime": {"config": {"workspace": str(workspace_path), "operations_path": str(path)}}},
            "execution": {"config": {"max_concurrent": 2}}}


def output_values(workspace, selections):
    pipeline = workspace.documents(fetcher=LocalFileContentFetcher(workspace.path.parent / "sources"))
    values = []
    for selection in selections:
        result = next(workspace.ledger.read_records([("result", selection.selected_result_id)]))[0].value
        output, = result.outcome.outputs
        values.extend(value for _, _, value in pipeline.rows(output.entity_id))
    return sorted(values, key=canonical_value_bytes)


def run_example(output: Path) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    config = prepare(output)
    instance_root = output / "dagster"
    instance_root.mkdir()
    with dagster.DagsterInstance.local_temp(str(instance_root)) as instance:
        with dagster.execute_job(dagster.reconstructable(processing_job), instance=instance, run_config=config) as result:
            if not result.success:
                raise RuntimeError(f"inspect native Dagster run {result.run_id}; processing did not complete")
            selected = tuple(admit_record(raw) for raw in result.output_for_node("execute_operation").values())
            run_id = result.run_id
    with CoreWorkspace(output / "workspace") as workspace:
        actual = output_values(workspace, selected)
        direct = []
        for call in task_source(output / "operations.jsonl"):
            direct.append(workspace.operations.resolve(call.definition, call.request, producer_resolver(call.definition),
                selection_id="direct:" + call.selection_id, target=call.target, reuse_policy=lambda _: False, fresh=True).selection)
        assert actual == output_values(workspace, direct)
    summary = {"verdict": "pass", "operations": len(selected), "cleanOutputValuesAgree": True, "dagsterRunId": run_id}
    (output / "dagster-example.json").write_bytes(canonical_value_bytes(summary))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run_example(parser.parse_args().output), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
