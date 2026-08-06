"""Ephemeral bounded record spooling shared by coordinator operations."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import TracebackType
from typing import Any, Protocol, Self


class RecordWorkspace(Protocol):
    """Spool canonical records without making scratch storage authoritative."""

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def add_record(
        self,
        collection: str,
        *,
        identity: str,
        source_item_id: str,
        record: Mapping[str, Any],
    ) -> None: ...

    def stream_records(self, collection: str) -> Iterator[dict[str, Any]]: ...

    def lookup_record(self, collection: str, identity: str) -> dict[str, Any] | None: ...


class RecordWorkspaceFactory(Protocol):
    """Create one fresh bounded spool per coordinator operation."""

    def create(self) -> RecordWorkspace: ...


__all__ = ["RecordWorkspace", "RecordWorkspaceFactory"]
