"""Format-neutral storage for partitioned logical record layers and typed table layers."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import duckdb
    import pyarrow as pa

from docspec.domain.references import BlobRef, LayerRef
from docspec.domain.streams import owned_iterator
from docspec.errors import IntegrityError, LimitExceededError
from docspec.domain.storage import PartitionPolicy, RecordSchema, TableSchema


BATCH_ROWS = 2048
BATCH_BYTES = 8 * 1024**2


class AdmittedRecordLayer(Protocol):
    """Checked immutable records retained for an operation's protected lifetime."""

    reference: LayerRef
    schema: RecordSchema
    partition_policy: PartitionPolicy

    def batches(self, *, partitions: frozenset[int] | None = None) -> Iterator[pa.RecordBatch]: ...

    def relation(self, *, partitions: frozenset[int] | None = None) -> AbstractContextManager[duckdb.DuckDBPyRelation]: ...


class AdmittedTableLayer(Protocol):
    """A checked immutable typed table: its own columns, with no routing columns or encoded payload."""

    reference: LayerRef
    schema: TableSchema

    @property
    def member_digest(self) -> str | None:
        """The table digest of the producer members a registered table holds unchanged; None for DocSpec's own writes."""
        ...

    def relation(self) -> AbstractContextManager[duckdb.DuckDBPyRelation]: ...


class RecordStorage(Protocol):
    """Write, verify, and stream immutable logical layers.

    Encoded-record layers and typed table layers share admission, file-level
    verification, physical references and deletion; each profile's own
    reads and writes refuse the other's layers.
    """

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

    def write_batches(
        self, batches: Iterable[pa.RecordBatch], *, layer_kind: str, schema: RecordSchema,
        partition_policy: PartitionPolicy, base: LayerRef | None = None,
        replace_partitions: frozenset[int] | None = None,
    ) -> LayerRef:
        """Retain admitted canonical bytes; callers own payload/routing admission.

        Batches have record_identity/string, partition_value/string and
        record_json/binary columns, plus any typed columns declared by the
        schema. External logical rows use write_layer.
        """
        ...

    def write_table(
        self, batches: Iterable[pa.RecordBatch], *, layer_kind: str, schema: TableSchema,
        sort_by: tuple[str, ...] = (),
    ) -> AdmittedTableLayer:
        """Write typed rows natively into a new table layer, each file sorted by ``sort_by``."""
        ...

    def append_table(
        self, base: LayerRef | AdmittedTableLayer, batches: Iterable[pa.RecordBatch], *,
        sort_by: tuple[str, ...] = (),
    ) -> AdmittedTableLayer:
        """Add rows as new files sharing every base file; a registered producer table refuses."""
        ...

    def register_parquet(
        self, members: Iterable[tuple[Path, str]], *, layer_kind: str, schema: TableSchema,
    ) -> AdmittedTableLayer:
        """Retain a producer table's Parquet members, ``(path, member digest)`` pairs, by reference as one table.

        Each member is unrewritten and sealed under its own digest; the layer records their table digest.
        """
        ...

    def admit(self, reference: LayerRef) -> AdmittedRecordLayer | AdmittedTableLayer: ...

    def admission_scope(self) -> AbstractContextManager[None]:
        """Bound admission reuse to a caller-owned scope preventing cleanup."""
        ...

    def admitted(self, reference: LayerRef) -> AdmittedRecordLayer | AdmittedTableLayer:
        """Reuse the protected scope's admission or freshly check availability."""
        ...

    def retain_batches(
        self, batches: Iterable[pa.RecordBatch], *, layer_kind: str, schema: RecordSchema,
        partition_policy: PartitionPolicy, base: AdmittedRecordLayer | None = None,
        replace_partitions: frozenset[int] | None = None,
        ordered: bool = True,
    ) -> AdmittedRecordLayer:
        """Write admitted bytes and reuse that proof for native reads in this scope."""
        ...

    def retain_relation(
        self, rows: duckdb.DuckDBPyRelation, *, cursor: duckdb.DuckDBPyConnection, layer_kind: str,
        schema: RecordSchema, partition_policy: PartitionPolicy,
    ) -> AdmittedRecordLayer:
        """Write encoded records natively from a relation on one of this store's cursors."""
        ...

    def verify(self, reference: LayerRef) -> None: ...

    def verify_members(self, reference: LayerRef) -> None:
        """Freshly check pinned physical files before a group of logical reads.

        Reads check the root and consumed rows. They do not repeat full-file
        hashes on every query. Callers admit immutable members once per owned
        reader lifetime; ``verify`` always performs a fresh complete audit.
        """
        ...

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

    def lookup_batches(self, reference: LayerRef, record_ids: Iterable[str]) -> Iterator[pa.RecordBatch]:
        """Select admitted canonical rows in native batches; omit missing keys."""
        ...

    def relations(self, references: Mapping[str, LayerRef], *, partitions=None, tables=None) -> AbstractContextManager[Mapping[str, duckdb.DuckDBPyRelation]]: ...

    def available(self, reference: LayerRef) -> AdmittedRecordLayer | AdmittedTableLayer: ...

    def physical_references(self, reference: LayerRef) -> Iterator[BlobRef]: ...

    def delete(self, reference: BlobRef) -> bool: ...

    def apply_changes(self, base: AdmittedRecordLayer, batches: Iterable[pa.RecordBatch]) -> AdmittedRecordLayer:
        """Upsert changed identities; null record_json removes an identity."""
        ...

    def union_disjoint(self, base: AdmittedRecordLayer, changes: AdmittedRecordLayer, *, exclude_existing=False) -> AdmittedRecordLayer:
        """Append disjoint logical identities while preserving the base snapshot."""
        ...

    def compact(self, base: AdmittedRecordLayer) -> AdmittedRecordLayer:
        """Repack immutable bytes and verify exact logical row equivalence."""
        ...

    def identity_field(self, reference: LayerRef) -> str: ...

    def schema(self, reference: LayerRef) -> RecordSchema | TableSchema: ...

    def partition_policy(self, reference: LayerRef) -> PartitionPolicy: ...


__all__ = ["PartitionPolicy", "RecordSchema", "RecordStorage", "TableSchema"]


def bounded_batches(
    batches: Iterable[pa.RecordBatch], *, byte_column: str | tuple[str, ...],
    max_value_bytes: int = BATCH_BYTES, allow_null: bool = False,
) -> Iterator[pa.RecordBatch]:
    """Slice native buffers by payload bytes and rows; never copy payloads to Python.

    A native read window may precede these slices. Only bounded slices cross
    the application boundary, and no additional batches are queued here.
    """
    import pyarrow.compute as pc

    with owned_iterator(batches) as source:
        for batch in source:
            for offset in range(0, batch.num_rows, BATCH_ROWS):
                window = batch.slice(offset, BATCH_ROWS)
                columns = (byte_column,) if isinstance(byte_column, str) else byte_column
                lengths = pc.binary_length(window.column(columns[0])).cast("int64")
                if allow_null:
                    lengths = pc.fill_null(lengths, 0)
                for name in columns[1:]:
                    lengths = pc.add(lengths, pc.fill_null(pc.binary_length(window.column(name)), 0))
                lengths = lengths.to_pylist()
                start = size = 0
                for index, length in enumerate(lengths):
                    if length is None:
                        raise IntegrityError("encoded record payload must not be null")
                    if length > min(max_value_bytes, BATCH_BYTES):
                        raise LimitExceededError("record exceeds the encoded value byte limit")
                    if index > start and size + length > BATCH_BYTES:
                        yield window.slice(start, index - start)
                        start, size = index, 0
                    size += length
                if start < window.num_rows:
                    yield window.slice(start)


def bounded_rows(values, *, size, max_row_bytes=BATCH_BYTES, max_rows=BATCH_ROWS):
    """Yield tuples of values bounded by row count and bytes, refusing an oversized row."""

    pending, total = [], 0
    with owned_iterator(values) as source:
        for value in source:
            length = size(value)
            if length > min(max_row_bytes, BATCH_BYTES):
                raise LimitExceededError("row exceeds the configured byte handoff limit")
            if pending and (len(pending) == max_rows or total + length > BATCH_BYTES):
                yield tuple(pending)
                pending, total = [], 0
            pending.append(value)
            total += length
        if pending:
            yield tuple(pending)
