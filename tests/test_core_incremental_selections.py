"""Computed member reuse is source-established, bounded, and safe after reopen."""

from contextlib import ExitStack, closing

import msgspec
import pytest

from docspec.application.core_edits import prepare_revision, prepare_value_edit
from docspec.application.core_execution import CoreOperations
from docspec.domain import core
from docspec.domain.core_admission import record_value
from docspec.domain.identity import canonical_value_bytes
from docspec.errors import IntegrityError
from docspec.ports.core_ledger import MetadataBatch, MetadataLink
from tests.test_core_selections import setup, import_root, select, fields
from tests.test_core_states import occurrence


VALUES = [("a", "e0", {"url": 1, "title": "old", "position": 2}),
          ("b", "e1", {"url": "1", "position": 1}),
          ("c", "e2", {"url": None, "position": 1}),
          ("d", "e3", {"position": 0})]


def revise(publisher, session):
    operations = CoreOperations(publisher)
    operation, edit = prepare_value_edit(operations, "e0", [
        {"op": "replace", "path": "/title", "value": "new"},
        {"op": "replace", "path": "/position", "value": 0},
    ], session=session)
    operations.publish((operation,), session=session)
    revision = core.Revision(format_version=1, revision_id="revision", base_state_id="root", result_state_id="changed",
        edits=(core.Put(sequence=0, member_key="a", occurrence_id=edit.result_occurrence_id),
               core.Remove(sequence=1, member_key="b")), value_edits=(edit,))
    operations.publish((prepare_revision(operations, revision, session=session),), session=session)
    return edit.result_occurrence_id


def parent_evidence(selections, session, state, definition):
    selected = core.SelectedValue(format_version=1, selected_value_id="oracle", definition=definition,
                                  origin=core.Origin(parent_entity_id=state), value=core.FromParent())
    return selections.evidence(session, selected)


def observe_computed(monkeypatch, selections):
    actual, calls = selections._computed_rows, []
    def computed(session, parent, definition):
        calls.append((parent, definition.scope))
        yield from actual(session, parent, definition)
    monkeypatch.setattr(selections, "_computed_rows", computed)
    return calls


@pytest.mark.parametrize("named,identity,ordered", [(False, False, False), (True, False, False),
                                                   (False, True, False), (True, False, True)])
def test_reopened_binding_uses_changed_values_and_retains_current_origins(tmp_path, monkeypatch, named, identity, ordered):
    definition = core.StateMembers(member_selector=fields("/url", identity=identity), material_keys=True,
        scope=("a", "b", "c", "d", "missing") if named else None, sort_rule="position" if ordered else None)
    with ExitStack() as stack:
        _, _, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, VALUES)
            if ordered:
                rule = core.OperationDefinition(format_version=1, definition_id="position", operation_kind="transformation",
                    implementation_id="docspec.sort.canonical-json", implementation_version="1",
                    configuration={"pointers": ["/position"], "descending": False})
                session.publish(MetadataBatch("sort", records=(rule,), retained=(("operation_definition", "position"),)))
            select(selections, session, "base-selected", definition)
    # A new publisher and connection must use legitimate retained computation,
    # not a same-process object or a matching user-supplied identifier.
    with ExitStack() as stack:
        records, _, _, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            current = revise(publisher, session)
            expected = parent_evidence(selections, session, "changed", definition)
            calls = observe_computed(monkeypatch, selections)
            before = {str(path) for root in (records.root, publisher.blobs.root) for path in root.rglob("*") if path.is_file()}
            assert selections.binding_evidence(session, core.StateInput(label="source", state_id="changed"), definition)[1] == expected
            assert calls == [("changed", ("a", "b"))]
            assert before == {str(path) for root in (records.root, publisher.blobs.root) for path in root.rglob("*") if path.is_file()}
            selected = select(selections, session, "changed-selected", definition, parent="changed")
            assert selections.evidence(session, selected) == expected
            with closing(selections.rows(session, selected)) as rows:
                origins = {key: entity for key, entity, _ in rows}
            assert origins["a"] == current and origins["c"] == "e2" and origins["d"] == "e3"
            assert (origins["b"] is None and origins["missing"] is None) if named else "b" not in origins
            assert calls == [("changed", ("a", "b"))]


def test_advertised_edits_cannot_hide_actual_changed_membership(tmp_path, monkeypatch):
    definition = core.StateMembers(member_selector=fields("/url"), material_keys=True)
    with ExitStack() as stack:
        _, _, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, VALUES)
            select(selections, session, "base", definition)
            altered = [(key, "replacement" if key == "a" else identity, {"url": "different"} if key == "a" else value)
                       for key, identity, value in VALUES]
            states.create(session, state_id="changed", representation_id="changed-root", unit_id="changed-root",
                entities=[occurrence(identity, value) for _, identity, value in altered],
                members=[core.Membership(member_key=key, occurrence_id=identity) for key, identity, _ in altered])
            representation = states.representation(session, "changed")
            # This is untrusted supplied revision metadata, not an execution
            # through prepare_revision. Reuse must check the actual addresses.
            dishonest = core.Revision(format_version=1, revision_id="claimed", base_state_id="root", result_state_id="changed", edits=())
            representation = msgspec.structs.replace(representation, representation_id="claimed-representation", revision_id="claimed")
            session.publish(MetadataBatch("claimed", records=(dishonest, representation), retained=(("state", "changed"),)))
            expected = parent_evidence(selections, session, "changed", definition)
            calls = observe_computed(monkeypatch, selections)
            assert selections.binding_evidence(session, core.StateInput(label="source", state_id="changed"), definition)[1] == expected
            assert calls == [("changed", ("a",))]


def test_supplied_direct_values_cannot_certify_or_poison_parent_evaluation(tmp_path, monkeypatch):
    definition = core.StateMembers(member_selector=fields("/url"), material_keys=True)
    with ExitStack() as stack:
        _, ledger, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, VALUES)
            states.create(session, state_id="other", representation_id="other-r", unit_id="other",
                entities=[occurrence("wrong", {"url": "false"})], members=[core.Membership(member_key="a", occurrence_id="wrong")])
            other = select(selections, session, "other-selection", definition, parent="other")
            supplied = msgspec.structs.replace(other, selected_value_id="computed:root", origin=core.Origin(parent_entity_id="root"))
            session.publish(MetadataBatch("supplied", records=(supplied,), retained=(("selected_value", supplied.selected_value_id),)))
            assert not [link for batch in ledger.read_links([("state", "root")]) for link in batch if link.relation == "computed_selection"]
            expected = parent_evidence(selections, session, "root", definition)
            calls = observe_computed(monkeypatch, selections)
            assert selections.binding_evidence(session, core.StateInput(label="source", state_id="root"), definition)[1] == expected
            assert calls == [("root", None)]
            with pytest.raises(IntegrityError, match="immutable retained record"):
                select(selections, session, supplied.selected_value_id, definition)
            assert not session.computed_selections
            assert not [link for batch in ledger.read_links([("state", "root")]) for link in batch if link.relation == "computed_selection"]
            # An untrusted portable ledger may contain forged relationship rows.
            # Its read-only admission must never treat them as local evaluation.
            from docspec.adapters.storage.core_selections import _selector_key
            ledger.commit(MetadataBatch("foreign-marker", links=(MetadataLink(("state", "root"), "computed_selection",
                _selector_key(definition), ("selected_value", supplied.selected_value_id)),)))
            class ForeignLedger:
                read_only = True
                def __getattr__(self, name):
                    return getattr(ledger, name)
            session.ledger = ForeignLedger()
            assert selections.binding_evidence(session, core.StateInput(label="source", state_id="root"), definition)[1] == expected


def test_cached_layer_removal_requires_fresh_parent_evaluation(tmp_path, monkeypatch):
    definition = core.StateMembers(member_selector=fields("/url"), material_keys=True)
    with ExitStack() as stack:
        _, ledger, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, VALUES)
            selected = select(selections, session, "base", definition)
            expected = selections.evidence(session, selected)
            binding = core.StateInput(label="source", state_id="root")
            assert selections.binding_evidence(session, binding, definition)[1] == expected
            with ledger._transaction(write=True) as connection:
                connection.execute("UPDATE retention SET available=0,evidence_version=evidence_version+1 WHERE kind='selected_value' AND record_id='base'")
            calls = observe_computed(monkeypatch, selections)
            assert selections.binding_evidence(session, binding, definition)[1] == expected
            assert calls == [("root", None)]
            with ledger._transaction(write=True) as connection:
                connection.execute("UPDATE retention SET available=0,evidence_version=evidence_version+1 WHERE kind='state' AND record_id='root'")
            with pytest.raises(IntegrityError, match="available"):
                selections.binding_evidence(session, binding, definition)


def test_computation_witness_must_match_exact_published_record(tmp_path):
    from docspec.adapters.storage.core_selections import _selector_key
    definition = core.StateMembers(member_selector=fields("/url"), material_keys=True)
    with ExitStack() as stack:
        _, _, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, VALUES)
            source = select(selections, session, "base", definition)
            wrong = msgspec.structs.replace(source, selected_value_id="other")
            key = "selected_value", "other"
            session.computed_selections[key] = (canonical_value_bytes(record_value(source)),
                MetadataLink(("state", "root"), "computed_selection", _selector_key(definition), key))
            with pytest.raises(IntegrityError, match="publication witness"):
                session.publish(MetadataBatch("mismatch", records=(wrong,), retained=(key,)))


@pytest.mark.parametrize("limit", ["BATCH_ROWS", "BATCH_BYTES"])
def test_overlay_budget_falls_back_without_retaining_partial_values(tmp_path, monkeypatch, limit):
    from docspec.adapters.storage import core_selections
    definition = core.StateMembers(member_selector=fields("/url"), material_keys=True)
    with ExitStack() as stack:
        _, _, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, VALUES)
            select(selections, session, "base", definition)
            revise(publisher, session)
            expected = parent_evidence(selections, session, "changed", definition)
            monkeypatch.setattr(core_selections, limit, 1)
            calls = observe_computed(monkeypatch, selections)
            assert selections.binding_evidence(session, core.StateInput(label="source", state_id="changed"), definition)[1] == expected
            assert calls[-1] == ("changed", None)
            assert not any(key[0] == "changed" for key in session.member_selections)
