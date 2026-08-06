"""Persistence boundary for immutable DocumentStore revisions."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Protocol

from docspec.domain.jobs import DocumentStore
from docspec.domain.references import LayerRef, StoreRef


class DocumentStoreRepository(Protocol):
    """Persist job revisions and one exact, ordered store population per plan."""

    def save(self, store: DocumentStore) -> StoreRef: ...

    def load(self, reference: StoreRef) -> DocumentStore: ...

    def latest(self, store_id: str) -> StoreRef | None: ...

    def revisions(self, store_id: str) -> tuple[StoreRef, ...]: ...

    def has_planned_store_ledger(self, plan_id: str) -> bool:
        """Return whether durable planning state exists for the plan.

        An existing but invalid path must fail closed rather than look absent.
        """

        ...

    def seal_planned_stores(self, plan_id: str, references: Iterable[StoreRef]) -> LayerRef:
        """Stream, verify, and immutably seal the complete planned population."""

        ...

    def planned_store_ledger(self, plan_id: str) -> LayerRef:
        """Open and verify the one sealed planned-store ledger for a plan."""

        ...

    def verify_planned_store_ledger(self, reference: LayerRef) -> None: ...

    def stream_planned_stores(self, reference: LayerRef) -> Iterator[StoreRef]: ...
