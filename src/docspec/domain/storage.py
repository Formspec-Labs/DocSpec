"""Format-neutral logical record, typed table and partition descriptions."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from docspec.domain.identity import canonical_value_bytes, require_text


# What an Iceberg scan yields: Iceberg has no 16-bit integer or second and
# millisecond timestamps. Every type except BLOB has a docspec-table-row/1
# spelling; the occurrence index stores its hashes as BLOB.
TABLE_TYPES = frozenset({"VARCHAR", "BOOLEAN", "INTEGER", "BIGINT", "DOUBLE", "DATE",
                         "TIMESTAMP", "TIMESTAMPTZ", "VARCHAR[]", "BLOB"})


def check_column_names(names: Iterable[str]) -> None:
    """Refuse no columns, an empty or NUL-bearing name, invalid Unicode, or a case-folded duplicate.

    SQL identifiers cannot carry NUL, and DuckDB folds identifier case.
    """
    folded = set()
    for name in names:
        if not isinstance(name, str) or not name or "\0" in name:
            raise ValueError("table column names must be nonempty strings without NUL")
        canonical_value_bytes(name)  # Shared owner refuses invalid Unicode.
        if name.casefold() in folded:
            raise ValueError("table column names must be distinct, including SQL case folding")
        folded.add(name.casefold())
    if not folded:
        raise ValueError("table rows require at least one column")


@dataclass(frozen=True, slots=True)
class TableSchema:
    """A closed typed table in physical column order, with no imposed identity or routing columns."""

    schema_id: str
    columns: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        require_text(self.schema_id, "schema_id")
        columns = tuple((name, kind) for name, kind in self.columns)
        check_column_names(name for name, _ in columns)
        if any(kind not in TABLE_TYPES for _, kind in columns):
            raise ValueError("table column type is outside the table profile")
        object.__setattr__(self, "columns", columns)

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.columns)


@dataclass(frozen=True, slots=True)
class RecordSchema:
    """Closed logical schema naming its identity and partition fields, with optional typed columns."""

    schema_id: str
    fields: tuple[str, ...]
    identity_field: str
    partition_field: str
    columns: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        require_text(self.schema_id, "schema_id")
        if not self.fields or len(set(self.fields)) != len(self.fields):
            raise ValueError("record schema fields must be non-empty and distinct")
        if self.identity_field not in self.fields or self.partition_field not in self.fields:
            raise ValueError("identity and partition fields must belong to the closed schema")
        if self.columns:
            names = ("record_identity", "partition_value", "record_json", *(name for name, _ in self.columns))
            if self.fields != names or len(set(names)) != len(names) or any(kind not in {"string", "binary"} for _, kind in self.columns):
                raise ValueError("typed record columns must match the closed physical schema")
            if (self.identity_field, self.partition_field) != ("record_identity", "partition_value"):
                raise ValueError("typed records use their native routing columns")


@dataclass(frozen=True, slots=True)
class PartitionPolicy:
    """Partition identity with a bucket count between 1 and 65536."""

    policy_id: str
    bucket_count: int

    def __post_init__(self) -> None:
        require_text(self.policy_id, "partition policy_id")
        if self.bucket_count <= 0 or self.bucket_count > 65_536:
            raise ValueError("partition bucket_count must be between 1 and 65536")


def record_key(value: object, label: str) -> str:
    """Physical routing accepts every string, including Core's empty member key."""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value


def partition_bucket(value: str, bucket_count: int) -> int:
    """Assign one logical identity to a stable SHA-256 bucket."""

    record_key(value, "partition value")
    if bucket_count <= 0 or bucket_count > 65_536:
        raise ValueError("bucket_count must be between 1 and 65536")
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % bucket_count


__all__ = ["PartitionPolicy", "RecordSchema", "TABLE_TYPES", "TableSchema", "check_column_names", "partition_bucket"]
