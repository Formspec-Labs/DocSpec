"""Exact existing-state reads preserve revisions and reuse one admission."""

from contextlib import closing, contextmanager
from dataclasses import replace
import json
import subprocess
import sys

import pytest

from docspec.domain import core
from docspec.errors import IntegrityError, StateTransitionError, StateValueRelationUnavailable
from docspec.runtime import CoreWorkspace


def _fixture(path):
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
