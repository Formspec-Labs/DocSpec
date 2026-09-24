"""Bounded operation payload reuse leaves availability, evidence and recovery live."""

from contextlib import ExitStack, closing
from dataclasses import replace

import pytest

from docspec.application.core_execution import CoreOperations, ResolveCall
from docspec.domain import core
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.core_ledger import MetadataBatch
from docspec.application.core_reuse import CoreReuse, ReuseRequest
from docspec.domain.core_admission import encode_record
from tests.test_core_dependencies import definition, request, retain
from tests.test_core_selections import import_root, setup
from tests.test_core_states import occurrence


def test_window_bulk_reads_detach_values_and_refresh_metadata(tmp_path, monkeypatch):
    with ExitStack() as stack:
        records, ledger, states, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            # Explicit records: the window reads ledger rows and their external bytes.
            keys = [("entity", f"e{i}") for i in range(4)]
            session.publish(MetadataBatch("entities", records=tuple(occurrence(f"e{i}", {"url": str(i)}) for i in range(4)),
                                          retained=tuple(keys)))
            lookups = []
            original = records.lookup_batches
            def lookup(*args, **kwargs):
                lookups.append(args[1])
                yield from original(*args, **kwargs)
            monkeypatch.setattr(records, "lookup_batches", lookup)
            with session.record_window(keys):
                assert len(lookups) == 1
                for key in keys:
                    row = next(session.read_records([key]))[0]
                    row.value.value.value["url"] = "callback mutation"
                    assert next(session.read_records([key]))[0].value.value.value["url"] != "callback mutation"
                assert len(lookups) == 1
                # Simulate metadata changes from a concurrent owner transaction;
                # normal cleanup itself is excluded by this session's guard.
                with ledger._transaction(write=True) as connection:
                    connection.execute("UPDATE retention SET evidence_version=3 WHERE record_id='e0'")
                    connection.execute("UPDATE retention SET available=0,evidence_version=4 WHERE record_id='e1'")
                assert next(session.read_records([keys[0]]))[0].evidence_version == 3
                unavailable = next(session.read_records([keys[1]]))[0]
                assert not unavailable.available and unavailable.evidence_version == 4 and unavailable.value is None
            assert session._record_window is None
            next(session.read_records([keys[0]]))
            assert len(lookups) == 2


def test_window_budget_falls_back_without_changing_valid_reads(tmp_path, monkeypatch):
    import docspec.application.core_publication as publication
    with ExitStack() as stack:
        records, _, states, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, [("key", "e", {"url": "u"})])
            lookups = []
            original = states.member_payloads
            def lookup(*args, **kwargs):
                lookups.append(True)
                yield from original(*args, **kwargs)
            monkeypatch.setattr(states, "member_payloads", lookup)
            monkeypatch.setattr(publication, "BATCH_BYTES", 1)
            with pytest.raises(RuntimeError, match="producer"):
                with session.record_window([("entity", "e")]):
                    assert session._record_window.byte_size == 0
                    assert next(session.read_records([("entity", "e")]))[0].value.value.value == {"url": "u"}
                    raise RuntimeError("producer")
            assert len(lookups) == 2 and session._record_window is None


def test_resolve_batch_failure_preserves_completed_member_and_exact_retry(tmp_path):
    with ExitStack() as stack:
        _, _, states, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            import_root(states, session, [("old", "old", {"url": "u"})])
        operations = CoreOperations(publisher)
        reached = []
        def calls(fail):
            for index in range(3):
                def producer(context, index=index):
                    reached.append(index)
                    if fail and index == 1:
                        raise RuntimeError("failed sibling")
                    context.generate(core.InlineValue(value=context.read_value("old")), label="data")
                yield ResolveCall(definition(), request(str(index)), producer,
                                  f"selection-{index}", core.Origin(parent_entity_id="old"), lambda result: False)
        with pytest.raises(RuntimeError, match="failed sibling"), closing(operations.resolve_many(calls(True))) as results:
            list(results)
        with closing(operations.resolve_many(calls(False))) as results:
            selected = list(results)
        assert reached == [0, 1, 1, 2] and len(selected) == 3
        with closing(operations.resolve_many(calls(False))) as results:
            assert list(results) == selected
        changed = ResolveCall(definition(), request("different"), lambda context: pytest.fail("changed retry ran"),
                              "selection-0", core.Origin(parent_entity_id="old"), lambda result: True)
        with pytest.raises(IntegrityError, match="another request"), closing(operations.resolve_many([changed])) as results:
            list(results)


def test_exact_reuse_binds_aggregate_metadata_budget(tmp_path, monkeypatch):
    import docspec.application.core_reuse as reuse
    with ExitStack() as stack:
        _, ledger, _, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            retain(session, "old", {"url": "u"})
        operation = CoreOperations(publisher)
        calls = [ResolveCall(definition(), request(str(i)), lambda context: None,
                             f"s{i}", core.Origin(parent_entity_id="old"), lambda result: False) for i in range(3)]
        with closing(operation.resolve_many(calls)) as resolutions:
            selected = list(resolutions)
        keys = [("selection", item.selection.selection_id) for item in selected]
        keys += [("result", item.result.result_id) for item in selected]
        keys += [("request", call.request.request_id) for call in calls]
        keys += [("operation_definition", "definition")]
        sizes = [len(encode_record(row.value)) for batch in ledger.read_records(keys) for row in batch if row is not None]
        monkeypatch.setattr(reuse, "BATCH_BYTES", max(sizes) + 1)
        wanted = [ReuseRequest(call.definition, call.request, call.selection_id, call.target, call.reuse_policy) for call in calls]
        with publisher.session() as session, pytest.raises(LimitExceededError, match="exact reuse group"):
            CoreReuse().existing_many(session, wanted)


@pytest.mark.parametrize("missing", [False, True])
def test_exact_reuse_refuses_result_lost_after_closure_check(tmp_path, monkeypatch, missing):
    with ExitStack() as stack:
        _, _, _, _, publisher = setup(stack, tmp_path)
        with publisher.session() as session:
            retain(session, "old", {"url": "u"})
        wanted = ReuseRequest(definition(), request("r"), "s", core.Origin(parent_entity_id="old"), lambda result: True)
        choice = CoreOperations(publisher).resolve(wanted.definition, wanted.request, lambda context: None,
            selection_id=wanted.selection_id, target=wanted.target, reuse_policy=wanted.policy)
        with publisher.session() as session:
            validated = False
            validate, read = session.validate, session.read_records
            def checked(batch):
                nonlocal validated
                value = validate(batch)
                validated = True
                return value
            def changed(keys, **kwargs):
                for batch in read(keys, **kwargs):
                    yield tuple(None if missing else replace(row, available=False, value=None)
                                if row is not None and row.key == ("result", choice.result.result_id) else row
                                for row in batch) if validated else batch
            monkeypatch.setattr(session, "validate", checked)
            monkeypatch.setattr(session, "read_records", changed)
            with pytest.raises(IntegrityError, match="exact selection result is unavailable"):
                CoreReuse().existing_many(session, [wanted])
