"""Portable execution profiles and small scheduler messages."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

from docspec.domain.identity import (
    canonical_json_file_bytes,
    ordered_json_sequence_digest,
    parse_canonical_json,
    require_sha256,
    require_text,
    sha256_digest,
    stable_urn,
    thaw_json,
)
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, LayerRef, StoreRef
from docspec.errors import IntegrityError, ProfileError

PROFILE_FORMAT = "docspec-execution-profile"
HANDOFF_FORMAT = "docspec-execution-handoff"
TASK_FORMAT = "docspec-store-task"
RESULT_FORMAT = "docspec-store-task-result"
FORMAT_VERSION = "1.0"
EXECUTE_AND_DELIVER_OPERATION_ID = "execute-and-deliver-store/v1"
MAX_PROFILE_BYTES = 32 * 1024
MAX_HANDOFF_BYTES = 64 * 1024
MAX_TASK_BYTES = 16 * 1024
MAX_RESULT_BYTES = 32 * 1024


def _integer(value: object, label: str, *, positive: bool = True) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or value < minimum:
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{label} must be a {qualifier} integer")
    return value


def _bounded(value: dict[str, Any], maximum: int, label: str) -> None:
    if len(canonical_json_file_bytes(value)) > maximum:
        raise ValueError(f"{label} exceeds its {maximum}-byte serialized limit")


def _parse(data: bytes, maximum: int, label: str) -> dict[str, Any]:
    if not isinstance(data, bytes):
        raise TypeError(f"{label} must be canonical JSON bytes")
    if len(data) > maximum:
        raise IntegrityError(f"{label} exceeds its {maximum}-byte serialized limit")
    value = thaw_json(parse_canonical_json(data, label=label))
    if not isinstance(value, dict):
        raise IntegrityError(f"{label} must be a JSON object")
    return value


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    """Operational bounds owned by an execution profile, not a processing plan."""

    worker_count: int
    max_concurrency_per_worker: int
    max_in_flight: int
    max_scratch_bytes_per_worker: int
    max_network_bytes_per_task: int
    request_rate_limit_per_second: int
    max_provider_concurrency: int
    max_task_attempts: int
    retry_initial_delay_milliseconds: int
    retry_max_delay_milliseconds: int

    def __post_init__(self) -> None:
        for label, value in (
            ("worker_count", self.worker_count),
            ("max_concurrency_per_worker", self.max_concurrency_per_worker),
            ("max_in_flight", self.max_in_flight),
            ("max_scratch_bytes_per_worker", self.max_scratch_bytes_per_worker),
            ("max_network_bytes_per_task", self.max_network_bytes_per_task),
            ("request_rate_limit_per_second", self.request_rate_limit_per_second),
            ("max_provider_concurrency", self.max_provider_concurrency),
            ("max_task_attempts", self.max_task_attempts),
        ):
            _integer(value, label)
        _integer(
            self.retry_initial_delay_milliseconds,
            "retry_initial_delay_milliseconds",
            positive=False,
        )
        _integer(
            self.retry_max_delay_milliseconds,
            "retry_max_delay_milliseconds",
            positive=False,
        )
        if self.retry_max_delay_milliseconds < self.retry_initial_delay_milliseconds:
            raise ProfileError("maximum retry delay must not be less than the initial retry delay")

    def to_dict(self) -> dict[str, int]:
        return {
            "workerCount": self.worker_count,
            "maxConcurrencyPerWorker": self.max_concurrency_per_worker,
            "maxInFlight": self.max_in_flight,
            "maxScratchBytesPerWorker": self.max_scratch_bytes_per_worker,
            "maxNetworkBytesPerTask": self.max_network_bytes_per_task,
            "requestRateLimitPerSecond": self.request_rate_limit_per_second,
            "maxProviderConcurrency": self.max_provider_concurrency,
            "maxTaskAttempts": self.max_task_attempts,
            "retryInitialDelayMilliseconds": self.retry_initial_delay_milliseconds,
            "retryMaxDelayMilliseconds": self.retry_max_delay_milliseconds,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        expected = {
            "workerCount",
            "maxConcurrencyPerWorker",
            "maxInFlight",
            "maxScratchBytesPerWorker",
            "maxNetworkBytesPerTask",
            "requestRateLimitPerSecond",
            "maxProviderConcurrency",
            "maxTaskAttempts",
            "retryInitialDelayMilliseconds",
            "retryMaxDelayMilliseconds",
        }
        if set(value) != expected:
            raise ProfileError("execution limits have an invalid closed shape")
        return cls(
            value["workerCount"],
            value["maxConcurrencyPerWorker"],
            value["maxInFlight"],
            value["maxScratchBytesPerWorker"],
            value["maxNetworkBytesPerTask"],
            value["requestRateLimitPerSecond"],
            value["maxProviderConcurrency"],
            value["maxTaskAttempts"],
            value["retryInitialDelayMilliseconds"],
            value["retryMaxDelayMilliseconds"],
        )


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    """Sealed operational choices for one scheduler or local runner."""

    adapter_id: str
    adapter_version: str
    worker_composition: ArtifactRef
    scheduler_configuration: ArtifactRef
    limits: ExecutionLimits
    deadline_epoch_seconds: int
    cache_profile: ArtifactRef | None = None
    cache_state: ArtifactRef | None = None

    def __post_init__(self) -> None:
        require_text(self.adapter_id, "execution adapter_id")
        require_text(self.adapter_version, "execution adapter_version")
        _integer(self.deadline_epoch_seconds, "deadline_epoch_seconds")
        if (self.cache_profile is None) != (self.cache_state is None):
            raise ProfileError("cache profile and initial cache state must both be present or both be absent")
        _bounded(self.to_dict(), MAX_PROFILE_BYTES, "execution profile")

    def identity_content(self) -> dict[str, Any]:
        return {
            "adapterId": self.adapter_id,
            "adapterVersion": self.adapter_version,
            "workerComposition": self.worker_composition.to_dict(),
            "schedulerConfiguration": self.scheduler_configuration.to_dict(),
            "limits": self.limits.to_dict(),
            "deadlineEpochSeconds": self.deadline_epoch_seconds,
            "cacheProfile": None if self.cache_profile is None else self.cache_profile.to_dict(),
            "cacheState": None if self.cache_state is None else self.cache_state.to_dict(),
        }

    @property
    def control_artifacts(self) -> tuple[ArtifactRef, ...]:
        """List every immutable control artifact required to execute this profile."""

        optional = () if self.cache_profile is None else (self.cache_profile, self.cache_state)
        return (self.worker_composition, self.scheduler_configuration, *optional)

    @property
    def profile_id(self) -> str:
        return stable_urn("execution-profile", self.identity_content())

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": PROFILE_FORMAT,
            "formatVersion": FORMAT_VERSION,
            "profileId": self.profile_id,
            **self.identity_content(),
        }

    def to_bytes(self) -> bytes:
        return canonical_json_file_bytes(self.to_dict())

    def artifact_ref(self, *, locator: str) -> ArtifactRef:
        payload = self.to_bytes()
        return ArtifactRef(
            self.profile_id,
            locator,
            sha256_digest(payload),
            "application/json",
            len(payload),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        expected = {
            "format",
            "formatVersion",
            "profileId",
            "adapterId",
            "adapterVersion",
            "workerComposition",
            "schedulerConfiguration",
            "limits",
            "deadlineEpochSeconds",
            "cacheProfile",
            "cacheState",
        }
        if set(value) != expected or value["format"] != PROFILE_FORMAT or value["formatVersion"] != FORMAT_VERSION:
            raise ProfileError("execution profile has an unknown format or invalid closed shape")
        result = cls(
            value["adapterId"],
            value["adapterVersion"],
            ArtifactRef.from_dict(value["workerComposition"]),
            ArtifactRef.from_dict(value["schedulerConfiguration"]),
            ExecutionLimits.from_dict(value["limits"]),
            value["deadlineEpochSeconds"],
            None if value["cacheProfile"] is None else ArtifactRef.from_dict(value["cacheProfile"]),
            None if value["cacheState"] is None else ArtifactRef.from_dict(value["cacheState"]),
        )
        if value["profileId"] != result.profile_id:
            raise ProfileError("execution profile identity differs")
        return result

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        return cls.from_dict(_parse(data, MAX_PROFILE_BYTES, "execution profile"))


@dataclass(frozen=True, slots=True)
class StoreTask:
    """One idempotent operation over a saved DocumentStore reference."""

    processing_plan_id: str
    operation_id: str
    input_store: StoreRef

    def __post_init__(self) -> None:
        require_text(self.processing_plan_id, "processing_plan_id")
        require_text(self.operation_id, "operation_id")
        _bounded(self.to_dict(), MAX_TASK_BYTES, "store task")

    def identity_content(self) -> dict[str, Any]:
        return {
            "processingPlanId": self.processing_plan_id,
            "operationId": self.operation_id,
            "inputStore": self.input_store.to_dict(),
        }

    @property
    def task_id(self) -> str:
        return stable_urn("store-task", self.identity_content())

    @property
    def idempotency_key(self) -> str:
        return stable_urn("store-task-idempotency", self.identity_content())

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": TASK_FORMAT,
            "formatVersion": FORMAT_VERSION,
            "taskId": self.task_id,
            "idempotencyKey": self.idempotency_key,
            **self.identity_content(),
        }

    def to_bytes(self) -> bytes:
        return canonical_json_file_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        expected = {
            "format",
            "formatVersion",
            "taskId",
            "idempotencyKey",
            "processingPlanId",
            "operationId",
            "inputStore",
        }
        if set(value) != expected or value["format"] != TASK_FORMAT or value["formatVersion"] != FORMAT_VERSION:
            raise IntegrityError("store task has an unknown format or invalid closed shape")
        result = cls(
            value["processingPlanId"],
            value["operationId"],
            StoreRef.from_dict(value["inputStore"]),
        )
        if value["taskId"] != result.task_id or value["idempotencyKey"] != result.idempotency_key:
            raise IntegrityError("store task identity or idempotency key differs")
        return result

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        return cls.from_dict(_parse(data, MAX_TASK_BYTES, "store task"))


def summarize_store_tasks(tasks: Iterable[StoreTask]) -> tuple[int, str]:
    """Digest a task stream without retaining its members."""

    count = 0

    def rows() -> Iterable[dict[str, Any]]:
        nonlocal count
        for task in tasks:
            count += 1
            yield task.to_dict()

    digest = ordered_json_sequence_digest(rows())
    return count, digest


def iter_store_tasks(
    processing_plan_id: str,
    operation_id: str,
    stores: Iterable[StoreRef],
) -> Iterator[StoreTask]:
    """Map a saved store-reference stream to the one portable task shape."""

    require_text(processing_plan_id, "processing_plan_id")
    require_text(operation_id, "operation_id")
    for reference in stores:
        yield StoreTask(processing_plan_id, operation_id, reference)


class StoreTaskStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StoreTaskResult:
    """One terminal reference-only scheduler result."""

    handoff_id: str
    task: StoreTask
    status: StoreTaskStatus
    output_store: StoreRef | None = None
    failure: ArtifactRef | None = None

    def __post_init__(self) -> None:
        require_text(self.handoff_id, "handoff_id")
        try:
            status = StoreTaskStatus(self.status)
        except (TypeError, ValueError) as error:
            raise IntegrityError("store task result status is not registered") from error
        object.__setattr__(self, "status", status)
        if status is StoreTaskStatus.SUCCEEDED:
            if self.output_store is None or self.failure is not None:
                raise IntegrityError("a succeeded task requires only an output store")
            if self.output_store.store_id != self.task.input_store.store_id:
                raise IntegrityError("store task result changed the store identity")
            if self.output_store.revision < self.task.input_store.revision:
                raise IntegrityError("store task result moved the store revision backwards")
        elif self.output_store is not None or self.failure is None:
            raise IntegrityError("a failed task requires only a persisted failure reference")
        _bounded(self.to_dict(), MAX_RESULT_BYTES, "store task result")

    @classmethod
    def succeeded(cls, *, handoff_id: str, task: StoreTask, output_store: StoreRef) -> Self:
        return cls(handoff_id, task, StoreTaskStatus.SUCCEEDED, output_store=output_store)

    @classmethod
    def failed(cls, *, handoff_id: str, task: StoreTask, failure: ArtifactRef) -> Self:
        return cls(handoff_id, task, StoreTaskStatus.FAILED, failure=failure)

    def identity_content(self) -> dict[str, Any]:
        return {
            "handoffId": self.handoff_id,
            "task": self.task.to_dict(),
            "status": self.status.value,
            "outputStore": None if self.output_store is None else self.output_store.to_dict(),
            "failure": None if self.failure is None else self.failure.to_dict(),
        }

    @property
    def result_id(self) -> str:
        return stable_urn("store-task-result", self.identity_content())

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": RESULT_FORMAT,
            "formatVersion": FORMAT_VERSION,
            "resultId": self.result_id,
            **self.identity_content(),
        }

    def to_bytes(self) -> bytes:
        return canonical_json_file_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        expected = {
            "format",
            "formatVersion",
            "resultId",
            "handoffId",
            "task",
            "status",
            "outputStore",
            "failure",
        }
        if set(value) != expected or value["format"] != RESULT_FORMAT or value["formatVersion"] != FORMAT_VERSION:
            raise IntegrityError("store task result has an unknown format or invalid closed shape")
        try:
            status = StoreTaskStatus(value["status"])
        except (TypeError, ValueError) as error:
            raise IntegrityError("store task result status is not registered") from error
        result = cls(
            value["handoffId"],
            StoreTask.from_dict(value["task"]),
            status,
            None if value["outputStore"] is None else StoreRef.from_dict(value["outputStore"]),
            None if value["failure"] is None else ArtifactRef.from_dict(value["failure"]),
        )
        if value["resultId"] != result.result_id:
            raise IntegrityError("store task result identity differs")
        return result

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        return cls.from_dict(_parse(data, MAX_RESULT_BYTES, "store task result"))


@dataclass(frozen=True, slots=True)
class ExecutionHandoff:
    """One sealed root for a bounded task ledger and its execution inputs."""

    processing_plan: ArtifactRef
    execution_profile: ArtifactRef
    worker_composition: ArtifactRef
    planned_store_ledger: LayerRef
    operation_id: str
    expected_task_count: int
    task_set_digest: str
    result_sink: ArtifactRef
    base_release: DocumentReleaseRef | None = None
    task_schema_version: str = FORMAT_VERSION
    result_schema_version: str = FORMAT_VERSION

    def __post_init__(self) -> None:
        require_text(self.operation_id, "execution operation_id")
        _integer(self.expected_task_count, "expected_task_count", positive=False)
        require_sha256(self.task_set_digest, "task set digest")
        if self.planned_store_ledger.layer_kind != "planned-document-stores":
            raise IntegrityError("execution handoff names an unexpected planned-store ledger kind")
        if self.planned_store_ledger.record_count != self.expected_task_count:
            raise IntegrityError("execution handoff task count differs from its planned-store ledger")
        if self.task_schema_version != FORMAT_VERSION or self.result_schema_version != FORMAT_VERSION:
            raise IntegrityError("execution handoff uses an unknown task or result schema version")
        _bounded(self.to_dict(), MAX_HANDOFF_BYTES, "execution handoff")

    def identity_content(self) -> dict[str, Any]:
        return {
            "processingPlan": self.processing_plan.to_dict(),
            "executionProfile": self.execution_profile.to_dict(),
            "workerComposition": self.worker_composition.to_dict(),
            "plannedStoreLedger": self.planned_store_ledger.to_dict(),
            "operationId": self.operation_id,
            "expectedTaskCount": self.expected_task_count,
            "taskSetDigest": self.task_set_digest,
            "resultSink": self.result_sink.to_dict(),
            "baseRelease": None if self.base_release is None else self.base_release.to_dict(),
            "taskSchemaVersion": self.task_schema_version,
            "resultSchemaVersion": self.result_schema_version,
        }

    @property
    def handoff_id(self) -> str:
        return stable_urn("execution-handoff", self.identity_content())

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": HANDOFF_FORMAT,
            "formatVersion": FORMAT_VERSION,
            "handoffId": self.handoff_id,
            **self.identity_content(),
        }

    def to_bytes(self) -> bytes:
        return canonical_json_file_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        expected = {
            "format",
            "formatVersion",
            "handoffId",
            "processingPlan",
            "executionProfile",
            "workerComposition",
            "plannedStoreLedger",
            "operationId",
            "expectedTaskCount",
            "taskSetDigest",
            "resultSink",
            "baseRelease",
            "taskSchemaVersion",
            "resultSchemaVersion",
        }
        if set(value) != expected or value["format"] != HANDOFF_FORMAT or value["formatVersion"] != FORMAT_VERSION:
            raise IntegrityError("execution handoff has an unknown format or invalid closed shape")
        result = cls(
            ArtifactRef.from_dict(value["processingPlan"]),
            ArtifactRef.from_dict(value["executionProfile"]),
            ArtifactRef.from_dict(value["workerComposition"]),
            LayerRef.from_dict(value["plannedStoreLedger"]),
            value["operationId"],
            value["expectedTaskCount"],
            value["taskSetDigest"],
            ArtifactRef.from_dict(value["resultSink"]),
            None if value["baseRelease"] is None else DocumentReleaseRef.from_dict(value["baseRelease"]),
            value["taskSchemaVersion"],
            value["resultSchemaVersion"],
        )
        if value["handoffId"] != result.handoff_id:
            raise IntegrityError("execution handoff identity differs")
        return result

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        return cls.from_dict(_parse(data, MAX_HANDOFF_BYTES, "execution handoff"))
