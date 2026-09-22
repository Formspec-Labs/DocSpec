"""Policy removal preserves live commitments and records interrupted outcomes."""

from contextlib import ExitStack

import pytest

from docspec.application.core_maintenance import CoreMaintenance
from docspec.domain import core
from docspec.domain.references import BlobRef
from docspec.errors import IntegrityError, StaleBaseError
from docspec.ports.core_ledger import MetadataBatch, RemovalContent
from tests.test_core_selections import setup, import_root, select, fields


def authorize(publisher, keys=(), *, orphans=False, identity="policy"):
    """Publish and retain a removal policy naming `keys`, optionally allowing orphan collection."""
    policy = core.RetentionPolicy(format_version=1, policy_id=identity,
                                  description={"remove": [list(key) for key in keys], "collect_unreferenced": orphans})
    with publisher.session() as session:
        session.publish(MetadataBatch(identity, records=(policy,), retained=(("retention_policy", identity),)))
    return identity


def records_for(ledger, keys):
    """Read the given keys and flatten the batches into one entry per key."""
    return [row for batch in ledger.read_records(keys) for row in batch]


ROOT_KEYS = (("state", "root"), ("state_representation", "root-r"), ("entity", "e"))


def test_current_switch_is_guarded_and_retains_both_states(tmp_path):
    """Switching current requires the expected base, and removing the current state refuses before any intent exists."""
    with ExitStack() as stack:
        records, ledger, states, _, publisher = setup(stack, tmp_path)
        maintenance = CoreMaintenance(publisher, records)
        with publisher.session() as session:
            import_root(states, session, [("key", "e", {"x": 1})])
            states.from_occurrences(session, state_id="other", representation_id="other-r", unit_id="other", occurrence_ids=["e"])
        assert maintenance.select_current("choose-root", "dataset", ("state", "root"), None)
        with pytest.raises(StaleBaseError):
            maintenance.select_current("stale", "dataset", ("state", "other"), None)
        assert maintenance.select_current("choose-other", "dataset", ("state", "other"), ("state", "root"))
        assert maintenance.select_current("back", "dataset", ("state", "root"), ("state", "other"))
        assert all(row.available for row in records_for(ledger, [("state", "root"), ("state", "other")]))
        authorize(publisher, ROOT_KEYS)
        with pytest.raises(IntegrityError, match="current selection"):
            maintenance.remove_under_policy("remove-current", "policy", ROOT_KEYS)
        assert ledger.removal("remove-current") is None


def test_policy_scope_and_recovery_commitments_refuse_before_intent(tmp_path):
    """An absent policy, out-of-scope keys, commitments outside the state and bare membership entities all refuse first."""
    with ExitStack() as stack:
        records, ledger, states, selections, publisher = setup(stack, tmp_path)
        maintenance = CoreMaintenance(publisher, records)
        with publisher.session() as session:
            import_root(states, session, [("key", "e", {"x": 1})])
            select(selections, session, "recovery", core.StateMembers(member_selector=fields("/x")), recover=True)
        with pytest.raises(IntegrityError, match="available retained policy"):
            maintenance.remove_under_policy("missing", "absent", ROOT_KEYS)
        authorize(publisher, ROOT_KEYS)
        with pytest.raises(IntegrityError, match="does not authorize"):
            maintenance.remove_under_policy("outside", "policy", [("selected_value", "recovery")])
        with pytest.raises(IntegrityError, match="commitments outside"):
            maintenance.remove_under_policy("required", "policy", ROOT_KEYS)
        with pytest.raises(IntegrityError, match="state membership"):
            maintenance.remove_under_policy("member", "policy", [("entity", "e")])
        assert ledger.removal("required") is None
        assert all(row.available for row in records_for(ledger, ROOT_KEYS))


def test_direct_selection_survives_actual_parent_bulk_byte_reclamation(tmp_path):
    """Direct selection evidence still answers after the parent's bulk bytes are deleted, and a repeat removal is idempotent."""
    with ExitStack() as stack:
        records, ledger, states, selections, publisher = setup(stack, tmp_path)
        maintenance = CoreMaintenance(publisher, records)
        with publisher.session() as session:
            import_root(states, session, [("key", "e", {"x": 1, "large": "z" * 80000})])
            direct = select(selections, session, "direct", core.StateMembers(member_selector=fields("/x")))
            expected = selections.evidence(session, direct)
            manifest = states.manifest(session, "root")
            entity_layer = states._references(manifest)["entities"]
            old_files = list(records.physical_references(entity_layer))
        authorize(publisher, ROOT_KEYS)
        with ledger._transaction() as connection:
            before = connection.execute("SELECT count(*) FROM records").fetchone()
        outcome = maintenance.remove_under_policy("remove", "policy", ROOT_KEYS)
        assert outcome["deleted"] >= 5
        assert all(not (records.root / ref.locator).exists() for ref in old_files)
        stored = records_for(ledger, ROOT_KEYS)
        assert all(row.retained and not row.available and row.evidence_version == 1 for row in stored)
        assert stored[-1].value is None
        with ledger._transaction() as connection:
            assert connection.execute("SELECT count(*) FROM records").fetchone() == before
            assert connection.execute("SELECT row_digest,source_layer FROM records WHERE record_id='e'").fetchone()[0].startswith("sha256:")
        assert maintenance.remove_under_policy("remove", "policy", ROOT_KEYS) == outcome
    with ExitStack() as stack:
        records, ledger, _, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            assert selections.evidence(session, direct) == expected
        assert records_for(ledger, [("entity", "e")])[0].value is None
        assert ledger.removal("remove")[2]


def test_shared_entity_source_layer_and_content_are_retained(tmp_path):
    """Removing one occurrence key keeps the entity source files that other member keys still depend on."""
    with ExitStack() as stack:
        records, ledger, states, _, publisher = setup(stack, tmp_path)
        maintenance = CoreMaintenance(publisher, records)
        with publisher.session() as session:
            import_root(states, session, [("a", "e", 1), ("b", "keep", 2)])
            manifest = states.manifest(session, "root")
            layer = states._references(manifest)["entities"]
            old_files = list(records.physical_references(layer))
        keys = ROOT_KEYS
        authorize(publisher, keys)
        outcome = maintenance.remove_under_policy("shared", "policy", keys)
        assert outcome["retained"] == len(old_files)
        assert all((records.root / ref.locator).exists() for ref in old_files)
        assert records_for(ledger, [("entity", "keep")])[0].value.value.value == 2
        assert records_for(ledger, [("entity", "e")])[0].value is None


@pytest.mark.parametrize("after_delete", [False, True])
def test_interrupted_deletion_reopens_and_resumes_durable_outcomes(tmp_path, monkeypatch, after_delete):
    """An interrupted deletion records deleted/failed/pending outcomes, and resume completes from them."""
    with ExitStack() as stack:
        records, ledger, states, _, publisher = setup(stack, tmp_path)
        maintenance = CoreMaintenance(publisher, records)
        with publisher.session() as session:
            import_root(states, session, [("key", "e", {"x": 1})])
        authorize(publisher, ROOT_KEYS)
        original = records.delete
        calls = 0
        def interrupted(reference):
            nonlocal calls
            calls += 1
            if calls == 2:
                if after_delete:
                    original(reference)
                raise OSError("interrupted physical deletion")
            return original(reference)
        monkeypatch.setattr(records, "delete", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            maintenance.remove_under_policy("remove", "policy", ROOT_KEYS)
        outcomes = [row for batch in ledger.removal_outcomes("remove") for row in batch]
        assert {row.status for row in outcomes} >= {"deleted", "failed", "pending"}
        assert not ledger.removal("remove")[2]
        assert all(not row.available for row in records_for(ledger, ROOT_KEYS))
    with ExitStack() as stack:
        records, ledger, _, _, publisher = setup(stack, tmp_path)
        counts = CoreMaintenance(publisher, records).resume("remove")
        assert set(counts) == ({"deleted", "absent"} if after_delete else {"deleted"})
        assert not list(ledger.pending_removals())
        assert ledger.removal("remove")[2]


def test_orphans_need_explicit_policy_and_cannot_name_live_bytes(tmp_path):
    """Orphan collection requires the policy's orphan flag and refuses bytes a retained commitment needs."""
    with ExitStack() as stack:
        records, ledger, _, _, publisher = setup(stack, tmp_path)
        maintenance = CoreMaintenance(publisher, records)
        with publisher.session() as session:
            value = session.retain_bytes([b"live" * 100])
            entity = core.Entity(format_version=1, entity_id="live", entity_type="occurrence", value=value)
            session.publish(MetadataBatch("live", records=(entity,), retained=(("entity", "live"),)))
            leftover = session.retain_bytes([b"crash leftover"])
        def physical(content):
            return RemovalContent("blobs", BlobRef(content.locator, content.digest, content.byte_size, content.media_type))
        authorize(publisher, orphans=True)
        with pytest.raises(IntegrityError, match="required by a retained commitment"):
            maintenance.remove_under_policy("bad", "policy", orphan_content=[physical(value)])
        assert maintenance.remove_under_policy("orphans", "policy", orphan_content=[physical(leftover)]) == {"deleted": 1}
        assert ledger.removal("orphans") == ("policy", (), True)
        assert publisher.blobs.stat(physical(value).reference)


def test_available_external_rows_never_hide_missing_bytes(tmp_path):
    with ExitStack() as stack:
        records, ledger, states, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, [("key", "e", 1)])
            manifest = states.manifest(session, "root")
            layer = states._references(manifest)["entities"]
            for reference in records.physical_references(layer):
                (records.root / reference.locator).unlink()
        with pytest.raises((IntegrityError, FileNotFoundError)):
            records_for(ledger, [("entity", "e")])


def test_reclaimed_occurrence_can_only_restore_its_exact_canonical_identity(tmp_path):
    """A reclaimed occurrence restores only its exact canonical value; a changed value refuses as immutable."""
    with ExitStack() as stack:
        records, ledger, states, _, publisher = setup(stack, tmp_path)
        maintenance = CoreMaintenance(publisher, records)
        with publisher.session() as session:
            import_root(states, session, [("key", "e", {"x": 1})])
        authorize(publisher, ROOT_KEYS)
        maintenance.remove_under_policy("remove", "policy", ROOT_KEYS)
        assert records_for(ledger, [("entity", "e")])[0].value is None
        from tests.test_core_states import occurrence
        with publisher.session() as session:
            with pytest.raises(IntegrityError, match="immutable|identity|changed"):
                session.publish(MetadataBatch("conflict", records=(occurrence("e", {"x": 2}),), retained=(("entity", "e"),)))
            session.publish(MetadataBatch("restore", records=(occurrence("e", {"x": 1}),), retained=(("entity", "e"),)))
        row = records_for(ledger, [("entity", "e")])[0]
        assert row.available and row.retained and row.value.value.value == {"x": 1}
        assert row.evidence_version == 2


def test_obsolete_representation_reclaims_stale_source_files_after_compaction(tmp_path):
    with ExitStack() as stack:
        records, ledger, states, selections, publisher = setup(stack, tmp_path)
        maintenance = CoreMaintenance(publisher, records)
        records.max_member_bytes = 4096
        with publisher.session() as session:
            import_root(states, session, [(str(i), f"e{i}", {"x": i, "body": "x" * 500}) for i in range(512)])
            old = states.manifest(session, "root")
            old_refs = {ref.locator for layer in states._references(old).values() for ref in records.physical_references(layer)}
            records.max_member_bytes = 1024**2
            states.checkpoint(session, "root", representation_id="checkpoint", unit_id="checkpoint")
            current = states.manifest(session, "root")
            current_refs = {ref.locator for layer in states._references(current).values() for ref in records.physical_references(layer)}
            selected = select(selections, session, "recover", core.StateMembers(member_selector=fields("/x")), recover=True)
            expected = selections.evidence(session, selected)
        assert old_refs - current_refs
        authorize(publisher, [("state_representation", "root-r")])
        result = maintenance.remove_under_policy("obsolete", "policy", [("state_representation", "root-r")])
        assert result["deleted"] >= len(old_refs - current_refs)
        assert all(not (records.root / locator).exists() for locator in old_refs - current_refs)
        assert all((records.root / locator).exists() for locator in current_refs)
        with publisher.session() as session:
            assert selections.evidence(session, selected) == expected
        assert all(row.available for row in records_for(ledger, [("entity", f"e{i}") for i in range(512)]))


def test_direct_opaque_members_keep_shared_blobs_when_parent_is_removed(tmp_path):
    with ExitStack() as stack:
        records, ledger, states, selections, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            content = session.retain_bytes([b"opaque document bytes"])
            entity = core.Entity(format_version=1, entity_id="e", entity_type="occurrence", value=content)
            states.create(session, state_id="root", representation_id="root-r", unit_id="root", entities=[entity],
                          members=[core.Membership(member_key="key", occurrence_id="e")])
            direct = select(selections, session, "direct", core.StateMembers(member_selector=core.Whole()))
            evidence = selections.evidence(session, direct)
        authorize(publisher, ROOT_KEYS)
        result = CoreMaintenance(publisher, records).remove_under_policy("remove", "policy", ROOT_KEYS)
        assert result["retained"] == 1
        with publisher.session() as session:
            assert selections.evidence(session, direct) == evidence
            assert b"".join(publisher.blobs.read(BlobRef(content.locator, content.digest, content.byte_size, content.media_type))) == b"opaque document bytes"
        assert not records_for(ledger, [("entity", "e")])[0].available


def test_resume_rechecks_new_retention_before_deleting_pending_orphan(tmp_path, monkeypatch):
    """Resume rechecks retention, so a pending orphan that became referenced is retained rather than deleted."""
    with ExitStack() as stack:
        records, ledger, _, _, publisher = setup(stack, tmp_path)
        maintenance = CoreMaintenance(publisher, records)
        with publisher.session() as session:
            content = session.retain_bytes([b"unpublished then referenced"])
        physical = RemovalContent("blobs", BlobRef(content.locator, content.digest, content.byte_size, content.media_type))
        authorize(publisher, orphans=True)
        original = publisher.blobs.delete
        def interrupted(reference):
            raise OSError("stop")
        monkeypatch.setattr(publisher.blobs, "delete", interrupted)
        with pytest.raises(OSError):
            maintenance.remove_under_policy("remove", "policy", orphan_content=[physical])
        assert list(ledger.pending_removals()) == [(('remove', 'policy', None),)]
        with publisher.session() as session:
            entity = core.Entity(format_version=1, entity_id="new", entity_type="occurrence", value=content)
            session.publish(MetadataBatch("publish", records=(entity,), retained=(("entity", "new"),)))
        monkeypatch.setattr(publisher.blobs, "delete", original)
        assert maintenance.resume("remove") == {"retained": 1}
        assert publisher.blobs.stat(physical.reference)


def test_cleanup_and_publication_hold_exclusive_and_shared_scopes_end_to_end(tmp_path, monkeypatch):
    """Cleanup holds an exclusive scope and publication a shared one, each refusing while the other is active."""
    from docspec.errors import StateTransitionError
    with ExitStack() as stack:
        records, ledger, states, _, publisher = setup(stack, tmp_path)
        other_records, other_ledger, _, _, other_publisher = setup(stack, tmp_path)
        maintenance = CoreMaintenance(publisher, records)
        other_maintenance = CoreMaintenance(other_publisher, other_records)
        with publisher.session() as session:
            import_root(states, session, [("key", "e", 1)])
        authorize(publisher, ROOT_KEYS)
        with publisher.session():
            with pytest.raises(StateTransitionError, match="protected"):
                other_maintenance.remove_under_policy("blocked", "policy", ROOT_KEYS)
        assert ledger.removal("blocked") is None
        original = records.delete
        def deleting(reference):
            with pytest.raises(StateTransitionError, match="protected"):
                with other_publisher.session():
                    pytest.fail("publication entered during physical deletion")
            return original(reference)
        monkeypatch.setattr(records, "delete", deleting)
        assert maintenance.remove_under_policy("remove", "policy", ROOT_KEYS)["deleted"] >= 5
        assert other_ledger.removal("remove")[2]


def test_orphan_collection_preserves_explicit_suspension_checkpoint(tmp_path):
    from docspec.application.core_execution import CoreOperations
    from tests.test_core_execution import definition, request
    from tests.test_core_continuation import start, finish
    with ExitStack() as stack:
        records, _, _, _, publisher = setup(stack, tmp_path)
        operations = CoreOperations(publisher)
        suspended = operations.run(definition(), request(), start)
        reference = suspended.checkpoint
        orphan = RemovalContent("blobs", BlobRef(reference.locator, reference.digest, reference.byte_size, reference.media_type))
        authorize(publisher, orphans=True)
        with pytest.raises(IntegrityError, match="required by a retained commitment"):
            CoreMaintenance(publisher, records).remove_under_policy("collect-checkpoint", "policy", orphan_content=[orphan])
        assert operations.resume(suspended.execution_id, finish, definition=definition(), verify=lambda state: True).outcome.status == "success"


def test_pending_publication_protects_adopted_inputs_and_generated_content(tmp_path, monkeypatch):
    """A pending publication protects adopted inputs, generated bytes and its journal until the result is retained."""
    from docspec.application.core_execution import CoreOperations
    from tests.test_core_execution import definition, request, progress
    with ExitStack() as stack:
        records, ledger, _, _, publisher = setup(stack, tmp_path)
        operations = CoreOperations(publisher)
        with publisher.session() as session:
            adopted = core.Entity(format_version=1, entity_id="adopted", entity_type="occurrence", value=core.InlineValue(value="existing"))
            session.publish(MetadataBatch("adopted", records=(adopted,), retained=(("entity", "adopted"),)))
        def produce(context):
            context.adopt("adopted", label="adopted")
            value = context.session.retain_bytes([b"prepared but not published"])
            context.generate(value, label="generated")
        pending = operations.prepare(definition(), request(inputs=(core.WholeInput(label="source", entity_id="adopted"),)), produce)
        original = ledger.commit
        def interrupted(batch):
            if batch.unit_id.startswith("operations:"):
                raise OSError("publication interrupted")
            return original(batch)
        monkeypatch.setattr(ledger, "commit", interrupted)
        with pytest.raises(OSError, match="publication interrupted"):
            operations.publish((pending,))
        monkeypatch.setattr(ledger, "commit", original)
        policy_keys = [("entity", "adopted")]
        authorize(publisher, policy_keys, orphans=True)
        maintenance = CoreMaintenance(publisher, records)
        with pytest.raises(IntegrityError, match="recoverable operation"):
            maintenance.remove_under_policy("remove-input", "policy", policy_keys)
        generated = next(record.value for record in pending.records if isinstance(record, core.Entity))
        generated_ref = RemovalContent("blobs", BlobRef(generated.locator, generated.digest, generated.byte_size, generated.media_type))
        journal = next(row["description"]["publication"] for row in progress(ledger, pending.execution.execution_id) if "publication" in row["description"])
        journal_ref = RemovalContent("blobs", BlobRef(journal["locator"], journal["digest"], journal["byte_size"], journal["media_type"]))
        for ordinal, content in enumerate([generated_ref, journal_ref]):
            with pytest.raises(IntegrityError, match="required by a retained commitment"):
                maintenance.remove_under_policy("orphan-" + str(ordinal), "policy", orphan_content=[content])
        assert operations.recover(pending.execution.execution_id) == pending.result
        # Publication recovery no longer needs its redundant journal after the
        # successful result is retained; explicit orphan policy may reclaim it.
        assert maintenance.remove_under_policy("finished-journal", "policy", orphan_content=[journal_ref]) == {"deleted": 1}
        assert operations.recover(pending.execution.execution_id) == pending.result


def test_suspended_output_retention_requires_authorizing_the_attempt_too(tmp_path):
    from docspec.application.core_execution import CoreOperations
    from tests.test_core_execution import definition, request, progress
    from tests.test_core_continuation import start
    from docspec.domain.identity import decode_canonical_json_value
    with ExitStack() as stack:
        records, ledger, _, _, publisher = setup(stack, tmp_path)
        operations = CoreOperations(publisher)
        suspended = operations.run(definition(), request(), start)
        with publisher.session() as session:
            checkpoint = session.read_json(progress(ledger, suspended.execution_id)[-1]["description"]["checkpoint"])
        output_key = next(tuple(key) for key in checkpoint["record_keys"] if key[0] == "entity")
        keys = (output_key, ("execution", suspended.execution_id))
        authorize(publisher, keys, orphans=True)
        maintenance = CoreMaintenance(publisher, records)
        with pytest.raises(IntegrityError, match="recoverable operation"):
            maintenance.remove_under_policy("output", "policy", [output_key])
        reference = suspended.checkpoint
        content = RemovalContent("blobs", BlobRef(reference.locator, reference.digest, reference.byte_size, reference.media_type))
        assert maintenance.remove_under_policy("abandon", "policy", keys, orphan_content=[content]) == {"deleted": 1}
        assert records_for(ledger, [output_key])[0].available is False
        assert decode_canonical_json_value(next(ledger.read_progress(suspended.execution_id))[-1], label="progress")["status"] == "incomplete"


def test_staged_validation_is_read_only_and_rejects_conflicting_reclaimed_identity(tmp_path):
    from tests.test_core_states import occurrence
    with ExitStack() as stack:
        records, ledger, states, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, [("key", "e", {"x": 1})])
        authorize(publisher, ROOT_KEYS)
        CoreMaintenance(publisher, records).remove_under_policy("remove", "policy", ROOT_KEYS)
        with publisher.session() as session:
            with pytest.raises(IntegrityError, match="immutable retained record"):
                session.validate(MetadataBatch("bad", records=(occurrence("e", {"x": 2}),), retained=(("entity", "e"),)))
            assert session.validate(MetadataBatch("good", records=(occurrence("e", {"x": 1}),), retained=(("entity", "e"),))) == (("entity", "e"),)
        assert not ledger.is_committed("good")
        assert records_for(ledger, [("entity", "e")])[0].value is None
