"""Small bounded local runner for portable store tasks."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from docspec.domain.execution import ExecutionHandoff, ExecutionProfile, StoreTask, StoreTaskResult
from docspec.domain.identity import OrderedJsonSequenceDigester
from docspec.domain.references import ArtifactRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.control_repository import ControlRepository
from docspec.ports.execution_backend import StoreTaskHandler


class _ExecutionProfileBinding:
    """Resolve one sealed profile through its configured control repository."""

    def __init__(
        self,
        profile: ExecutionProfile,
        profile_reference: ArtifactRef,
        controls: ControlRepository,
        clock: Callable[[], float],
    ) -> None:
        self.profile = profile
        self.reference = profile_reference
        self._controls = controls
        self._clock = clock

    def require_handoff(self, handoff: ExecutionHandoff) -> None:
        if handoff.execution_profile != self.reference:
            raise IntegrityError("execution handoff does not pin the configured execution profile")
        try:
            resolved = ExecutionProfile.from_dict(self._controls.load(self.reference))
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"execution profile reference is invalid: {error}") from error
        if resolved != self.profile:
            raise IntegrityError("execution profile reference resolves to different profile content")
        self._controls.verify(resolved.worker_composition)
        if handoff.worker_composition != self.profile.worker_composition:
            raise IntegrityError("execution handoff and profile name different worker compositions")
        self.require_active_deadline()

    def require_active_deadline(self) -> None:
        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise TypeError("execution clock must return epoch seconds")
        if now >= self.profile.deadline_epoch_seconds:
            raise LimitExceededError("execution profile deadline has expired")


class _TaskStreamVerifier:
    def __init__(self, handoff: ExecutionHandoff) -> None:
        self._handoff = handoff
        self._count = 0
        self._digest = OrderedJsonSequenceDigester()
        self._finished = False

    def accept(self, task: StoreTask) -> StoreTask:
        if self._finished:
            raise IntegrityError("store task stream is already complete")
        if task.processing_plan_id != self._handoff.processing_plan.artifact_id:
            raise IntegrityError("store task names a different processing plan")
        if task.operation_id != self._handoff.operation_id:
            raise IntegrityError("store task names a different execution operation")
        if self._count >= self._handoff.expected_task_count:
            raise IntegrityError("store task stream exceeds the expected task count")
        self._digest.accept(task.to_dict())
        self._count += 1
        return task

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        actual = self._digest.finish()
        if self._count != self._handoff.expected_task_count:
            raise IntegrityError("store task stream count differs from the sealed handoff")
        if actual != self._handoff.task_set_digest:
            raise IntegrityError("store task stream digest differs from the sealed handoff")


def _validate_result(
    result: StoreTaskResult,
    handoff: ExecutionHandoff,
    task: StoreTask | None = None,
) -> None:
    if not isinstance(result, StoreTaskResult):
        raise TypeError("a store task handler must return StoreTaskResult")
    if result.handoff_id != handoff.handoff_id:
        raise IntegrityError("store task result names a different execution handoff")
    if result.task.processing_plan_id != handoff.processing_plan.artifact_id:
        raise IntegrityError("store task result names a different processing plan")
    if task is not None and result.task != task:
        raise IntegrityError("store task handler returned a result for a different task")


class LocalExecutionBackend:
    """Run the portable task shape locally with bounded completion-order streaming."""

    def __init__(
        self,
        profile: ExecutionProfile,
        handler: StoreTaskHandler,
        *,
        profile_reference: ArtifactRef,
        controls: ControlRepository,
        max_workers: int = 1,
        max_in_flight: int = 1,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._binding = _ExecutionProfileBinding(profile, profile_reference, controls, clock)
        self._handler = handler
        self._max_workers = max_workers
        self._max_in_flight = max_in_flight
        if type(self._max_workers) is not int or self._max_workers <= 0:
            raise ValueError("max_workers must be a positive integer")
        if type(max_in_flight) is not int or max_in_flight <= 0:
            raise ValueError("max_in_flight must be a positive integer")

    def execute(
        self,
        handoff: ExecutionHandoff,
        tasks: Iterable[StoreTask],
    ) -> Iterator[StoreTaskResult]:
        self._binding.require_handoff(handoff)
        capacity = min(self._max_workers, self._max_in_flight)
        source = iter(tasks)
        verifier = _TaskStreamVerifier(handoff)
        pending: dict[Future[StoreTaskResult], StoreTask] = {}
        exhausted = False
        with ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="docspec-store") as executor:
            while pending or not exhausted:
                while not exhausted and len(pending) < capacity:
                    self._binding.require_active_deadline()
                    try:
                        task = verifier.accept(next(source))
                    except StopIteration:
                        exhausted = True
                    else:
                        pending[executor.submit(self._handler, handoff, task)] = task
                if not pending:
                    continue
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    task = pending.pop(future)
                    result = future.result()
                    self._binding.require_active_deadline()
                    _validate_result(result, handoff, task)
                    yield result
        verifier.finish()
