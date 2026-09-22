"""Selected outputs preserve exact origins, result reuse and current dependency evidence.

Stale pins or unavailable choices refuse, as do competing selections read under an old selection-set pin,
later dependency omissions, wrong-origin membership, opaque or damaged retained bytes, and missing definitions.
"""

from contextlib import closing

import pytest
from msgspec.structs import replace

from docspec.application.core_dependencies import CoreDependencies
from docspec.domain import core
from docspec.domain.core_admission import record_value
from docspec.domain.identity import OrderedJsonSequenceDigester, canonical_value_bytes
from docspec.errors import IntegrityError, StaleBaseError, StateTransitionError, StateValueRelationUnavailable
from docspec.ports.core_ledger import MetadataBatch
from docspec.runtime import CoreWorkspace
from tests.test_core_dependencies import omission


DEFINITION = core.OperationDefinition(format_version=1, definition_id="normalize",
    implementation_id="test.normalize", implementation_version="1", operation_kind="transformation", configuration={})


def resolve(workspace, state="source", *, identity="first", target_key="notice", output="inline", fresh=False,
            definition=DEFINITION, selected_labels=("keys",)):
    """Resolve a keys output for the source member and return the resolution; ``output`` selects
    inline, content or opaque retention."""
    with workspace.open_state(state) as source:
        entity = source.lookup("notice")
    request = core.Request(format_version=1, request_id=identity, definition_id=definition.definition_id,
        inputs=(core.WholeInput(label="source", entity_id=entity.entity_id),),
        dependencies=(core.Dependency(label="id", binding_label="source",
            selection=core.JsonFields(selectors=(core.Field(label="id", pointer="/id"),))),))
    def produce(context):
        document = context.read_value(entity.entity_id)
        value = {"identifiers": [document["id"].casefold()]}
        retained = (context.session.retain_value(value) if output == "content" else
                    context.session.retain_bytes([b"opaque"]) if output == "opaque" else core.InlineValue(value=value))
        context.generate(retained, label="keys")
        context.generate(core.InlineValue(value="not selected"), label="hidden")
    return workspace.operations.resolve(definition, request, produce, selection_id=identity + ":selection",
        target=core.Origin(parent_entity_id=entity.entity_id, state_id=state, member_key=target_key),
        output_labels=selected_labels, reuse_policy=lambda result: True, fresh=fresh)


def read(workspace, state="source", **kwargs):
    """Open the ``normalize`` selected outputs for the ``keys`` label."""
    return workspace.open_selected_outputs(state, definition_id="normalize", output_labels=("keys",), **kwargs)


@pytest.mark.parametrize("kind", ["inline", "content"])
def test_exact_outputs_and_reopened_pins_without_new_retained_data(tmp_path, kind):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("notice", {"id": "ABC", "title": "Original"})])
        result = resolve(workspace, output=kind)
        files = {str(path): path.read_bytes() for folder in ("blobs", "records")
                 for path in (tmp_path / folder).rglob("*") if path.is_file()}
        records = [row.key for batch in workspace.ledger.retained_records() for row in batch]
        with read(workspace) as reader:
            with pytest.raises(StateTransitionError, match="completed traversal"):
                reader.pin
            rows = list(reader.rows())
            pin, source_pin = reader.pin, reader.state_pin
            assert rows[0].value == {"identifiers": ["abc"]}
            assert rows[0].origin == result.selection.target
            assert rows[0].selection_id == result.selection.selection_id
            assert rows[0].result_id == result.result.result_id
            assert rows[0].output_id == result.result.outcome.outputs[0].entity_id
            assert rows[0].label == "keys" and len(rows) == 1
            keys = [("selection", rows[0].selection_id), ("result", rows[0].result_id), ("entity", rows[0].output_id)]
            pins = [record.row_digest for batch in workspace.ledger.read_records(keys) for record in batch]
            expected = OrderedJsonSequenceDigester(prefix=("docspec-selected-outputs", 1, source_pin,
                                                          reader.definition_pin, ["keys"]))
            expected.accept_admitted_payload(canonical_value_bytes([record_value(rows[0].origin, core.Origin),
                rows[0].selection_id, pins[0], rows[0].result_id, pins[1], rows[0].output_id, pins[2], "keys"]))
            assert pin == expected.finish()
        assert records == [row.key for batch in workspace.ledger.retained_records() for row in batch]
        assert files == {str(path): path.read_bytes() for folder in ("blobs", "records")
                         for path in (tmp_path / folder).rglob("*") if path.is_file()}
        with pytest.raises(StateTransitionError, match="closed"):
            list(reader.rows())
    with CoreWorkspace(tmp_path, create=False) as workspace:
        with read(workspace, expected_state_pin=source_pin, expected_pin=pin) as reader:
            assert list(reader.rows()) == rows and reader.pin == pin


def test_unrelated_change_reuses_result_but_origin_is_current_member(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("notice", {"id": "ABC", "title": "Original"})])
        original = resolve(workspace)
        revised = workspace.upsert("source", [("notice", {"id": "ABC", "title": "Revised"})], batch_id="retitle")
        reused = resolve(workspace, revised.state_id, identity="reused")
        assert reused.result == original.result and reused.selection.target != original.selection.target
        with read(workspace, revised.state_id) as reader:
            rows = list(reader.rows())
        assert len(rows) == 1 and rows[0].origin == reused.selection.target
        assert rows[0].result_id == original.result.result_id


def test_unselected_output_and_missing_choice_are_not_exposed(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("notice", {"id": "ABC"}), ("unprocessed", {"id": "XYZ"})])
        resolve(workspace)
        with workspace.open_selected_outputs("source", definition_id="normalize", output_labels=("hidden",)) as reader:
            assert list(reader.rows()) == [] and reader.pin.startswith("sha256:")
        with read(workspace) as reader:
            assert [row.origin.member_key for row in reader.rows()] == ["notice"]


def test_competing_choices_remain_visible_and_change_selection_pin(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("notice", {"id": "ABC"})])
        first = resolve(workspace)
        with read(workspace) as reader:
            list(reader.rows())
            pin = reader.pin
        second = resolve(workspace, identity="second", fresh=True)
        with read(workspace) as reader:
            rows = list(reader.rows())
            assert {row.result_id for row in rows} == {first.result.result_id, second.result.result_id}
            assert reader.pin != pin
        with read(workspace, expected_pin=pin) as reader:
            with pytest.raises(StaleBaseError, match="selection-set pin"):
                list(reader.rows())


def test_explicit_choices_adopt_one_competing_result_without_scanning_all(tmp_path, monkeypatch):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("notice", {"id": "ABC"})])
        first = resolve(workspace)
        second = resolve(workspace, identity="second", fresh=True)
        monkeypatch.setattr(workspace.ledger, "retained_records", lambda **kwargs: pytest.fail("scanned all selections"))
        choices = (second.selection.selection_id,)
        with read(workspace, selection_ids=choices) as reader:
            rows = list(reader.rows())
            pin = reader.pin
            assert [row.result_id for row in rows] == [second.result.result_id]
        with read(workspace, selection_ids=choices, expected_pin=pin) as reader:
            assert list(reader.rows()) == rows and reader.pin == pin
        with read(workspace, selection_ids=(first.selection.selection_id,), expected_pin=pin) as reader:
            with pytest.raises(StaleBaseError, match="selection-set pin"):
                list(reader.rows())


def test_explicit_choice_bounds_order_and_request_group_bytes(tmp_path, monkeypatch):
    import docspec.runtime.selected_outputs as selected_outputs
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("notice", {"id": "ABC"})])
        choices = [resolve(workspace, identity=str(i), fresh=True) for i in range(3)]
        ids = tuple(item.selection.selection_id for item in choices)
        with read(workspace, selection_ids=ids) as reader:
            rows = list(reader.rows())
            pin = reader.pin
        with read(workspace, selection_ids=tuple(reversed(ids)), expected_pin=pin) as reader:
            assert list(reader.rows()) == rows and reader.pin == pin
        for invalid in ((), (ids[0], ids[0]), ("",), tuple(str(i) for i in range(selected_outputs.BATCH_ROWS + 1))):
            with pytest.raises(ValueError, match="selection IDs"), read(workspace, selection_ids=invalid):
                pytest.fail("invalid selection scope admitted")
        monkeypatch.setattr(selected_outputs, "BATCH_BYTES", 1)
        with pytest.raises(ValueError, match="byte limit"), read(workspace, selection_ids=ids):
            pytest.fail("oversized scope admitted")
        descriptions = [row.value for batch in workspace.ledger.read_records(
            [key for choice in choices for key in (("selection", choice.selection.selection_id), ("request", choice.selection.request_id))])
            for row in batch]
        sizes = [len(canonical_value_bytes(record_value(value))) for value in descriptions]
        monkeypatch.setattr(selected_outputs, "BATCH_BYTES", max(sum(sizes[i:i + 2]) for i in range(0, len(sizes), 2)))
        with read(workspace, selection_ids=ids) as reader:
            groups = list(reader._selections())
            assert [len(group) for group in groups] == [1, 1, 1]
            assert list(reader.rows()) == rows
        with read(workspace, selection_ids=("not-retained",)) as reader:
            with pytest.raises(IntegrityError, match="unavailable"):
                list(reader.rows())


@pytest.mark.parametrize("foreign", ["state", "definition", "label", "unavailable"])
def test_explicit_choice_must_match_requested_scope_and_be_available(tmp_path, foreign):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("notice", {"id": "ABC"})])
        resolve(workspace)
        workspace.create("other", [("notice", {"id": "ABC"})])
        other = resolve(workspace, "other" if foreign == "state" else "source", identity="other",
                        definition=replace(DEFINITION, definition_id="other-definition") if foreign == "definition" else DEFINITION,
                        selected_labels=("hidden",) if foreign == "label" else ("keys",))
        if foreign == "unavailable":
            policy = core.RetentionPolicy(format_version=1, policy_id="remove-explicit", description={})
            workspace.ledger.commit(MetadataBatch("policy-explicit", records=(policy,),
                retained=(("retention_policy", policy.policy_id),)))
            workspace.ledger.begin_removal("remove-explicit", policy.policy_id, [("selection", other.selection.selection_id)])
        with read(workspace, selection_ids=(other.selection.selection_id,)) as reader:
            with pytest.raises(IntegrityError):
                list(reader.rows())


def test_later_dependency_omission_refuses_previously_admitted_selection(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("notice", {"id": "ABC"})])
        choice = resolve(workspace)
        with workspace.publisher.session() as session:
            CoreDependencies().record_evidence(session, omission(result_id=choice.result.result_id))
        with read(workspace) as reader:
            with pytest.raises(StaleBaseError, match="dependency evidence"):
                list(reader.rows())


@pytest.mark.parametrize("target_key", ["missing", "different"])
def test_wrong_origin_membership_refuses(tmp_path, target_key):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("notice", {"id": "ABC"}), ("different", {"id": "XYZ"})])
        resolve(workspace, target_key=target_key)
        with read(workspace) as reader:
            with pytest.raises(StaleBaseError, match="source member occurrence"):
                list(reader.rows())


def test_opaque_output_is_not_misreported_as_json(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("notice", {"id": "ABC"})])
        resolve(workspace, output="opaque")
        with read(workspace) as reader:
            with pytest.raises(StateValueRelationUnavailable, match="opaque bytes"):
                list(reader.rows())


def test_missing_definition_and_wrong_source_pin_refuse(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("notice", {"id": "ABC"})])
        with pytest.raises(IntegrityError, match="operation_definition"):
            with read(workspace):
                pytest.fail("missing definition admitted")
        resolve(workspace)
        with pytest.raises(IntegrityError, match="expected read pin"):
            with read(workspace, expected_state_pin="wrong"):
                pytest.fail("wrong source admitted")


def test_kind_filter_does_not_decode_other_retained_records(tmp_path, monkeypatch):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("notice", {"id": "ABC"})])
        choice = resolve(workspace)
        original = workspace.ledger.read_records
        observed = []
        def capture(keys, **kwargs):
            keys = list(keys)
            observed.extend(keys)
            yield from original(keys, **kwargs)
        monkeypatch.setattr(workspace.ledger, "read_records", capture)
        with closing(workspace.ledger.retained_records(kind="selection")) as rows:
            assert [row.value for batch in rows for row in batch] == [choice.selection]
        assert observed == [("selection", choice.selection.selection_id)]


def test_bounded_member_group_does_not_readmit_source_for_each_choice(tmp_path, monkeypatch):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("notice", {"id": "ABC"})])
        for index in range(12):
            resolve(workspace, identity=f"selection-{index}")
        with read(workspace) as reader:
            calls = []
            original = workspace.states.relation
            def capture(*args, **kwargs):
                calls.append(kwargs.get("scope"))
                return original(*args, **kwargs)
            monkeypatch.setattr(workspace.states, "relation", capture)
            monkeypatch.setattr(workspace.records, "admit", lambda *args, **kwargs: pytest.fail("source readmitted"))
            assert len(list(reader.rows())) == 12
            assert calls == [("notice",)]


def test_damaged_retained_output_bytes_refuse(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("notice", {"id": "ABC"})])
        choice = resolve(workspace, output="content")
        output_id = choice.result.outcome.outputs[0].entity_id
        entity = next(workspace.ledger.read_records([("entity", output_id)]))[0].value
        path = workspace.blobs.root / entity.value.locator
        raw = path.read_bytes()
        path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
        with read(workspace) as reader:
            with pytest.raises(IntegrityError):
                list(reader.rows())


@pytest.mark.parametrize("foreign", ["definition", "label"])
def test_unavailable_foreign_choice_does_not_block_requested_definition(tmp_path, foreign):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("notice", {"id": "ABC"})])
        wanted = resolve(workspace)
        other_definition = (replace(DEFINITION, definition_id="other-definition", implementation_version="2")
                            if foreign == "definition" else DEFINITION)
        other = resolve(workspace, identity="other", definition=other_definition,
                        selected_labels=("keys",) if foreign == "definition" else ("hidden",))
        policy = core.RetentionPolicy(format_version=1, policy_id="remove", description={"reason": "test"})
        workspace.ledger.commit(MetadataBatch("policy", records=(policy,), retained=(("retention_policy", "remove"),)))
        workspace.ledger.begin_removal("other-removal", "remove",
            [("selection", other.selection.selection_id), ("request", other.selection.request_id)])
        with read(workspace) as reader:
            assert [row.result_id for row in reader.rows()] == [wanted.result.result_id]
        workspace.ledger.begin_removal("wanted-removal", "remove",
            [("selection", wanted.selection.selection_id), ("request", wanted.selection.request_id)])
        with read(workspace) as reader:
            with pytest.raises(IntegrityError, match="unavailable"):
                list(reader.rows())


def test_early_close_has_no_pin_and_releases_owned_streams(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("notice", {"id": "ABC"})])
        resolve(workspace)
        with read(workspace) as reader:
            with closing(reader.rows()) as rows:
                assert next(rows).value == {"identifiers": ["abc"]}
            with pytest.raises(StateTransitionError, match="completed traversal"):
                reader.pin
        with read(workspace) as reopened:
            assert len(list(reopened.rows())) == 1


def test_unavailable_selected_output_refuses(tmp_path):
    with CoreWorkspace(tmp_path) as workspace:
        workspace.create("source", [("notice", {"id": "ABC"})])
        choice = resolve(workspace)
        policy = core.RetentionPolicy(format_version=1, policy_id="remove", description={"reason": "test"})
        workspace.ledger.commit(MetadataBatch("policy", records=(policy,), retained=(("retention_policy", "remove"),)))
        workspace.ledger.begin_removal("output-removal", "remove",
            [("entity", choice.result.outcome.outputs[0].entity_id)])
        with read(workspace) as reader:
            with pytest.raises(IntegrityError):
                list(reader.rows())
