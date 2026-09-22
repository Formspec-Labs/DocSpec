"""Optional native Dagster scheduling over the ordinary Core lifecycle.

Resources construct CoreOperations and producer implementations in each worker.
Dagster owns scheduling, retries, cancellation, process isolation, and events;
Core owns attempts, retained outputs, and exact selection recovery.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Literal

import msgspec

from docspec.application.core_execution import CoreOperations
from docspec.application.core_reuse import Resolution
from docspec.domain import core
from docspec.domain.core_admission import admit_record, encode_record, record_value
from docspec.domain.identity import canonical_value_bytes, decode_canonical_json_value, sha256_digest
from docspec.domain.streams import owned_iterator
from docspec.errors import DocSpecError, IntegrityError, LimitExceededError, StateTransitionError
from docspec.ports.record_storage import BATCH_BYTES

DAGSTER_JOB_NAME = "docspec_operations"
DAGSTER_RUNTIME_RESOURCE_KEY = "docspec_runtime"


class DagsterAdapterError(DocSpecError):
    """Dagster is unavailable or its injected Core resource is invalid."""


class ScheduledOperation(core.Fixed, kw_only=True):
    """Bounded control data; producers and dataset values stay in their owners."""

    format: Literal["docspec-dagster-operation"] = "docspec-dagster-operation"
    version: Literal[1] = 1
    definition: core.OperationDefinition
    request: core.Request
    selection_id: core.Identifier
    target: core.Origin
    fresh: bool = False
    output_labels: tuple[core.Identifier, ...] | None = None
    capture_origin: core.Origin | None = None


def encode_operation(operation: ScheduledOperation) -> bytes:
    """Encode a scheduled operation as canonical bytes, refusing a mismatched definition or an oversized payload."""

    value = record_value(operation, ScheduledOperation)
    definition, request = admit_record(encode_record(value["definition"])), admit_record(encode_record(value["request"]))
    if definition.definition_id != request.definition_id:
        raise IntegrityError("scheduled request names a different operation definition")
    payload = canonical_value_bytes(value)
    if len(payload) > BATCH_BYTES:
        raise LimitExceededError("scheduled Core operation exceeds the 8 MiB control limit")
    return payload


def decode_operation(payload: bytes) -> ScheduledOperation:
    """Decode and re-encode-check a scheduled operation, refusing non-bytes or an oversized payload."""

    if not isinstance(payload, bytes):
        raise IntegrityError("scheduled Core operation must be encoded bytes")
    if len(payload) > BATCH_BYTES:
        raise LimitExceededError("scheduled Core operation exceeds the 8 MiB control limit")
    value = decode_canonical_json_value(payload, label="scheduled Core operation")
    operation = msgspec.convert(record_value(value, ScheduledOperation), type=ScheduledOperation, strict=True)
    encode_operation(operation)
    return operation


def _eligible(result):
    return result.outcome.status == "success"


@dataclass(frozen=True, slots=True)
class DagsterRuntime:
    """Per-worker Core operations plus the task source, producer resolver and reuse policy Dagster injects."""

    operations: CoreOperations
    task_source: Callable[[], Iterable[ScheduledOperation]]
    producer_resolver: Callable[[core.OperationDefinition], Callable]
    reuse_policy: Callable[[core.Result], bool] = _eligible


def _load_dagster() -> ModuleType:
    try:
        return importlib.import_module("dagster")
    except ModuleNotFoundError as error:
        raise DagsterAdapterError("the optional 'dagster' package is required to build Dagster definitions") from error


def _runtime(context: Any) -> DagsterRuntime:
    value = getattr(context.resources, DAGSTER_RUNTIME_RESOURCE_KEY)
    if (not isinstance(value, DagsterRuntime) or not isinstance(value.operations, CoreOperations)
            or not all(callable(item) for item in (value.task_source, value.producer_resolver, value.reuse_policy))):
        raise DagsterAdapterError("docspec_runtime must supply CoreOperations, a task source, and producer and reuse callbacks")
    return value


def _task_payloads(runtime: DagsterRuntime) -> Iterator[tuple[str, bytes]]:
    with owned_iterator(runtime.task_source()) as source:
        for operation in source:
            payload = encode_operation(operation)
            yield sha256_digest(operation.selection_id.encode()).split(":", 1)[1], payload


def _execute_operation(runtime: DagsterRuntime, operation: ScheduledOperation) -> Resolution:
    def produce(context):
        producer = runtime.producer_resolver(operation.definition)
        if not callable(producer):
            raise DagsterAdapterError("producer_resolver must return a Core producer callback")
        return producer(context)
    resolved = runtime.operations.resolve(operation.definition, operation.request, produce,
        selection_id=operation.selection_id, target=operation.target, reuse_policy=runtime.reuse_policy,
        output_labels=operation.output_labels, fresh=operation.fresh, capture_origin=operation.capture_origin)
    if not isinstance(resolved, Resolution):
        raise StateTransitionError("scheduled operation suspended; use the Core continuation API to verify and resume it")
    return resolved


def build_dagster_definitions(resource_defs: Mapping[str, Any], *, executor_def=None, retry_policy=None) -> Any:
    """Map supplied Core operations using native Dagster resources and retries.

    Supply a DagsterRuntime through ``resource_defs['docspec_runtime']``. A
    generator resource should open and close its CoreWorkspace in each worker.
    Retain inputs before scheduling. Stable selection IDs recover acknowledged
    work on retry; another requested observation uses a new selection ID.
    ``retry_policy`` and ``executor_def`` are ordinary native Dagster settings.
    """
    if DAGSTER_RUNTIME_RESOURCE_KEY not in resource_defs:
        raise DagsterAdapterError("resource_defs must include docspec_runtime")
    dagster = _load_dagster()

    @dagster.op(name="emit_operations", required_resource_keys={DAGSTER_RUNTIME_RESOURCE_KEY}, out=dagster.DynamicOut(bytes))
    def emit_operations(context) -> Iterator[Any]:
        with owned_iterator(_task_payloads(_runtime(context))) as payloads:
            for mapping_key, payload in payloads:
                operation = decode_operation(payload)
                yield dagster.DynamicOutput(payload, mapping_key=mapping_key, metadata={
                    "request_id": operation.request.request_id, "selection_id": operation.selection_id,
                    "definition_id": operation.definition.definition_id, "fresh": operation.fresh,
                })

    @dagster.op(name="execute_operation", required_resource_keys={DAGSTER_RUNTIME_RESOURCE_KEY}, out=dagster.Out(bytes), retry_policy=retry_policy)
    def execute_operation(context, operation_payload: bytes) -> bytes:
        operation = decode_operation(operation_payload)
        resolved = _execute_operation(_runtime(context), operation)
        context.add_output_metadata({"request_id": operation.request.request_id, "selection_id": resolved.selection.selection_id,
            "result_id": resolved.result.result_id, "execution_id": resolved.result.execution_id,
            "status": resolved.result.outcome.status})
        return encode_record(resolved.selection)

    @dagster.job(name=DAGSTER_JOB_NAME, resource_defs=dict(resource_defs),
                 executor_def=dagster.multiprocess_executor if executor_def is None else executor_def)
    def docspec_operations():
        emit_operations().map(execute_operation)

    return dagster.Definitions(jobs=[docspec_operations])
