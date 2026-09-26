"""Bounded Arrow handoffs for encoded values, without decoding their payloads."""

from collections.abc import Iterable, Iterator
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.compute as pc

from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.record_storage import bounded_rows
from docspec.adapters.streams import BATCH_BYTES as BATCH_BYTES, BATCH_ROWS as BATCH_ROWS, owned_iterator


ENCODED_RECORD_SCHEMA = pa.schema([
    ("record_identity", pa.string()), ("partition_value", pa.string()), ("record_json", pa.binary()),
])


def encoded_batches(
    rows: Iterable[tuple], schema: pa.Schema, *, byte_column: int | tuple[int, ...],
    max_value_bytes: int = BATCH_BYTES,
) -> Iterator[pa.RecordBatch]:
    """Encode Arrow arrays from bounded tuples; JSON admission belongs to callers."""
    def size(row):
        indexes = (byte_column,) if isinstance(byte_column, int) else byte_column
        return sum(len(value.encode("utf-8") if isinstance(value, str) else value)
                   for index in indexes if (value := row[index]) is not None)
    with owned_iterator(bounded_rows(rows, size=size, max_row_bytes=max_value_bytes)) as chunks:
        for chunk in chunks:
            yield pa.record_batch(list(zip(*chunk, strict=True)), schema=schema)


def spilled_order(batches: Iterable[pa.RecordBatch], *, count: int, window: int, directory: str | Path | None,
                  max_bytes: int) -> Iterator[pa.RecordBatch]:
    """Put (position, payload) batches that arrive in any order into position order, in one spill pass.

    Positions run 1..count, each exactly once. Every row is written once to an
    LZ4-compressed Arrow IPC file for its window of ``window`` positions; each
    file is then read, combined and sorted alone. Memory holds about two copies
    of one window, never every payload, and each finished window returns its
    memory before the next is read. Output batches hold BATCH_ROWS rows; only
    each window's last may be shorter.
    """
    windows = -(-count // window)
    with TemporaryDirectory(prefix="docspec-order-", dir=directory, ignore_cleanup_errors=True) as spill:
        paths, written, writers = [Path(spill) / f"{index}.arrow" for index in range(windows)], 0, []
        with owned_iterator(batches) as source, ExitStack() as files:
            for batch in source:
                if not writers:
                    options = pa.ipc.IpcWriteOptions(compression="lz4")
                    writers = [files.enter_context(pa.ipc.new_file(str(path), batch.schema, options=options)) for path in paths]
                buckets = pc.divide(pc.subtract(batch.column(0), 1), window)
                order = pc.sort_indices(buckets)
                batch, runs, start = batch.take(order), pc.value_counts(buckets.take(order)), 0
                for bucket, rows in zip(runs.field("values").to_pylist(), runs.field("counts").to_pylist(), strict=True):
                    writers[bucket].write_batch(batch.slice(start, rows))
                    start += rows
                written += batch.nbytes
                if written > max_bytes:
                    raise LimitExceededError("ordered read spill exceeds the scratch allowance")
        if not writers:
            raise IntegrityError("ordered read lost its member positions")
        for index, path in enumerate(paths):
            first, rows = index * window + 1, min(window, count - index * window)
            with pa.OSFile(str(path)) as source:
                table = pa.ipc.open_file(source).read_all().combine_chunks()
            if table.num_rows != rows:
                raise IntegrityError("ordered read lost or repeated a member position")
            order = pc.sort_indices(table.column(0))
            for start in range(0, rows, BATCH_ROWS):
                batch = table.take(order.slice(start, BATCH_ROWS)).to_batches()[0]
                if not batch.column(0).equals(pa.array(range(first + start, first + start + batch.num_rows), pa.int64())):
                    raise IntegrityError("ordered read lost or repeated a member position")
                yield batch
            del table, order
            pa.default_memory_pool().release_unused()
