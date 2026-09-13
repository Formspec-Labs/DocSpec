"""Deployment-owned Dagster composition used by the process-boundary test."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from typing import Any

import dagster

from docspec.adapters.dagster import DAGSTER_JOB_NAME, build_dagster_definitions
from docspec.runtime import prepare_local_run
from docspec.cli.requests import _local_run_arguments, _local_run_request
from docspec.domain.execution import ExecutionHandoff, StoreTask, StoreTaskResult
from docspec.domain.identity import canonical_json_file_bytes
from docspec.domain.references import ArtifactRef, StoreRef


@dagster.resource(
    config_schema={
        "handoff_path": str,
        "task_ledger_path": str,
        "worker_evidence_root": str,
    }
)
def runtime_resource(context) -> Any:  # type: ignore[no-untyped-def]
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

    return SimpleNamespace(handoff=handoff, task_source=task_source, execute_task=handler)


def reconstructable_job() -> Any:
    """Return the same thin job definition in the coordinator and workers."""

    return build_dagster_definitions({"docspec_runtime": runtime_resource}).get_job_def(DAGSTER_JOB_NAME)


@dagster.resource(
    config_schema={
        "run_request_path": str,
        "handoff_reference_path": str,
        "worker_evidence_root": str,
        "fail_task_id": dagster.Field(str, is_required=False),
    }
)
def application_runtime_resource(context) -> Any:  # type: ignore[no-untyped-def]
    """Reconstruct the real DocSpec task graph from deployment-owned resources."""

    config = context.resource_config
    request_path = Path(config["run_request_path"])
    handoff_reference = ArtifactRef.from_dict(
        json.loads(Path(config["handoff_reference_path"]).read_text(encoding="utf-8"))
    )
    prepared = prepare_local_run(
        **_local_run_arguments(_local_run_request(request_path)), handoff_ref=handoff_reference,
    )
    evidence_root = Path(config["worker_evidence_root"])
    evidence_root.mkdir(parents=True, exist_ok=True)
    fail_task_id = config.get("fail_task_id")

    execute_task = prepared.execute_task

    def handler(self, current_handoff: ExecutionHandoff, task: StoreTask) -> StoreTaskResult:
        if current_handoff != prepared.handoff:
            raise ValueError("Dagster worker reconstructed a different handoff")
        result = execute_task(current_handoff, task)
        if task.task_id == fail_task_id:
            evidence = {
                "pid": os.getpid(),
                "result": result.to_dict(),
                "status": "injected-failure",
                "task": task.to_dict(),
            }
            (evidence_root / f"{task.task_id.rsplit(':', 1)[-1]}.json").write_bytes(canonical_json_file_bytes(evidence))
            raise RuntimeError("injected Dagster worker failure")
        evidence = {
            "pid": os.getpid(),
            "status": "succeeded",
            "task": task.to_dict(),
            "result": result.to_dict(),
        }
        (evidence_root / f"{task.task_id.rsplit(':', 1)[-1]}.json").write_bytes(canonical_json_file_bytes(evidence))
        return result

    with prepared, patch.object(type(prepared), "execute_task", handler):
        yield prepared


def reconstructable_application_job() -> Any:
    """Return a native Dagster job backed by DocSpec's real application services."""

    return build_dagster_definitions({"docspec_runtime": application_runtime_resource}).get_job_def(DAGSTER_JOB_NAME)
