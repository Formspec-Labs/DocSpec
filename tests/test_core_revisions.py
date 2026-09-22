"""Revision resolution pins ordered edits and refuses invalid intermediate edits hidden by a later write.

Also covers native partition reuse, puts checked against both revision inputs, explicit branch bases, and
value-edit evidence matching the retained output.
"""
from tests.support.iceberg_records import files


from contextlib import ExitStack

import pytest
from hypothesis import given, settings

from docspec.domain import core
from docspec.domain.identity import decode_canonical_json_value
from docspec.domain.storage import partition_bucket
from docspec.errors import IntegrityError
from docspec.application.core_execution import CoreOperations
from docspec.application.core_edits import prepare_revision, prepare_value_edit
from docspec.ports.core_ledger import MetadataBatch
from tests.support.core_strategies import membership_histories
from tests.test_core_states import open_core, occurrence


def revision(edits):
    """Build a revision over the root state with fixed revision/base/result identifiers."""
    return core.Revision(format_version=1, revision_id="revision", base_state_id="root", result_state_id="changed", edits=tuple(edits))


def root(states, session, size=2):
    """Create the root state with ``size`` occurrences and members ``a``/``b`` (``key-N`` when size != 2)."""
    states.create(session, state_id="root", representation_id="root-r", unit_id="root",
                  entities=[occurrence(f"e{i}", i) for i in range(size)],
                  members=[core.Membership(member_key=chr(97 + i) if size == 2 else f"key-{i}", occurrence_id=f"e{i}") for i in range(size)])


def membership(layer):
    """Decode a membership layer into a ``{member_key: occurrence_id}`` mapping."""
    return {value["member_key"]: value["occurrence_id"] for batch in layer.batches()
            for payload in batch.column("record_json").to_pylist()
            for value in [decode_canonical_json_value(payload)]}


def test_repeated_edits_preserve_order_and_full_partition_results_agree(tmp_path):
    with ExitStack() as stack:
        _, _, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            root(states, session)
            change = revision([core.Remove(sequence=3, member_key="a"), core.Put(sequence=5, member_key="a", occurrence_id="e1"),
                               core.Put(sequence=1, member_key="c", occurrence_id="e0"), core.Remove(sequence=2, member_key="c")])
            assert membership(states.resolve_membership(session, change)) == {"a": "e1", "b": "e1"}
            assert membership(states.resolve_membership(session, change, full=True)) == {"a": "e1", "b": "e1"}
            with states.relation(session, "root") as original:
                assert [(key, entity) for key, entity, _ in original.order("member_key").fetchall()] == [("a", "e0"), ("b", "e1")]


@pytest.mark.parametrize("edits", [
    [core.Remove(sequence=1, member_key="absent"), core.Put(sequence=2, member_key="absent", occurrence_id="e0")],
    [core.Remove(sequence=1, member_key="a"), core.Remove(sequence=2, member_key="a"), core.Put(sequence=3, member_key="a", occurrence_id="e0")],
    [core.Put(sequence=1, member_key="a", occurrence_id="e0"), core.Put(sequence=1, member_key="a", occurrence_id="e1")],
    [core.Put(sequence=-1, member_key="a", occurrence_id="e0")],
])
def test_invalid_intermediate_edits_are_not_hidden_by_the_last_write(tmp_path, edits):
    with ExitStack() as stack:
        _, _, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            root(states, session)
            for full in (False, True):
                with pytest.raises(IntegrityError):
                    states.resolve_membership(session, revision(edits), full=full)


def test_small_edit_reuses_untouched_partition_files_and_avoids_payload_decoding(tmp_path, monkeypatch):
    with ExitStack() as stack:
        records, _, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            root(states, session, 256)
            manifest = states.manifest(session, "root")
            before = records.available(states._references(manifest)["membership"])
            def no_audit(*args, **kwargs):
                raise AssertionError("revision reparsed retained rows")
            monkeypatch.setattr(records, "_rows", no_audit)
            monkeypatch.setattr(records, "admit", no_audit)
            after = states.resolve_membership(session, revision([core.Remove(sequence=0, member_key="key-0")]))
            untouched = files(records, before)
            assert untouched
            assert all(member in files(after._storage, after) for member in untouched)
            assert len(membership(after)) == 255
            assert len(membership(before)) == 256


def test_retained_edit_array_uses_the_same_resolver(tmp_path):
    with ExitStack() as stack:
        _, _, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            root(states, session)
            edits = session.retain_value([{"kind": "remove", "sequence": 0, "member_key": "a"}])
            change = core.Revision(format_version=1, revision_id="revision", base_state_id="root", result_state_id="changed", edits=edits)
            assert membership(states.resolve_membership(session, change)) == {"b": "e1"}


@settings(max_examples=15, deadline=None)
@given(membership_histories())
def test_native_resolution_matches_independent_history_model(history):
    from tempfile import TemporaryDirectory
    from pathlib import Path
    initial, edits, expected = history
    with TemporaryDirectory() as directory, ExitStack() as stack:
        _, _, states, publisher = open_core(stack, Path(directory))
        with publisher.session() as session:
            root(states, session)
            typed = [core.Remove(sequence=edit["sequence"], member_key=edit["member_key"]) if edit["op"] == "remove"
                     else core.Put(sequence=edit["sequence"], member_key=edit["member_key"], occurrence_id=edit["occurrence_id"])
                     for edit in edits]
            assert initial == {"a": "e0", "b": "e1"}
            assert membership(states.resolve_membership(session, revision(typed))) == expected
            assert membership(states.resolve_membership(session, revision(typed), full=True)) == expected


def test_value_edit_then_membership_composition_preserve_states_and_provenance_on_reopen(tmp_path):
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        operations = CoreOperations(publisher)
        with publisher.session() as session:
            root(states, session)
            value, evidence = prepare_value_edit(operations, "e0", [{"op": "replace", "path": "", "value": {"title": "changed"}}], session=session)
            operations.publish((value,), session=session)
            change = core.Revision(format_version=1, revision_id="revision", base_state_id="root", result_state_id="changed",
                                   edits=(core.Put(sequence=0, member_key="a", occurrence_id=evidence.result_occurrence_id),
                                          core.Remove(sequence=1, member_key="b")), value_edits=(evidence,))
            prepared = prepare_revision(operations, change, session=session)
            result = operations.publish((prepared,), session=session)[0]
            assert result.generations[0].entity_id == "changed"
            assert result.derivations[0].used_entity_id == "root"
            assert next(ledger.read_records([("revision", "revision")]))[0].available
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            with states.relation(session, "root") as old:
                assert [(key, entity) for key, entity, _ in old.order("member_key").fetchall()] == [("a", "e0"), ("b", "e1")]
            with states.relation(session, "changed") as new:
                rows = new.fetchall()
                assert [(key, entity) for key, entity, _ in rows] == [("a", evidence.result_occurrence_id)]
                assert decode_canonical_json_value(rows[0][2])["value"]["value"] == {"title": "changed"}
            assert CoreOperations(publisher).recover(result.execution_id) == result


def test_missing_transient_put_refuses_even_when_later_removed(tmp_path):
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            root(states, session)
            change = revision([core.Put(sequence=0, member_key="c", occurrence_id="absent"), core.Remove(sequence=1, member_key="c")])
            with pytest.raises(IntegrityError, match="available occurrence"):
                prepare_revision(CoreOperations(publisher), change, session=session)
            assert next(ledger.read_records([("state", "changed")]))[0] is None


def test_revision_completeness_uses_checked_puts_without_rechecking_unchanged_members(tmp_path, monkeypatch):
    with ExitStack() as stack:
        _, _, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            root(states, session, 256)
            checked_sizes = []
            original = states._match_members
            def check(layers):
                checked_sizes.append(layers["membership"].reference.record_count)
                return original(layers)
            monkeypatch.setattr(states, "_match_members", check)
            operations = CoreOperations(publisher)
            prepared = prepare_revision(operations, revision([
                core.Put(sequence=0, member_key="key-0", occurrence_id="e1")]), session=session)
            operations.publish((prepared,), session=session)
            assert checked_sizes == [1]  # The new input set is independently admitted.
            with states.relation(session, "changed") as rows:
                assert rows.aggregate("count(*)").fetchone() == (256,)
                assert rows.filter("member_key = 'key-0'").project("occurrence_id").fetchone() == ("e1",)


@pytest.mark.parametrize("identity", ["e0", "missing", "elsewhere"])
def test_direct_revision_checks_puts_against_both_inputs_including_transient_puts(tmp_path, identity):
    with ExitStack() as stack:
        records, _, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            root(states, session)
            states.create(session, state_id="empty", representation_id="empty-r", unit_id="empty", entities=[], members=[])
            states.create(session, state_id="other", representation_id="other-r", unit_id="other",
                          entities=[occurrence("elsewhere", 9)],
                          members=[core.Membership(member_key="other", occurrence_id="elsewhere")])
            change = revision([core.Put(sequence=0, member_key="c", occurrence_id=identity),
                               core.Remove(sequence=1, member_key="c")])
            if identity != "e0":
                with pytest.raises(IntegrityError, match="revision inputs"):
                    states.revision_representation(session, change, representation_id="changed-r", occurrences_state_id="empty")
            else:
                result = states.revision_representation(session, change, representation_id="changed-r", occurrences_state_id="empty")
                manifest = session.read_json(result.membership)
                layer = records.available(states._references(manifest)["membership"])
                assert membership(layer) == {"a": "e0", "b": "e1"}


def test_noop_revision_still_generates_a_distinct_state_and_preserves_files(tmp_path):
    with ExitStack() as stack:
        records, _, states, publisher = open_core(stack, tmp_path)
        operations = CoreOperations(publisher)
        with publisher.session() as session:
            root(states, session)
            before = states.manifest(session, "root")
            prepared = prepare_revision(operations, revision([]), session=session)
            operations.publish((prepared,), session=session)
            after = states.manifest(session, "changed")
            assert before == after
            assert prepared.result.generations[0].entity_id == "changed"
            assert records.available(states._references(after)["membership"])


def test_revision_shares_base_payload_files_even_when_new_ids_hit_every_bucket(tmp_path):
    with ExitStack() as stack:
        records, _, states, publisher = open_core(stack, tmp_path)
        operations = CoreOperations(publisher)
        with publisher.session() as session:
            root(states, session, 256)
            before = records.available(states._references(states.manifest(session, "root"))["entities"])
            ids = {}
            index = 0
            while len(ids) < 64:
                identity = f"added-{index}"
                ids.setdefault(partition_bucket(identity, 64), identity)
                index += 1
            additions = list(ids.values())
            states.create(session, state_id="additions", representation_id="additions-r", unit_id="additions",
                          entities=[occurrence(identity, "wide" * 1024) for identity in additions],
                          members=[core.Membership(member_key=identity, occurrence_id=identity) for identity in additions])
            change = revision([core.Put(sequence=i, member_key=f"new-{i}", occurrence_id=identity)
                               for i, identity in enumerate([*additions, "e0"])])
            operations.publish((prepare_revision(operations, change, session=session),), session=session)
            after = records.available(states._references(states.manifest(session, "changed"))["entities"])
            assert {member["path"] for member in files(before._storage, before)} <= {member["path"] for member in files(after._storage, after)}
            assert after.reference.record_count == 320  # Repeated e0 adds membership, not another payload.
            records.verify(after.reference)
            with states.relation(session, "changed") as relation:
                assert relation.aggregate("count(*)").fetchone() == (321,)


def test_bulk_revision_uses_two_state_inputs_beyond_the_metadata_row_limit(tmp_path, monkeypatch):
    with ExitStack() as stack:
        records, ledger, states, publisher = open_core(stack, tmp_path)
        operations = CoreOperations(publisher)
        with publisher.session() as session:
            root(states, session, 2049)
            original = records.relations
            checked_sizes = []
            def relations(*args, **kwargs):
                wanted = kwargs.get("tables", {}).get("wanted")
                if wanted is not None and "wanted_id" in wanted.column_names:
                    checked_sizes.append(wanted.num_rows)
                return original(*args, **kwargs)
            monkeypatch.setattr(records, "relations", relations)
            change = revision([core.Put(sequence=i, member_key=f"new-{i}", occurrence_id=f"e{i}") for i in range(2049)])
            prepared = prepare_revision(operations, change, session=session)
            operations.publish((prepared,), session=session)
            assert checked_sizes == [2048, 1]
            saved_request = next(ledger.read_records([("request", prepared.execution.request_id)]))[0].value
            assert len(saved_request.inputs) == 2
            assert all(isinstance(binding, core.StateInput) for binding in saved_request.inputs)
            with states.relation(session, "changed") as state:
                assert state.aggregate("count(*)").fetchone() == (4098,)


def test_branches_keep_their_explicit_bases_and_chains_use_the_named_parent(tmp_path):
    with ExitStack() as stack:
        _, _, states, publisher = open_core(stack, tmp_path)
        operations = CoreOperations(publisher)
        with publisher.session() as session:
            root(states, session)
            for identity, base, key in [("left", "root", "a"), ("right", "root", "b"), ("chain", "left", "b")]:
                change = core.Revision(format_version=1, revision_id=identity + ":revision", base_state_id=base, result_state_id=identity,
                                       edits=(core.Remove(sequence=0, member_key=key),))
                operations.publish((prepare_revision(operations, change, session=session),), session=session)
            for identity, expected in [("root", ["a", "b"]), ("left", ["b"]), ("right", ["a"]), ("chain", [])]:
                with states.relation(session, identity) as state:
                    assert [row[0] for row in state.order("member_key").fetchall()] == expected


def test_inline_base_uses_the_existing_writer_before_native_resolution(tmp_path):
    with ExitStack() as stack:
        _, _, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            state = core.State(format_version=1, state_id="root")
            representation = core.StateRepresentation(format_version=1, representation_id="inline", state_id="root",
                                                       membership=(core.Membership(member_key="a", occurrence_id="e0"),))
            session.publish(MetadataBatch("inline", records=(occurrence("e0", 1), state, representation), retained=(("state", "root"),)))
            assert membership(states.resolve_membership(session, revision([core.Remove(sequence=0, member_key="a")]))) == {}
            with states.relation(session, "root") as original:
                assert original.project("member_key, occurrence_id").fetchall() == [("a", "e0")]


def test_revision_refuses_value_edit_evidence_that_disagrees_with_retained_output(tmp_path):
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        operations = CoreOperations(publisher)
        with publisher.session() as session:
            root(states, session)
            prepared, evidence = prepare_value_edit(operations, "e0", [{"op": "replace", "path": "", "value": 5}], session=session)
            operations.publish((prepared,), session=session)
            wrong = core.ValueEdit(source_occurrence_id="e0", result_occurrence_id=evidence.result_occurrence_id,
                                   execution_id=evidence.execution_id, patch=({"op": "replace", "path": "", "value": 6},))
            change = core.Revision(format_version=1, revision_id="revision", base_state_id="root", result_state_id="changed",
                                   edits=(core.Put(sequence=0, member_key="a", occurrence_id=evidence.result_occurrence_id),), value_edits=(wrong,))
            with pytest.raises(IntegrityError, match="retained result value"):
                prepare_revision(operations, change, session=session)
            assert next(ledger.read_records([("state", "changed")]))[0] is None
