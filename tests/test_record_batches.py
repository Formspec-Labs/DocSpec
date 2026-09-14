"""Native handoffs preserve bytes and the storage owner's existing failure rules."""

from contextlib import closing

import pyarrow as pa
import pyarrow.compute as pc
import pytest

from docspec.adapters.storage import records as records_module
from docspec.ports.record_storage import bounded_batches
from docspec.adapters.storage.batches import BATCH_BYTES, BATCH_ROWS, encoded_batches
from docspec.adapters.storage.records import LocalParquetRecordStorage
from docspec.domain.identity import canonical_json_bytes
from docspec.domain.storage import PartitionPolicy, RecordSchema, partition_bucket
from docspec.errors import IntegrityError, LimitExceededError
from tests.test_parquet_arrow_stream import POLICY, SCHEMA, _row


PHYSICAL = pa.schema([
    ("record_identity", pa.string()), ("partition_value", pa.string()), ("record_json", pa.binary()),
])


def batches(count):
    return encoded_batches(
        ((_row(n)["recordId"], _row(n)["sourceItemId"], canonical_json_bytes(_row(n))) for n in range(count)),
        PHYSICAL, byte_column=2,
    )


def write(storage, values):
    return storage.write_batches(values, layer_kind="test-records", schema=SCHEMA, partition_policy=POLICY)


@pytest.mark.parametrize("partition_by_identity", [False, True])
def test_disjoint_union_shares_all_files_and_refuses_duplicate_identities(tmp_path, partition_by_identity):
    schema = RecordSchema(SCHEMA.schema_id, SCHEMA.fields, SCHEMA.identity_field,
                          SCHEMA.identity_field if partition_by_identity else SCHEMA.partition_field)
    with closing(LocalParquetRecordStorage(tmp_path)) as storage:
        def layer(start, stop):
            return storage.available(storage.write_batches(encoded_batches(
                ((_row(n)["recordId"], _row(n)[schema.partition_field], canonical_json_bytes(_row(n))) for n in range(start, stop)),
                PHYSICAL, byte_column=2), layer_kind="test-records", schema=schema, partition_policy=POLICY))
        base, delta = layer(0, 256), layer(256, 512)
        before = set(tmp_path.rglob("*.parquet"))
        combined = storage.union_disjoint(base, delta)
        assert set(tmp_path.rglob("*.parquet")) == before
        assert {member["path"] for member in combined._root["members"]} == {
            member["path"] for source in (base, delta) for member in source._root["members"]}
        storage.verify(combined.reference)
        assert list(storage.stream(combined.reference)) == [_row(n) for n in range(512)]
        assert storage.union_disjoint(base, layer(0, 0)) is base
        with pytest.raises(IntegrityError, match="disjoint"):
            storage.union_disjoint(base, layer(255, 257))
        extended = storage.union_disjoint(base, layer(255, 257), exclude_existing=True)
        assert list(storage.stream(extended.reference)) == [_row(n) for n in range(257)]
        assert storage.union_disjoint(base, layer(0, 1), exclude_existing=True) is base


def test_native_joins_reuse_admission_and_reject_another_store(tmp_path, monkeypatch):
    with closing(LocalParquetRecordStorage(tmp_path / "source")) as storage, closing(LocalParquetRecordStorage(tmp_path / "other")) as other:
        reference = write(storage, batches(300))
        admitted = storage.admit(reference)
        with monkeypatch.context() as guarded:
            def unexpected(*args, **kwargs):
                raise AssertionError("operation-scoped admission was repeated")
            guarded.setattr(storage, "_verified_root", unexpected)
            guarded.setattr(records_module, "_contained", unexpected)
            with storage.relations({"left_rows": admitted, "right_rows": admitted}) as relations:
                left = relations["left_rows"].project("record_identity AS left_id")
                right = relations["right_rows"].project("record_identity AS right_id")
                assert left.join(right, "left_id = right_id").aggregate("count(*)").fetchone() == (300,)
            assert sum(batch.num_rows for batch in admitted.batches()) == 300
            with pytest.raises(IntegrityError, match="another record store"), other.relations({"rows": admitted}):
                pass
        # A fresh reference still validates its descriptor.
        descriptor = storage.root / reference.state_ref
        descriptor.write_bytes(b"{}")
        with pytest.raises(IntegrityError, match="differs from its reference"), storage.relations({"rows": reference}):
            pass


def test_incremental_write_reuses_admitted_base_but_checks_fresh_references(tmp_path, monkeypatch):
    with closing(LocalParquetRecordStorage(tmp_path)) as storage:
        reference = write(storage, batches(300))
        admitted = storage.available(reference)
        with monkeypatch.context() as guarded:
            def unexpected(*args, **kwargs):
                raise AssertionError("incremental write repeated its base admission")
            guarded.setattr(storage, "_verified_root", unexpected)
            result = storage.retain_batches([], layer_kind="test-records", schema=SCHEMA, partition_policy=POLICY,
                                             base=admitted, replace_partitions=frozenset())
            assert result.reference == reference
        (storage.root / admitted._root["members"][0]["path"]).unlink()
        with pytest.raises(IntegrityError, match="unavailable"):
            storage.write_batches([], layer_kind="test-records", schema=SCHEMA, partition_policy=POLICY,
                                  base=reference, replace_partitions=frozenset())


@pytest.mark.parametrize("count", [0, 1, 4097])
def test_admitted_native_copy_and_queries_do_not_reparse_payloads(tmp_path, monkeypatch, count):
    with closing(LocalParquetRecordStorage(tmp_path)) as storage:
        original = write(storage, batches(count))
        admitted = storage.admit(original)
        with monkeypatch.context() as guarded:
            def unexpected(*args, **kwargs):
                raise AssertionError("admitted payload was parsed or encoded again")

            guarded.setattr(records_module, "parse_canonical_json", unexpected)
            # Root sealing uses canonical_json_file_bytes, independently of row admission.
            guarded.setattr(records_module, "canonical_json_bytes", unexpected)
            copied = write(storage, admitted.batches())
            with admitted.relation() as relation:
                assert relation.aggregate("count(*)").fetchone() == (count,)
                assert relation.filter("record_identity = 'record-00000'").fetchall() == (
                    [("record-00000", "source-00", canonical_json_bytes(_row(0)))] if count else []
                )
        storage.verify(copied)
        assert list(storage.stream(copied)) == [_row(n) for n in range(count)]


def test_native_partition_selection_and_cancel_keep_other_readers_usable(tmp_path):
    with closing(LocalParquetRecordStorage(tmp_path)) as storage:
        reference = write(storage, batches(1000))
        admitted = storage.admit(reference)
        with closing(admitted.batches()) as interrupted:
            assert next(interrupted).num_rows > 0
        bucket = partition_bucket("source-00", POLICY.bucket_count)
        selected = list(admitted.batches(partitions=frozenset({bucket})))
        actual = [row["record_identity"] for batch in selected for row in batch.to_pylist()]
        assert actual == [
            _row(n)["recordId"] for n in range(1000)
            if partition_bucket(_row(n)["sourceItemId"], POLICY.bucket_count) == bucket
        ]
        assert list(admitted.batches(partitions=frozenset())) == []
        with pytest.raises(ValueError, match="outside"):
            list(admitted.batches(partitions=frozenset({POLICY.bucket_count})))
        assert sum(batch.num_rows for batch in admitted.batches()) == 1000


@pytest.mark.parametrize("fail_after", [0, 3])
def test_native_producer_failure_keeps_original_error_and_closes_once(tmp_path, fail_after):
    failure = RuntimeError("native batch source failed")
    closed = []

    def source():
        try:
            for index, batch in enumerate(batches(8192)):
                if index == fail_after:
                    raise failure
                yield batch
        finally:
            closed.append(True)

    with closing(LocalParquetRecordStorage(tmp_path)) as storage:
        with pytest.raises(RuntimeError) as caught:
            write(storage, source())
        assert caught.value is failure
        assert closed == [True]
        assert not list(tmp_path.rglob("*.parquet"))
        assert not list(tmp_path.rglob("*.json"))
        storage.verify(write(storage, batches(1)))


@pytest.mark.parametrize("fault", ["duplicate", "null", "shape", "oversized"])
def test_native_writer_rejects_invalid_storage_handoffs(tmp_path, fault):
    batch = next(batches(1))
    if fault == "duplicate":
        incoming = [batch, batch]
    elif fault == "null":
        incoming = [batch.set_column(2, PHYSICAL.field(2), pa.array([None], type=pa.binary()))]
    elif fault == "shape":
        incoming = [batch.select(["record_json"])]
    else:
        incoming = [batch.set_column(2, PHYSICAL.field(2), pa.array([b"x" * (BATCH_BYTES + 1)]))]
    with closing(LocalParquetRecordStorage(tmp_path)) as storage:
        with pytest.raises((IntegrityError, LimitExceededError)):
            write(storage, incoming)
        assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("length,count", [(1, BATCH_ROWS + 1), (BATCH_BYTES // 2 + 1, 3), (BATCH_BYTES, 1)])
def test_row_and_native_handoffs_share_byte_and_row_limits(length, count):
    schema = pa.schema([("value", pa.binary())])
    value = b"x" * length
    encoded = list(encoded_batches(((value,) for _ in range(count)), schema, byte_column=0))
    native = list(bounded_batches([pa.record_batch([[value] * count], schema=schema)], byte_column="value"))
    assert [batch.num_rows for batch in encoded] == [batch.num_rows for batch in native]
    for batch in [*encoded, *native]:
        assert batch.num_rows <= BATCH_ROWS
        assert pc.sum(pc.binary_length(batch.column(0))).as_py() <= BATCH_BYTES


def test_native_slices_reuse_buffers_and_close_on_early_exit():
    value = b"x" * (BATCH_BYTES // 2 + 1)
    batch = pa.record_batch([[value, value, value]], names=["value"])
    closed = []

    def source():
        try:
            yield batch
            raise AssertionError("cancellation consumed the next source batch")
        finally:
            closed.append(True)

    with closing(bounded_batches(source(), byte_column="value")) as stream:
        part = next(stream)
        assert part.num_rows == 1
        assert part.column(0).buffers()[2].address == batch.column(0).buffers()[2].address
    assert closed == [True]


def test_new_write_admission_uses_one_writer_without_payload_rereads(tmp_path, monkeypatch):
    with closing(LocalParquetRecordStorage(tmp_path)) as storage:
        def unexpected(*args, **kwargs):
            raise AssertionError("newly written records were audited again")
        with monkeypatch.context() as guarded:
            guarded.setattr(storage, "admit", unexpected)
            guarded.setattr(storage, "_admit_members", unexpected)
            guarded.setattr(storage, "_rows", unexpected)
            admitted = storage.retain_batches(batches(4097), layer_kind="test-records", schema=SCHEMA, partition_policy=POLICY)
            copied = storage.retain_batches(admitted.batches(), layer_kind="test-records", schema=SCHEMA, partition_policy=POLICY)
            with copied.relation() as relation:
                assert relation.aggregate("count(*)").fetchone() == (4097,)
        storage.verify(copied.reference)


def test_member_keys_include_empty_whitespace_and_unicode_across_reopen(tmp_path):
    schema = RecordSchema("core-membership", ("member_key", "occurrence_id"), "member_key", "member_key")
    policy = PartitionPolicy("core-membership", 4)
    keys = ["", " ", "a", "é", "😀"]
    rows = [{"member_key": key, "occurrence_id": "same-occurrence"} for key in keys]
    with closing(LocalParquetRecordStorage(tmp_path)) as storage:
        reference = storage.write_layer(rows, layer_kind="core-membership", schema=schema, partition_policy=policy)
    with closing(LocalParquetRecordStorage(tmp_path)) as storage:
        storage.verify(reference)
        assert list(storage.stream(reference)) == rows
        for row in rows:
            assert storage.lookup(reference, row["member_key"], partition_value=row["member_key"]) == row


def test_native_joins_avoid_input_sorts_and_public_readers_keep_record_order(tmp_path):
    with closing(LocalParquetRecordStorage(tmp_path, max_member_bytes=16 * 1024)) as storage:
        admitted = storage.retain_batches(batches(600), layer_kind="test-records", schema=SCHEMA, partition_policy=POLICY)
        expected = [_row(index) for index in range(600)]
        with admitted.relation() as relation:
            assert "ORDER_BY" not in relation.explain()
            assert relation.aggregate("count(*)").fetchone() == (600,)
        with storage.relations({"left_rows": admitted.reference, "right_rows": admitted.reference}) as relations:
            left = relations["left_rows"].project("record_identity AS left_id")
            right = relations["right_rows"].project("record_identity AS right_id")
            joined = left.join(right, "left_id = right_id")
            assert "ORDER_BY" not in joined.explain()
            assert joined.aggregate("count(*)").fetchone() == (600,)
        with closing(admitted.batches()) as incoming:
            payloads = [payload for batch in incoming for payload in batch.column("record_json").to_pylist()]
        assert payloads == [canonical_json_bytes(row) for row in expected]
        assert list(storage.stream(admitted.reference)) == expected
        with closing(storage.lookup_batches(admitted.reference, [row["recordId"] for row in reversed(expected)])) as incoming:
            selected = [payload for batch in incoming for payload in batch.column("record_json").to_pylist()]
        assert selected == payloads
