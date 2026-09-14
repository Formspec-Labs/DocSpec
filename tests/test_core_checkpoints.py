"""Physical maintenance preserves logical states, history and recovery paths."""
from tests.support.iceberg_records import files


from contextlib import ExitStack

import pytest

from docspec.application.core_edits import prepare_revision
from docspec.application.core_execution import CoreOperations
from docspec.domain import core
from docspec.errors import IntegrityError
from tests.test_core_selections import setup, select, fields
from tests.test_core_states import occurrence
from tests.test_core_revisions import membership


def root(states, session, count=2):
    states.create(session, state_id="root", representation_id="root-r", unit_id="root",
                  entities=(occurrence(f"e{i}", {"x": i, "body": "x" * 500}) for i in range(count)),
                  members=(core.Membership(member_key=f"key-{i}", occurrence_id=f"e{i}") for i in range(count)))


def test_checkpoint_repacks_and_prunes_values_without_new_states_or_provenance(tmp_path, monkeypatch):
    with ExitStack() as stack:
        records, ledger, states, selections, publisher = setup(stack, tmp_path)
        records.max_member_bytes = 4096
        operations = CoreOperations(publisher)
        with publisher.session() as session:
            root(states, session, 512)
            revision = core.Revision(format_version=1, revision_id="revision", base_state_id="root", result_state_id="changed",
                                     edits=(core.Remove(sequence=0, member_key="key-0"), core.Put(sequence=1, member_key="alias", occurrence_id="e1")))
            operations.publish((prepare_revision(operations, revision, session=session),), session=session)
            source = states.representation(session, "changed")
            old_manifest = states.manifest(session, "changed")
            old_values = records.available(states._references(old_manifest)["entities"])
            definition = core.StateMembers(member_selector=fields("/x"), material_keys=True)
            direct = select(selections, session, "direct", definition, parent="changed")
            recovered = select(selections, session, "recovered", definition, parent="changed", recover=True)
            expected = selections.evidence(session, direct)
            ledger.select_current("current", "dataset", ("state", "changed"), None)
            with ledger._transaction() as connection:
                logical_counts = connection.execute("SELECT (SELECT count(*) FROM records WHERE kind='state'),(SELECT count(*) FROM records WHERE kind='entity'),(SELECT count(*) FROM provenance_events)").fetchone()
            old_files = set(records.root.rglob("*"))
            records.max_member_bytes = 1024**2
            checkpoint = states.checkpoint(session, "changed", representation_id="compact", unit_id="compact")
            assert checkpoint.state_id == "changed" and checkpoint.revision_id == "revision"
            assert states.representation(session, "changed") == checkpoint
            compacted = states.manifest(session, "changed")
            new_values = records.available(states._references(compacted)["entities"])
            assert new_values.reference.record_count == 511
            assert {item["path"] for item in files(records, new_values)}.isdisjoint(item["path"] for item in files(records, old_values))
            assert new_values.reference.record_count < old_values.reference.record_count
            assert old_files <= set(records.root.rglob("*"))
            assert ledger.current("dataset") == ("state", "changed")
            assert next(ledger.read_records([("state_representation", source.representation_id)]))[0].available
            assert selections.evidence(session, direct) == selections.evidence(session, recovered) == expected
            replayed = states.resolve_membership(session, revision)
            assert membership(replayed) == membership(records.available(states._references(compacted)["membership"]))
            with ledger._transaction() as connection:
                assert connection.execute("SELECT (SELECT count(*) FROM records WHERE kind='state'),(SELECT count(*) FROM records WHERE kind='entity'),(SELECT count(*) FROM provenance_events)").fetchone() == logical_counts
    with ExitStack() as stack:
        _, ledger, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            def unexpected(*args, **kwargs):
                raise AssertionError("checkpoint lookup replayed history")
            monkeypatch.setattr(states, "resolve_membership", unexpected)
            assert states.representation(session, "changed") == checkpoint
            assert selections.evidence(session, direct) == selections.evidence(session, recovered) == expected
            with states.relation(session, "root") as old:
                assert old.aggregate("count(*)").fetchone() == (512,)
            with states.relation(session, "changed", scope=("key-0", "key-1", "alias")) as changed:
                assert "ORDER_BY" not in changed.explain()
                assert changed.project("member_key, occurrence_id").order("member_key").fetchall() == [("alias", "e1"), ("key-0", None), ("key-1", "e1")]
            assert ledger.current("dataset") == ("state", "changed")


def test_generic_compaction_rejects_a_changed_native_copy(tmp_path, monkeypatch):
    with ExitStack() as stack:
        records, _, states, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            root(states, session)
            states.create(session, state_id="other", representation_id="other-r", unit_id="other",
                          entities=[occurrence("different", 8)], members=[core.Membership(member_key="key-0", occurrence_id="different")])
            original = records.available(states._references(states.manifest(session, "root"))["membership"])
            changed = records.available(states._references(states.manifest(session, "other"))["membership"])
            monkeypatch.setattr(records, "retain_batches", lambda *args, **kwargs: changed)
            with pytest.raises(IntegrityError, match="logical records"):
                records.compact(original)


@pytest.mark.parametrize("after_commit", [False, True])
def test_interrupted_checkpoint_reopens_and_retries_the_same_publication(tmp_path, monkeypatch, after_commit):
    with ExitStack() as stack:
        _, ledger, states, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            root(states, session)
            original = ledger.commit
            def fail(batch):
                if batch.unit_id == "checkpoint":
                    if after_commit:
                        original(batch)
                    raise OSError("lost checkpoint response")
                return original(batch)
            monkeypatch.setattr(ledger, "commit", fail)
            with pytest.raises(OSError, match="lost checkpoint"):
                states.checkpoint(session, "root", representation_id="checkpoint", unit_id="checkpoint")
    with ExitStack() as stack:
        _, ledger, states, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            assert states.representation(session, "root").representation_id == ("checkpoint" if after_commit else "root-r")
            with states.relation(session, "root") as relation:
                assert relation.aggregate("count(*)").fetchone() == (2,)
            states.checkpoint(session, "root", representation_id="checkpoint", unit_id="checkpoint")
            assert ledger.is_committed("checkpoint")
            assert states.representation(session, "root").representation_id == "checkpoint"


def test_changed_membership_is_refused_before_selecting_a_checkpoint(tmp_path, monkeypatch):
    with ExitStack() as stack:
        records, ledger, states, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            root(states, session)
            states.create(session, state_id="empty", representation_id="empty-r", unit_id="empty", entities=[], members=[])
            empty = records.available(states._references(states.manifest(session, "empty"))["membership"])
            # Corrupt the native copy, keeping the compaction owner
            # responsible for its exact equality check.
            monkeypatch.setattr(records, "retain_batches", lambda *args, **kwargs: empty)
            with pytest.raises(IntegrityError, match="logical records"):
                states.checkpoint(session, "root", representation_id="bad", unit_id="bad")
            assert not ledger.is_committed("bad")
            assert states.representation(session, "root").representation_id == "root-r"
