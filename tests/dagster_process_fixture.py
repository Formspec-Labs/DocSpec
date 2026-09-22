"""Native Dagster workers reconstruct the same Core workspace and callbacks."""

import os
from pathlib import Path

import dagster

from docspec.adapters.dagster import DAGSTER_JOB_NAME, DagsterRuntime, build_dagster_definitions, decode_operation
from docspec.domain import core
from docspec.domain.identity import canonical_value_bytes
from docspec.runtime.core import CoreWorkspace


@dagster.resource(config_schema={"workspace": str, "operations_path": str, "evidence_root": str, "fail_input": dagster.Field(str, default_value="")})
def runtime_resource(context):
    """Native Dagster resource that replays recorded operations, writes per-execution evidence and doubles input values."""
    config = context.resource_config
    evidence = Path(config["evidence_root"])
    evidence.mkdir(parents=True, exist_ok=True)
    def source():
        with Path(config["operations_path"]).open("rb") as stream:
            for payload in stream:
                yield decode_operation(payload.removesuffix(b"\n"))
    def resolver(definition):
        def produce(operation):
            identity = operation.execution.execution_id
            (evidence / (identity.rsplit(":", 1)[-1] + ".json")).write_bytes(canonical_value_bytes({
                "pid": os.getpid(), "request_id": operation.execution.request_id, "execution_id": identity,
            }))
            if definition.configuration["input"] == config["fail_input"]:
                raise RuntimeError("injected worker failure")
            value = operation.read_value(definition.configuration["input"])
            operation.generate(core.InlineValue(value=value * 2), label="double")
        return produce
    with CoreWorkspace(config["workspace"]) as workspace:
        yield DagsterRuntime(workspace.operations, source, resolver)


def reconstructable_job():
    """Return the reconstructable job definition used by the multiprocess Dagster tests."""
    return build_dagster_definitions({"docspec_runtime": runtime_resource}).get_job_def(DAGSTER_JOB_NAME)
