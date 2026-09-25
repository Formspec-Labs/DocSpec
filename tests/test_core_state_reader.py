"""Reopening an existing state reads exact membership, occurrence and value rows while checking pinned bytes.

Mismatched pins or damaged payloads refuse before delivery, published-state reuse must not repeat a full
semantic admission, and a reader needs retained metadata rather than merely available rows. Ordered reads
join payloads per window of compact addresses yet deliver exactly one global order.
"""

from contextlib import closing, contextmanager
from dataclasses import replace
import json
import subprocess
import sys

import pyarrow as pa
import pytest

from docspec.domain import core
from docspec.errors import IntegrityError, StateTransitionError, StateValueRelationUnavailable
from docspec.ports.record_storage import BATCH_ROWS, bounded_batches
from docspec.runtime import CoreWorkspace


def _fixture(path):
    """Create the initial/updated/selected state fixture these tests reopen."""
    with CoreWorkspace(path) as workspace:
        workspace.create("initial", [("keep", {"title": "Kept"}), ("update", {"title": "Old"}),
                                     ("remove", {"title": "Removed"})])
        changed = workspace.upsert("initial", [("update", {"title": "Updated"})], batch_id="update")
        workspace.revise(core.Revision(format_version=1, revision_id="remove", base_state_id=changed.state_id,
            result_state_id="selected", edits=(core.Remove(sequence=0, member_key="remove"),)))


@pytest.mark.parametrize("existing", [False, True])
def test_open_existing_refuses_without_initializing(tmp_path, existing):
    path = tmp_path / "missing"
    if existing:
        path.mkdir()
    before = list(tmp_path.rglob("*"))
    with pytest.raises(IntegrityError, match="existing directory"):
        CoreWorkspace(path, create=False)
    assert list(tmp_path.rglob("*")) == before


def test_reopened_reader_preserves_membership_pin_and_native_rows(tmp_path):
    _fixture(tmp_path)
    with CoreWorkspace(tmp_path, create=False) as workspace:
        with workspace.open_state("selected") as reader:
            pin = reader.pin
            assert pin.startswith("sha256:") and reader.state_id == "selected" and reader.record_count == 2
            rows = dict(reader.rows())
            assert {key: entity.value.value for key, entity in rows.items()} == {
                "keep": {"title": "Kept"}, "update": {"title": "Updated"},
            }
            assert reader.lookup("remove") is None
            assert reader.lookup("update", occurrence_id=rows["update"].entity_id) == rows["update"]
            with pytest.raises(IntegrityError, match="occurrence identity"):
                reader.lookup("update", occurrence_id="not-this-occurrence")
            with pytest.raises(IntegrityError, match="occurrence identity"):
                reader.lookup("remove", occurrence_id="old")
            with closing(reader.batches()) as batches:
                assert [row["member_key"] for batch in batches for row in batch.to_pylist()] == ["keep", "update"]
            with reader.relation() as relation:
                assert relation.order("member_key").project("member_key").fetchall() == [("keep",), ("update",)]
                sql = relation.sql_query()
                assert "iceberg_scan" in sql and "version_name_format" in sql
            with reader.value_relation() as relation:
                assert relation.order("member_key").project("value::VARCHAR").fetchall() == [
                    ('{"title":"Kept"}',), ('{"title":"Updated"}',),
                ]
        with pytest.raises(StateTransitionError, match="closed"):
            reader.lookup("keep")
    with CoreWorkspace(tmp_path, create=False) as workspace:
        with workspace.open_state("selected", expected_pin=pin) as reader:
            assert reader.pin == pin and reader.read_value("update") == {"title": "Updated"}


def test_windowed_payload_joins_deliver_one_global_order(tmp_path):
    """A small engine allowance forces several payload windows over occurrence IDs unrelated to key order.

    Rows, order, bytes and batch bounds equal one global sort of the whole join, read at the default
    allowance; values and changes equal an independent Python diff; a closed session refuses the next batch.
    """
    count = 6000
    values = {f"key-{index:05d}": {"n": index, "text": "x" * (index % 8191)} for index in range(count)}
    edited = {key: {"n": -value["n"]} for key, value in values.items() if value["n"] % 2 == 0}
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("wide", values.items())
        newer = workspace.upsert("wide", edited.items(), batch_id="edit").state_id
        with workspace.open_state(newer) as reader, reader.relation() as relation, \
                closing(relation.order("member_key").to_arrow_reader(BATCH_ROWS)) as ordered:
            expected = list(bounded_batches(ordered, byte_column="occurrence_record"))
    with CoreWorkspace(tmp_path, create=False, engine_memory_bytes=32 * 1024**2) as workspace:
        with workspace.open_state("wide") as older, workspace.open_state(newer) as reader:
            window = workspace.states._window_rows(reader._layers["entities"])
            assert window % BATCH_ROWS == 0 and window < len(edited) < count
            with closing(reader.batches()) as batches:
                actual = list(batches)
            assert [batch.num_rows for batch in actual] == [batch.num_rows for batch in expected]
            assert pa.Table.from_batches(actual).equals(pa.Table.from_batches(expected))
            assert [(key, value) for key, _, value in reader.values()] == sorted({**values, **edited}.items())
            assert [(key, value) for key, _, value in reader.changes(older)] == sorted(edited.items())
            batches = reader.batches()
            next(batches)
        with pytest.raises(StateTransitionError, match="closed"):
            next(batches)


def test_nested_ordered_reads_keep_their_own_addresses(tmp_path):
    """A connection's cursors share relation views; an ordered read inside another's loop must not disturb it."""
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("outer", [(f"o{index}", index) for index in range(3)])
        workspace.create("inner", [("i0", 0), ("i1", -1)])
        nested = [(key, entity.value.value, [(inner, value.value.value) for inner, value in workspace.rows("inner")])
                  for key, entity in workspace.rows("outer")]
        assert nested == [(f"o{index}", index, [("i0", 0), ("i1", -1)]) for index in range(3)]


def test_lookup_selects_one_membership_and_occurrence_without_readmission(tmp_path, monkeypatch):
    _fixture(tmp_path)
    with CoreWorkspace(tmp_path, create=False) as workspace, workspace.open_state("selected") as reader:
        expected = reader.lookup("keep").entity_id
        selected = []
        original = workspace.records._relation
        @contextmanager
        def observe(layer, **kwargs):
            selected.append((layer.reference.layer_kind, kwargs.get("record_ids")))
            with original(layer, **kwargs) as relation:
                yield relation
        monkeypatch.setattr(workspace.records, "_relation", observe)
        def unexpected(*args, **kwargs):
            raise AssertionError("reader repeated full payload admission")
        monkeypatch.setattr(workspace.records, "admit", unexpected)
        assert reader.lookup("keep").entity_id == expected
        assert selected == [("core-membership", ["keep"]), ("core-entities", [expected])]


def test_scoped_values_use_one_bounded_address_group_and_preserve_pin(tmp_path, monkeypatch):
    _fixture(tmp_path)
    with CoreWorkspace(tmp_path, create=False) as workspace, workspace.open_state("selected") as reader:
        pin = reader.pin
        full = {row[0]: row for row in reader.values()}
        scopes = []
        original = workspace.states.ordered_batches
        def observe(*args, **kwargs):
            scopes.append(kwargs.get("scope"))
            return original(*args, **kwargs)
        monkeypatch.setattr(workspace.states, "ordered_batches", observe)
        monkeypatch.setattr(workspace.records, "admit", lambda *args, **kwargs: pytest.fail("source readmitted"))
        assert list(reader.values(member_keys=["update"])) == [full["update"]]
        assert scopes == [("update",)] and reader.pin == pin
        assert list(reader.values(member_keys=[])) == []
        with pytest.raises(LookupError, match="does not exist"):
            list(reader.values(member_keys=["missing"]))
        with pytest.raises(ValueError, match="distinct"):
            list(reader.values(member_keys=["keep", "keep"]))


def test_fresh_process_reuses_published_semantics_and_checks_pinned_bytes(tmp_path):
    _fixture(tmp_path)
    with CoreWorkspace(tmp_path, create=False) as workspace, workspace.open_state("selected") as reader:
        pin = reader.pin
    program = '''
import json
import sys
from docspec.adapters.storage.core_states import CoreStateStorage
from docspec.adapters.storage.records import IcebergRecordStorage
from docspec.runtime import CoreWorkspace

def repeated(*args, **kwargs):
    raise AssertionError("reopened published state repeated a full semantic audit")

IcebergRecordStorage.admit = repeated
IcebergRecordStorage._rows = repeated
CoreStateStorage._match_members = repeated
checked = []
verify = IcebergRecordStorage.verify_members
def verify_bytes(self, reference):
    verify(self, reference)
    checked.append(reference.layer_kind)
IcebergRecordStorage.verify_members = verify_bytes

with CoreWorkspace(sys.argv[1], create=False) as workspace:
    with workspace.open_state("selected", expected_pin=sys.argv[2]) as reader:
        assert reader.record_count == 2
        assert reader.read_value("update") == {"title": "Updated"}
        assert reader.lookup("remove") is None
        with reader.value_relation() as relation:
            assert relation.order("member_key").project("member_key").fetchall() == [("keep",), ("update",)]
        assert sorted(checked) == ["core-entities", "core-membership"]
        print(json.dumps({"pin": reader.pin, "checked": checked}))
'''
    result = subprocess.run([sys.executable, "-I", "-c", program, str(tmp_path), pin],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["pin"] == pin


@pytest.mark.parametrize("kind", ["state", "state_representation"])
def test_reader_requires_successful_retention_not_only_available_metadata(tmp_path, monkeypatch, kind):
    _fixture(tmp_path)
    with CoreWorkspace(tmp_path, create=False) as workspace:
        read = workspace.ledger.read_records
        def unretained(keys, **kwargs):
            for batch in read(keys, **kwargs):
                yield tuple(replace(row, retained=False) if row is not None and row.key[0] == kind else row
                            for row in batch)
        monkeypatch.setattr(workspace.ledger, "read_records", unretained)
        with pytest.raises(IntegrityError, match="requires retained available"):
            with workspace.open_state("selected"):
                raise AssertionError("unretained state was delivered")


def test_explicit_layer_audit_still_checks_logical_rows(tmp_path, monkeypatch):
    _fixture(tmp_path)
    with CoreWorkspace(tmp_path, create=False) as workspace, workspace.publisher.session() as session:
        layers = workspace.states.layers(session, "selected")
        observed = []
        original = workspace.records._rows
        def checked(layer, **kwargs):
            for row in original(layer, **kwargs):
                observed.append(layer.reference.layer_kind)
                yield row
        monkeypatch.setattr(workspace.records, "_rows", checked)
        for layer in layers.values():
            workspace.records.verify(layer.reference)
        assert sorted(observed) == sorted(kind for layer in layers.values()
                                          for kind in [layer.reference.layer_kind] * layer.reference.record_count)


def test_mismatched_pin_refuses_before_payload_delivery(tmp_path, monkeypatch):
    _fixture(tmp_path)
    with CoreWorkspace(tmp_path, create=False) as workspace:
        def unexpected(*args, **kwargs):
            raise AssertionError("mismatched pin reached payload admission")
        monkeypatch.setattr(workspace.records, "admit", unexpected)
        with pytest.raises(IntegrityError, match="expected read pin"):
            with workspace.open_state("selected", expected_pin={"stateId": "other"}):
                raise AssertionError("mismatched pin was delivered")


def test_content_values_use_owner_codecs_and_missing_differs_from_null(tmp_path, monkeypatch):
    with CoreWorkspace(tmp_path) as workspace:
        with workspace.publisher.session() as session:
            json_value = session.retain_value({"title": "Retained JSON"})
            opaque = session.retain_bytes([b"opaque\x00bytes"])
            workspace.states.create_keyed(session, state_id="values", representation_id="values:physical", unit_id="values:import",
                rows=[(key, core.Entity(format_version=1, entity_id=key, entity_type="occurrence", value=value))
                      for key, value in [("json", json_value), ("opaque", opaque), ("null", core.InlineValue(value=None))]])
    with CoreWorkspace(tmp_path, create=False) as workspace, workspace.open_state("values") as reader:
        assert reader.read_value("json") == {"title": "Retained JSON"}
        assert reader.read_value("opaque") == b"opaque\x00bytes"
        assert reader.read_value("null") is None
        with pytest.raises(StateValueRelationUnavailable, match="decoded values stream"):
            with reader.value_relation():
                raise AssertionError("native relation delivered content references as values")
        with pytest.raises(LookupError, match="does not exist"):
            reader.read_value("missing")
        def unexpected(*args, **kwargs):
            raise AssertionError("value streaming performed a per-row lookup")
        monkeypatch.setattr(reader, "lookup", unexpected)
        assert {key: value for key, _, value in reader.values()} == {
            "json": {"title": "Retained JSON"}, "opaque": b"opaque\x00bytes", "null": None,
        }


def test_open_state_refuses_damaged_payload(tmp_path):
    _fixture(tmp_path)
    with CoreWorkspace(tmp_path, create=False) as workspace:
        with workspace.open_state("selected") as reader:
            pin = reader.pin
        with workspace.publisher.session() as session:
            layer = workspace.states.layers(session, "selected")["entities"]
            reference = next(ref for ref in workspace.records.physical_references(layer.reference)
                             if ref.locator.endswith(".parquet"))
        path = workspace.records.root / reference.locator
        with path.open("r+b") as stream:
            first = stream.read(1)
            stream.seek(0)
            stream.write(bytes([first[0] ^ 1]))
        with pytest.raises(IntegrityError, match="checksum"):
            with workspace.open_state("selected", expected_pin=pin):
                raise AssertionError("damaged payload was delivered")
