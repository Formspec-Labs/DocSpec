"""Production selection, direct retention, and exact parent recovery."""

from collections import Counter
from contextlib import ExitStack

import pytest

from docspec.adapters.storage.core_selections import CoreSelectionStorage
from docspec.application.core_edits import prepare_revision, prepare_value_edit
from docspec.application.core_execution import CoreOperations
from docspec.domain import core
from docspec.domain.identity import decode_canonical_json_value
from docspec.errors import IntegrityError
from docspec.ports.core_ledger import MetadataBatch
from tests.support.core_reference import State, value_key
from tests.test_core_states import open_core, occurrence


def setup(stack, path):
    records, ledger, states, publisher = open_core(stack, path)
    selections = CoreSelectionStorage(records, states)
    publisher.selections = selections
    return records, ledger, states, selections, publisher


def fields(*pointers, identity=False):
    return core.JsonFields(selectors=tuple(core.Field(label=f"f{i}", pointer=pointer) for i, pointer in enumerate(pointers)),
                           comparison="identity" if identity else "value")


def import_root(states, session, values):
    states.create(session, state_id="root", representation_id="root-r", unit_id="root", entities=[occurrence(entity, value) for _, entity, value in values],
                  members=[core.Membership(member_key=key, occurrence_id=entity) for key, entity, _ in values])


def select(selections, session, identity, definition, *, parent="root", recover=False):
    return selections.retain(session, selected_value_id=identity, definition=definition,
                             origin=core.Origin(parent_entity_id=parent), from_parent=recover)


def test_binding_evidence_preserves_whole_state_keys_and_optional_identity(tmp_path):
    with ExitStack() as stack:
        _, _, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            for state, entity, key in [("left", "e0", "a"), ("equal", "e1", "a"), ("rekeyed", "e2", "b")]:
                states.create(session, state_id=state, representation_id=state + "-r", unit_id=state,
                              entities=[occurrence(entity, {"x": 1})],
                              members=[core.Membership(member_key=key, occurrence_id=entity)])
            def evidence(state, *, identity=False):
                return selections.binding_evidence(session, core.StateInput(label="input", state_id=state),
                                                   core.Whole(comparison="identity" if identity else "value"))
            left = evidence("left")
            assert left == evidence("equal")
            assert left != evidence("rekeyed")
            assert left[0] == core.StateMembers(member_selector=core.Whole(), material_keys=True)
            assert evidence("left", identity=True) != evidence("equal", identity=True)
            assert evidence("left", identity=True)[1].entity_id == "left"


def test_whole_selected_binding_keeps_its_field_definition_in_comparison(tmp_path):
    with ExitStack() as stack:
        _, _, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, [("a", "e", {"x": 1, "y": 1})])
            left = select(selections, session, "left", fields("/x"), parent="e")
            right = select(selections, session, "right", fields("/y"), parent="e")
            def evidence(selected):
                return selections.binding_evidence(session, core.SelectedInput(label="input", selected_value_id=selected.selected_value_id), core.Whole())
            assert evidence(left)[1] == evidence(right)[1]
            assert evidence(left)[0] != evidence(right)[0]
            with pytest.raises(IntegrityError, match="whole selection or repeat"):
                selections.binding_evidence(session, core.SelectedInput(label="input", selected_value_id="left"), fields("/different"))


@pytest.mark.parametrize("scope,keys,identities", [(None, False, False), (["missing", "a", "b", "c", "d"], True, True), ([], False, False)])
def test_direct_and_parent_recovery_match_the_independent_model_after_reopen(tmp_path, scope, keys, identities):
    values = [("a", "e0", {}), ("b", "e1", {"x": None}), ("c", "e2", {"x": 1}), ("d", "e3", {"x": "1"})]
    definition = core.StateMembers(member_selector=fields("/x", identity=identities), scope=None if scope is None else tuple(scope), material_keys=keys)
    expected = State.root("root", values).selected_members(selectors=[{"label": "f0", "pointer": "/x"}], scope=scope,
                                                         material_keys=keys, material_entities=identities)
    with ExitStack() as stack:
        _, _, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, values)
            direct = select(selections, session, "direct", definition)
            recovered = select(selections, session, "recovered", definition, recover=True)
    with ExitStack() as stack:
        records, _, _, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            before_files = set(records.root.rglob("*"))
            expected_evidence = None
            for selected in (direct, recovered):
                rows = list(selections.rows(session, selected))
                assert Counter(value_key(decode_canonical_json_value(row[2])) for row in rows) == Counter(map(value_key, expected))
                assert {key: entity for key, entity, _ in rows} == {key: dict((k, e) for k, e, _ in values).get(key) for key in (scope if scope is not None else [k for k, _, _ in values])}
                evidence = selections.evidence(session, selected)
                if expected_evidence is not None:
                    assert evidence == expected_evidence
                expected_evidence = evidence
            assert set(records.root.rglob("*")) == before_files


def test_single_fields_use_the_same_native_rules_and_whole_values_keep_their_codec(tmp_path):
    with ExitStack() as stack:
        _, _, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, [("k", "e", {"a/b": [None, True, 1, "1"], "~": {"01": 8}})])
            definition = fields("/a~1b/0", "/a~1b/1", "/a~1b/2", "/a~1b/3", "/a~1b/01", "/~0/01")
            direct = select(selections, session, "single", definition, parent="e")
            recovered = select(selections, session, "single-recovered", definition, parent="e", recover=True)
            assert selections.value(session, direct) == [["f0", "present", None], ["f1", "present", True], ["f2", "present", 1],
                                                         ["f3", "present", "1"], ["f4", "absent"], ["f5", "present", 8]]
            assert type(selections.value(session, direct)[1][2]) is bool
            assert type(selections.value(session, direct)[2][2]) is int
            assert selections.evidence(session, direct) == selections.evidence(session, recovered)
            whole = select(selections, session, "whole", core.Whole(), parent="e")
            assert selections.value(session, whole) == {"a/b": [None, True, 1, "1"], "~": {"01": 8}}


def test_retained_json_and_opaque_members_keep_types_and_recoverable_bytes(tmp_path):
    with ExitStack() as stack:
        _, _, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            json_content = session.retain_value({"x": True})
            binary_content = session.retain_bytes([b"\x00opaque\xff"])
            states.create(session, state_id="root", representation_id="r", unit_id="root", entities=[
                core.Entity(format_version=1, entity_id="json", entity_type="occurrence", value=json_content),
                core.Entity(format_version=1, entity_id="binary", entity_type="occurrence", value=binary_content)],
                members=[core.Membership(member_key="json", occurrence_id="json"), core.Membership(member_key="binary", occurrence_id="binary")])
            picked = select(selections, session, "json-fields", core.StateMembers(member_selector=fields("/x"), scope=("json",)))
            assert decode_canonical_json_value(next(selections.rows(session, picked))[2]) == ["present", [["f0", "present", True]]]
            whole = select(selections, session, "opaque", core.Whole(), parent="binary")
            assert selections.value(session, whole) == b"\x00opaque\xff"
            assert selections.evidence(session, whole).codec == "bytes-v1"
            members = select(selections, session, "members", core.StateMembers(member_selector=core.Whole()))
            recovered = select(selections, session, "members-recovered", members.definition, recover=True)
            assert selections.evidence(session, members) == selections.evidence(session, recovered)
            with pytest.raises(IntegrityError, match="JSON"):
                select(selections, session, "bad", core.StateMembers(member_selector=fields("/x")))


def test_direct_selection_survives_parent_storage_loss_but_recovery_requires_parent(tmp_path):
    with ExitStack() as stack:
        records, _, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, [("k", "e", {"x": 1})])
            definition = core.StateMembers(member_selector=fields("/x"))
            direct = select(selections, session, "direct", definition)
            recovered = select(selections, session, "recovered", definition, recover=True)
            parent = records.available(states._references(states.manifest(session, "root"))["entities"])
            expected = selections.evidence(session, direct)
            # Simulate loss outside authorized cleanup. The retained selected
            # value has its own files and does not secretly reread its parent.
            (records.root / next(ref.locator for ref in records.physical_references(parent.reference) if ref.locator.endswith(".parquet"))).unlink()
        with publisher.session() as session:
            assert selections.evidence(session, direct) == expected
            with pytest.raises(IntegrityError, match="unavailable"):
                selections.evidence(session, recovered)


def test_materiality_multiplicity_and_title_only_revisions(tmp_path):
    with ExitStack() as stack:
        _, _, states, selections, publisher = setup(stack, tmp_path)
        operations = CoreOperations(publisher)
        with publisher.session() as session:
            import_root(states, session, [("a", "e0", {"url": "u", "title": "old"}), ("b", "e1", {"url": "u"})])
            definitions = [core.StateMembers(member_selector=fields("/url")), core.StateMembers(member_selector=fields("/url", identity=True)),
                           core.StateMembers(member_selector=fields("/url"), material_keys=True),
                           core.StateMembers(member_selector=core.Whole(), material_keys=True)]
            original = [select(selections, session, f"original-{i}", definition) for i, definition in enumerate(definitions)]
            edited, evidence = prepare_value_edit(operations, "e0", [{"op": "replace", "path": "/title", "value": "new"}], session=session)
            operations.publish((edited,), session=session)
            change = core.Revision(format_version=1, revision_id="revision", base_state_id="root", result_state_id="changed",
                                   edits=(core.Put(sequence=0, member_key="a", occurrence_id=evidence.result_occurrence_id),), value_edits=(evidence,))
            operations.publish((prepare_revision(operations, change, session=session),), session=session)
            changed = [select(selections, session, f"changed-{i}", definition, parent="changed") for i, definition in enumerate(definitions)]
            assert [selections.evidence(session, before) == selections.evidence(session, after) for before, after in zip(original, changed)] == [True, False, True, False]
            one = select(selections, session, "one", core.StateMembers(member_selector=fields("/url"), scope=("a",)))
            assert selections.evidence(session, one) != selections.evidence(session, original[0])
            updated, url_edit = prepare_value_edit(operations, "e0", [{"op": "replace", "path": "/url", "value": "different"}], session=session)
            operations.publish((updated,), session=session)
            url_revision = core.Revision(format_version=1, revision_id="url-revision", base_state_id="root", result_state_id="url-changed",
                                         edits=(core.Put(sequence=0, member_key="a", occurrence_id=url_edit.result_occurrence_id),), value_edits=(url_edit,))
            operations.publish((prepare_revision(operations, url_revision, session=session),), session=session)
            urls = select(selections, session, "new-urls", definitions[0], parent="url-changed")
            assert selections.evidence(session, urls) != selections.evidence(session, original[0])


def test_retained_sorting_rule_controls_consumed_sequence_and_survives_reopen(tmp_path):
    with ExitStack() as stack:
        _, ledger, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, [("a", "e0", {"rank": "b", "x": 1}), ("b", "e1", {"rank": "a", "x": 2})])
            rule = core.OperationDefinition(format_version=1, definition_id="sort", implementation_id="docspec.sort.canonical-json",
                                             implementation_version="1", operation_kind="transformation", configuration={"pointers": ["/rank"], "descending": False})
            session.publish(MetadataBatch("sort", records=(rule,), retained=(("operation_definition", "sort"),)))
            definition = core.StateMembers(member_selector=fields("/x"), sort_rule="sort")
            direct = select(selections, session, "ordered", definition)
            recovered = select(selections, session, "ordered-recovered", definition, recover=True)
            assert [key for key, _, _ in selections.rows(session, direct)] == ["b", "a"]
            assert selections.evidence(session, direct) == selections.evidence(session, recovered)
            links = [link for batch in ledger.read_links([("selected_value", "ordered")]) for link in batch]
            assert any(link.target == ("operation_definition", "sort") for link in links)
    with ExitStack() as stack:
        _, _, _, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            assert [key for key, _, _ in selections.rows(session, direct)] == ["b", "a"]
            assert selections.evidence(session, direct) == selections.evidence(session, recovered)


@pytest.mark.parametrize("definition", [core.StateMembers(member_selector=fields("/bad~2")), core.StateMembers(member_selector=fields("/x"), scope=("a", "a"))])
def test_invalid_definitions_refuse_before_reading_a_parent(tmp_path, definition):
    with ExitStack() as stack:
        _, _, _, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session, pytest.raises(IntegrityError):
            select(selections, session, "bad", definition, parent="missing")


def test_bulk_selection_exceeds_metadata_unit_without_changing_recovery_files(tmp_path):
    count = 2050
    with ExitStack() as stack:
        records, _, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, [(f"key-{i}", f"e{i}", {"x": i % 3}) for i in range(count)])
            definition = core.StateMembers(member_selector=fields("/x"))
            direct = select(selections, session, "bulk", definition)
            recovered = select(selections, session, "bulk-recovered", definition, recover=True)
            before = set(records.root.rglob("*"))
            assert sum(1 for _ in selections.rows(session, direct)) == count
            assert selections.evidence(session, direct) == selections.evidence(session, recovered)
            assert set(records.root.rglob("*")) == before


def test_array_positions_and_material_keys_recompute_against_the_new_state(tmp_path):
    with ExitStack() as stack:
        _, _, states, selections, publisher = setup(stack, tmp_path)
        operations = CoreOperations(publisher)
        with publisher.session() as session:
            import_root(states, session, [("old-key", "e", {"items": ["old", "next"]})])
            definition = core.StateMembers(member_selector=fields("/items/0"))
            keyed = core.StateMembers(member_selector=definition.member_selector, material_keys=True)
            before = [select(selections, session, f"old-{i}", item) for i, item in enumerate((definition, keyed))]
            rename = core.Revision(format_version=1, revision_id="rename", base_state_id="root", result_state_id="renamed",
                                   edits=(core.Remove(sequence=0, member_key="old-key"), core.Put(sequence=1, member_key="new-key", occurrence_id="e")))
            operations.publish((prepare_revision(operations, rename, session=session),), session=session)
            after = [select(selections, session, f"new-{i}", item, parent="renamed") for i, item in enumerate((definition, keyed))]
            assert [selections.evidence(session, a) == selections.evidence(session, b) for a, b in zip(before, after)] == [True, False]
            edited, evidence = prepare_value_edit(operations, "e", [{"op": "remove", "path": "/items/0"}], session=session)
            operations.publish((edited,), session=session)
            scalar = select(selections, session, "new-first", definition.member_selector, parent=evidence.result_occurrence_id)
            assert selections.value(session, scalar) == [["f0", "present", "next"]]


@pytest.mark.parametrize("comparison", [
    '["present",[["wrong","present",1]]]',
    '["present",[["f0","present",1]],["key","wrong"]]',
    '["absent"]',
    '[ "present",[["f0","present",1]]]',
])
def test_external_selected_rows_are_admitted_before_successful_retention(tmp_path, comparison):
    from docspec.adapters.storage.core_selections import _SCHEMA, _POLICY
    with ExitStack() as stack:
        records, ledger, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, [("k", "e", {"x": 1})])
            definition = core.StateMembers(member_selector=fields("/x"))
            good = select(selections, session, "good", definition)
            manifest = selections._manifest(session, good)
            layer = records.write_layer([{"record_identity": "k", "partition_value": "k", "occurrence_id": "e", "record_json": comparison.encode(), "sort_key": "[]", "content": None}],
                                        layer_kind="core-selected-members", schema=_SCHEMA, partition_policy=_POLICY)
            content = session.retain_value({**manifest, "layer": layer.to_dict()}, media_type=good.value.media_type)
            bad = core.SelectedValue(format_version=1, selected_value_id="bad", definition=definition, origin=good.origin, value=content, member_origins=content)
            with pytest.raises(IntegrityError):
                session.publish(MetadataBatch("bad", records=(bad,), retained=(("selected_value", "bad"),)))
            assert next(ledger.read_records([("selected_value", "bad")]))[0] is None


def test_selected_bytes_are_native_columns_and_compaction_preserves_them(tmp_path):
    from docspec.domain.identity import canonical_value_bytes
    with ExitStack() as stack:
        records, _, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            value = {"text": 'quote"\\\n\u0000' * 1024, "n": 1, "s": "1"}
            states.create(session, state_id="root", representation_id="root-r", unit_id="root", entities=[occurrence("e", value)],
                          members=[core.Membership(member_key=key, occurrence_id="e") for key in ("a", "alias")])
            selected = select(selections, session, "whole", core.StateMembers(member_selector=core.Whole(), scope=("a", "alias", "missing")))
            layer = records.admit(selections._reference(selections._manifest(session, selected)))
            native = [row for batch in layer.batches() for row in batch.to_pylist()]
            assert {row["record_identity"] for row in native} == {"a", "alias", "missing"}
            assert native[0]["record_json"] == canonical_value_bytes(["present", ["present", value]])
            assert native[0]["content"] is None
            assert "comparison_json" not in native[0]
            compacted = records.compact(layer)
            records.verify(compacted.reference)
            assert list(records.stream(compacted.reference)) == native


def test_external_selected_rows_refuse_mismatched_routing(tmp_path):
    from docspec.adapters.storage.core_selections import _SCHEMA, _POLICY
    with ExitStack() as stack:
        records, ledger, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, [("k", "e", {"x": 1})])
            good = select(selections, session, "good", core.StateMembers(member_selector=fields("/x")))
            manifest = selections._manifest(session, good)
            row = list(records.stream(selections._reference(manifest)))[0]
            row["partition_value"] = "different-key"
            layer = records.write_layer([row], layer_kind="core-selected-members", schema=_SCHEMA, partition_policy=_POLICY)
            content = session.retain_value({**manifest, "layer": layer.to_dict()}, media_type=good.value.media_type)
            bad = core.SelectedValue(format_version=1, selected_value_id="bad", definition=good.definition, origin=good.origin,
                                     value=content, member_origins=content)
            with pytest.raises(IntegrityError, match="origin"):
                session.publish(MetadataBatch("bad", records=(bad,), retained=(("selected_value", "bad"),)))
            assert next(ledger.read_records([("selected_value", "bad")]))[0] is None


def test_large_whole_opaque_selection_has_a_closable_stream(tmp_path):
    from docspec.errors import LimitExceededError
    from docspec.ports.record_storage import BATCH_BYTES
    with ExitStack() as stack:
        _, _, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            piece = b"x" * (1024 * 1024)
            content = session.retain_bytes(piece for _ in range(9))
            states.create(session, state_id="root", representation_id="r", unit_id="root",
                          entities=[core.Entity(format_version=1, entity_id="opaque", entity_type="occurrence", value=content)],
                          members=[core.Membership(member_key="k", occurrence_id="opaque")])
            selected = select(selections, session, "whole-opaque", core.Whole(), parent="opaque")
            with pytest.raises(LimitExceededError):
                selections.value(session, selected)
            assert sum(len(chunk) for chunk in selections.read_chunks(session, selected)) == content.byte_size > BATCH_BYTES


def test_noncanonical_or_wrongly_framed_content_fields_do_not_publish(tmp_path):
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            content = session.retain_value([["wrong", "present", 1]])
            bad = core.SelectedValue(format_version=1, selected_value_id="bad", definition=fields("/x"),
                                      origin=core.Origin(parent_entity_id="unretained-parent"), value=content)
            with pytest.raises(IntegrityError, match="selected field"):
                session.publish(MetadataBatch("bad", records=(bad,), retained=(("selected_value", "bad"),)))
            assert next(ledger.read_records([("selected_value", "bad")]))[0] is None


def test_retained_comparison_bytes_preserve_the_shared_lowercase_escape(tmp_path):
    with ExitStack() as stack:
        _, _, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, [("key", "e", {"x": "\u001f"})])
            definition = core.StateMembers(member_selector=fields("/x"))
            for index, recover in enumerate((False, True)):
                selected = select(selections, session, f"control-{index}", definition, recover=recover)
                assert next(selections.rows(session, selected))[2] == b'["present",[["f0","present","\\u001f"]]]'
