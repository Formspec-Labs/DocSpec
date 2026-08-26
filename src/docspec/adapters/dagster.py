"""Thin optional Dagster mapping for DocSpec's scheduler-neutral task messages.

Dagster owns execution, retries, run state, event storage, and worker processes.
The injected resource reconstructs DocSpec's application services in each
process; only bounded ``StoreTask`` and ``StoreTaskResult`` bytes cross Dagster
step boundaries.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from docspec.domain.execution import ExecutionHandoff, StoreTask, StoreTaskResult
from docspec.domain.identity import OrderedJsonSequenceDigester
from docspec.errors import DocSpecError, IntegrityError
from docspec.ports.execution_backend import StoreTaskHandler

DAGSTER_JOB_NAME = "docspec_store_tasks"
DAGSTER_RUNTIME_RESOURCE_KEY = "docspec_runtime"


class DagsterAdapterError(DocSpecError):
    """Dagster is unavailable or an injected runtime has an invalid shape."""


TaskSource = Callable[[ExecutionHandoff], Iterable[StoreTask]]


@dataclass(frozen=True, slots=True)
class DagsterRuntime:
    """Process-local application services injected through one Dagster resource.

    A production ``ResourceDefinition`` should build this value from deployment
    configuration in every worker process. The task source streams the sealed
    ledger; the handler is the same scheduler-neutral ``StoreTaskHandler`` used
    by other execution backends.
    """

    handoff: ExecutionHandoff
    task_source: TaskSource
    handler: StoreTaskHandler

    def __post_init__(self) -> None:
        if not isinstance(self.handoff, ExecutionHandoff):
            raise TypeError("Dagster runtime handoff must be an ExecutionHandoff")
        if not callable(self.task_source):
            raise TypeError("Dagster runtime task_source must be callable")
        if not callable(self.handler):
            raise TypeError("Dagster runtime handler must be callable")


def _load_dagster() -> ModuleType:
    try:
        return importlib.import_module("dagster")
    except ModuleNotFoundError as error:
        raise DagsterAdapterError("the optional 'dagster' package is required to build Dagster definitions") from error


def _runtime(context: Any) -> DagsterRuntime:
    value = getattr(context.resources, DAGSTER_RUNTIME_RESOURCE_KEY)
    if not isinstance(value, DagsterRuntime):
        raise DagsterAdapterError("the Dagster resource must return DagsterRuntime")
    return value


def _mapping_key(task: StoreTask) -> str:
    """Use the stable digest part of DocSpec's task identity as Dagster's key."""

    return task.task_id.rsplit(":", 1)[-1]


def _task_payloads(runtime: DagsterRuntime) -> Iterator[tuple[str, bytes]]:
    """Stream and verify the exact task population sealed by the handoff."""

    handoff = runtime.handoff
    count = 0
    digest = OrderedJsonSequenceDigester()
    for value in runtime.task_source(handoff):
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

    if count != handoff.expected_task_count:
        raise IntegrityError("Dagster task stream count differs from the sealed handoff")
    if digest.finish() != handoff.task_set_digest:
        raise IntegrityError("Dagster task stream digest differs from the sealed handoff")


def _execute_task(runtime: DagsterRuntime, task_payload: bytes) -> StoreTaskResult:
    """Call the scheduler-neutral handler and verify its one terminal message."""

    handoff = runtime.handoff
    task = StoreTask.from_bytes(task_payload)
    if task.processing_plan_id != handoff.processing_plan.artifact_id or task.operation_id != handoff.operation_id:
        raise IntegrityError("Dagster mapped task is outside its execution handoff")
    result = runtime.handler(handoff, task)
    if not isinstance(result, StoreTaskResult):
        raise TypeError("Dagster StoreTaskHandler must return StoreTaskResult")
    if result.handoff_id != handoff.handoff_id or result.task != task:
        raise IntegrityError("Dagster StoreTaskHandler returned a result for a different handoff or task")
    return StoreTaskResult.from_bytes(result.to_bytes())


def build_dagster_definitions(
    runtime_resource: Any,
    *,
    executor_def: Any | None = None,
    retry_policy: Any | None = None,
) -> Any:
    """Build DocSpec's real dynamic Dagster job.

    ``runtime_resource`` is a Dagster resource definition (or another value
    Dagster accepts as a resource). The default executor is Dagster's maintained
    multiprocess executor. A composition root may inject another executor and a
    native ``RetryPolicy`` derived from its ``ExecutionProfile``.
    """

    dagster = _load_dagster()
    selected_executor = dagster.multiprocess_executor if executor_def is None else executor_def

    @dagster.op(
        name="emit_store_tasks",
        required_resource_keys={DAGSTER_RUNTIME_RESOURCE_KEY},
        out=dagster.DynamicOut(bytes),
    )
    def emit_store_tasks(context) -> Iterator[Any]:  # type: ignore[no-untyped-def]
        for mapping_key, payload in _task_payloads(_runtime(context)):
            task = StoreTask.from_bytes(payload)
            yield dagster.DynamicOutput(payload, mapping_key=mapping_key, metadata={"task_id": task.task_id})

    @dagster.op(
        name="execute_store_task",
        required_resource_keys={DAGSTER_RUNTIME_RESOURCE_KEY},
        out=dagster.Out(bytes),
        retry_policy=retry_policy,
    )
    def execute_store_task(context, task_payload: bytes) -> bytes:  # type: ignore[no-untyped-def]
        result = _execute_task(_runtime(context), task_payload)
        context.add_output_metadata(
            {
                "task_id": result.task.task_id,
                "result_id": result.result_id,
                "status": result.status.value,
            }
        )
        return result.to_bytes()

    @dagster.job(
        name=DAGSTER_JOB_NAME,
        resource_defs={DAGSTER_RUNTIME_RESOURCE_KEY: runtime_resource},
        executor_def=selected_executor,
    )
    def docspec_store_tasks() -> None:
        emit_store_tasks().map(execute_store_task)

    return dagster.Definitions(jobs=[docspec_store_tasks])
