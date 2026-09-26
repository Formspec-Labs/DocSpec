"""Bounded Arrow handoffs for encoded values and typed table rows, without decoding payloads."""

from collections.abc import Iterable, Iterator

import pyarrow as pa

from docspec.ports.record_storage import bounded_rows
from docspec.adapters.streams import BATCH_BYTES as BATCH_BYTES, BATCH_ROWS as BATCH_ROWS, owned_iterator
from docspec.errors import IntegrityError


ENCODED_RECORD_SCHEMA = pa.schema([
    ("record_identity", pa.string()), ("partition_value", pa.string()), ("record_json", pa.binary()),
])
_TABLE_ARROW_TYPES = {
    "VARCHAR": pa.string(), "BOOLEAN": pa.bool_(), "INTEGER": pa.int32(), "BIGINT": pa.int64(),
    "DOUBLE": pa.float64(), "DATE": pa.date32(), "TIMESTAMP": pa.timestamp("us"),
    "TIMESTAMPTZ": pa.timestamp("us", tz="UTC"), "VARCHAR[]": pa.list_(pa.string()), "BLOB": pa.binary(),
}


def table_arrow_schema(columns) -> pa.Schema:
    """The Arrow schema typed table rows cross the boundary in; TIMESTAMPTZ travels as UTC microseconds."""
    return pa.schema([(name, _TABLE_ARROW_TYPES[kind]) for name, kind in columns])


def conform_table_batch(batch: pa.RecordBatch, schema: pa.Schema) -> pa.RecordBatch:
    """Return ``batch`` in ``schema``, refusing any difference but a TIMESTAMPTZ zone name.

    A zone names the same UTC instants; any other cast, such as nanoseconds
    to microseconds or int64 to int32, would change or lose values.
    """
    def same(actual, expected):
        return actual == expected or (pa.types.is_timestamp(actual) and actual.unit == "us"
                                      and actual.tz is not None and expected.tz == "UTC")
    if batch.schema.names != schema.names or not all(map(same, batch.schema.types, schema.types)):
        raise IntegrityError("table batch differs from its declared schema")
    return batch if batch.schema.equals(schema) else batch.cast(schema)


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
