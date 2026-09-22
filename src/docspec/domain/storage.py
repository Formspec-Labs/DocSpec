"""Format-neutral logical record and partition descriptions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from docspec.domain.identity import require_text


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


__all__ = ["PartitionPolicy", "RecordSchema", "partition_bucket"]
