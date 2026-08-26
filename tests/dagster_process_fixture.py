"""Deployment-owned Dagster composition used by the process-boundary test."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import dagster

from docspec.adapters.dagster import DAGSTER_JOB_NAME, DagsterRuntime, build_dagster_definitions
from docspec.domain.execution import ExecutionHandoff, StoreTask, StoreTaskResult
from docspec.domain.identity import canonical_json_file_bytes
from docspec.domain.references import StoreRef


@dagster.resource(
    config_schema={
        "handoff_path": str,
        "task_ledger_path": str,
        "worker_evidence_root": str,
    }
)
def runtime_resource(context) -> DagsterRuntime:  # type: ignore[no-untyped-def]
    """Reconstruct the runtime from references in each Dagster worker."""

    config = context.resource_config
    handoff = ExecutionHandoff.from_bytes(Path(config["handoff_path"]).read_bytes())
    task_ledger_path = Path(config["task_ledger_path"])
    evidence_root = Path(config["worker_evidence_root"])

    def task_source(current_handoff: ExecutionHandoff) -> Iterator[StoreTask]:
        if current_handoff != handoff:
            raise ValueError("Dagster worker reconstructed a different handoff")
        with task_ledger_path.open("rb") as ledger:
            for line in ledger:
                yield StoreTask.from_bytes(line)

    def handler(current_handoff: ExecutionHandoff, task: StoreTask) -> StoreTaskResult:
        if current_handoff != handoff:
            raise ValueError("Dagster worker reconstructed a different handoff")
        evidence = canonical_json_file_bytes({"pid": os.getpid(), "taskId": task.task_id})
        (evidence_root / f"{task.task_id.rsplit(':', 1)[-1]}.json").write_bytes(evidence)
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

    return DagsterRuntime(handoff, task_source, handler)


def reconstructable_job() -> Any:
    """Return the same thin job definition in the coordinator and workers."""

    return build_dagster_definitions(runtime_resource).get_job_def(DAGSTER_JOB_NAME)
