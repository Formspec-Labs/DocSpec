"""Count actual root admission work and preserve immutable byte checks."""

from collections import Counter
from contextlib import ExitStack, contextmanager
import sys
from unittest.mock import patch

import pytest

from docspec.domain import core, core_admission, identity
from docspec.domain.core_admission import AdmittedRecord, record_parts
from docspec.errors import IntegrityError
from docspec.ports.core_ledger import MetadataBatch
from docspec.runtime import CoreWorkspace


@contextmanager
def entity_codec_counts():
    """Count entity schema conversions and canonical encodes/decodes by patching all docspec modules."""
    counts = Counter()
    convert, encode, decode = core_admission._convert, identity.canonical_value_bytes, identity.decode_canonical_json_value
    def converted(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("kind") == "entity":
            counts["schema_conversions"] += 1
        return convert(value, *args, **kwargs)
    def encoded(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("kind") == "entity":
            counts["canonical_encodes"] += 1
        return encode(value, *args, **kwargs)
    def decoded(value, *args, **kwargs):
        result = decode(value, *args, **kwargs)
        if isinstance(result, dict) and result.get("kind") == "entity":
            counts["canonical_decodes"] += 1
        return result
    with ExitStack() as stack:
        stack.enter_context(patch.object(core_admission, "_convert", converted))
        for module in tuple(sys.modules.values()):
            if getattr(module, "__name__", "").startswith("docspec."):
                for name, function in tuple(vars(module).items()):
                    if function is encode:
                        stack.enter_context(patch.object(module, name, encoded))
                    elif function is decode:
                        stack.enter_context(patch.object(module, name, decoded))
        yield counts


def test_root_entity_handoff_does_not_repeat_encoding_or_selection_admission(tmp_path):
    """Root admission encodes and decodes each entity exactly once and reopens to the same rows."""
    count = 8
    with CoreWorkspace(tmp_path) as workspace, entity_codec_counts() as counts:
        workspace.create("root", ((str(index), {"body": "x" * 8192, "url": f"u{index}"}) for index in range(count)))
    # Before the admitted-byte handoff these actual counts were 5 / 3 / 2
    # per entity. Initial Python admission and persisted-byte admission remain.
    assert counts == {"schema_conversions": count * 2, "canonical_encodes": count, "canonical_decodes": count}
    with CoreWorkspace(tmp_path) as reopened:
        assert [entity.value.value["url"] for _, entity in reopened.rows("root")] == [f"u{i}" for i in range(count)]


def test_snapshot_detaches_input_and_public_values_without_stale_bytes(tmp_path):
    """An admitted snapshot's payload is immutable: mutating inputs or the reader copy cannot change it."""
    value = {"nested": [True]}
    entity = core.Entity(format_version=1, entity_id="one", entity_type="occurrence", value=core.InlineValue(value=value))
    snapshot = AdmittedRecord(entity)
    before = snapshot.payload
    value["nested"][0] = 1
    plain, payload = record_parts(snapshot)
    plain["value"]["value"]["nested"][0] = "changed"
    assert payload == before and record_parts(snapshot)[0]["value"]["value"]["nested"] == [True]
    with pytest.raises(AttributeError):
        snapshot.payload = b"wrong"
    with CoreWorkspace(tmp_path) as workspace:
        workspace.retain((snapshot,), unit_id="first", roots=(("entity", "one"),))
        with pytest.raises(IntegrityError, match="immutable"):
            workspace.ledger.commit(MetadataBatch("conflict", records=(AdmittedRecord(entity),)))
        assert workspace.inspect("entity", "one")["record"]["value"]["value"]["nested"] == [True]


def test_snapshot_native_reads_preserve_defaults_types_and_original_bytes():
    """Readers see defaults filled in and JSON-native types, while the admitted bytes stay untouched."""
    # Raw admission permits omitted defaults, but every value reader receives
    # the complete typed record. The original canonical bytes remain immutable.
    payload = b'{"entity_id":"one","entity_type":"occurrence","format_version":1,"kind":"entity","value":{"kind":"inline","value":[null,true,1,"1",{"nested":[]}]}}'
    snapshot = AdmittedRecord(payload)
    plain, original = record_parts(snapshot)
    assert original == payload
    assert plain["value"]["codec"] == "json-v1"
    values = plain["value"]["value"]
    assert [type(value) for value in values] == [type(None), bool, int, str, dict]
    values[-1]["nested"].append("mutated")
    assert snapshot.value["value"]["value"][-1] == {"nested": []}


def test_publication_commits_its_admitted_snapshot_without_reencoding(tmp_path, monkeypatch):
    """Mutating the caller's value after admission cannot change published bytes or the reopened record."""
    value = {"nested": [True]}
    entity = core.Entity(format_version=1, entity_id="one", entity_type="occurrence", value=core.InlineValue(value=value))
    with CoreWorkspace(tmp_path) as workspace:
        commit = workspace.ledger.commit
        def mutate_before_commit(batch):
            value["nested"][0] = "changed after admission"
            return commit(batch)
        monkeypatch.setattr(workspace.ledger, "commit", mutate_before_commit)
        with entity_codec_counts() as counts:
            workspace.retain((entity,), unit_id="first", roots=(("entity", "one"),))
        assert counts == {"schema_conversions": 1, "canonical_encodes": 1}
        assert workspace.inspect("entity", "one")["record"]["value"]["value"]["nested"] == [True]
    with CoreWorkspace(tmp_path) as reopened:
        assert reopened.inspect("entity", "one")["record"]["value"]["value"]["nested"] == [True]


@pytest.mark.parametrize("value", [
    b'{"entity_id":"one","entity_id":"two","entity_type":"occurrence","format_version":1,"kind":"entity","value":{"kind":"inline","value":1}}',
    {"kind": "entity", "format_version": 1, "entity_id": "one", "entity_type": "invalid", "value": {"kind": "inline", "value": 1}},
    core.Entity(format_version=1, entity_id="one", entity_type="occurrence", value=core.InlineValue(value={"bytes": b"invalid"})),
])
def test_snapshot_has_no_unchecked_admission_path(value):
    with pytest.raises((IntegrityError, ValueError)):
        AdmittedRecord(value)


def test_checkpoint_compares_existing_metadata_without_rereading_payloads(tmp_path, monkeypatch):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("root", ((str(index), {"url": f"u{index}", "body": "x" * 8192}) for index in range(8)))
        before = list(workspace.rows("root"))
        def unexpected(*args, **kwargs):
            raise AssertionError("incoming occurrence comparison reread an old payload")
        with monkeypatch.context() as guarded:
            guarded.setattr(workspace.records, "lookup_batches", unexpected)
            with workspace.publisher.session() as session:
                workspace.states.checkpoint(session, "root", representation_id="checkpoint", unit_id="checkpoint")
            altered = core.Entity(format_version=1, entity_id=before[0][1].entity_id,
                                  entity_type="occurrence", value=core.InlineValue(value={"changed": True}))
            with pytest.raises(IntegrityError, match="immutable"):
                workspace.retain((AdmittedRecord(altered),), unit_id="conflict", roots=(("entity", altered.entity_id),))
        assert list(workspace.rows("root")) == before
        assert not workspace.ledger.is_committed("conflict")
