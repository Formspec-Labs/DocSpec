"""Bounded Arrow handoffs for encoded values, without decoding their payloads."""

from collections.abc import Iterable, Iterator

import pyarrow as pa

from docspec.ports.record_storage import bounded_rows
from docspec.adapters.streams import BATCH_BYTES as BATCH_BYTES, BATCH_ROWS as BATCH_ROWS, owned_iterator


ENCODED_RECORD_SCHEMA = pa.schema([
    ("record_identity", pa.string()), ("partition_value", pa.string()), ("record_json", pa.binary()),
])


def encoded_batches(
    rows: Iterable[tuple], schema: pa.Schema, *, byte_column: int,
    max_value_bytes: int = BATCH_BYTES,
) -> Iterator[pa.RecordBatch]:
    """Encode Arrow arrays from bounded tuples; JSON admission belongs to callers."""
    def size(row):
        value = row[byte_column]
        return len(value.encode("utf-8") if isinstance(value, str) else value)
    with owned_iterator(bounded_rows(rows, size=size, max_row_bytes=max_value_bytes)) as chunks:
        for chunk in chunks:
            yield pa.record_batch(list(zip(*chunk, strict=True)), schema=schema)
