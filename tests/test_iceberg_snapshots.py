"""The production Iceberg writer retains independent snapshots and writes only changed partitions.

An interrupted publication leaves no head behind; retained reads need no catalog, writes require
DOCSPEC_ICEBERG_URI, a rewritten file plus matching checksum still fails verification, and a partition
replacement must not duplicate an identity held elsewhere.
"""

from contextlib import closing
from pathlib import Path
import hashlib
import shutil

import pytest

from docspec.adapters.storage import IcebergCatalog, IcebergRecordStorage
from docspec.adapters.storage.batches import ENCODED_RECORD_SCHEMA, encoded_batches
from docspec.domain.identity import canonical_json_bytes
from docspec.domain.storage import PartitionPolicy, RecordSchema
from docspec.errors import IntegrityError
from docspec.runtime import CoreWorkspace

SCHEMA = RecordSchema("snapshot-test:1", ("id", "value"), "id", "id")
POLICY = PartitionPolicy("keys", 1)


def changes(rows):
    """Encode ``(key, value)`` rows as deletion-or-value change batches for ``apply_changes``."""
    return encoded_batches(((key, key, None if value is None else canonical_json_bytes({"id": key, "value": value}))
                            for key, value in rows), ENCODED_RECORD_SCHEMA, byte_column=0)


def data_files(layer):
    """Map each data-file path in ``layer`` to its scan task."""
    return {task.file.file_path: task.file for task in layer.table.scan().plan_files()}


def test_row_edits_keep_base_files_and_independent_branches(tmp_path, monkeypatch):
    with closing(IcebergRecordStorage(tmp_path)) as records:
        rows = [{"id": f"{index:04d}", "value": index} for index in range(256)]
        base = records.available(records.write_layer(rows, layer_kind="test", schema=SCHEMA, partition_policy=POLICY))
        original = {path: hashlib.sha256(Path(path).read_bytes()).digest() for path in data_files(base)}
        left = records.apply_changes(base, changes([("0001", 999), ("0002", None), ("new", 7)]))
        right = records.apply_changes(base, changes([("0003", None)]))
        for layer in (left, right):
            assert original.keys() <= data_files(layer).keys()
            assert all(hashlib.sha256(Path(path).read_bytes()).digest() == digest for path, digest in original.items())
            records.verify(layer.reference)
        assert sum(file.record_count for path, file in data_files(left).items() if path not in original) == 2
        assert data_files(right).keys() == original.keys()
        assert sum(len(task.delete_files) for task in left.table.scan().plan_files()) > 0
        assert list(records.stream(base.reference)) == rows
        expected = [dict(row, value=999) if row["id"] == "0001" else row for row in rows if row["id"] != "0002"]
        assert list(records.stream(left.reference)) == expected + [{"id": "new", "value": 7}]
        assert list(records.stream(right.reference)) == [row for row in rows if row["id"] != "0003"]
        # Retained reads need no catalog, including after the writer closes.
        records.close()
        monkeypatch.delenv("DOCSPEC_ICEBERG_URI", raising=False)
        with closing(IcebergRecordStorage(tmp_path)) as offline:
            assert list(offline.stream(left.reference)) == expected + [{"id": "new", "value": 7}]
        # Reopening the same owner also reattaches the catalog for later writes.
        again = records.apply_changes(right, changes([("0004", None)]))
        assert again.reference.record_count == 254
        records.close()
        moved = tmp_path.with_name(tmp_path.name + "-moved")
        hidden = tmp_path.with_name(tmp_path.name + "-hidden")
        shutil.copytree(tmp_path, moved)
        tmp_path.rename(hidden)
        try:
            with closing(IcebergRecordStorage(moved, catalog=records.catalog)) as offline:
                assert list(offline.stream(left.reference)) == expected + [{"id": "new", "value": 7}]
                offline.verify(left.reference)
                with pytest.raises(IntegrityError, match="relocated"):
                    offline.apply_changes(offline.available(left.reference), changes([("0001", 3)]))
        finally:
            hidden.rename(tmp_path)


def test_failed_pin_leaves_no_catalog_head_or_changed_base(tmp_path, monkeypatch):
    with closing(IcebergRecordStorage(tmp_path)) as records:
        base = records.available(records.write_layer([{"id": "a", "value": 1}], layer_kind="test", schema=SCHEMA, partition_policy=POLICY))
        client = records._catalog_client
        before = set(client.list_tables(records.catalog.namespace))
        def fail(*args, **kwargs):
            raise OSError("publication interrupted")
        monkeypatch.setattr(records, "_pin", fail)
        with pytest.raises(OSError, match="interrupted"):
            records.apply_changes(base, changes([("a", 2)]))
        assert set(client.list_tables(records.catalog.namespace)) == before
        assert list(records.stream(base.reference)) == [{"id": "a", "value": 1}]


def test_logical_retry_reuses_publication_but_refuses_changed_input(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("root", [("a", 1)])
        with workspace.publisher.session() as session:
            before = workspace.states.representation(session, "root")
        workspace.create("root", [("a", 1)])
        with workspace.publisher.session() as session:
            assert workspace.states.representation(session, "root") == before
        with pytest.raises(IntegrityError, match="immutable"):
            workspace.create("root", [("a", 2)])
        with pytest.raises(IntegrityError, match="membership"):
            workspace.create("root", [])
        assert len(list(workspace.rows("root"))) == 1


def test_writes_require_a_catalog_but_constructing_storage_does_not(tmp_path, monkeypatch):
    monkeypatch.delenv("DOCSPEC_ICEBERG_URI", raising=False)
    assert IcebergCatalog.environment() is None
    with closing(IcebergRecordStorage(tmp_path)) as records:
        with pytest.raises(IntegrityError, match="DOCSPEC_ICEBERG_URI"):
            records.write_layer([], layer_kind="test", schema=SCHEMA, partition_policy=POLICY)


def test_replacing_a_file_and_its_checksum_cannot_repin_a_snapshot(tmp_path):
    import json
    from docspec.domain.identity import canonical_value_bytes, sha256_digest

    with closing(IcebergRecordStorage(tmp_path)) as records:
        layer = records.write_layer([{"id": "a", "value": 1}], layer_kind="test", schema=SCHEMA, partition_policy=POLICY)
        data = next(ref for ref in records.physical_references(layer) if ref.locator.endswith('.parquet'))
        path = records.root / data.locator
        original = path.read_bytes()
        changed = bytes([original[0] ^ 1]) + original[1:]
        path.write_bytes(changed)
        receipt = path.with_name(path.name + '.sha256')
        value = json.loads(receipt.read_bytes())
        value['file']['digest'] = sha256_digest(changed)
        receipt.write_bytes(canonical_value_bytes(value))
        with pytest.raises(IntegrityError, match="checksum"):
            records.verify(layer)


def test_partition_replacement_refuses_an_identity_kept_elsewhere(tmp_path):
    from docspec.domain.storage import partition_bucket

    schema = RecordSchema("groups", ("id", "group"), "id", "group")
    policy = PartitionPolicy("groups", 8)
    group = next(str(index) for index in range(100) if partition_bucket(str(index), 8) != partition_bucket("a", 8))
    with closing(IcebergRecordStorage(tmp_path)) as records:
        base = records.write_layer([{"id": "one", "group": "a"}], layer_kind="test", schema=schema, partition_policy=policy)
        with pytest.raises(IntegrityError, match="duplicates"):
            records.write_layer([{"id": "one", "group": group}], layer_kind="test", schema=schema, partition_policy=policy,
                                base=base, replace_partitions=frozenset({partition_bucket(group, 8)}))
        assert list(records.stream(base)) == [{"id": "one", "group": "a"}]
