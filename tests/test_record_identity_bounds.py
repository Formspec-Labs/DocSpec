"""File pruning preserves exact identities and requires truthful admission."""

from contextlib import closing, contextmanager

import pyarrow.parquet as pq
import pytest

from docspec.adapters.storage.batches import ENCODED_RECORD_SCHEMA, encoded_batches
from docspec.adapters.storage.records import LocalParquetRecordStorage
from docspec.domain.identity import canonical_json_bytes
from docspec.domain.storage import PartitionPolicy, RecordSchema
from docspec.errors import IntegrityError
from tests.test_bounded_partitions import _pin_layer


SCHEMA = RecordSchema("bounds/1", ("id", "group", "value"), "id", "group")
POLICY = PartitionPolicy("single", 1)


def layer(storage, identities, *, ordered=True):
    return storage.retain_batches(
        encoded_batches(((identity, "same", canonical_json_bytes({"id": identity, "group": "same", "value": identity}))
                         for identity in sorted(identities, reverse=not ordered)), ENCODED_RECORD_SCHEMA, byte_column=2),
        layer_kind="bounds", schema=SCHEMA, partition_policy=POLICY,
        target_member_bytes=160, ordered=ordered,
    )


@contextmanager
def observed_files(storage, monkeypatch):
    """Observe actual file lists entering native scans, without replacing queries."""
    cursor = storage._cursor
    opened = []

    class ObservedCursor:
        def __init__(self, native):
            self.native = native

        def __getattr__(self, name):
            return getattr(self.native, name)

        def read_parquet(self, paths, **kwargs):
            opened.append(tuple(paths))
            return self.native.read_parquet(paths, **kwargs)

    @contextmanager
    def observe():
        with cursor() as native:
            yield ObservedCursor(native)

    with monkeypatch.context() as guarded:
        guarded.setattr(storage, "_cursor", observe)
        yield opened


@pytest.mark.parametrize("ordered", [True, False])
def test_writer_bounds_preserve_long_unicode_identities_and_native_lookup(tmp_path, monkeypatch, ordered):
    identities = ["", "\0", "A", "a", "e\u0301", "é", "中", "\uffff", "😀", "z" * 10_000]
    with closing(LocalParquetRecordStorage(tmp_path)) as storage:
        admitted = layer(storage, identities, ordered=ordered)
        for member in admitted._root["members"]:
            actual = pq.read_table(storage.root / member["path"], columns=["record_identity"]).column(0).to_pylist()
            assert (member["identityMin"], member["identityMax"]) == (min(actual), max(actual))
        storage.admit(admitted.reference)
        with observed_files(storage, monkeypatch) as opened:
            rows = [row for batch in storage.lookup_batches(admitted.reference, ["e\u0301", "é", "absent"]) for row in batch.to_pylist()]
        assert [row["record_identity"] for row in rows] == ["e\u0301", "é"]
        assert len(set(path for paths in opened for path in paths)) < len(admitted._root["members"])
        assert storage.lookup(admitted.reference, "z" * 10_000)["value"] == "z" * 10_000


@pytest.mark.parametrize("bounds", [("n", "z"), ("a", "l")])
def test_full_admission_rejects_repinned_bounds_that_hide_real_rows(tmp_path, bounds):
    with closing(LocalParquetRecordStorage(tmp_path)) as storage:
        admitted = layer(storage, ["m"])
        member, = admitted._root["members"]
        forged = _pin_layer(storage, SCHEMA, POLICY, [{**member, "identityMin": bounds[0], "identityMax": bounds[1]}])
        storage.verify_members(forged)  # Real pinned bytes and schema do not establish logical bounds.
        with pytest.raises(IntegrityError, match="bound"):
            storage.lookup(forged, "m")
        with pytest.raises(IntegrityError, match="bound"):
            storage.admit(forged)


@pytest.mark.parametrize("bounds", [("z", "a"), (None, "z"), ("a", 4)])
def test_invalid_bounds_are_rejected_before_opening_members(tmp_path, bounds):
    with closing(LocalParquetRecordStorage(tmp_path)) as storage:
        admitted = layer(storage, ["m"])
        member, = admitted._root["members"]
        forged = _pin_layer(storage, SCHEMA, POLICY, [{**member, "identityMin": bounds[0], "identityMax": bounds[1]}])
        with pytest.raises((IntegrityError, ValueError)):
            storage.available(forged)


def test_disjoint_range_union_skips_base_scans_and_keeps_every_file(tmp_path, monkeypatch):
    with closing(LocalParquetRecordStorage(tmp_path)) as storage:
        base = layer(storage, [f"root-{i:04}" for i in range(40)])
        delta = layer(storage, ["added-1", "added-2"])
        base_files = {str(storage.root / member["path"]) for member in base._root["members"]}
        with observed_files(storage, monkeypatch) as opened:
            combined = storage.union_disjoint(base, delta)
        assert base_files.isdisjoint(path for paths in opened for path in paths)
        assert {member["path"] for member in combined._root["members"]} == {
            member["path"] for item in (base, delta) for member in item._root["members"]}
        storage.admit(combined.reference)
        assert [row["id"] for row in storage.stream(combined.reference)] == ["added-1", "added-2", *[f"root-{i:04}" for i in range(40)]]
        overlapping = layer(storage, ["root-0000", "root-9999"])
        with pytest.raises(IntegrityError, match="disjoint"):
            storage.union_disjoint(base, overlapping)
        filtered = storage.union_disjoint(base, overlapping, exclude_existing=True)
        assert filtered.reference.record_count == 41
        assert storage.lookup(filtered.reference, "root-9999")["value"] == "root-9999"
