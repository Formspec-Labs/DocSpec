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
    stored = core_admission.stored_record
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
    def validated(payload):
        result = stored(payload)
        if isinstance(result, core.Entity):
            counts["stored_validations"] += 1
        return result
    with ExitStack() as stack:
        stack.enter_context(patch.object(core_admission, "_convert", converted))
        stack.enter_context(patch.object(core_admission, "stored_record", validated))
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
    # per entity, then 2 / 1 / 1. Initial Python admission encodes once;
    # persisted rows are still validated, by the typed native decode, without
    # re-proving the canonical form that encoding established.
    assert counts == {"schema_conversions": count, "canonical_encodes": count, "stored_validations": count}
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


@contextmanager
def canonical_work():
    """Count canonical bytes encoded and canonical parses across all docspec modules."""
    counts = Counter()
    encode, parse = identity._artifact_canonical_json_bytes, identity._artifact_parse_canonical_json
    def encoded(value, *args, **kwargs):
        result = encode(value, *args, **kwargs)
        counts["encoded_bytes"] += len(result)
        return result
    def parsed(value, *args, **kwargs):
        counts["parses"] += 1
        return parse(value, *args, **kwargs)
    with patch.object(identity, "_artifact_canonical_json_bytes", encoded), \
            patch.object(identity, "_artifact_parse_canonical_json", parsed):
        yield counts


def test_derive_encodes_each_value_once_and_reads_without_reparsing(tmp_path):
    """Derive encodes each value once; writing and reading its rows parse none of them again.

    The marginal cost between two batch sizes excludes fixed per-derive and
    per-open records (definition, request, result and state manifests).
    """
    body = "x" * 8192
    definition = core.OperationDefinition(format_version=1, definition_id="urn:test:encode-once",
        implementation_id="test.encode-once", implementation_version="1", operation_kind="transformation",
        configuration={})
    work = {}
    for count in (16, 32):
        rows = [(f"k{index:02d}", {"body": body, "n": index}) for index in range(count)]
        with CoreWorkspace(tmp_path / str(count)) as workspace:
            with canonical_work() as derived:
                state = workspace.derive(iter(rows), batch_id="once", definition=definition, inputs=())
            with canonical_work() as read, workspace.open_state(state.state_id) as reader:
                assert [(key, value) for key, _, value in reader.values()] == rows
        work[count] = derived, read
    # Before, each added row cost seven encodes of its value and two parses to
    # derive, and one parse to read. Opening a state still parses its records.
    (derived_16, read_16), (derived_32, read_32) = work[16], work[32]
    per_row = (derived_32["encoded_bytes"] - derived_16["encoded_bytes"]) / 16
    assert len(body) <= per_row < len(body) + 1024
    assert derived_32["parses"] == derived_16["parses"] and read_32["parses"] == read_16["parses"]
    assert read_32["encoded_bytes"] == read_16["encoded_bytes"]


def test_inline_occurrence_payload_equals_the_full_record_encoding():
    """The spliced occurrence record is byte-identical to encoding the whole record."""
    # Canonical JSON admits no binary floats; keys sort by UTF-16 code units.
    samples = [None, True, 0, -1, 2**53 - 1, "", "é\n\"\u0001\U0001f600", [], {},
               {"\U0001f600": 1, "\ue000": 2, "a": [{"z": None, "b": [1, -2]}]},
               {"value": {"value": None}, "kind": "entity"}]
    for index, value in enumerate(samples):
        entity_id = f"urn:test:occurrence:{index}"
        expected = core_admission.encode_record(core.Entity(format_version=1, entity_id=entity_id,
            entity_type="occurrence", value=core.InlineValue(value=value)))
        spliced = core_admission.inline_occurrence_payload(entity_id, identity.canonical_value_bytes(value))
        assert spliced == expected
        assert core_admission.stored_record(spliced) == core_admission.admit_record(spliced)


@pytest.mark.parametrize("payload", [
    b'{"entity_id":"one","entity_type":"occurrence","format_version":1,"kind":"entity","value":{"kind":"inline","value":[1',
    b'{"entity_id":"one","entity_type":"elsewhere","format_version":1,"kind":"entity","value":{"kind":"inline","value":1}}',
    b'{"entity_id":1,"entity_type":"occurrence","format_version":1,"kind":"entity","value":{"kind":"inline","value":1}}',
])
def test_stored_rows_are_still_validated(payload):
    """A truncated or mistyped persisted row is refused without the canonical re-proof."""
    with pytest.raises(IntegrityError):
        core_admission.stored_record(payload)
    with pytest.raises(IntegrityError):
        core_admission.stored_snapshot(payload)
