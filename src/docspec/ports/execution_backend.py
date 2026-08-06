"""Scheduler-neutral execution of bounded, serialized store tasks."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Protocol

from docspec.domain.execution import ExecutionHandoff, StoreTask, StoreTaskResult


class StoreTaskHandler(Protocol):
    def __call__(self, handoff: ExecutionHandoff, task: StoreTask, /) -> StoreTaskResult: ...


class SerializedTaskDispatcher(Protocol):
    def dispatch(self, *, handoff: bytes, tasks: Iterable[bytes]) -> Iterable[bytes]: ...


class ExecutionBackend(Protocol):
    def execute(
        self,
        handoff: ExecutionHandoff,
        tasks: Iterable[StoreTask],
    ) -> Iterator[StoreTaskResult]: ...
