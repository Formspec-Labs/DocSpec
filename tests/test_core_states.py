"""General roots retain native values while SQLite owns their logical identities."""

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
from docspec.errors import IntegrityError
from docspec.ports.core_ledger import MetadataBatch
from docspec.ports.record_storage import BATCH_BYTES


def open_core(stack, path):
    records = stack.enter_context(closing(IcebergRecordStorage(path / "records")))
    ledger = stack.enter_context(closing(LocalSqliteCoreLedger(path / "ledger.sqlite", record_storage=records)))
    states = CoreStateStorage(records)
    publisher = CorePublisher(ledger, LocalContentAddressedBlobStore(path / "blobs"), states=states)
    return records, ledger, states, publisher


def occurrence(identity, value):
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
            assert connection.execute("SELECT count(*) FROM records WHERE kind='entity' AND payload IS NULL AND source_layer IS NOT NULL").fetchone() == (4,)
            assert connection.execute("SELECT count(*) FROM provenance_events").fetchone() == (0,)
    with ExitStack() as stack:
        records, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            rows = [row for batch in ledger.read_records([("entity", "d"), ("entity", "absent"), ("entity", "a"), ("entity", "d")]) for row in batch]
            assert [None if row is None else row.value for row in rows] == [entities[3], None, entities[0], entities[3]]
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
        assert next(ledger.read_records([("entity", "entity-04096")]))[0].value.value.value == 4096


def test_entity_publication_coalesces_reads_and_retries_across_chunk_sizes(tmp_path, monkeypatch):
    from docspec.adapters.storage import core_states

    count = 2305
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            states.create_keyed(session, state_id="root", representation_id="r", unit_id="root",
                                rows=((str(i), occurrence(f"e{i:05}", i)) for i in range(count)))
            admitted = states.layers(session, "root")["entities"]
            with closing(admitted.batches()) as batches:
                assert sum(1 for _ in batches) == 10
            with ledger._transaction() as connection:
                original_units = connection.execute("SELECT * FROM units ORDER BY unit_id").fetchall()
                assert connection.execute("SELECT count(*) FROM units WHERE unit_id LIKE ?",
                                          (admitted.reference.layer_id + ":entities:%",)).fetchone() == (2,)
            batches = type(admitted).batches
            def smaller_batches(layer, **kwargs):
                with closing(batches(layer, **kwargs)) as source:
                    for batch in source:
                        for offset in range(0, batch.num_rows, 37):
                            yield batch.slice(offset, 37)
            monkeypatch.setattr(type(admitted), "batches", smaller_batches)
            canonical = core_states.canonical_value_bytes
            def identity_only(value):
                assert isinstance(value, str), "entity publication re-encoded an admitted record"
                return canonical(value)
            monkeypatch.setattr(core_states, "canonical_value_bytes", identity_only)
            states._retain_entities(session, admitted)
            with ledger._transaction() as connection:
                assert connection.execute("SELECT * FROM units ORDER BY unit_id").fetchall() == original_units
            rows = [row for batch in ledger.read_records(("entity", f"e{i:05}") for i in range(count)) for row in batch]
            assert [row.value.value.value for row in rows] == list(range(count))


def test_entity_publication_reserves_metadata_receipt_bytes(tmp_path):
    from docspec.domain.core_admission import encode_record

    # Both payloads fit one read batch, but their publication receipt does not.
    identities = ('escaped"identity', 'other\\identity')
    entities = [occurrence(identity, "x" * (BATCH_BYTES // 2 - len(encode_record(occurrence(identity, ""))) - 8))
                for identity in identities]
    assert sum(len(encode_record(entity)) for entity in entities) == BATCH_BYTES - 16
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            states.create_keyed(session, state_id="root", representation_id="r", unit_id="root",
                                rows=zip(identities, entities, strict=True))
            admitted = states.layers(session, "root")["entities"]
            with closing(admitted.batches()) as batches:
                assert [batch.num_rows for batch in batches] == [2]
            with ledger._transaction() as connection:
                assert connection.execute("SELECT count(*) FROM units WHERE unit_id LIKE ?",
                                          (admitted.reference.layer_id + ":entities:%",)).fetchone() == (2,)
            assert [row.value for batch in ledger.read_records(("entity", identity) for identity in identities) for row in batch] == entities


def test_missing_member_and_conflicting_occurrence_do_not_publish_root(tmp_path):
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            with pytest.raises(IntegrityError, match="missing occurrence"):
                states.create(session, state_id="missing", representation_id="m", unit_id="missing-root", entities=[],
                              members=[core.Membership(member_key="", occurrence_id="absent")])
            assert not ledger.is_committed("missing-root")
            states.create(session, state_id="first", representation_id="first-r", unit_id="first", entities=[occurrence("same-id", 1)],
                          members=[core.Membership(member_key="k", occurrence_id="same-id")])
            with pytest.raises(IntegrityError, match="immutable"):
                states.create(session, state_id="conflict", representation_id="conflict-r", unit_id="conflict", entities=[occurrence("same-id", "1")],
                              members=[core.Membership(member_key="k", occurrence_id="same-id")])
            assert not ledger.is_committed("conflict")
            assert next(ledger.read_records([("entity", "same-id")]))[0].value.value.value == 1


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
            retained = next(ledger.read_records([("entity", "raw")]))[0].value.value
            blob = next(blob for blob in session.ready if blob.digest == retained.digest)
            assert b"".join(session.blobs.read(blob)) == b"opaque\x00\xffbytes"


def test_external_entity_reads_observe_payload_byte_limit(tmp_path):
    from docspec.domain.core_admission import encode_record
    body = "x" * (2 * 1024**2)
    with ExitStack() as stack:
        _, ledger, states, publisher = open_core(stack, tmp_path)
        with publisher.session() as session:
            states.create(session, state_id="root", representation_id="r", unit_id="root",
                          entities=(occurrence(str(i), body) for i in range(5)),
                          members=(core.Membership(member_key=str(i), occurrence_id=str(i)) for i in range(5)))
            batches = list(ledger.read_records(("entity", str(i)) for i in range(5)))
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
