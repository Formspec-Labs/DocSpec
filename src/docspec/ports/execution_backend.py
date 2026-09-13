"""Scheduler-neutral execution of bounded, serialized store tasks."""

from __future__ import annotations

from typing import Protocol

from docspec.domain.execution import ExecutionHandoff, StoreTask, StoreTaskResult


class StoreTaskHandler(Protocol):
    def __call__(self, handoff: ExecutionHandoff, task: StoreTask, /) -> StoreTaskResult: ...
