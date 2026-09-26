"""The production Iceberg writer retains independent snapshots and writes only changed partitions.

An interrupted publication leaves no head behind; retained reads need no catalog, writes use an
in-process catalog only their own store reaches, a rewritten file plus matching checksum still fails
verification, and a partition replacement must not duplicate an identity held elsewhere.
"""

from contextlib import closing
from pathlib import Path
import gc
import hashlib
import shutil
import socket
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pyarrow.parquet as pq
import pytest

from docspec.adapters.storage import IcebergRecordStorage
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


def test_row_edits_keep_base_files_and_independent_branches(tmp_path):
    with closing(IcebergRecordStorage(tmp_path)) as records:
        rows = [{"id": f"{index:04d}", "value": index} for index in range(256)]
        base = records.available(records.write_layer(rows, layer_kind="test", schema=SCHEMA, partition_policy=POLICY))
        original = {path: hashlib.sha256(Path(path).read_bytes()).digest() for path in data_files(base)}
        # The writer states its codec, so data files do not depend on which catalog created the table.
        assert {pq.ParquetFile(path).metadata.row_group(0).column(0).compression for path in original} == {"ZSTD"}
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
        with closing(IcebergRecordStorage(tmp_path)) as offline:
            assert list(offline.stream(left.reference)) == expected + [{"id": "new", "value": 7}]
            assert offline._catalog is None
        # Reopening the same owner also reattaches the catalog for later writes.
        again = records.apply_changes(right, changes([("0004", None)]))
        assert again.reference.record_count == 254
        records.close()
        moved = tmp_path.with_name(tmp_path.name + "-moved")
        hidden = tmp_path.with_name(tmp_path.name + "-hidden")
        shutil.copytree(tmp_path, moved)
        tmp_path.rename(hidden)
        try:
            with closing(IcebergRecordStorage(moved)) as offline:
                assert list(offline.stream(left.reference)) == expected + [{"id": "new", "value": 7}]
                offline.verify(left.reference)
                with pytest.raises(IntegrityError, match="relocated"):
                    offline.apply_changes(offline.available(left.reference), changes([("0001", 3)]))
        finally:
            hidden.rename(tmp_path)


def test_failed_pin_leaves_no_catalog_head_or_changed_base(tmp_path, monkeypatch):
    with closing(IcebergRecordStorage(tmp_path)) as records:
        base = records.available(records.write_layer([{"id": "a", "value": 1}], layer_kind="test", schema=SCHEMA, partition_policy=POLICY))
        client = records._client()
        before = set(client.list_tables(records._catalog.namespace))
        def fail(*args, **kwargs):
            raise OSError("publication interrupted")
        monkeypatch.setattr(records, "_pin", fail)
        with pytest.raises(OSError, match="interrupted"):
            records.apply_changes(base, changes([("a", 2)]))
        assert set(client.list_tables(records._catalog.namespace)) == before
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


def serving(port):
    """Whether anything still accepts connections on a loopback port (a refusal by HTTP status still counts)."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=5).close()
    except ConnectionRefusedError:
        return False
    return True


def test_writes_use_an_in_process_catalog_only_their_store_reaches(tmp_path):
    # URL syntax in a scratch path must neither move the catalog file nor let two stores share it.
    scratch = tmp_path / "scratch?x=1#y"
    with closing(IcebergRecordStorage(tmp_path / "a", merge_scratch_root=scratch)) as records, \
            closing(IcebergRecordStorage(tmp_path / "b", merge_scratch_root=scratch)) as other:
        assert records._catalog is None
        layer = records.write_layer([{"id": "a", "value": 1}], layer_kind="test", schema=SCHEMA, partition_policy=POLICY)
        other.write_layer([{"id": "b", "value": 2}], layer_kind="test", schema=SCHEMA, partition_policy=POLICY)
        assert (Path(records._scratch.name) / "iceberg-catalog.sqlite").is_file()
        assert sorted(path.name for path in tmp_path.iterdir()) == ["a", "b", scratch.name]
        catalog = records._catalog
        assert catalog.client().list_tables(catalog.namespace) == []
        port = catalog._server.server_port
        config = f"http://127.0.0.1:{port}/v1/config"
        with pytest.raises(HTTPError) as refused:
            urlopen(config, timeout=5)
        assert refused.value.code == 401
        register = Request(config.replace("config", "namespaces/docspec/register"), data=b"{}",
                           headers={"Authorization": "Bearer " + catalog._server.token})
        with pytest.raises(HTTPError) as unsupported:
            urlopen(register, timeout=5)
        assert unsupported.value.code == 400 and b"unsupported" in unsupported.value.read()
    assert not serving(port)
    # A store dropped without close() still stops its catalog.
    dropped = IcebergRecordStorage(tmp_path / "a")
    dropped.write_layer([{"id": "c", "value": 3}], layer_kind="test", schema=SCHEMA, partition_policy=POLICY)
    port = dropped._catalog._server.server_port
    assert serving(port)
    del dropped
    gc.collect()
    assert not serving(port)
    with closing(IcebergRecordStorage(tmp_path / "a")) as reopened:
        assert list(reopened.stream(layer)) == [{"id": "a", "value": 1}]


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
