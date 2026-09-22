"""Bounded Iceberg record partitions: streaming writes, exact pins and semantic refusals.

The Parquet writer may never read a whole member into Python, a hot partition must still
shard into immutable members, and genuine file/root byte pins may not bypass logical row
admission.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pytest

from docspec.adapters.storage import IcebergRecordStorage
from docspec.domain.identity import canonical_json_bytes
from docspec.domain.storage import PartitionPolicy, RecordSchema, partition_bucket
from docspec.errors import IntegrityError, LimitExceededError


def _records_in_distinct_partitions(count: int, bucket_count: int) -> list[dict[str, object]]:
    """Build one record per distinct bucket so a stress fixture never collides."""
    records: list[dict[str, object]] = []
    partitions: set[int] = set()
    candidate_number = 0
    while len(records) < count:
        record_id = f"record-{candidate_number:08d}"
        candidate_number += 1
        partition = partition_bucket(record_id, bucket_count)
        if partition in partitions:
            continue
        partitions.add(partition)
        records.append({"recordId": record_id, "sourceItemId": record_id, "value": len(records)})
    return records


def test_parquet_writer_streams_many_partitions_without_whole_python_member_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """512 partitions stream out with no whole-member Python read and distinct snapshot identities."""
    policy = PartitionPolicy("stress-sha256-v1", 4096)
    schema = RecordSchema(
        "docspec-test-record/1.0",
        ("recordId", "sourceItemId", "value"),
        "recordId",
        "sourceItemId",
    )
    records = _records_in_distinct_partitions(512, policy.bucket_count)
    merge_scratch = tmp_path / "merge-scratch"
    merge_scratch.mkdir()
    storage = IcebergRecordStorage(
        tmp_path / "records",
        max_member_bytes=64 * 1024,
        merge_scratch_root=merge_scratch,
    )
    original_read_bytes = Path.read_bytes

    def reject_whole_member_read(path: Path) -> bytes:
        if path.suffix == ".parquet":
            raise AssertionError(f"record member was read whole: {path}")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_whole_member_read)

    first = storage.write_layer(records, layer_kind="stress-records", schema=schema, partition_policy=policy)
    second = storage.write_layer(records, layer_kind="stress-records", schema=schema, partition_policy=policy)

    assert first != second  # Physical snapshots have distinct Iceberg identities.
    assert list(storage.stream(second)) == records
    storage.verify(second)
    root = json.loads((storage.root / second.state_ref).read_text())
    assert root["format"] == "docspec-iceberg-records"
    assert 'members' not in root and len(json.dumps(root)) < 2048
    storage.close()
    assert list(merge_scratch.iterdir()) == []


def test_parquet_writer_shards_a_hot_partition_and_reuses_immutable_members(tmp_path: Path) -> None:
    """A one-bucket policy shards rows into immutable members and still round-trips every row."""
    policy = PartitionPolicy("single-partition-v1", 1)
    schema = RecordSchema(
        "docspec-test-record/1.0",
        ("recordId", "sourceItemId", "value"),
        "recordId",
        "sourceItemId",
    )
    records = [
        {"recordId": f"record-{index:04d}", "sourceItemId": "hot",
         "value": hashlib.shake_256(str(index).encode()).hexdigest(2048)}
        for index in range(24)
    ]
    storage = IcebergRecordStorage(
        tmp_path / "hot-records",
        max_member_bytes=256 * 1024,
        max_record_bytes=8 * 1024,
    )

    first = storage.write_layer(records, layer_kind="hot-records", schema=schema, partition_policy=policy)
    second = storage.write_layer(records, layer_kind="hot-records", schema=schema, partition_policy=policy)
    root = json.loads((storage.root / first.state_ref).read_text())

    assert first != second
    assert root["recordCount"] == len(records)
    assert list(storage.stream(first)) == records
    storage.close()


def _physical_layer(storage, schema, policy, partitions, *, physical_schema=None):
    """Use the real Iceberg writer while intentionally bypassing row admission."""
    if physical_schema is not None:
        from uuid import uuid4
        from docspec.adapters.storage.iceberg import identifier
        with storage._cursor() as cursor:
            client = storage._client()
            name = 'invalid_' + uuid4().hex
            key = (storage.catalog.namespace, name)
            location = storage.root / 'iceberg' / name
            (location / 'metadata').mkdir(parents=True)
            (location / 'data').mkdir()
            table = client.create_table(key, schema=pa.schema([*physical_schema, ('bucket', pa.int32())]),
                location=str(location))
            try:
                count = 0
                for partition, rows in partitions:
                    cursor.register('fixture', pa.Table.from_pylist([{**row, 'bucket': partition} for row in rows]))
                    cursor.execute(f'INSERT INTO iceberg.{identifier(key[0])}.{identifier(key[1])} SELECT * FROM fixture')
                    cursor.unregister('fixture')
                    count += len(rows)
                return storage._pin(table.refresh(), schema=schema, partition_policy=policy,
                                    layer_kind='fixture-records', record_count=count).reference
            finally:
                client.drop_table(key)
    with storage._cursor() as cursor, storage._write_table(cursor, schema) as (target, table):

        count = 0
        for partition, rows in partitions:
            values = [{**row, 'bucket': partition} for row in rows]
            batch = pa.Table.from_pylist(values)
            cursor.register('fixture', batch)
            cursor.execute(f'INSERT INTO {target} SELECT * FROM fixture')
            cursor.unregister('fixture')
            count += len(rows)
        return storage._pin(table(), schema=schema, partition_policy=policy,
                            layer_kind='fixture-records', record_count=count).reference


def test_parquet_scan_rejects_cross_member_duplicates_and_cleans_scratch(tmp_path: Path) -> None:
    """Duplicate record identity across members is refused by `verify` even when each member's pins hold."""
    policy = PartitionPolicy("duplicate-stress-v1", 8)
    schema = RecordSchema(
        "docspec-test-record/1.0",
        ("recordId", "sourceItemId", "value"),
        "recordId",
        "sourceItemId",
    )
    scratch = tmp_path / "duplicate-scratch"
    scratch.mkdir()
    storage = IcebergRecordStorage(
        tmp_path / "duplicate-records",
        merge_scratch_root=scratch,
    )
    values_by_partition: dict[int, str] = {}
    candidate = 0
    while len(values_by_partition) < 3:
        value = f"partition-value-{candidate}"
        candidate += 1
        values_by_partition.setdefault(partition_bucket(value, policy.bucket_count), value)
    partitions = sorted(values_by_partition)[:3]
    identities = ("duplicate", "middle", "duplicate")
    rows_by_partition = []
    for partition, identity in zip(partitions, identities, strict=True):
        record = {
            "recordId": identity,
            "sourceItemId": values_by_partition[partition],
            "value": partition,
        }
        rows_by_partition.append((partition, [{"record_identity": identity,
            "partition_value": record["sourceItemId"], "record_json": canonical_json_bytes(record)}]))

    layer = _physical_layer(storage, schema, policy, rows_by_partition)
    storage.verify_members(layer)
    with pytest.raises(IntegrityError, match="globally unique"):
        storage.verify(layer)
    storage.close()
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("corruption", ["identity", "partition", "noncanonical", "physical-schema", "open-row"])
def test_parquet_refuses_repinned_false_routing_or_payload_schema(tmp_path: Path, corruption: str) -> None:
    """Genuine physical pins do not admit a false routing column, noncanonical bytes or an open row."""
    storage = IcebergRecordStorage(tmp_path / "records")
    schema = RecordSchema("docspec-test-record/1.0", ("recordId", "sourceItemId", "value"), "recordId", "sourceItemId")
    policy = PartitionPolicy("single", 1)
    record = {"recordId": "a", "sourceItemId": "source", "value": {"optional": None}}
    physical = {"record_identity": "a", "partition_value": "source", "record_json": canonical_json_bytes(record)}
    physical_schema = None
    if corruption == "identity":
        physical["record_identity"] = "other"
    elif corruption == "partition":
        physical["partition_value"] = "other"
    elif corruption == "noncanonical":
        physical["record_json"] = json.dumps(record).encode()
    elif corruption == "open-row":
        physical["record_json"] = canonical_json_bytes({**record, "extra": True})
    else:
        physical["record_json"] = physical["record_json"].decode()
        physical_schema = pa.schema([
            ("record_identity", pa.string()), ("partition_value", pa.string()), ("record_json", pa.string()),
        ])
    layer = _physical_layer(storage, schema, policy, [(0, [physical])], physical_schema=physical_schema)
    # The complete file/root byte pins are genuine; semantic admission must
    # still refuse a false routing column, noncanonical bytes or open row.
    if corruption != "physical-schema":
        storage.verify_members(layer)
    with pytest.raises(IntegrityError):
        storage.verify(layer)
    storage.close()


def test_parquet_physical_member_cap_includes_encoding_overhead(tmp_path: Path) -> None:
    """A record below the byte cap still refuses when Parquet framing pushes the member over."""
    storage = IcebergRecordStorage(tmp_path / "records", max_member_bytes=128, max_record_bytes=128)
    schema = RecordSchema("docspec-test-record/1.0", ("recordId", "sourceItemId", "value"), "recordId", "sourceItemId")
    record = {"recordId": "a", "sourceItemId": "source", "value": 1}
    assert len(canonical_json_bytes(record)) < storage.max_member_bytes
    with pytest.raises(LimitExceededError, match="member"):
        storage.write_layer([record], layer_kind="test-records", schema=schema, partition_policy=PartitionPolicy("single", 1))
    assert not list(storage.root.glob("record-layers/**/*.json"))
    storage.close()


@pytest.mark.parametrize("violation", ["wrong-bucket", "oversized-row"])
def test_physical_admission_does_not_replace_logical_row_checks(tmp_path: Path, violation: str) -> None:
    """Physical verification passes but logical admission still refuses a wrong bucket or oversized row."""
    storage = IcebergRecordStorage(tmp_path / "records", max_record_bytes=1024)
    schema = RecordSchema("test/1", ("recordId", "sourceItemId", "value"), "recordId", "sourceItemId")
    policy = PartitionPolicy("source-buckets", 8)
    row = {"recordId": "a", "sourceItemId": "source", "value": "x" * (8192 if violation == "oversized-row" else 8)}
    partition = partition_bucket(row["sourceItemId"], policy.bucket_count)
    if violation == "wrong-bucket":
        partition = (partition + 1) % policy.bucket_count
    layer = _physical_layer(storage, schema, policy, [(partition, [{
        "record_identity": row["recordId"], "partition_value": row["sourceItemId"],
        "record_json": canonical_json_bytes(row),
    }])])
    storage.verify_members(layer)  # Valid Parquet, exact pins, matching routing columns.
    error = IntegrityError if violation == "wrong-bucket" else LimitExceededError
    with pytest.raises(error, match="wrong partition" if violation == "wrong-bucket" else "record exceeds"):
        storage.verify(layer)
    storage.close()


def test_record_root_profile_covers_every_supported_occupied_partition(tmp_path: Path) -> None:
    """A 65,536-bucket root stays under 2048 bytes, and a root cap one byte smaller refuses `available`."""
    with __import__('contextlib').closing(IcebergRecordStorage(tmp_path)) as storage:
        schema = RecordSchema('all/1', ('id',), 'id', 'id')
        reference = storage.write_layer([{'id': 'one'}], layer_kind='bounds', schema=schema,
                                       partition_policy=PartitionPolicy('all', 65536))
        payload = (storage.root / reference.state_ref).read_bytes()
        assert len(payload) < 2048
        with __import__('contextlib').closing(IcebergRecordStorage(tmp_path, max_root_bytes=len(payload)-1)) as small:
            with pytest.raises(LimitExceededError):
                small.available(reference)
