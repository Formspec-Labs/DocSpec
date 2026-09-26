"""Typed table layers: native writes, producer files registered by reference, and profiles refusing each other.

Registration must keep the producer's exact bytes, seal them under the member
digest in a directory named by it, read the file's own columns, and refuse a
stage outside the store, field IDs, a footer unlike its declaration, an
oversized row group, a type Iceberg cannot hold and a changed byte, keeping
the stage. A retry reuses an interrupted placement. A relocated copy must
verify, forged roots must not, and each record profile must refuse the
other's layers.
"""

from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import math
import shutil
import struct

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from docspec.adapters.storage import IcebergRecordStorage, records as records_module
from docspec.adapters.storage.batches import table_arrow_schema
from docspec.domain.identity import canonical_json_file_bytes, sha256_digest, stable_urn
from docspec.domain.references import LayerRef
from docspec.domain.storage import PartitionPolicy, RecordSchema, TableSchema
from docspec.errors import IntegrityError, LimitExceededError

COLUMNS = (("key", "VARCHAR"), ("flag", "BOOLEAN"), ("count", "INTEGER"), ("big", "BIGINT"), ("ratio", "DOUBLE"),
           ("day", "DATE"), ("moment", "TIMESTAMP"), ("zoned", "TIMESTAMPTZ"), ("items", "VARCHAR[]"), ("hash", "BLOB"))
SCHEMA = TableSchema("table-test:1", COLUMNS)
NEGATIVE_NAN = struct.unpack(">d", bytes.fromhex("fff8000000000001"))[0]
ROWS = [
    {"key": "a\x1f\"\\u001F", "flag": True, "count": -(2**31), "big": 2**53 + 1, "ratio": NEGATIVE_NAN,
     "day": date(1, 1, 1), "moment": datetime(2026, 9, 25, 1, 2, 3, 4),
     "zoned": datetime(2026, 9, 25, 1, 2, 3, tzinfo=timezone.utc), "items": ["x\x00", None], "hash": bytes(32)},
    {"key": "b", "flag": None, "count": None, "big": -(2**63), "ratio": -0.0, "day": date(9999, 12, 31),
     "moment": None, "zoned": None, "items": [], "hash": None},
]
RECORDS = RecordSchema("records-test:1", ("id", "value"), "id", "id")
POLICY = PartitionPolicy("keys", 1)
TABLE_PROFILE = "urn:docspec:profile:table-storage:iceberg:1"


def arrow(rows, columns=COLUMNS):
    """Build an Arrow table in the declared table-profile types."""
    return pa.Table.from_pylist(rows, schema=table_arrow_schema(columns))


def producer_file(path, table, **options):
    """Write ``table`` as a producer would, returning its member digest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, **options)
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def member_directory(records, digest):
    return records.root / "iceberg" / f"member-{digest[7:]}"


def forged(records, layer, **changes):
    """Write ``layer``'s root with ``changes`` and return a reference that matches the forged root."""
    root = {**layer._root, **changes}
    payload = canonical_json_file_bytes(root)
    digest = sha256_digest(payload)
    locator = f"record-layers/sha256/{digest[7:9]}/{digest[7:]}.json"
    (records.root / locator).parent.mkdir(parents=True, exist_ok=True)
    (records.root / locator).write_bytes(payload)
    return LayerRef(stable_urn("record-layer", root), root["layerKind"], root["schema"]["schemaId"],
                    layer.reference.profile_id, locator, digest, root["recordCount"])


def rows_of(layer):
    """Read a layer's rows through Arrow, which converts zoned timestamps without pytz."""
    with layer.relation() as relation:
        return relation.order("key").to_arrow_table().to_pylist()


def test_native_write_reads_back_its_declared_columns_with_field_ids(tmp_path):
    with closing(IcebergRecordStorage(tmp_path)) as records:
        layer = records.write_table(arrow(ROWS).to_batches(), layer_kind="test-table", schema=SCHEMA)
        assert layer.reference.profile_id == TABLE_PROFILE and layer.reference.record_count == 2
        assert layer.member_digest is None and records.schema(layer.reference) == SCHEMA
        records.verify(layer.reference)
        with records.relations({"table": layer.reference}) as relations:
            assert relations["table"].columns == list(SCHEMA.fields)
        first, second = rows_of(records.available(layer.reference))
        assert {**first, "ratio": None} == {**ROWS[0], "ratio": None} and math.isnan(first["ratio"])
        assert second == ROWS[1] and math.copysign(1, second["ratio"]) == -1
        for locator in records.data_files(layer.reference):
            assert all(b"PARQUET:field_id" in field.metadata for field in pq.read_schema(tmp_path / locator))


def test_native_writes_bound_row_groups_files_and_sort_each_file(tmp_path):
    limit = 4 * 1024**2
    schema = TableSchema("sorted:1", (("hash", "BLOB"), ("text", "VARCHAR")))
    digests = [hashlib.sha256(str(index).encode()).digest() for index in range(60_000)]
    table = pa.table({"hash": digests, "text": [digest.hex() * 2 for digest in digests]},
                     schema=table_arrow_schema(schema.columns))
    with closing(IcebergRecordStorage(tmp_path, max_member_bytes=limit)) as records:
        layer = records.write_table(table.to_batches(max_chunksize=4096), layer_kind="sorted", schema=schema,
                                    sort_by=("hash",))
        files = [tmp_path / locator for locator in records.data_files(layer.reference)]
        groups = [pq.ParquetFile(path).metadata for path in files]
        assert sum(metadata.num_row_groups for metadata in groups) > 1
        assert all(path.stat().st_size <= limit for path in files)
        # The writer targets 1 MiB row groups and half-limit files.
        assert all(metadata.row_group(index).total_byte_size <= 2 * 1024**2
                   for metadata in groups for index in range(metadata.num_row_groups))
        for path in files:
            values = pq.read_table(path).column("hash").to_pylist()
            assert values == sorted(values)
        assert layer.reference.record_count == 60_000
        with pytest.raises(ValueError, match="sort columns"):
            records.write_table(table.to_batches(), layer_kind="sorted", schema=schema, sort_by=("absent",))


def test_table_batches_must_match_their_declared_schema(tmp_path):
    schema = TableSchema("moments:1", (("at", "TIMESTAMP"), ("zoned", "TIMESTAMPTZ"), ("count", "INTEGER")))
    values = {"at": [datetime(2026, 1, 1)], "zoned": [datetime(2026, 1, 1, tzinfo=timezone.utc)], "count": [1]}
    def typed(at, zoned, count):
        return pa.table(values, schema=pa.schema([("at", at), ("zoned", zoned), ("count", count)]))
    with closing(IcebergRecordStorage(tmp_path)) as records:
        for table in (typed(pa.timestamp("ns"), pa.timestamp("us", "UTC"), pa.int32()),
                      typed(pa.timestamp("us"), pa.timestamp("us", "UTC"), pa.int64()),
                      typed(pa.timestamp("us"), pa.timestamp("us"), pa.int32()),
                      typed(pa.timestamp("us"), pa.timestamp("us", "UTC"), pa.int32()).select(["at", "count"])):
            with pytest.raises(IntegrityError, match="declared schema"):
                records.write_table(table.to_batches(), layer_kind="moments", schema=schema)
        # Another zone names the same instants; only that difference conforms.
        layer = records.write_table(typed(pa.timestamp("us"), pa.timestamp("us", "America/New_York"), pa.int32()).to_batches(),
                                    layer_kind="moments", schema=schema)
        with layer.relation() as relation:
            assert relation.to_arrow_table().to_pylist() == [
                {"at": datetime(2026, 1, 1), "zoned": datetime(2026, 1, 1, tzinfo=timezone.utc), "count": 1}]


def test_appends_share_base_files_and_leave_the_base_unchanged(tmp_path):
    with closing(IcebergRecordStorage(tmp_path)) as records:
        base = records.write_table(arrow(ROWS[:1]).to_batches(), layer_kind="test-table", schema=SCHEMA)
        grown = records.append_table(base.reference, arrow(ROWS[1:]).to_batches())
        assert set(records.data_files(base.reference)) < set(records.data_files(grown.reference))
        assert (base.reference.record_count, grown.reference.record_count) == (1, 2)
        assert [row["key"] for row in rows_of(records.available(base.reference))] == [ROWS[0]["key"]]
        assert [row["key"] for row in rows_of(grown)] == [ROWS[0]["key"], "b"]
        records.verify(grown.reference)
        assert records.append_table(grown, arrow([]).to_batches()) is grown


def test_typed_changes_replace_rows_by_key_in_one_snapshot_sharing_base_files(tmp_path):
    columns = (("key", "VARCHAR"), ("part", "INTEGER"), ("text", "VARCHAR"))
    schema, arrow_schema = TableSchema("parts:1", columns), table_arrow_schema(columns)
    rows = [{"key": key, "part": part, "text": f"{key}{part}"} for key in "ab" for part in range(2)]
    keys = pa.schema([("key", pa.string()), ("part", pa.int32())])
    with closing(IcebergRecordStorage(tmp_path)) as records:
        base = records.write_table(pa.Table.from_pylist(rows, schema=arrow_schema).to_batches(), layer_kind="parts",
                                   schema=schema, sort_by=("key", "part"))
        with records._cursor() as cursor:
            def changes(values):
                return cursor.from_arrow(pa.Table.from_pylist(values, schema=arrow_schema))

            def removed(values):
                return cursor.from_arrow(pa.Table.from_pylist(values, schema=keys))
            revised = records.apply_changes(base, changes([{"key": "a", "part": 1, "text": "new"},
                                                           {"key": "c", "part": 0, "text": "added"}]),
                                            key=("key", "part"), removed=removed([{"key": "b", "part": 0}]), cursor=cursor)
            assert records.apply_changes(revised, changes([]), key=("key", "part"), removed=removed([]),
                                         cursor=cursor) is revised
            with pytest.raises(IntegrityError, match="repeat a key"):
                records.apply_changes(revised, changes([rows[0], {**rows[0], "text": "again"}]), key=("key", "part"),
                                      removed=removed([]), cursor=cursor)
            with pytest.raises(IntegrityError, match="table and key columns"):
                records.apply_changes(revised, changes([]), key=("key",), removed=removed([]), cursor=cursor)
        assert set(records.data_files(base.reference)) < set(records.data_files(revised.reference))
        assert revised.reference.record_count == 4
        with revised.relation() as relation:
            assert relation.order("key, part").fetchall() == [("a", 0, "a0"), ("a", 1, "new"), ("b", 1, "b1"),
                                                              ("c", 0, "added")]
        records.verify(revised.reference)
        assert sorted(row["text"] for row in rows_of(records.available(base.reference))) == ["a0", "a1", "b0", "b1"]


def test_encoded_and_table_profiles_refuse_each_other(tmp_path):
    with closing(IcebergRecordStorage(tmp_path)) as records:
        table = records.write_table(arrow(ROWS).to_batches(), layer_kind="test-table", schema=SCHEMA)
        encoded = records.available(records.write_layer([{"id": "a", "value": 1}], layer_kind="records",
                                                        schema=RECORDS, partition_policy=POLICY))
        encoded_only = [
            lambda: list(records.stream(table.reference)),
            lambda: records.lookup(table.reference, "a"),
            lambda: list(records.lookup_batches(table.reference, ["a"])),
            lambda: records.union_disjoint(table, table),
            lambda: records.compact(table),
            lambda: records.partition_policy(table.reference),
            lambda: records.identity_field(table.reference),
            lambda: records.write_batches(iter(()), layer_kind="records", schema=RECORDS, partition_policy=POLICY,
                                          base=table.reference, replace_partitions=frozenset()),
        ]
        for call in encoded_only:
            with pytest.raises(IntegrityError, match="encoded-record profile refuses"):
                call()
        # A typed table takes whole rows and removed keys, never routing batches.
        with pytest.raises(ValueError, match="typed changes require"):
            records.apply_changes(table, iter(()))
        with pytest.raises(ValueError, match="only a typed table"):
            records.apply_changes(encoded, iter(()), key=("id",))
        with pytest.raises(IntegrityError, match="encoded-record profile refuses"):
            with records.relations({"table": table.reference}, identities={"table": ["a"]}):
                pytest.fail("record routing applied to a typed table")
        with pytest.raises(IntegrityError, match="table profile refuses"):
            records.append_table(encoded, arrow(ROWS).to_batches())
        with pytest.raises(IntegrityError, match="write_table"):
            records.write_batches(iter(()), layer_kind="records", schema=SCHEMA, partition_policy=POLICY)
        with pytest.raises(IntegrityError, match="write_batches"):
            records.write_table(iter(()), layer_kind="test-table", schema=RECORDS)
        # The profile is part of the reference the root must reproduce.
        for reference, profile in ((table.reference, "urn:docspec:profile:record-storage:iceberg:1"),
                                   (encoded.reference, TABLE_PROFILE)):
            with pytest.raises(IntegrityError, match="differs from its reference"):
                records.available(replace(reference, profile_id=profile))


def test_registration_keeps_the_producer_bytes_and_reads_their_columns(tmp_path):
    columns = (("document_number", "VARCHAR"), ("pages", "BIGINT"), ("ratio", "DOUBLE"))
    table = pa.table({"document_number": [f"2026-{index:05d}" for index in range(4_000)],
                      "pages": list(range(4_000)), "ratio": [index / 7 for index in range(4_000)]})
    store = tmp_path / "store"
    with closing(IcebergRecordStorage(store, max_member_bytes=32 * 1024)) as records:
        staged = records.staging_directory / "generation" / "federal_register.parquet"
        digest = producer_file(staged, table, row_group_size=500)
        original = staged.read_bytes()
        assert len(original) > records.max_member_bytes
        layer = records.register_parquet(staged, layer_kind="producer-table",
                                         schema=TableSchema("federal-register:1", columns), member_digest=digest)
        assert not staged.exists() and layer.member_digest == digest
        assert layer.reference.profile_id == TABLE_PROFILE and layer.reference.record_count == 4_000
        [locator] = records.data_files(layer.reference)
        assert store / locator == member_directory(records, digest) / "data" / "member.parquet"
        assert (store / locator).read_bytes() == original
        assert {ref.locator: ref.digest for ref in records.physical_references(layer.reference)}[locator] == digest
        assert "schema.name-mapping.default" in layer.table.metadata.properties
        records.verify(layer.reference)
        with records.relations({"table": layer.reference}) as relations:
            assert relations["table"].columns == [name for name, _ in columns]
            assert relations["table"].order("document_number").fetchall() == list(zip(*table.to_pydict().values()))


def test_a_store_copied_without_its_staging_directory_stages_and_registers(tmp_path):
    """git cannot track the empty staging directory, so a store copied from a tracked fixture has none."""
    columns = (("key", "VARCHAR"), ("value", "BIGINT"))
    table = pa.table({"key": ["a", "b"], "value": [1, 2]})
    with closing(IcebergRecordStorage(tmp_path / "store")):
        pass
    (tmp_path / "store" / ".staging").rmdir()
    with closing(IcebergRecordStorage(tmp_path / "store", create=False)) as records:
        assert not (records.root / ".staging").exists()
        staged = records.staging_directory / "values.parquet"
        pq.write_table(table, staged)  # Not producer_file, which would make the directory itself.
        layer = records.register_parquet(staged, layer_kind="producer-table", schema=TableSchema("values:1", columns),
                                         member_digest=sha256_digest(staged.read_bytes()))
        records.verify(layer.reference)
        with records.relations({"table": layer.reference}) as relations:
            assert relations["table"].aggregate("count(*), sum(value)").fetchone() == (2, 3)


def test_a_relocated_registered_table_verifies_and_a_flipped_byte_refuses(tmp_path):
    columns = (("key", "VARCHAR"), ("value", "BIGINT"))
    table = pa.table({"key": [f"k{index}" for index in range(1_000)], "value": list(range(1_000))})
    store, moved, hidden = tmp_path / "store", tmp_path / "moved", tmp_path / "hidden"
    with closing(IcebergRecordStorage(store)) as records:
        staged = records.staging_directory / "values.parquet"
        layer = records.register_parquet(staged, layer_kind="producer-table", schema=TableSchema("values:1", columns),
                                         member_digest=producer_file(staged, table))
        [locator] = records.data_files(layer.reference)
    shutil.copytree(store, moved)
    store.rename(hidden)
    with closing(IcebergRecordStorage(moved, create=False)) as relocated:
        relocated.verify(layer.reference)
        with relocated.relations({"table": layer.reference}) as relations:
            assert relations["table"].aggregate("count(*), sum(value)").fetchone() == (1_000, sum(range(1_000)))
        payload = bytearray((moved / locator).read_bytes())
        payload[len(payload) // 2] ^= 0xFF
        (moved / locator).write_bytes(payload)
        with pytest.raises(IntegrityError, match="differs from its checksum"):
            relocated.verify(layer.reference)


@pytest.mark.parametrize("case", ["outside", "symlink", "field-ids", "footer", "nanoseconds", "utc-nanoseconds",
                                  "row-group", "flipped-byte"])
def test_registration_refusals_keep_the_stage(tmp_path, case):
    columns = (("key", "VARCHAR"), ("at", "TIMESTAMP"))
    fields = [pa.field("key", pa.string()), pa.field("at", pa.timestamp("us"))]
    if case == "field-ids":
        fields = [field.with_metadata({"PARQUET:field_id": str(index + 1)}) for index, field in enumerate(fields)]
    elif case == "footer":
        columns = (("key", "VARCHAR"), ("at", "TIMESTAMPTZ"))
    elif case == "nanoseconds":
        fields[1] = pa.field("at", pa.timestamp("ns"))
    elif case == "utc-nanoseconds":
        # DuckDB reads these as TIMESTAMPTZ, truncating; Iceberg refuses them.
        columns, fields[1] = (("key", "VARCHAR"), ("at", "TIMESTAMPTZ")), pa.field("at", pa.timestamp("ns", "UTC"))
    table = pa.table({"key": [f"k{index}" for index in range(2_000)],
                      "at": [datetime(2026, 1, 1, second=index % 60) for index in range(2_000)]}, schema=pa.schema(fields))
    with closing(IcebergRecordStorage(tmp_path / "store", max_member_bytes=16 * 1024)) as records:
        staged = records.staging_directory / "values.parquet"
        if case == "outside":
            staged = tmp_path / "outside" / "values.parquet"
        digest = producer_file(staged, table, row_group_size=2_000 if case == "row-group" else 200)
        if case == "symlink":
            target = tmp_path / "elsewhere.parquet"
            staged.rename(target)
            staged.symlink_to(target)
        if case == "flipped-byte":
            payload = bytearray(staged.read_bytes())
            payload[len(payload) // 3] ^= 0x01
            staged.write_bytes(payload)
        original = staged.read_bytes()
        expected = {"outside": (IntegrityError, "staging directory"), "symlink": (IntegrityError, "staging directory"),
                    "field-ids": (IntegrityError, "field IDs"), "footer": (IntegrityError, "footer differs"),
                    "nanoseconds": (IntegrityError, "footer differs"), "utc-nanoseconds": (IntegrityError, "Iceberg refused"),
                    "row-group": (LimitExceededError, "row group"), "flipped-byte": (IntegrityError, "member digest")}[case]
        with pytest.raises(expected[0], match=expected[1]):
            records.register_parquet(staged, layer_kind="producer-table", schema=TableSchema("values:1", columns),
                                     member_digest=digest)
        assert staged.read_bytes() == original and not member_directory(records, digest).exists()


def test_a_retry_reuses_an_interrupted_placement_and_refuses_other_bytes(tmp_path):
    columns = (("key", "VARCHAR"), ("value", "BIGINT"))
    table = pa.table({"key": ["a", "b"], "value": [1, 2]})
    with closing(IcebergRecordStorage(tmp_path / "store")) as records:
        staged = records.staging_directory / "values.parquet"
        digest = producer_file(staged, table)
        # An attempt interrupted after placing the member left its directory behind.
        placed = member_directory(records, digest) / "data" / "member.parquet"
        placed.parent.mkdir(parents=True)
        placed.write_bytes(staged.read_bytes())
        layer = records.register_parquet(staged, layer_kind="producer-table", schema=TableSchema("values:1", columns),
                                         member_digest=digest)
        assert not staged.exists() and records.data_files(layer.reference) == (placed.relative_to(records.root).as_posix(),)
        records.verify(layer.reference)
        # A directory named for this member but holding other bytes refuses and is left alone.
        other = pa.table({"key": ["c"], "value": [3]})
        staged = records.staging_directory / "other.parquet"
        other_digest = producer_file(staged, other)
        foreign = member_directory(records, other_digest) / "data" / "member.parquet"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"not the member")
        with pytest.raises(IntegrityError, match="member digest"):
            records.register_parquet(staged, layer_kind="producer-table", schema=TableSchema("values:1", columns),
                                     member_digest=other_digest)
        assert staged.exists() and foreign.read_bytes() == b"not the member"


def test_forged_table_roots_and_appends_to_sealed_tables_refuse(tmp_path, monkeypatch):
    columns = (("key", "VARCHAR"), ("value", "BIGINT"))
    table = pa.table({"key": ["a", "b"], "value": [1, 2]})
    with closing(IcebergRecordStorage(tmp_path / "store")) as records:
        staged = records.staging_directory / "values.parquet"
        layer = records.register_parquet(staged, layer_kind="producer-table", schema=TableSchema("values:1", columns),
                                         member_digest=producer_file(staged, table))
        with pytest.raises(IntegrityError, match="sealed and refuses appended rows"):
            records.append_table(layer, pa.table(table.to_pydict(), schema=table_arrow_schema(columns)).to_batches())
        with records._cursor() as cursor, pytest.raises(IntegrityError, match="sealed and refuses changed rows"):
            records.apply_changes(layer, cursor.from_arrow(table), key=("key",), removed=cursor.from_arrow(table.select(["key"])),
                                  cursor=cursor)
        narrowed = {"schemaId": "values:1", "columns": [["key", "VARCHAR"], ["value", "INTEGER"]]}
        with pytest.raises(IntegrityError, match="pinned Iceberg metadata"):
            records.available(forged(records, layer, schema=narrowed))
        with pytest.raises(IntegrityError, match="row count differs"):
            records.verify(forged(records, layer, recordCount=3))
        with pytest.raises(IntegrityError, match="registered table differs from its producer member"):
            records.verify(forged(records, layer, memberDigest="sha256:" + "0" * 64))
        # Should DuckDB read a column otherwise than the Iceberg mapping says, verify sees it.
        monkeypatch.setattr(records_module, "table_columns", lambda schema: (("key", "VARCHAR"), ("value", "INTEGER")))
        with pytest.raises(IntegrityError, match="scan columns differ"):
            records.verify(forged(records, layer, schema=narrowed))
