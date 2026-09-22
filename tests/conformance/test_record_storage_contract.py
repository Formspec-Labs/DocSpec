"""Conformance: local Iceberg record storage implements the shared layer contract.

One logical layer is written, streamed, looked up, updated by partition and
tampered with, so every registered record profile exposes the same port surface
with the same ordering, immutability and fail-closed guarantees.
"""

from __future__ import annotations

from tests.support.iceberg_records import files

import hashlib
from pathlib import Path

import pytest

from docspec.domain.storage import PartitionPolicy, RecordSchema
from docspec.errors import IntegrityError
from docspec.adapters.storage.records import IcebergRecordStorage

# One shared logical layer fixture: every registered record profile must
# expose exactly these records through the same port surface.
SCHEMA = RecordSchema(
    "docspec-conformance-record/1.0",
    ("recordId", "sourceItemId", "value"),
    "recordId",
    "sourceItemId",
)
POLICY = PartitionPolicy("source-item-sha256-v1", 8)
BASE_RECORDS = (
    {"recordId": "alpha", "sourceItemId": "source-alpha", "value": 1},
    {"recordId": "bravo", "sourceItemId": "source-bravo", "value": 2},
    {"recordId": "charlie", "sourceItemId": "source-charlie", "value": 3},
    {"recordId": "prune-me", "sourceItemId": "source-prune", "value": 4},
)


def _bucket(value: str) -> int:
    """Return the partition bucket the fixture policy assigns to one source-item identity."""
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big") % POLICY.bucket_count


def test_local_record_storage_passes_the_shared_layer_contract(tmp_path: Path) -> None:
    pruned_partition = _bucket("source-prune")
    replacement = tuple(
        {**record, "value": record["value"] * 10}
        for record in BASE_RECORDS
        if _bucket(record["sourceItemId"]) == pruned_partition and record["recordId"] != "prune-me"
    )
    survivors = tuple(
        record
        for record in BASE_RECORDS
        if _bucket(record["sourceItemId"]) != pruned_partition
    ) + replacement

    storage = IcebergRecordStorage(tmp_path / "records")
    base = storage.write_layer(
        BASE_RECORDS,
        layer_kind="conformance-records",
        schema=SCHEMA,
        partition_policy=POLICY,
    )
    storage.verify(base)
    assert base.record_count == len(BASE_RECORDS)
    assert storage.schema(base) == SCHEMA
    assert storage.identity_field(base) == "recordId"
    assert storage.partition_policy(base) == POLICY

    assert list(storage.stream(base)) == sorted(BASE_RECORDS, key=lambda record: record["recordId"])
    assert list(storage.stream(base, partitions=frozenset({pruned_partition}))) == sorted(
        (record for record in BASE_RECORDS if _bucket(record["sourceItemId"]) == pruned_partition),
        key=lambda record: record["recordId"],
    )
    assert list(storage.scan_partition_value(base, "source-alpha")) == [BASE_RECORDS[0]]
    assert storage.lookup(base, "bravo") == BASE_RECORDS[1]
    assert storage.lookup(base, "bravo", partition_value="source-bravo") == BASE_RECORDS[1]
    assert storage.lookup(base, "absent") is None

    updated = storage.write_layer(
        replacement,
        layer_kind="conformance-records",
        schema=SCHEMA,
        partition_policy=POLICY,
        base=base,
        replace_partitions=frozenset({pruned_partition}),
    )
    storage.verify(updated)
    assert list(storage.stream(updated)) == sorted(survivors, key=lambda record: record["recordId"])
    assert storage.lookup(updated, "prune-me") is None, "a replaced partition must prune its removed records"
    assert storage.lookup(base, "prune-me") == BASE_RECORDS[3], "the base layer must stay immutable"

    assert {item['path'] for item in files(storage, base)} <= {item['path'] for item in files(storage, updated)}


def test_local_record_storage_rejects_unordered_and_duplicate_identities(tmp_path: Path) -> None:
    storage = IcebergRecordStorage(tmp_path / "records")
    with pytest.raises(IntegrityError, match="strictly ordered"):
        storage.write_layer(
            (BASE_RECORDS[1], BASE_RECORDS[0]),
            layer_kind="conformance-records",
            schema=SCHEMA,
            partition_policy=POLICY,
        )
    with pytest.raises(IntegrityError, match="strictly ordered"):
        storage.write_layer(
            (BASE_RECORDS[0], BASE_RECORDS[0]),
            layer_kind="conformance-records",
            schema=SCHEMA,
            partition_policy=POLICY,
        )


def test_local_record_storage_fails_closed_on_tampered_member_bytes(tmp_path: Path) -> None:
    storage = IcebergRecordStorage(tmp_path / "records")
    layer = storage.write_layer(
        BASE_RECORDS,
        layer_kind="conformance-records",
        schema=SCHEMA,
        partition_policy=POLICY,
    )
    storage.verify(layer)
    members = files(storage, layer)
    assert members
    for member in members:
        path = storage.root / member["path"]
        original = path.read_bytes()
        path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(IntegrityError):
        storage.verify_members(layer)
    with pytest.raises(IntegrityError):
        storage.verify(layer)
