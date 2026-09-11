"""Shared checkpoints fixtures, extracted from tests.test_stage_checkpoint_recovery."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from docspec.adapters.storage import (
    LocalDocumentStoreRepository,
)
from docspec.domain.jobs import DocumentStore
from docspec.domain.references import StoreRef


class _WorkerInterrupted(RuntimeError):
    pass


class _InterruptAfterStageRepository:
    """Persist a selected partial revision, then model abrupt worker loss."""

    def __init__(
        self,
        delegate: LocalDocumentStoreRepository,
        predicate: Callable[[DocumentStore], bool],
    ) -> None:
        self._delegate = delegate
        self._predicate = predicate
        self._armed = True

    def save(self, store: DocumentStore) -> StoreRef:
        reference = self._delegate.save(store)
        if self._armed and self._predicate(store):
            self._armed = False
            raise _WorkerInterrupted("worker disappeared after the durable stage checkpoint")
        return reference

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)
