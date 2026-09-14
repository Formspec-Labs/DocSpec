from __future__ import annotations


import hashlib
from tests.support.iceberg_records import files

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event, Lock
from typing import Any

import docspec.adapters.storage.files as storage_module
from docspec.domain.identity import canonical_json_bytes

import pytest

import docspec.adapters.storage.records as records_module
from docspec.adapters.storage import (
    IcebergRecordStorage,
)

from docspec.domain.storage import PartitionPolicy, RecordSchema
from docspec.errors import IntegrityError

SCHEMA = RecordSchema(
    "docspec-test-record/1.0",
    ("recordId", "sourceItemId", "value"),
    "recordId",
    "sourceItemId",
)
POLICY = PartitionPolicy("source-item-sha256-v1", 8)


def _bucket(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big") % POLICY.bucket_count


def test_record_layer_streams_stably_and_reuses_untouched_partitions(tmp_path: Path) -> None:
    storage = IcebergRecordStorage(tmp_path / "records", max_member_bytes=10_000)
    initial_records = [
        {"recordId": "a", "sourceItemId": "source-a", "value": 1},
        {"recordId": "b", "sourceItemId": "source-b", "value": 2},
        {"recordId": "c", "sourceItemId": "source-c", "value": 3},
    ]
    initial = storage.write_layer(
        initial_records,
        layer_kind="test-records",
        schema=SCHEMA,
        partition_policy=POLICY,
    )
    changed_partition = _bucket("source-b")
    replacement_records = [
        {"recordId": "b", "sourceItemId": "source-b", "value": 20},
    ]
    if _bucket("source-c") == changed_partition:
        replacement_records.append({"recordId": "c", "sourceItemId": "source-c", "value": 3})
    updated = storage.write_layer(
        replacement_records,
        layer_kind="test-records",
        schema=SCHEMA,
        partition_policy=POLICY,
        base=initial,
        replace_partitions=frozenset({changed_partition}),
    )

    assert list(storage.stream(updated)) == [
        {"recordId": "a", "sourceItemId": "source-a", "value": 1},
        {"recordId": "b", "sourceItemId": "source-b", "value": 20},
        {"recordId": "c", "sourceItemId": "source-c", "value": 3},
    ]
    assert list(storage.stream(updated, partitions=frozenset({changed_partition}))) == replacement_records
    assert {item['path'] for item in files(storage, initial)} <= {item['path'] for item in files(storage, updated)}
    storage.close()


def test_record_snapshots_publish_atomically_under_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = IcebergRecordStorage(tmp_path / "records")
    first_writer_entered = Event()
    release_first_writer = Event()
    call_lock = Lock()
    first_call = True
    original_fdopen = storage_module.os.fdopen

    def gated_fdopen(descriptor: int, *args: Any, **kwargs: Any):
        nonlocal first_call
        with call_lock:
            should_wait = first_call
            first_call = False
        if should_wait:
            first_writer_entered.set()
            assert release_first_writer.wait(timeout=5)
        return original_fdopen(descriptor, *args, **kwargs)

    monkeypatch.setattr(storage_module.os, "fdopen", gated_fdopen)

    def write_empty_layer():
        return storage.write_layer(
            (),
            layer_kind="test-records",
            schema=SCHEMA,
            partition_policy=POLICY,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write_empty_layer)
        assert first_writer_entered.wait(timeout=5)
        second = pool.submit(write_empty_layer)
        try:
            second_reference = second.result(timeout=5)
        finally:
            release_first_writer.set()
        first_reference = first.result(timeout=5)

    assert first_reference != second_reference
    storage.verify(second_reference)
    assert list(storage.stream(second_reference)) == []
    storage.verify(first_reference)
    storage.verify_members(first_reference)
    assert list(storage.stream(first_reference)) == []
    assert list(storage.scan_partition_value(first_reference, "missing")) == []
    assert storage.lookup(first_reference, "missing") is None


def test_parquet_preserves_arbitrary_payloads_and_exact_partition_routing(tmp_path: Path) -> None:
    storage = IcebergRecordStorage(tmp_path / "records")
    records = [
        {"recordId": "a", "sourceItemId": "z", "value": {"mixed": [None, 1, True, "café 🧪"], "nested": {}}},
        {"recordId": "b", "sourceItemId": "a", "value": {"optional": None}},
        {"recordId": "c", "sourceItemId": "z", "value": {}},
    ]
    # One physical bucket makes the source filter prove exact routing rather
    # than returning every peer in the selected bucket.
    layer = storage.write_layer(records, layer_kind="test-records", schema=SCHEMA,
                                partition_policy=PartitionPolicy("single", 1))
    assert [canonical_json_bytes(row) for row in storage.stream(layer)] == [canonical_json_bytes(row) for row in records]
    assert list(storage.scan_partition_value(layer, "z")) == [records[0], records[2]]
    assert storage.lookup(layer, "a", partition_value="a") is None
    assert storage.lookup(layer, "a", partition_value="z") == records[0]
    storage.close()


def test_workspace_names_do_not_override_physical_routing_columns(tmp_path: Path) -> None:
    storage = IcebergRecordStorage(tmp_path / "partition_value=shadow" / "record_identity=shadow" / "quote's")
    row = {"recordId": "actual-id", "sourceItemId": "actual-source", "value": 1}
    layer = storage.write_layer([row], layer_kind="test-records", schema=SCHEMA, partition_policy=POLICY)
    storage.verify(layer)
    assert list(storage.stream(layer)) == [row]
    assert storage.lookup(layer, "actual-id", partition_value="actual-source") == row
    storage.close()


def test_parquet_live_streams_and_concurrent_point_reads_do_not_clobber_results(tmp_path: Path) -> None:
    storage = IcebergRecordStorage(tmp_path / "records")
    records = [{"recordId": f"record-{index:05d}", "sourceItemId": f"source-{index % 3}", "value": index}
               for index in range(4097)]
    layer = storage.write_layer(records, layer_kind="test-records", schema=SCHEMA, partition_policy=POLICY)
    storage.verify_members(layer)
    with closing(storage.stream(layer)) as left, closing(storage.stream(layer)) as right:
        assert next(left) == next(right) == records[0]
        with ThreadPoolExecutor(max_workers=4) as workers:
            def lookup(index):
                row = records[index]
                return storage.lookup(layer, row["recordId"], partition_value=row["sourceItemId"])

            indices = (0, 37, 2048, 4096)
            assert list(workers.map(lookup, indices)) == [records[index] for index in indices]
        for expected in records[1:]:
            assert next(left) == expected
            assert next(right) == expected
        assert next(left, None) is next(right, None) is None
    storage.close()
    # The adapter lazily opens a fresh native connection after explicit close.
    assert list(storage.stream(layer)) == records
    storage.close()


def test_record_layer_rejects_unsorted_open_or_tampered_records(tmp_path: Path) -> None:
    storage = IcebergRecordStorage(tmp_path / "records")
    with pytest.raises(IntegrityError, match="strictly ordered"):
        storage.write_layer(
            [
                {"recordId": "b", "sourceItemId": "source-b", "value": 2},
                {"recordId": "a", "sourceItemId": "source-a", "value": 1},
            ],
            layer_kind="test-records",
            schema=SCHEMA,
            partition_policy=POLICY,
        )

    layer = storage.write_layer(
        [{"recordId": "a", "sourceItemId": "source-a", "value": 1}],
        layer_kind="test-records",
        schema=SCHEMA,
        partition_policy=POLICY,
    )
    path = storage.root / files(storage, layer)[0]["path"]
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(IntegrityError):
        storage.verify(layer)


@pytest.mark.parametrize("operation", ["lookup", "lookup-partition", "scan"])
@pytest.mark.parametrize("corruption", ["root", "member"])
def test_record_lookup_reads_one_root_and_rechecks_later_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, corruption: str,
) -> None:
    storage = IcebergRecordStorage(tmp_path / "records")
    record = {"recordId": "a", "sourceItemId": "source-a", "value": 1}
    layer = storage.write_layer(
        [record], layer_kind="test-records", schema=SCHEMA, partition_policy=POLICY,
    )
    root_path = storage.root / layer.state_ref
    member_path = storage.root / files(storage, layer)[0]["path"]
    root_reads = []
    read_exact = records_module._read_exact

    def observed_read(root, locator, **kwargs):
        if locator == layer.state_ref:
            root_reads.append(locator)
        return read_exact(root, locator, **kwargs)

    monkeypatch.setattr(records_module, "_read_exact", observed_read)

    def read():
        if operation == "scan":
            return list(storage.scan_partition_value(layer, "source-a"))
        partition_value = "source-a" if operation == "lookup-partition" else None
        return [storage.lookup(layer, "a", partition_value=partition_value)]

    assert read() == [record]
    assert root_reads == [layer.state_ref]
    path = root_path if corruption == "root" else member_path
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(IntegrityError):
        if corruption == "member":
            storage.verify_members(layer)
        else:
            read()
    assert root_reads == [layer.state_ref, layer.state_ref]
