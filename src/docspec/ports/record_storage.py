"""Format-neutral storage for partitioned logical record layers."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Protocol

from docspec.domain.references import LayerRef
from docspec.domain.storage import PartitionPolicy, RecordSchema


class RecordStorage(Protocol):
    """Write, verify, and stream immutable logical layers."""

    def write_layer(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        layer_kind: str,
        schema: RecordSchema,
        partition_policy: PartitionPolicy,
        base: LayerRef | None = None,
        replace_partitions: frozenset[int] | None = None,
    ) -> LayerRef: ...

    def verify(self, reference: LayerRef) -> None: ...

    def stream(
        self,
        reference: LayerRef,
        *,
        partitions: frozenset[int] | None = None,
    ) -> Iterator[dict[str, Any]]: ...

    def scan_partition_value(
        self,
        reference: LayerRef,
        partition_value: str,
    ) -> Iterator[dict[str, Any]]: ...

    def lookup(
        self,
        reference: LayerRef,
        record_id: str,
        *,
        partition_value: str | None = None,
    ) -> dict[str, Any] | None: ...

    def identity_field(self, reference: LayerRef) -> str: ...

    def partition_policy(self, reference: LayerRef) -> PartitionPolicy: ...


__all__ = ["PartitionPolicy", "RecordSchema", "RecordStorage"]
