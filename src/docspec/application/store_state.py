"""Shared recovery helpers for immutable DocumentStore revisions."""

from __future__ import annotations

from docspec.domain.jobs import DocumentStore
from docspec.domain.references import StoreRef
from docspec.ports.document_store_repository import DocumentStoreRepository


def load_latest_store(
    repository: DocumentStoreRepository,
    requested: StoreRef,
) -> tuple[StoreRef, DocumentStore]:
    """Load a requested store, preferring a newer verified durable revision."""

    requested_store = repository.load(requested)
    latest = repository.latest(requested.store_id)
    if latest is None or latest.revision <= requested.revision:
        return requested, requested_store
    return latest, repository.load(latest)
