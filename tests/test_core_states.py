"""State roots retain native Iceberg values while SQLite holds their logical membership.

An import either publishes whole or leaves nothing behind; the suite pins sampling in compare, bounded
ready-state descriptors with eviction readmission, keyed and bulk import streaming, immutable occurrence
identities, and membership agreement between inline and bulk representations.
"""

from contextlib import ExitStack, closing, contextmanager
from concurrent.futures import ThreadPoolExecutor

import pytest

from docspec.adapters.storage.blobs import LocalContentAddressedBlobStore
from docspec.adapters.storage.core_states import CoreStateStorage
from docspec.adapters.storage.ledger import LocalSqliteCoreLedger
from docspec.adapters.storage.records import IcebergRecordStorage
from docspec.application.core_publication import CorePublisher
from docspec.domain import core
from docspec.domain.core_admission import admit_record
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.core_ledger import MetadataBatch
from docspec.ports.record_storage import BATCH_BYTES


def open_core(stack, path):
    """Open records, ledger, states and publisher on ``path``, registering the closables on ``stack``."""
    records = stack.enter_context(closing(IcebergRecordStorage(path / "records")))
    ledger = stack.enter_context(closing(LocalSqliteCoreLedger(path / "ledger.sqlite", record_storage=records)))
    states = CoreStateStorage(records)
    publisher = CorePublisher(ledger, LocalContentAddressedBlobStore(path / "blobs"), states=states)
    return records, ledger, states, publisher


def occurrence(identity, value):
    """Build an inline occurrence entity with the given identity and value."""
    return core.Entity(format_version=1, entity_id=identity, entity_type="occurrence", value=core.InlineValue(value=value))


def test_compare_counts_membership_and_reads_only_sampled_occurrences(tmp_path, monkeypatch):
    before = {"": occurrence("null-old", None), "alias-a": occurrence("shared-old", {"x": 1}),
              "alias-b": occurrence("shared-old", {"x": 1}), "gone": occurrence("gone", None),
              "typed": occurrence("bool", True), "unchanged": occurrence("large", "x" * 65536)}
    after = {"": occurrence("null-new", None), "alias-a": occurrence("shared-new", {"x": 1}),
             "alias-b": occurrence("shared-new", {"x": 1}), "new": occurrence("added", None),
             "typed": occurrence("int", 1), "unchanged": before["unchanged"]}
    expected = [("", "null-old", "null-new", "changed", False),
                ("alias-a", "shared-old", "shared-new", "changed", False),
                ("alias-b", "shared-old", "shared-new", "changed", False),
                ("gone", "gone", None, "removed", True),
                ("new", None, "added", "added", True),
                ("typed", "bool", "int", "changed", True)]
    with ExitStack() as stack:
        records, _, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            for name, rows in (("old", before), ("new", after), ("empty", {})):
                states.create(session, state_id=name, representation_id=name + ":physical", unit_id=name,
                              entities={entity.entity_id: entity for entity in rows.values()}.values(),
                              members=[core.Membership(member_key=key, occurrence_id=entity.entity_id)
                                       for key, entity in rows.items()])
        read_ids = set()
        original = records._relation
        @contextmanager
        def sampled(layer, **kwargs):
            if layer.reference.layer_kind == "core-entities":
                identities = kwargs.get("record_ids")
                assert identities is not None, "comparison scanned unsampled occurrence values"
                read_ids.update(identities)
            with original(layer, **kwargs) as relation:
                yield relation
        monkeypatch.setattr(records, "_relation", sampled)
        for limit in (0, 2, 20):
            read_ids.clear()
            with publisher.session() as session:
                result = states.compare(session, "old", "new", sample_limit=limit)
            assert result["counts"] == {"added": 1, "removed": 1, "changed": 4}
            assert [tuple(row.values()) for row in result["sample"]] == expected[:limit]
            assert read_ids <= {identity for row in expected[:limit] for identity in row[1:3] if identity is not None}
        with publisher.session() as session:
            read_ids.clear()
            for state in ("old", "empty"):
                result = states.compare(session, state, state)
                assert result["counts"] == {"added": 0, "removed": 0, "changed": 0}
                assert result["sample"] == []
            assert not read_ids
        def compare(limit):
            with publisher.session() as session:
                return states.compare(session, "old", "new", sample_limit=limit)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compare, (2, 20)))
        assert [len(result["sample"]) for result in results] == [2, 6]
        assert all(result["counts"] == {"added": 1, "removed": 1, "changed": 4} for result in results)


def test_ready_state_descriptors_are_bounded_and_eviction_readmits(tmp_path, monkeypatch):
    with ExitStack() as stack:
        records, _, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            for index in range(6):
                states.create_keyed(session, state_id=f"s{index}", representation_id=f"r{index}", unit_id=f"u{index}",
                                    rows=[("key", occurrence(f"e{index}", index))])
            assert len(session.ready_states) == 4
            with monkeypatch.context() as guarded:
                def unexpected(*args, **kwargs):
                    raise AssertionError("protected ready state repeated descriptor admission")
                guarded.setattr(records, "_verified_root", unexpected)
                with states.relation(session, "s5") as rows:
                    assert rows.project("member_key, occurrence_id").fetchall() == [("key", "e5")]
            original = records.available
            readmitted = []
            def available(reference):
                readmitted.append(reference)
                return original(reference)
            monkeypatch.setattr(records, "available", available)
            with states.relation(session, "s0") as rows:
                assert rows.project("occurrence_id").fetchall() == [("e0",)]
            assert len(readmitted) == 2 and len(session.ready_states) == 4
            reference = states.layers(session, "s0")["entities"].reference
        # A new session must not reuse an earlier session's file admission.
        member = next(records.physical_references(reference))
        (records.root / member.locator).unlink()
        with publisher.session() as session, pytest.raises(IntegrityError, match="unavailable"):
            states.layers(session, "s0")


@pytest.mark.parametrize("count", [0, 4097])
def test_keyed_import_consumes_one_shot_input_and_recovers_all_members(tmp_path, count):
    closed = []
    def rows():
        try:
            for index in range(count):
                yield f"key-{index:05}", occurrence(f"entity-{index:05}", {"index": index})
        finally:
            closed.append(True)
    with ExitStack() as stack:
        _, _, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            states.create_keyed(session, state_id="keyed", representation_id="keyed-r", unit_id="keyed-import", rows=rows())
            with states.relation(session, "keyed") as relation:
                assert relation.aggregate("count(*)").fetchone() == (count,)
        assert closed == [True]


def test_keyed_import_failure_closes_source_without_publishing(tmp_path):
    closed = []
    def rows():
        try:
            yield "key", occurrence("first", None)
            raise RuntimeError("keyed producer failed")
        finally:
            closed.append(True)
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session, pytest.raises(RuntimeError, match="keyed producer failed"):
            states.create_keyed(session, state_id="keyed", representation_id="keyed-r", unit_id="keyed-import", rows=rows())
        assert closed == [True]
        assert not ledger.is_committed("keyed-import")


def test_bulk_root_recovers_scalar_and_structured_values_without_invented_provenance(tmp_path, monkeypatch):
    entities = [occurrence("a", None), occurrence("b", {"position": 3, "text": "same"}),
                occurrence("c", {"position": 3, "text": "same"}), occurrence("d", [True, 1, "1"])]
    members = [core.Membership(member_key=key, occurrence_id=identity)
               for key, identity in [("", "a"), (" ", "b"), ("k", "b"), ("z", "c"), ("é", "d")]]
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            states.create(session, state_id="root", representation_id="root-physical", unit_id="import-root", entities=entities, members=members)
        with ledger._transaction() as connection:
            # Members are registered by the state's manifest, not one ledger row each.
            assert connection.execute("SELECT count(*) FROM records WHERE kind='entity'").fetchone() == (0,)
            assert connection.execute("SELECT count(*) FROM provenance_events").fetchone() == (0,)
    with ExitStack() as stack:
        records, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            keys = [("entity", "d"), ("entity", "absent"), ("entity", "a"), ("entity", "d")]
            rows = [row for batch in session.read_records(keys) for row in batch]
            assert [None if row is None else row.value for row in rows] == [entities[3], None, entities[0], entities[3]]
            assert all(row is None for batch in ledger.read_records(keys) for row in batch)
            def unexpected(*args, **kwargs):
                raise AssertionError("retained bulk state reread all payloads for admission")
            monkeypatch.setattr(records, "admit", unexpected)
            monkeypatch.setattr(records, "_rows", unexpected)
            with states.relation(session, "root") as relation:
                assert relation.aggregate("count(*)").fetchone() == (5,)
                assert "ORDER_BY" not in relation.explain()
                actual = relation.order("member_key").fetchall()
            assert [(key, identity, admit_record(payload)) for key, identity, payload in actual] == [
                (member.member_key, member.occurrence_id, next(e for e in entities if e.entity_id == member.occurrence_id)) for member in members
            ]
            assert list(states.rows(session, "root")) == [(key, admit_record(payload)) for key, _, payload in actual]


def test_root_larger_than_metadata_unit_and_empty_root_use_same_path(tmp_path):
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        for count in (0, 4097):
            with publisher.session() as session:
                state_id = f"root-{count}"
                states.create(session, state_id=state_id, representation_id=f"physical-{count}", unit_id=f"import-{count}",
                              entities=(occurrence(f"entity-{i:05}", i) for i in range(count)),
                              members=(core.Membership(member_key=f"key-{i:05}", occurrence_id=f"entity-{i:05}") for i in range(count)))
                with states.relation(session, state_id) as relation:
                    assert relation.aggregate("count(*)").fetchone() == (count,)
        with publisher.session() as session:
            assert next(session.read_records([("entity", "entity-04096")]))[0].value.value.value == 4096


def ledger_rows(ledger):
    """Count ledger records and retention rows by kind."""
    with ledger._transaction() as connection:
        return (dict(connection.execute("SELECT kind,count(*) FROM records GROUP BY kind").fetchall()),
                dict(connection.execute("SELECT kind,count(*) FROM retention GROUP BY kind").fetchall()))


def test_bulk_state_registers_members_per_layer_not_per_row(tmp_path):
    """A 1,000-member state adds its state and representation rows only; members resolve through its layer.

    Before layer registration each member cost one records row and one
    retention row, about 700 bytes and 0.25 ms of ledger work.
    """
    count = 1000
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            states.create_keyed(session, state_id="root", representation_id="r", unit_id="root",
                                rows=((f"k{i:04}", occurrence(f"e{i:04}", {"n": i})) for i in range(count)))
        expected = {"state": 1, "state_representation": 1}
        assert ledger_rows(ledger) == (expected, expected)
        with ledger._transaction() as connection:
            assert connection.execute("SELECT unit_id FROM units").fetchall() == [("root",)]
        with publisher.session() as session:
            wanted = [("entity", "e0500"), ("entity", "e0999"), ("entity", "absent")]
            rows = [row for batch in session.read_records(wanted) for row in batch]
        assert [row and (row.value.value.value, row.retained, row.available) for row in rows] == [
            ({"n": 500}, True, True), ({"n": 999}, True, True), None]
        assert ledger_rows(ledger) == (expected, expected)


def test_member_reads_by_identity_stay_within_the_batch_byte_limit(tmp_path):
    from docspec.domain.core_admission import encode_record

    # Both payloads fit one read batch; escaped identities reach the native lookup intact.
    identities = ('escaped"identity', 'other\\identity')
    entities = [occurrence(identity, "x" * (BATCH_BYTES // 2 - len(encode_record(occurrence(identity, ""))) - 8))
                for identity in identities]
    assert sum(len(encode_record(entity)) for entity in entities) == BATCH_BYTES - 16
    with ExitStack() as stack:
        _, _, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            states.create_keyed(session, state_id="root", representation_id="r", unit_id="root",
                                rows=zip(identities, entities, strict=True))
        with publisher.session() as session:
            batches = list(session.read_records(("entity", identity) for identity in identities))
        assert [len(batch) for batch in batches] == [2]
        assert [row.value for row in batches[0]] == entities


def test_missing_member_and_conflicting_occurrence_do_not_publish_root(tmp_path):
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            with pytest.raises(IntegrityError, match="missing occurrence"):
                states.create(session, state_id="missing", representation_id="m", unit_id="missing-root", entities=[],
                              members=[core.Membership(member_key="", occurrence_id="absent")])
            assert not ledger.is_committed("missing-root")
            members = [core.Membership(member_key="k", occurrence_id="same-id")]
            states.create(session, state_id="first", representation_id="first-r", unit_id="first", entities=[occurrence("same-id", 1)],
                          members=members)
            # A retry cannot change the bytes of the occurrences it already retained.
            with pytest.raises(IntegrityError, match="immutable"):
                states.create(session, state_id="first", representation_id="first-r", unit_id="first-retry",
                              entities=[occurrence("same-id", "1")], members=members)
            assert not ledger.is_committed("first-retry")
            # Nor can an explicit record reassign a retained member's identity.
            with pytest.raises(IntegrityError, match="immutable"):
                session.publish(MetadataBatch("conflict-record", records=(occurrence("same-id", "1"),), retained=(("entity", "same-id"),)))
            assert not ledger.is_committed("conflict-record")
            assert next(session.read_records([("entity", "same-id")]))[0].value.value.value == 1
            # Another state is not compared at write: DocSpec scopes the IDs it
            # mints. A caller that reuses one with other bytes makes every
            # lookup by identity refuse.
            states.create(session, state_id="conflict", representation_id="conflict-r", unit_id="conflict",
                          entities=[occurrence("same-id", "1")], members=members)
        with publisher.session() as session:
            with pytest.raises(IntegrityError, match="different values"):
                next(session.read_records([("entity", "same-id")]))
            with pytest.raises(IntegrityError, match="different values"):
                session.publish(MetadataBatch("pin", retained=(("entity", "same-id"),)))
        assert not ledger.is_committed("pin")


def test_root_producer_failure_closes_stream_and_leaves_no_published_state(tmp_path):
    closed = []
    def entities():
        try:
            yield occurrence("a", 1)
            raise RuntimeError("producer stopped")
        finally:
            closed.append(True)
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session, pytest.raises(RuntimeError, match="producer stopped"):
            states.create(session, state_id="root", representation_id="r", unit_id="broken", entities=entities(), members=[])
        assert closed == [True]
        assert not ledger.is_committed("broken")


def test_source_key_survives_changed_occurrence_and_opaque_values_stay_exact(tmp_path):
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            content = session.retain_bytes([b"opaque\x00\xffbytes"])
            raw = core.Entity(format_version=1, entity_id="raw", entity_type="occurrence", value=content)
            for state_id, entity in [("old", occurrence("old-value", {"title": "old", "position": 7})),
                                     ("new", occurrence("new-value", {"title": "new", "position": 7})), ("binary", raw)]:
                states.create(session, state_id=state_id, representation_id=state_id + "-r", unit_id=state_id,
                              entities=[entity], members=[core.Membership(member_key="stable-source-key", occurrence_id=entity.entity_id)])
            for state_id, expected in [("old", "old-value"), ("new", "new-value"), ("binary", "raw")]:
                with states.relation(session, state_id) as relation:
                    key, identity, _ = relation.fetchone()
                    assert (key, identity) == ("stable-source-key", expected)
            retained = next(session.read_records([("entity", "raw")]))[0].value.value
            blob = next(blob for blob in session.ready if blob.digest == retained.digest)
            assert b"".join(session.blobs.read(blob)) == b"opaque\x00\xffbytes"


def test_external_entity_reads_observe_payload_byte_limit(tmp_path):
    """By-identity reads bound their batches whether a member is read through its layer or its pin."""
    from docspec.domain.core_admission import encode_record
    body = "x" * (2 * 1024**2)
    keys = [("entity", str(i)) for i in range(5)]
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            states.create(session, state_id="root", representation_id="r", unit_id="root",
                          entities=(occurrence(str(i), body) for i in range(5)),
                          members=(core.Membership(member_key=str(i), occurrence_id=str(i)) for i in range(5)))
            unpinned = list(session.read_records(keys))
            session.publish(MetadataBatch("pin", retained=tuple(keys[:2])))
            session.publish(MetadataBatch("pin-rest", retained=tuple(keys[2:])))
        for batches in (unpinned, list(ledger.read_records(keys))):
            assert sum(len(batch) for batch in batches) == 5
            assert len(batches) > 1
            assert all(sum(len(encode_record(row.value)) for row in batch) <= BATCH_BYTES for batch in batches)


def test_inline_and_bulk_representations_share_one_logical_membership_rule(tmp_path):
    with ExitStack() as stack:
        _, _, states, publisher = open_core(stack, tmp_path)
        members = (core.Membership(member_key="", occurrence_id="e"),)
        with publisher.session() as session:
            states.create(session, state_id="root", representation_id="bulk", unit_id="root", entities=[occurrence("e", 1)], members=members)
            inline = core.StateRepresentation(format_version=1, state_id="root", representation_id="inline", membership=members)
            assert session.publish(MetadataBatch("inline", records=(inline,), retained=(("state", "root"),)))
            changed = core.StateRepresentation(format_version=1, state_id="root", representation_id="changed", membership=())
            with pytest.raises(IntegrityError, match="disagree"):
                session.publish(MetadataBatch("changed", records=(changed,), retained=(("state", "root"),)))


def test_bulk_admission_replaces_inline_storage_without_changing_entity(tmp_path):
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        entity = occurrence("e", {"keep": "the same logical value"})
        with publisher.session() as session:
            session.publish(MetadataBatch("inline-entity", records=(entity,), retained=(("entity", "e"),)))
            states.create(session, state_id="root", representation_id="r", unit_id="root", entities=[entity],
                          members=[core.Membership(member_key="k", occurrence_id="e")])
            with ledger._transaction() as connection:
                assert connection.execute("SELECT payload IS NULL,source_layer IS NOT NULL FROM records WHERE kind='entity' AND record_id='e'").fetchone() == (1, 1)
            assert next(ledger.read_records([("entity", "e")]))[0].value == entity


def test_import_order_is_immaterial_and_duplicate_keys_refuse(tmp_path):
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        entities = [occurrence("z", 2), occurrence("a", 1)]
        members = [core.Membership(member_key="z", occurrence_id="z"), core.Membership(member_key="", occurrence_id="a")]
        with publisher.session() as session:
            for suffix, values, membership in [("first", entities, members), ("second", entities[::-1], members[::-1])]:
                states.create(session, state_id="root", representation_id=suffix, unit_id=suffix, entities=values, members=membership)
            with states.relation(session, "root") as relation:
                assert relation.project("member_key,occurrence_id").order("member_key").fetchall() == [("", "a"), ("z", "z")]
            with pytest.raises(IntegrityError, match="duplicate"):
                states.create(session, state_id="invalid", representation_id="invalid", unit_id="invalid", entities=entities,
                              members=[members[0], members[1], members[0]])
            assert not ledger.is_committed("invalid")


def member_rows(ledger, identity):
    """Return one entity's ledger pin and retention rows."""
    with ledger._transaction() as connection:
        return (connection.execute("SELECT payload IS NULL,source_layer,byte_size FROM records "
                                   "WHERE kind='entity' AND record_id=?", (identity,)).fetchall(),
                connection.execute("SELECT unit_id,available FROM retention WHERE kind='entity' AND record_id=?",
                                   (identity,)).fetchall())


def test_whole_input_reference_pins_a_member_once_at_its_layer(tmp_path):
    """The first reference by identity pins a member with one row and one retention row; later ones add none."""
    from docspec.application.core_edits import prepare_value_edit
    from docspec.application.core_execution import CoreOperations
    from docspec.domain.core_admission import encode_record
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        operations = CoreOperations(publisher)
        with publisher.session() as session:
            states.create_keyed(session, state_id="root", representation_id="r", unit_id="root",
                                rows=[("a", occurrence("e0", {"n": 0})), ("b", occurrence("e1", {"n": 1}))])
            layer = states.layers(session, "root")["entities"].reference
        assert member_rows(ledger, "e0") == ([], [])
        pinned = None
        for replacement in ({"n": 10}, {"n": 20}):
            with publisher.session() as session:
                value, _ = prepare_value_edit(operations, "e0", [{"op": "replace", "path": "", "value": replacement}], session=session)
                operations.publish((value,), session=session)
            records, retention = member_rows(ledger, "e0")
            assert records == [(1, layer.layer_id, len(encode_record(occurrence("e0", {"n": 0}))))]
            assert len(retention) == 1 and retention[0][1] == 1
            pinned = pinned or retention
            assert retention == pinned
        assert member_rows(ledger, "e1") == ([], [])
        assert next(ledger.read_records([("entity", "e0")]))[0].value == occurrence("e0", {"n": 0})


def test_retried_unit_that_pinned_members_keeps_its_identity(tmp_path):
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            states.create_keyed(session, state_id="root", representation_id="r", unit_id="root",
                                rows=[("a", occurrence("e0", 0)), ("b", occurrence("e1", 1))])
        batch = MetadataBatch("reference", retained=(("entity", "e0"), ("entity", "e1")))
        with publisher.session() as session:
            assert session.publish(batch) is True
        pinned = [member_rows(ledger, identity) for identity in ("e0", "e1")]
        assert all(len(records) == 1 and retention == [("reference", 1)] for records, retention in pinned)
        # The retry finds its members pinned; pins stay out of the unit's receipt.
        for _ in range(2):
            with publisher.session() as session:
                assert session.publish(batch) is False
        with publisher.session() as session:
            assert session.publish(MetadataBatch("again", retained=(("entity", "e0"),))) is True
        assert [member_rows(ledger, identity) for identity in ("e0", "e1")] == pinned


def test_revision_put_of_a_member_copies_it_without_a_pin(tmp_path):
    """A revision's puts resolve by identity and join its layers; only the result state is published."""
    from docspec.application.core_edits import prepare_revision
    from docspec.application.core_execution import CoreOperations
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        operations = CoreOperations(publisher)
        with publisher.session() as session:
            states.create_keyed(session, state_id="root", representation_id="r", unit_id="root", rows=[("a", occurrence("e0", 0))])
            states.create_keyed(session, state_id="other", representation_id="o", unit_id="other", rows=[("x", occurrence("e2", 2))])
            change = core.Revision(format_version=1, revision_id="revision", base_state_id="root", result_state_id="changed",
                                   edits=(core.Put(sequence=0, member_key="c", occurrence_id="e2"),))
            operations.publish((prepare_revision(operations, change, session=session),), session=session)
            assert [(key, entity.entity_id, entity.value.value) for key, entity in states.rows(session, "changed")] == [
                ("a", "e0", 0), ("c", "e2", 2)]
        with ledger._transaction() as connection:
            assert connection.execute("SELECT count(*) FROM records WHERE kind='entity'").fetchone() == (0,)


def test_existing_member_rows_take_precedence_over_layer_search(tmp_path, monkeypatch):
    """A workspace written before layer registration keeps its row per member; reads and references use it as before."""
    from docspec.domain.core_admission import AdmittedRecord
    entities = [occurrence("e0", 0), occurrence("e1", 1)]
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            states.create_keyed(session, state_id="root", representation_id="r", unit_id="root",
                                rows=zip(("a", "b"), entities, strict=True))
            layer = states.layers(session, "root")["entities"].reference
        # The rows DocSpec 0.9.1 published for every member of a new state.
        ledger.commit(MetadataBatch(layer.layer_id + ":entities:0", records=tuple(AdmittedRecord(entity) for entity in entities),
                                    retained=(("entity", "e0"), ("entity", "e1")), record_layer=layer))
        before = [member_rows(ledger, identity) for identity in ("e0", "e1")]
        def unexpected(*args, **kwargs):
            raise AssertionError("a member with a ledger row was searched for in the layers")
        monkeypatch.setattr(states, "find_members", unexpected)
        with publisher.session() as session:
            assert [row.value for batch in session.read_records([("entity", "e0"), ("entity", "e1")]) for row in batch] == entities
            assert session.publish(MetadataBatch("reference", retained=(("entity", "e0"),))) is True
        assert [member_rows(ledger, identity) for identity in ("e0", "e1")] == before


def searches(monkeypatch, states):
    """Record the identities each all-layer member search looks for."""
    calls, find = [], states.find_members
    def recorded(session, identities, *, layers=None):
        identities = set(identities)
        if layers is None:
            calls.append(identities)
        return find(session, identities, layers=layers)
    monkeypatch.setattr(states, "find_members", recorded)
    return calls


def test_caller_identities_cannot_collide_with_bulk_members(tmp_path):
    """A caller-chosen entity or state identity is checked against the bulk layers; a new member against the ledger."""
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        members = [core.Membership(member_key="k", occurrence_id="member")]
        with publisher.session() as session:
            states.create(session, state_id="root", representation_id="root-r", unit_id="root",
                          entities=[occurrence("member", 1)], members=members)
            artifact = core.Entity(format_version=1, entity_id="member", entity_type="artifact", value=core.InlineValue(value=1))
            with pytest.raises(IntegrityError, match="immutable"):
                session.publish(MetadataBatch("artifact", records=(artifact,), retained=(("entity", "member"),)))
            with pytest.raises(IntegrityError, match="ambiguous"):
                states.create(session, state_id="member", representation_id="named-r", unit_id="named",
                              entities=[occurrence("other", 2)], members=[core.Membership(member_key="k", occurrence_id="other")])
            with pytest.raises(IntegrityError, match="ambiguous"):
                states.create(session, state_id="reuse", representation_id="reuse-r", unit_id="reuse",
                              entities=[occurrence("root", 3)], members=[core.Membership(member_key="k", occurrence_id="root")])
        assert not any(ledger.is_committed(unit) for unit in ("artifact", "named", "reuse"))


def test_a_new_state_cannot_reuse_a_ledger_identity_with_other_bytes(tmp_path):
    """Rows take precedence over layers when read by identity, so a differing bulk copy is refused when written."""
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            states.create(session, state_id="root", representation_id="root-r", unit_id="root",
                          entities=[occurrence("pinned", 1)], members=[core.Membership(member_key="k", occurrence_id="pinned")])
            session.publish(MetadataBatch("pin", retained=(("entity", "pinned"),)))
            session.publish(MetadataBatch("explicit", records=(occurrence("explicit", 1),), retained=(("entity", "explicit"),)))
            for identity in ("pinned", "explicit"):
                with pytest.raises(IntegrityError, match="immutable"):
                    states.create(session, state_id="copy-" + identity, representation_id="copy-" + identity, unit_id="copy-" + identity,
                                  entities=[occurrence(identity, 2)], members=[core.Membership(member_key="k", occurrence_id=identity)])
            # Identical copies are the same occurrence.
            states.create(session, state_id="same", representation_id="same-r", unit_id="same",
                          entities=[occurrence("pinned", 1), occurrence("explicit", 1)],
                          members=[core.Membership(member_key="a", occurrence_id="pinned"), core.Membership(member_key="b", occurrence_id="explicit")])
        assert not any(ledger.is_committed("copy-" + identity) for identity in ("pinned", "explicit"))


def test_ledger_refuses_member_pins_that_contradict_its_rows(tmp_path):
    """The ledger checks a pin's digest and data identity itself, whatever the caller resolved."""
    from docspec.domain.core_admission import AdmittedRecord
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            states.create(session, state_id="root", representation_id="root-r", unit_id="root",
                          entities=[occurrence("a", 1), occurrence("b", 1)],
                          members=[core.Membership(member_key="a", occurrence_id="a"), core.Membership(member_key="b", occurrence_id="b")])
            layer = states.layers(session, "root")["entities"].reference
        ledger.commit(MetadataBatch("pin-a", members=((AdmittedRecord(occurrence("a", 1)), layer),)))
        with pytest.raises(IntegrityError, match="immutable"):
            ledger.commit(MetadataBatch("changed-a", members=((AdmittedRecord(occurrence("a", 2)), layer),)))
        with pytest.raises(IntegrityError, match="ambiguous"):
            ledger.commit(MetadataBatch("state-named", members=((AdmittedRecord(occurrence("root", 1)), layer),)))
        assert not ledger.is_committed("changed-a") and not ledger.is_committed("state-named")


def test_a_session_sees_a_differing_copy_published_after_it_read_a_member(tmp_path):
    """Publishing a bulk representation clears the session's member cache, so a later read compares every copy."""
    with ExitStack() as stack:
        _, _, states, publisher = open_core(stack, tmp_path)
        members = [core.Membership(member_key="k", occurrence_id="shared")]
        with publisher.session() as session:
            states.create(session, state_id="first", representation_id="first-r", unit_id="first",
                          entities=[occurrence("shared", 1)], members=members)
            assert next(session.read_records([("entity", "shared")]))[0].value.value.value == 1
            states.create(session, state_id="second", representation_id="second-r", unit_id="second",
                          entities=[occurrence("shared", 2)], members=members)
            with pytest.raises(IntegrityError, match="different values"):
                next(session.read_records([("entity", "shared")]))


def test_referenced_members_count_against_the_closure_before_their_bytes_are_read(tmp_path, monkeypatch):
    body = "x" * (3 * 1024**2)
    keys = tuple(("entity", str(index)) for index in range(3))
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            states.create(session, state_id="root", representation_id="root-r", unit_id="root",
                          entities=(occurrence(str(index), body) for index in range(3)),
                          members=(core.Membership(member_key=str(index), occurrence_id=str(index)) for index in range(3)))
        def unexpected(*args, **kwargs):
            raise AssertionError("member bytes were read before the closure size was checked")
        monkeypatch.setattr(states, "member_payloads", unexpected)
        with publisher.session() as session, pytest.raises(LimitExceededError, match="closure"):
            session.publish(MetadataBatch("too-large", retained=keys))
        assert not ledger.is_committed("too-large")


def test_member_searches_are_admitted_once_skip_named_states_and_remember_misses(tmp_path, monkeypatch):
    with ExitStack() as stack:
        records, _, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            for index in range(12):
                states.create_keyed(session, state_id=f"s{index}", representation_id=f"r{index}", unit_id=f"u{index}",
                                    rows=[("key", occurrence(f"e{index}", index))])
        calls = searches(monkeypatch, states)
        admissions, available = [], records.available
        monkeypatch.setattr(records, "available", lambda reference: admissions.append(reference) or available(reference))
        with publisher.session() as session:
            for index in (0, 5, 11):
                assert next(session.read_records([("entity", f"e{index}")]))[0].value.value.value == index
            first = len(admissions)
            assert next(session.read_records([("entity", "e3")]))[0].value.value.value == 3
            # More layers than the thread's eight cached admissions, each admitted once.
            assert first <= 12 and len(admissions) == first
            for _ in range(2):
                assert next(session.read_records([("entity", "absent")]))[0] is None
            # A call naming both forms of an identity resolves it itself.
            assert [row is not None for batch in session.read_records([("entity", "s1"), ("state", "s1")]) for row in batch] == [False, True]
        assert calls == [{"e0"}, {"e5"}, {"e11"}, {"e3"}, {"absent"}]


def test_minted_outputs_never_search_the_layers(tmp_path, monkeypatch):
    """Derive, upsert and generated outputs carry identities DocSpec mints, so they stay flat as layers accumulate."""
    from docspec.domain.identity import stable_urn
    from docspec.runtime import CoreWorkspace
    definition = core.OperationDefinition(format_version=1, definition_id=stable_urn("core-derive-definition", ["minted", {}]),
                                          implementation_id="test.minted", implementation_version="1",
                                          operation_kind="transformation", configuration={})
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        for index in range(3):
            workspace.create(f"other{index}", [("k", index)])
        calls = searches(monkeypatch, workspace.states)
        base = workspace.derive([("a", 1), ("b", 2)], batch_id="seed", definition=definition, inputs=())
        derived = workspace.derive([("a", 3)], batch_id="next", definition=definition, inputs=(), base_state_id=base.state_id)
        workspace.upsert(derived.state_id, [("c", 4)], batch_id="upsert")
        assert calls == []
