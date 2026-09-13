"""Thin optional Dagster mapping for DocSpec's scheduler-neutral task messages.

Dagster owns execution, retries, run state, event storage, and worker processes.
The injected resource reconstructs DocSpec's application services in each
process; only bounded ``StoreTask`` and ``StoreTaskResult`` bytes cross Dagster
step boundaries.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator, Mapping
from contextlib import closing
from types import ModuleType
from typing import Any

from docspec.domain.execution import ExecutionHandoff, StoreTask, StoreTaskResult
from docspec.domain.identity import OrderedJsonSequenceDigester
from docspec.errors import DocSpecError, IntegrityError

DAGSTER_JOB_NAME = "docspec_store_tasks"
DAGSTER_RUNTIME_RESOURCE_KEY = "docspec_runtime"


class DagsterAdapterError(DocSpecError):
    """Dagster is unavailable or an injected runtime has an invalid shape."""


def _load_dagster() -> ModuleType:
    try:
        return importlib.import_module("dagster")
    except ModuleNotFoundError as error:
        raise DagsterAdapterError("the optional 'dagster' package is required to build Dagster definitions") from error


def _runtime(context: Any) -> Any:
    value = getattr(context.resources, DAGSTER_RUNTIME_RESOURCE_KEY)
    if (
        not isinstance(getattr(value, "handoff", None), ExecutionHandoff)
        or not callable(getattr(value, "task_source", None))
        or not callable(getattr(value, "execute_task", None))
    ):
        raise DagsterAdapterError("the docspec_runtime resource must provide a prepared DocSpec run")
    return value


def _mapping_key(task: StoreTask) -> str:
    """Use the stable digest part of DocSpec's task identity as Dagster's key."""

    return task.task_id.rsplit(":", 1)[-1]


def _task_payloads(runtime: Any) -> Iterator[tuple[str, bytes]]:
    """Stream and verify the exact task population sealed by the handoff."""

    handoff = runtime.handoff
    count = 0
    digest = OrderedJsonSequenceDigester()
    source = iter(runtime.task_source(handoff))
    try:
        for value in source:
            if not isinstance(value, StoreTask):
                raise IntegrityError("Dagster task source yielded a non-StoreTask value")
            if value.processing_plan_id != handoff.processing_plan.artifact_id:
                raise IntegrityError("Dagster task names a different processing plan")
            if value.operation_id != handoff.operation_id:
                raise IntegrityError("Dagster task names a different execution operation")
            if count >= handoff.expected_task_count:
                raise IntegrityError("Dagster task stream exceeds the sealed task count")
            digest.accept(value.to_dict())
            count += 1
            yield _mapping_key(value), value.to_bytes()
    finally:
        close = getattr(source, "close", None)
        if close is not None:
            close()

    if count != handoff.expected_task_count:
        raise IntegrityError("Dagster task stream count differs from the sealed handoff")
    if digest.finish() != handoff.task_set_digest:
        raise IntegrityError("Dagster task stream digest differs from the sealed handoff")


def _execute_task(runtime: Any, task_payload: bytes) -> StoreTaskResult:
    """Call the scheduler-neutral handler and verify its one terminal message."""

    handoff = runtime.handoff
    task = StoreTask.from_bytes(task_payload)
    if task.processing_plan_id != handoff.processing_plan.artifact_id or task.operation_id != handoff.operation_id:
        raise IntegrityError("Dagster mapped task is outside its execution handoff")
    result = runtime.execute_task(handoff, task)
    if not isinstance(result, StoreTaskResult):
        raise TypeError("Dagster StoreTaskHandler must return StoreTaskResult")
    if result.handoff_id != handoff.handoff_id or result.task != task:
        raise IntegrityError("Dagster StoreTaskHandler returned a result for a different handoff or task")
    return StoreTaskResult.from_bytes(result.to_bytes())


def build_dagster_definitions(
    resource_defs: Mapping[str, Any],
    *,
    executor_def: Any | None = None,
    retry_policy: Any | None = None,
) -> Any:
    """Build the native dynamic job with dependency-injected resources.

    ``resource_defs["docspec_runtime"]`` supplies the existing prepared run.
    Use a native generator resource with ``with prepare_local_experiment(...)``
    to close each process's temporary task index. Other native resources can
    supply its fetcher, processors, workspace, or deployment configuration.

    Dagster owns executor configuration, retries, cancellation, and event
    storage. The default executor is its multiprocess executor. The job emits
    bounded task/result messages; callers reconcile those through DocSpec's
    existing API without a second scheduler or run ledger.
    """

    if DAGSTER_RUNTIME_RESOURCE_KEY not in resource_defs:
        raise DagsterAdapterError("resource_defs must include docspec_runtime")
    dagster = _load_dagster()
    selected_executor = dagster.multiprocess_executor if executor_def is None else executor_def

    @dagster.op(
        name="emit_store_tasks",
        required_resource_keys={DAGSTER_RUNTIME_RESOURCE_KEY},
        out=dagster.DynamicOut(bytes),
    )
    def emit_store_tasks(context) -> Iterator[Any]:  # type: ignore[no-untyped-def]
        runtime = _runtime(context)
        with closing(_task_payloads(runtime)) as payloads:
            for mapping_key, payload in payloads:
                task = StoreTask.from_bytes(payload)
                yield dagster.DynamicOutput(
                    payload, mapping_key=mapping_key,
                    metadata={
                        "handoff_id": runtime.handoff.handoff_id,
                        "execution_profile_ref": runtime.handoff.execution_profile.to_dict(),
                        "task_id": task.task_id,
                        "input_store_ref": task.input_store.to_dict(),
                    },
                )

    @dagster.op(
        name="execute_store_task",
        required_resource_keys={DAGSTER_RUNTIME_RESOURCE_KEY},
        out=dagster.Out(bytes),
        retry_policy=retry_policy,
    )
    def execute_store_task(context, task_payload: bytes) -> bytes:  # type: ignore[no-untyped-def]
        runtime = _runtime(context)
        result = _execute_task(runtime, task_payload)
        context.add_output_metadata(
            {
                "handoff_id": result.handoff_id,
                "execution_profile_ref": runtime.handoff.execution_profile.to_dict(),
                "task_id": result.task.task_id,
                "input_store_ref": result.task.input_store.to_dict(),
                "result_id": result.result_id,
                "status": result.status.value,
                **({} if result.output_store is None else {"output_store_ref": result.output_store.to_dict()}),
            }
        )
        return result.to_bytes()

    @dagster.job(
        name=DAGSTER_JOB_NAME,
        resource_defs=dict(resource_defs),
        executor_def=selected_executor,
    )
    def docspec_store_tasks() -> None:
        emit_store_tasks().map(execute_store_task)

    return dagster.Definitions(jobs=[docspec_store_tasks])
