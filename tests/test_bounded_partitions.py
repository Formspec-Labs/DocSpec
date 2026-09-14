from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from docspec.adapters.storage import LocalParquetRecordStorage
from docspec.domain.identity import canonical_json_bytes, canonical_json_file_bytes, sha256_digest, stable_urn
from docspec.domain.references import LayerRef
from docspec.domain.storage import PartitionPolicy, RecordSchema, partition_bucket
from docspec.errors import IntegrityError, LimitExceededError


def _records_in_distinct_partitions(count: int, bucket_count: int) -> list[dict[str, object]]:
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
    storage = LocalParquetRecordStorage(
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

    assert first == second
    assert list(storage.stream(second)) == records
    storage.verify_members(second)
    storage.verify(second)
    root = json.loads((storage.root / second.state_ref).read_text())
    assert root["formatVersion"] == "4.0"
    assert len(root["members"]) == len(records)
    assert all(member["mediaType"] == "application/vnd.apache.parquet" for member in root["members"])
    assert all((storage.root / member["path"]).stat().st_size == member["byteSize"] for member in root["members"])
    storage.close()
    assert list(storage._staging.iterdir()) == []
    assert list(merge_scratch.iterdir()) == []


def test_parquet_writer_shards_a_hot_partition_and_reuses_immutable_members(tmp_path: Path) -> None:
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
    storage = LocalParquetRecordStorage(
        tmp_path / "hot-records",
        max_member_bytes=16 * 1024,
        max_record_bytes=8 * 1024,
    )

    first = storage.write_layer(records, layer_kind="hot-records", schema=schema, partition_policy=policy)
    second = storage.write_layer(records, layer_kind="hot-records", schema=schema, partition_policy=policy)
    root = json.loads((storage.root / first.state_ref).read_text())

    assert first == second
    assert len(root["members"]) > 1
    assert [member["sequence"] for member in root["members"]] == list(range(len(root["members"])))
    assert all(member["partition"] == 0 for member in root["members"])
    assert all(member["byteSize"] <= storage.max_member_bytes for member in root["members"])
    assert list(storage.stream(first)) == records
    storage.close()


def _physical_layer(storage, schema, policy, partitions, *, physical_schema=None):
    """Write actual Parquet bytes and pin them, including intentionally false rows."""
    physical_schema = physical_schema or pa.schema([
        ("record_identity", pa.string()), ("partition_value", pa.string()), ("record_json", pa.binary()),
    ])
    members = []
    for index, (partition, rows) in enumerate(partitions):
        temporary = storage.root / f"fixture-{index}.parquet"
        pq.write_table(pa.Table.from_pylist(rows, schema=physical_schema), temporary)
        digest = sha256_digest(temporary.read_bytes())
        locator = storage._member_locator(digest)
        path = storage.root / locator
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.replace(path)
        members.append({
            "partition": partition, "sequence": 0, "path": locator,
            "mediaType": "application/vnd.apache.parquet", "byteSize": path.stat().st_size,
            "digest": digest, "recordCount": len(rows), "schemaId": schema.schema_id,
            "identityMin": min(row["record_identity"] for row in rows),
            "identityMax": max(row["record_identity"] for row in rows),
        })
    return _pin_layer(storage, schema, policy, members)


def _pin_layer(storage, schema, policy, members):
    content = {
        "layerKind": "fixture-records",
        "schema": {"schemaId": schema.schema_id, "fields": list(schema.fields),
                   "identityField": schema.identity_field, "partitionField": schema.partition_field, "columns": []},
        "profileId": "urn:docspec:profile:record-storage:local-parquet:3",
        "partitionPolicy": {"policyId": policy.policy_id, "bucketCount": policy.bucket_count},
        "members": members, "recordCount": sum(member["recordCount"] for member in members),
    }
    root = {"format": "docspec-record-layer", "formatVersion": "4.0",
            "layerId": stable_urn("record-layer", content), **content}
    payload = canonical_json_file_bytes(root)
    digest = sha256_digest(payload)
    locator = f"record-layers/sha256/{digest[7:9]}/{digest[7:]}.json"
    path = storage.root / locator
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return LayerRef(root["layerId"], root["layerKind"], schema.schema_id,
                    root["profileId"], locator, digest, root["recordCount"])


def test_parquet_refuses_one_member_path_claimed_by_multiple_shards(tmp_path: Path) -> None:
    storage = LocalParquetRecordStorage(tmp_path / "records")
    schema = RecordSchema("docspec-test-record/1.0", ("recordId", "sourceItemId", "value"), "recordId", "sourceItemId")
    policy = PartitionPolicy("single", 1)
    record = {"recordId": "a", "sourceItemId": "source", "value": 1}
    row = {"record_identity": "a", "partition_value": "source", "record_json": canonical_json_bytes(record)}
    original = _physical_layer(storage, schema, policy, [(0, [row])])
    storage.verify_members(original)
    member, = json.loads((storage.root / original.state_ref).read_bytes())["members"]
    repeated = _pin_layer(storage, schema, policy, [member, {**member, "sequence": 1}])
    with pytest.raises(IntegrityError):
        storage.verify_members(repeated)
    storage.close()


def test_parquet_scan_rejects_cross_member_duplicates_and_cleans_scratch(tmp_path: Path) -> None:
    policy = PartitionPolicy("duplicate-stress-v1", 8)
    schema = RecordSchema(
        "docspec-test-record/1.0",
        ("recordId", "sourceItemId", "value"),
        "recordId",
        "sourceItemId",
    )
    scratch = tmp_path / "duplicate-scratch"
    scratch.mkdir()
    storage = LocalParquetRecordStorage(
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
    storage = LocalParquetRecordStorage(tmp_path / "records")
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
    storage = LocalParquetRecordStorage(tmp_path / "records", max_member_bytes=128, max_record_bytes=128)
    schema = RecordSchema("docspec-test-record/1.0", ("recordId", "sourceItemId", "value"), "recordId", "sourceItemId")
    record = {"recordId": "a", "sourceItemId": "source", "value": 1}
    assert len(canonical_json_bytes(record)) < storage.max_member_bytes
    with pytest.raises(LimitExceededError, match="member"):
        storage.write_layer([record], layer_kind="test-records", schema=schema, partition_policy=PartitionPolicy("single", 1))
    assert not list(storage.root.glob("record-layers/**/*.json"))
    storage.close()
    assert list(storage._staging.iterdir()) == []


@pytest.mark.parametrize("violation", ["wrong-bucket", "oversized-row"])
def test_physical_admission_does_not_replace_logical_row_checks(tmp_path: Path, violation: str) -> None:
    storage = LocalParquetRecordStorage(tmp_path / "records", max_record_bytes=1024)
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
    storage = LocalParquetRecordStorage(tmp_path / "default-limits")
    members = []
    for partition in range(65_536):
        digest = sha256_digest(f"partition-{partition}".encode())
        members.append({
            "partition": partition,
            "sequence": 0,
            "path": f"record-members/sha256/{digest[7:9]}/{digest[7:]}.parquet",
            "mediaType": "application/vnd.apache.parquet",
            "byteSize": 2,
            "digest": digest,
            "recordCount": 1,
            "identityMin": "a", "identityMax": "a",
            "schemaId": "docspec-test-record/1.0",
        })
    content = {
        "layerKind": "boundary-records",
        "schema": {
            "schemaId": "docspec-test-record/1.0",
            "fields": ["recordId", "sourceItemId", "value"],
            "identityField": "recordId",
            "partitionField": "sourceItemId",
            "columns": [],
        },
        "profileId": "urn:docspec:profile:record-storage:local-parquet:3",
        "partitionPolicy": {"policyId": "all-supported-partitions-v1", "bucketCount": 65_536},
        "members": members,
        "recordCount": 65_536,
    }
    root = {
        "format": "docspec-record-layer",
        "formatVersion": "4.0",
        "layerId": stable_urn("record-layer", content),
        **content,
    }
    payload = canonical_json_file_bytes(root)
    reference = LayerRef(
        root["layerId"],
        root["layerKind"],
        root["schema"]["schemaId"],
        root["profileId"],
        "boundary.json",
        sha256_digest(payload),
        root["recordCount"],
    )
    record_root = tmp_path / "root-boundary"
    record_root.mkdir()
    (record_root / reference.state_ref).write_bytes(payload)

    assert 16 * 1024**2 < len(payload) <= storage.max_root_bytes
    undersized = LocalParquetRecordStorage(record_root, max_root_bytes=len(payload) - 1)
    with pytest.raises(LimitExceededError, match="storage member exceeds"):
        undersized._load_root(reference)
    exact = LocalParquetRecordStorage(record_root, max_root_bytes=len(payload))
    assert exact._load_root(reference)["recordCount"] == 65_536
    storage.close()
    undersized.close()
    exact.close()
