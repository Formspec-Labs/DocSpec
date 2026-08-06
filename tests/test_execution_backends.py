from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import replace

import pytest

from docspec.adapters.execution import ExternalExecutionBackend, LocalExecutionBackend
from docspec.adapters.storage import LocalJsonControlRepository
from docspec.domain.execution import (
    EXECUTE_AND_DELIVER_OPERATION_ID,
    MAX_TASK_BYTES,
    ExecutionHandoff,
    ExecutionLimits,
    ExecutionProfile,
    StoreTask,
    StoreTaskResult,
    summarize_store_tasks,
)
from docspec.domain.identity import canonical_json_file_bytes, sha256_digest
from docspec.domain.references import ArtifactRef, LayerRef, StoreRef
from docspec.errors import IntegrityError, LimitExceededError


def _artifact(name: str, value: object | None = None) -> ArtifactRef:
    payload = canonical_json_file_bytes({"name": name} if value is None else value)
    return ArtifactRef(
        f"urn:docspec:test:{name}",
        f"memory://{name}",
        sha256_digest(payload),
        "application/json",
        len(payload),
    )


def _profile(*, max_in_flight: int = 2, with_cache: bool = False) -> ExecutionProfile:
    return ExecutionProfile(
        "docspec.local-threaded",
        "1.0.0",
        _artifact("worker-composition"),
        _artifact("scheduler-configuration"),
        ExecutionLimits(1, 4, max_in_flight, 1_000_000, 1_000_000, 100, 4, 1, 0, 0),
        2_000_000_000,
        _artifact("cache-profile") if with_cache else None,
        _artifact("cache-state") if with_cache else None,
    )


def _tasks(count: int) -> tuple[StoreTask, ...]:
    return tuple(
        StoreTask(
            "urn:docspec:test:plan",
            EXECUTE_AND_DELIVER_OPERATION_ID,
            StoreRef(
                f"store-{index}",
                0,
                f"memory://store-{index}",
                sha256_digest(str(index).encode()),
            ),
        )
        for index in range(count)
    )


def _handoff(
    profile: ExecutionProfile,
    profile_reference: ArtifactRef,
    tasks: tuple[StoreTask, ...],
) -> ExecutionHandoff:
    count, digest = summarize_store_tasks(tasks)
    plan = _artifact("plan")
    plan = ArtifactRef(
        tasks[0].processing_plan_id if tasks else "urn:docspec:test:plan",
        plan.locator,
        plan.digest,
        plan.media_type,
        plan.byte_size,
    )
    ledger = LayerRef(
        "urn:docspec:test:planned-store-ledger",
        "planned-document-stores",
        "docspec-planned-store-reference/1.0",
        "urn:docspec:test:document-store-profile",
        "memory://planned-store-ledger",
        sha256_digest(b"planned-store-ledger"),
        count,
    )
    return ExecutionHandoff(
        plan,
        profile_reference,
        profile.worker_composition,
        ledger,
        EXECUTE_AND_DELIVER_OPERATION_ID,
        count,
        digest,
        _artifact("result-sink"),
    )


def _persisted_profile(tmp_path, profile: ExecutionProfile):
    controls = LocalJsonControlRepository(tmp_path / "controls")
    worker = controls.put(
        kind="worker-compositions",
        artifact_id=profile.worker_composition.artifact_id,
        value={"name": "worker-composition"},
    )
    scheduler = controls.put(
        kind="scheduler-configurations",
        artifact_id=profile.scheduler_configuration.artifact_id,
        value={"name": "scheduler-configuration"},
    )
    cache_profile = None
    cache_state = None
    if profile.cache_profile is not None:
        cache_profile = controls.put(
            kind="processor-cache-profiles",
            artifact_id=profile.cache_profile.artifact_id,
            value={"name": "cache-profile"},
        )
        assert profile.cache_state is not None
        cache_state = controls.put(
            kind="processor-cache-states",
            artifact_id=profile.cache_state.artifact_id,
            value={"name": "cache-state", "profile": cache_profile.to_dict()},
        )
    profile = replace(
        profile,
        worker_composition=worker,
        scheduler_configuration=scheduler,
        cache_profile=cache_profile,
        cache_state=cache_state,
    )
    reference = controls.put(
        kind="execution-profiles",
        artifact_id=profile.profile_id,
        value=profile.to_dict(),
    )
    return controls, profile, reference


def test_execution_messages_are_closed_canonical_and_idempotent(tmp_path) -> None:
    profile = _profile()
    _, profile, profile_reference = _persisted_profile(tmp_path, profile)
    task = _tasks(1)[0]
    handoff = _handoff(profile, profile_reference, (task,))
    output = StoreRef(task.input_store.store_id, 1, task.input_store.locator, task.input_store.digest)
    result = StoreTaskResult.succeeded(
        handoff_id=handoff.handoff_id,
        task=task,
        output_store=output,
    )

    assert ExecutionProfile.from_bytes(profile.to_bytes()) == profile
    assert StoreTask.from_bytes(task.to_bytes()) == task
    assert ExecutionHandoff.from_bytes(handoff.to_bytes()) == handoff
    assert StoreTaskResult.from_bytes(result.to_bytes()) == result
    assert StoreTaskResult.succeeded(
        handoff_id=handoff.handoff_id,
        task=task,
        output_store=output,
    ).to_bytes() == result.to_bytes()
    tampered = task.to_dict()
    tampered["taskId"] = "urn:docspec:store-task:v1:" + "0" * 64
    with pytest.raises(IntegrityError, match="identity"):
        StoreTask.from_dict(tampered)
    with pytest.raises(IntegrityError, match="canonical"):
        StoreTask.from_bytes(task.to_bytes().rstrip(b"\n"))
    with pytest.raises(IntegrityError, match="serialized limit"):
        StoreTask.from_bytes(b"{" + b"x" * MAX_TASK_BYTES)


def test_local_backend_uses_the_portable_messages_with_bounded_concurrency(tmp_path) -> None:
    profile = _profile(max_in_flight=2)
    controls, profile, profile_reference = _persisted_profile(tmp_path, profile)
    tasks = _tasks(8)
    handoff = _handoff(profile, profile_reference, tasks)
    active = 0
    peak = 0
    lock = threading.Lock()
    two_started = threading.Event()

    def handler(current_handoff: ExecutionHandoff, task: StoreTask) -> StoreTaskResult:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
        assert two_started.wait(1)
        with lock:
            active -= 1
        output = StoreRef(task.input_store.store_id, 1, task.input_store.locator, task.input_store.digest)
        return StoreTaskResult.succeeded(
            handoff_id=current_handoff.handoff_id,
            task=task,
            output_store=output,
        )

    results = tuple(
        LocalExecutionBackend(
            profile,
            handler,
            profile_reference=handoff.execution_profile,
            controls=controls,
            max_workers=4,
        ).execute(handoff, tasks)
    )

    assert {item.task.task_id for item in results} == {item.task_id for item in tasks}
    assert all(item.output_store is not None and item.output_store.revision == 1 for item in results)
    assert peak == 2


class _ReversingDispatcher:
    def __init__(self) -> None:
        self.received: tuple[bytes, ...] = ()

    def dispatch(self, *, handoff: bytes, tasks: Iterable[bytes]) -> Iterable[bytes]:
        restored_handoff = ExecutionHandoff.from_bytes(handoff)
        self.received = tuple(tasks)
        restored_tasks = tuple(StoreTask.from_bytes(item) for item in self.received)
        return tuple(
            StoreTaskResult.succeeded(
                handoff_id=restored_handoff.handoff_id,
                task=task,
                output_store=StoreRef(
                    task.input_store.store_id,
                    1,
                    task.input_store.locator,
                    task.input_store.digest,
                ),
            ).to_bytes()
            for task in reversed(restored_tasks)
        )


def test_external_boundary_delegates_one_serialized_task_stream_without_a_closure(tmp_path) -> None:
    profile = _profile()
    controls, profile, profile_reference = _persisted_profile(tmp_path, profile)
    tasks = _tasks(3)
    handoff = _handoff(profile, profile_reference, tasks)
    dispatcher = _ReversingDispatcher()

    results = tuple(
        ExternalExecutionBackend(
            profile,
            dispatcher,
            profile_reference=profile_reference,
            controls=controls,
        ).execute(handoff, iter(tasks))
    )

    assert tuple(item.task.task_id for item in results) == tuple(item.task_id for item in reversed(tasks))
    assert dispatcher.received == tuple(item.to_bytes() for item in tasks)


def test_backend_rejects_a_task_stream_that_differs_from_the_sealed_handoff(tmp_path) -> None:
    profile = _profile()
    controls, profile, profile_reference = _persisted_profile(tmp_path, profile)
    tasks = _tasks(2)
    handoff = _handoff(profile, profile_reference, tasks)

    def handler(current_handoff: ExecutionHandoff, task: StoreTask) -> StoreTaskResult:
        return StoreTaskResult.succeeded(
            handoff_id=current_handoff.handoff_id,
            task=task,
            output_store=task.input_store,
        )

    with pytest.raises(IntegrityError, match="count"):
        tuple(
            LocalExecutionBackend(
                profile,
                handler,
                profile_reference=handoff.execution_profile,
                controls=controls,
            ).execute(handoff, tasks[:1])
        )


def test_local_backend_rejects_an_expired_profile_before_starting_work(tmp_path) -> None:
    profile = _profile()
    controls, profile, profile_reference = _persisted_profile(tmp_path, profile)
    tasks = _tasks(1)
    handoff = _handoff(profile, profile_reference, tasks)
    called = False

    def handler(current_handoff: ExecutionHandoff, task: StoreTask) -> StoreTaskResult:
        nonlocal called
        called = True
        return StoreTaskResult.succeeded(
            handoff_id=current_handoff.handoff_id,
            task=task,
            output_store=task.input_store,
        )

    with pytest.raises(LimitExceededError, match="deadline"):
        tuple(
            LocalExecutionBackend(
                profile,
                handler,
                profile_reference=handoff.execution_profile,
                controls=controls,
                clock=lambda: 2_000_000_000,
            ).execute(handoff, tasks)
        )

    assert called is False


def test_backend_verifies_the_complete_profile_reference_before_work(tmp_path) -> None:
    profile = _profile()
    controls, profile, profile_reference = _persisted_profile(tmp_path, profile)
    tasks = _tasks(1)
    tampered_reference = replace(profile_reference, digest=sha256_digest(b"different profile bytes"))
    handoff = _handoff(profile, tampered_reference, tasks)
    called = False

    def handler(current_handoff: ExecutionHandoff, task: StoreTask) -> StoreTaskResult:
        nonlocal called
        called = True
        return StoreTaskResult.succeeded(
            handoff_id=current_handoff.handoff_id,
            task=task,
            output_store=task.input_store,
        )

    with pytest.raises(IntegrityError, match="bytes differ"):
        tuple(
            LocalExecutionBackend(
                profile,
                handler,
                profile_reference=tampered_reference,
                controls=controls,
            ).execute(handoff, tasks)
        )
    assert called is False


def test_backend_verifies_nested_cache_state_before_work(tmp_path) -> None:
    profile = _profile(with_cache=True)
    controls, profile, _ = _persisted_profile(tmp_path, profile)
    assert profile.cache_state is not None
    tampered_state = replace(profile.cache_state, digest=sha256_digest(b"different cache state"))
    tampered_profile = replace(profile, cache_state=tampered_state)
    profile_reference = controls.put(
        kind="execution-profiles",
        artifact_id=tampered_profile.profile_id,
        value=tampered_profile.to_dict(),
    )
    tasks = _tasks(1)
    handoff = _handoff(tampered_profile, profile_reference, tasks)
    called = False

    def handler(current_handoff: ExecutionHandoff, task: StoreTask) -> StoreTaskResult:
        nonlocal called
        called = True
        return StoreTaskResult.succeeded(
            handoff_id=current_handoff.handoff_id,
            task=task,
            output_store=task.input_store,
        )

    with pytest.raises(IntegrityError, match="bytes differ"):
        tuple(
            LocalExecutionBackend(
                tampered_profile,
                handler,
                profile_reference=profile_reference,
                controls=controls,
            ).execute(handoff, tasks)
        )
    assert called is False


def test_local_backend_rejects_a_completion_after_its_deadline(tmp_path) -> None:
    profile = _profile()
    controls, profile, profile_reference = _persisted_profile(tmp_path, profile)
    tasks = _tasks(1)
    handoff = _handoff(profile, profile_reference, tasks)
    times = iter((0, 0, profile.deadline_epoch_seconds))

    def handler(current_handoff: ExecutionHandoff, task: StoreTask) -> StoreTaskResult:
        return StoreTaskResult.succeeded(
            handoff_id=current_handoff.handoff_id,
            task=task,
            output_store=task.input_store,
        )

    with pytest.raises(LimitExceededError, match="deadline"):
        tuple(
            LocalExecutionBackend(
                profile,
                handler,
                profile_reference=profile_reference,
                controls=controls,
                clock=lambda: next(times),
            ).execute(handoff, tasks)
        )


class _ReplayingDispatcher:
    def dispatch(self, *, handoff: bytes, tasks: Iterable[bytes]) -> Iterable[bytes]:
        restored_handoff = ExecutionHandoff.from_bytes(handoff)
        for payload in tasks:
            task = StoreTask.from_bytes(payload)
            result = StoreTaskResult.succeeded(
                handoff_id=restored_handoff.handoff_id,
                task=task,
                output_store=task.input_store,
            ).to_bytes()
            yield result
            yield result


def test_external_backend_bounds_replayed_results_by_the_sealed_attempt_limit(tmp_path) -> None:
    profile = _profile()
    controls, profile, profile_reference = _persisted_profile(tmp_path, profile)
    tasks = _tasks(1)
    handoff = _handoff(profile, profile_reference, tasks)

    with pytest.raises(LimitExceededError, match="result stream"):
        tuple(
            ExternalExecutionBackend(
                profile,
                _ReplayingDispatcher(),
                profile_reference=profile_reference,
                controls=controls,
            ).execute(handoff, tasks)
        )
