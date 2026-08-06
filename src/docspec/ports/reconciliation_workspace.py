"""Ephemeral, bounded workspace for assembling one reconciled run."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from docspec.ports.record_workspace import RecordWorkspace


class ReconciliationWorkspace(RecordWorkspace, Protocol):
    """Spool logical rows without making the workspace an authority."""

    def mark_affected(self, source_item_id: str) -> None: ...

    def is_affected(self, source_item_id: str) -> bool: ...

    def retain_records(
        self,
        collection: str,
        records: Iterable[Mapping[str, Any]],
        *,
        identity_field: str,
        source_item_field: str,
    ) -> None: ...

class ReconciliationWorkspaceFactory(Protocol):
    """Create a fresh workspace for each reconciliation attempt."""

    def create(self) -> ReconciliationWorkspace: ...


__all__ = ["ReconciliationWorkspace", "ReconciliationWorkspaceFactory"]
