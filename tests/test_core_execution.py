"""Attempts, outcomes and recovery through the actual shared publication path."""

from contextlib import ExitStack, closing
from functools import partial
from queue import Queue
from threading import Barrier

import pytest

from docspec.adapters.storage.blobs import LocalContentAddressedBlobStore
from docspec.adapters.execution import bounded_map
from docspec.adapters.storage.ledger import LocalSqliteCoreLedger
from docspec.application.core_execution import CoreOperations
from docspec.application.core_publication import CorePublisher
from docspec.domain import core
from docspec.domain.core_admission import record_parts, record_value
from docspec.domain.identity import decode_canonical_json_value
from docspec.errors import IntegrityError, LimitExceededError, StateTransitionError


def setup(stack, path):
    """Open a SQLite ledger (closed with the stack) and build CoreOperations over a local blob store."""
    ledger = stack.enter_context(closing(LocalSqliteCoreLedger(path / "ledger.sqlite")))
    return ledger, CoreOperations(CorePublisher(ledger, LocalContentAddressedBlobStore(path / "blobs")))


def definition(capture=False):
    """Build a transformation definition, or a capture definition when `capture` is true."""
    return core.OperationDefinition(format_version=1, definition_id="definition", implementation_id="test-operation", implementation_version="1",
                                    operation_kind="capture" if capture else "transformation", configuration={})


def request(identity="request", inputs=()):
    """Build a request for `definition` with the given identity and whole inputs."""
    return core.Request(format_version=1, request_id=identity, definition_id="definition", inputs=inputs, dependencies=())


def progress(ledger, identity):
    """Decode every recorded progress row for one execution, in ledger order."""
    return [decode_canonical_json_value(payload, label="progress") for batch in ledger.read_progress(identity) for payload in batch]


def test_identical_requests_get_fresh_attempts_and_explicit_empty_null_success(tmp_path):
    """Identical requests still get fresh execution and result ids, and `empty` and `null` outcomes stay distinct."""
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        first = operations.run(definition(), request(), lambda context: None)
        second = operations.run(definition(), request(), lambda context: core.Outcome(status="success", value="null"))
        assert first.execution_id != second.execution_id
        assert first.result_id != second.result_id
        assert first.outcome.value == "empty"
        assert second.outcome.value == "null"
        assert all(row.available for batch in ledger.read_records([("result", first.result_id), ("result", second.result_id)]) for row in batch)


def test_capture_and_adoption_preserve_one_original_generation(tmp_path):
    """Adoption reuses the original capture generation rather than recording a second one."""
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        origin = core.Origin(parent_entity_id="source-occurrence", state_id="source-state", member_key="source-key")
        def capture(context):
            value = context.session.retain_bytes([b"captured source"])
            context.generate(value, label="raw", role="raw")
        result = operations.run(definition(capture=True), request(), capture, capture_origin=origin)
        output = result.outcome.outputs[0]
        adoption = operations.run(definition(capture=True), request("adopt"), lambda context: context.adopt(output.entity_id, label="kept", role="raw"), capture_origin=origin)
        assert adoption.generations == ()
        assert adoption.outcome.outputs[0].production == "adopted"
        with ledger._transaction() as connection:
            assert connection.execute("SELECT count(*) FROM provenance_events WHERE event_kind='generation'").fetchone() == (1,)
        original = next(ledger.read_records([("execution", result.execution_id)]))[0].value
        assert original.capture_origin == origin


def test_fused_operations_exchange_values_before_publishing_together(tmp_path):
    """Prepared operations exchange entities before either result is published, then publish together."""
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        with operations.publisher.session() as session:
            first = operations.prepare(definition(), request("first"), lambda context: (context.generate(core.InlineValue(value={"number": 3}), label="value"), None)[1], session=session)
            source = first.result.outcome.outputs[0].entity_id
            assert next(ledger.read_records([("result", first.result.result_id)]))[0] is None
            def double(context):
                value = context.read_value(source)
                output = context.generate(core.InlineValue(value=value["number"] * 2), label="double")
                context.derive(output.entity_id, source)
            second = operations.prepare(definition(), request("second", (core.WholeInput(label="value", entity_id=source),)), double,
                                        upstream=(first,), session=session)
            assert next(ledger.read_records([("result", first.result.result_id)]))[0] is None
            operations.publish((second,), session=session)
            assert next(ledger.read_records([("result", first.result.result_id)]))[0].available
        assert second.result.usages[0].entity_id == source
        assert second.result.derivations[0].used_entity_id == source
        row = next(ledger.read_records([("entity", second.result.outcome.outputs[0].entity_id)]))[0]
        assert row.value.value.value == 6


@pytest.mark.parametrize("after_commit", [False, True])
def test_completed_producer_recovers_after_lost_publication_response(tmp_path, monkeypatch, after_commit):
    """A lost publication response is reconciled by unit id, so recover returns the same result without rerunning."""
    calls, attempted = [], []
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        original = ledger.commit
        def fail(batch):
            if batch.unit_id.startswith("operations:"):
                attempted.extend(value["execution_id"] for record in batch.records
                                 for value, _ in (record_parts(record),) if value["kind"] == "result")
                if after_commit:
                    original(batch)
                raise OSError("lost publication response")
            return original(batch)
        monkeypatch.setattr(ledger, "commit", fail)
        def producer(context):
            calls.append(True)
            context.generate(context.session.retain_value({"answer": 7}), label="answer")
        with pytest.raises(OSError, match="lost publication"):
            operations.run(definition(), request(), producer)
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        result = operations.recover(attempted[0])
        assert result.outcome.status == "success"
        assert operations.recover(attempted[0]) == result
        assert calls == [True]
        assert next(ledger.read_records([("result", result.result_id)]))[0].available


@pytest.mark.parametrize("exception,status", [(RuntimeError("producer failed"), "failed"), (KeyboardInterrupt(), "interrupted")])
def test_failures_are_authoritative_without_successful_retention(tmp_path, exception, status):
    """The producer's exception is re-raised unchanged, and the failed/interrupted result is recorded but not retained."""
    seen = []
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        def producer(context):
            seen.append(context.execution.execution_id)
            context.generate(core.InlineValue(value="partial"), label="partial")
            raise exception
        with pytest.raises(type(exception)) as caught:
            operations.run(definition(), request(), producer)
        assert caught.value is exception
        row = next(ledger.read_records([("result", seen[0] + ":result")]))[0]
        assert row.value.outcome.status == status
        assert not row.retained
        assert progress(ledger, seen[0])[-1]["status"] == status
        with pytest.raises(StateTransitionError, match="fresh attempt"):
            operations.recover(seen[0])


def test_direct_batch_calls_share_the_bounded_worker_and_close_the_source(tmp_path):
    """run_many runs four calls through one shared bounded worker pool and closes the call source once."""
    closed, reached = [], []
    gate = Barrier(2)
    def producer(context):
        reached.append(context.execution.execution_id)
        gate.wait(timeout=5)
    def calls():
        try:
            for index in range(4):
                yield {"definition": definition(), "request": request(str(index)), "producer": producer}
        finally:
            closed.append(True)
    with ExitStack() as stack:
        _, operations = setup(stack, tmp_path)
        results = list(operations.run_many(calls(), map_operations=partial(bounded_map, max_workers=2, max_in_flight=2)))
        assert len(results) == len(set(reached)) == 4
        assert closed == [True]


@pytest.mark.parametrize("native", [False, True])
def test_cancelled_batch_closes_without_consuming_the_remaining_calls(tmp_path, native):
    """Closing run_many early consumes only the first call and still closes the source exactly once."""
    consumed, closed = [], []
    def calls():
        try:
            for index in range(10):
                consumed.append(index)
                yield {"definition": definition(), "request": request(str(index)), "producer": lambda context: None}
        finally:
            closed.append(True)
    with ExitStack() as stack:
        _, operations = setup(stack, tmp_path)
        with closing(operations.run_many(calls(), map_operations=bounded_map if native else map)) as results:
            assert next(results).outcome.value == "empty"
        assert consumed == [0]
        assert closed == [True]


def test_invalid_output_binding_still_records_the_failed_attempt(tmp_path):
    attempts = []
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        def invalid(context):
            attempts.append(context.execution.execution_id)
            context.generate(core.InlineValue(value=1), label="duplicate")
            context.generate(core.InlineValue(value=2), label="duplicate")
        with pytest.raises(IntegrityError, match="duplicate"):
            operations.run(definition(), request(), invalid)
        failure = next(ledger.read_records([("result", attempts[0] + ":result")]))[0]
        assert failure.value.outcome.status == "failed"
        assert len(failure.value.generations) == 1
        assert not failure.retained


def test_streamed_entities_flow_between_operations_before_retention(tmp_path):
    channel = Queue(maxsize=1)
    count = 3
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        capture_definition = core.OperationDefinition(format_version=1, definition_id="capture", implementation_id="stream-source",
                                                       implementation_version="1", operation_kind="capture", configuration={})
        capture_request = core.Request(format_version=1, request_id="stream-source", definition_id="capture", inputs=(), dependencies=())
        def capture(context):
            for index in range(count):
                entity = context.generate(core.InlineValue(value=index), entity_id=f"chunk-{index}", label=f"chunk-{index}", role="raw")
                channel.put(entity, timeout=5)
        def consume(context):
            inputs, total = [], 0
            for _ in range(count):
                entity = channel.get(timeout=5)
                assert next(ledger.read_records([("entity", entity.entity_id)]))[0] is None
                context.use(entity.entity_id)
                inputs.append(entity.entity_id)
                total += entity.value.value
            output = context.generate(core.InlineValue(value=total), label="sum")
            for identity in inputs:
                context.derive(output.entity_id, identity)
        calls = [
            {"definition": capture_definition, "request": capture_request, "producer": capture,
             "capture_origin": core.Origin(parent_entity_id="source")},
            {"definition": definition(), "request": request("consume", tuple(core.WholeInput(label=str(i), entity_id=f"chunk-{i}") for i in range(count))),
             "producer": consume},
        ]
        prepared = list(bounded_map(lambda arguments: operations.prepare(**arguments), calls, max_workers=2, max_in_flight=2))
        results = operations.publish(prepared)
        total = next(result for result in results if result.outcome.outputs[0].label == "sum")
        assert len(total.usages) == len(total.derivations) == count
        assert next(ledger.read_records([("entity", total.outcome.outputs[0].entity_id)]))[0].value.value.value == 3


def test_recover_published_result_uses_the_ledger_without_rereading_content(tmp_path, monkeypatch):
    with ExitStack() as stack:
        _, operations = setup(stack, tmp_path)
        result = operations.run(definition(), request(), lambda context: None)
        def unexpected(*args, **kwargs):
            raise AssertionError("historical success reread its output journal")
        monkeypatch.setattr(operations.publisher.blobs, "read", unexpected)
        assert operations.recover(result.execution_id) == result


def test_publication_journal_keeps_keys_without_copying_generated_values(tmp_path):
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        payload = "large prepared metadata " * 10000
        result = operations.run(definition(), request(),
                                lambda context: (context.generate(core.InlineValue(value=payload), label="metadata"), None)[1])
        content = next(row["description"]["publication"] for row in progress(ledger, result.execution_id)
                       if "publication" in row["description"])
        with operations.publisher.session() as session:
            journal = session.read_json(content)
        assert journal["version"] == 2 and "records" not in journal
        assert content["byte_size"] < len(payload) // 100
        assert ["result", result.result_id] in journal["record_keys"]
        output = next(ledger.read_records([("entity", result.outcome.outputs[0].entity_id)]))[0]
        assert output.available and output.value.value.value == payload


def test_legacy_publication_journal_recovers_without_a_new_producer(tmp_path):
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        pending = operations.prepare(definition(), request(),
                                     lambda context: (context.generate(core.InlineValue(value={"answer": 7}), label="answer"), None)[1])
        journal = {"format": "docspec-operation-publication", "version": 1, "unit_id": "legacy-publication",
                   "executions": [pending.execution.execution_id],
                   "records": [record_value(record) for record in (*pending.records, pending.result)],
                   "roots": [["result", pending.result.result_id]]}
        with operations.publisher.session() as session:
            content = session.retain_value(journal)
        ledger.record_progress("legacy-prepared", pending.execution.execution_id, "progress",
                               {"publication": record_value(content, core.ContentRef)})
    with ExitStack() as stack:
        _, operations = setup(stack, tmp_path)
        assert operations.recover(pending.execution.execution_id) == pending.result


@pytest.mark.parametrize("missing", [False, True])
def test_recovery_refuses_missing_or_oversized_staged_records(tmp_path, monkeypatch, missing):
    import docspec.application.core_execution as execution
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        pending = operations.prepare(definition(), request(), lambda context: None)
        with monkeypatch.context() as patch:
            def interrupted(*args, **kwargs):
                raise OSError("publish interrupted")
            patch.setattr(operations, "_publish_journal", interrupted)
            with pytest.raises(OSError, match="publish interrupted"):
                operations.publish((pending,))
        if missing:
            read = ledger.read_records
            def missing_record(keys, **kwargs):
                for batch in read(keys, **kwargs):
                    yield tuple(None for _ in batch)
            monkeypatch.setattr(ledger, "read_records", missing_record)
        else:
            monkeypatch.setattr(execution, "BATCH_BYTES", 1)
        expected = IntegrityError if missing else LimitExceededError
        with pytest.raises(expected, match="missing a staged record" if missing else "recovery records exceed"):
            operations.recover(pending.execution.execution_id)


def test_large_opaque_values_use_the_streaming_path(tmp_path):
    with ExitStack() as stack:
        _, operations = setup(stack, tmp_path)
        with operations.publisher.session() as session:
            content = session.retain_bytes((b"x" * 1024**2 for _ in range(9)))
            source = core.Entity(format_version=1, entity_id="source", entity_type="artifact", value=content)
            def copy(context):
                retained = context.session.retain_bytes(context.read_chunks("source"))
                output = context.generate(retained, label="copy")
                context.derive(output.entity_id, "source")
            result = operations.run(definition(), request(inputs=(core.WholeInput(label="source", entity_id="source"),)), copy,
                                    input_records=(source,), session=session)
            assert len(result.usages) == 1
            assert result.usages[0].entity_id == "source"
            retained = next(operations.ledger.read_records([("entity", result.outcome.outputs[0].entity_id)]))[0].value.value
            assert retained.digest == content.digest and retained.byte_size == 9 * 1024**2


def test_control_row_exhaustion_records_failure_and_only_the_accepted_prefix(tmp_path, monkeypatch):
    """Control row exhaustion records a failed result whose generations and outputs are exactly the accepted prefix."""
    monkeypatch.setattr("docspec.application.core_execution.BATCH_ROWS", 12)
    seen = []
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        def producer(context):
            seen.append(context)
            for index in range(12):
                context.generate(core.InlineValue(value=index), label=f"output-{index}", entity_id=f"entity-{index}")
        with pytest.raises(LimitExceededError, match="control metadata"):
            operations.run(definition(), request(), producer)
        context = seen[0]
        result = next(ledger.read_records([("result", context.execution.execution_id + ":result")]))[0].value
        assert result.outcome.status == "failed"
        assert len(result.generations) == len(result.outcome.outputs) == len(context.records) == 8
        assert result.generations == tuple(context.generations)
        assert next(ledger.read_records([("entity", "entity-8")]))[0] is None
        assert progress(ledger, context.execution.execution_id)[-1]["status"] == "failed"


def test_control_byte_exhaustion_preserves_actual_events_and_can_finish_a_smaller_output(tmp_path):
    """After a byte-exhausted generate refuses, the operation can still succeed with its accepted output."""
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        def producer(context):
            context.generate(core.InlineValue(value="x" * (4 * 1024**2)), label="accepted", entity_id="accepted")
            with pytest.raises(LimitExceededError, match="control metadata"):
                context.generate(core.InlineValue(value="y" * (4 * 1024**2)), label="refused", entity_id="refused")
            assert len(context.outputs) == len(context.generations) == len(context.records) == 1
        result = operations.run(definition(), request(), producer)
        assert result.outcome.status == "success"
        assert [event.entity_id for event in result.generations] == ["accepted"]
        assert next(ledger.read_records([("entity", "refused")]))[0] is None


@pytest.mark.parametrize("huge_name", [False, True])
def test_huge_or_invalid_diagnostic_does_not_prevent_failure_accounting(tmp_path, huge_name):
    """A 9 MiB lone-surrogate error message is truncated in the failed record but does not stop failure accounting."""
    error_type = type("E" * (9 * 1024**2), (RuntimeError,), {}) if huge_name else RuntimeError
    error = error_type("\ud800" + "x" * (9 * 1024**2))
    seen = []
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        def producer(context):
            seen.append(context.execution.execution_id)
            context.generate(core.InlineValue(value="prefix"), label="prefix")
            raise error
        with pytest.raises(RuntimeError) as caught:
            operations.run(definition(), request(), producer)
        assert caught.value is error
        failure = next(ledger.read_records([("result", seen[0] + ":result")]))[0].value
        assert failure.outcome.status == "failed" and len(failure.generations) == 1
        assert len(failure.outcome.error) < 5000 and failure.outcome.error.endswith("[truncated]")
        assert progress(ledger, seen[0])[-1]["status"] == "failed"


def test_combined_staging_limit_records_incomplete_attempts_before_publication(tmp_path):
    """Oversized staging records both attempts incomplete; each can still publish alone."""
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        def producer(context):
            context.generate(core.InlineValue(value="x" * (5 * 1024**2)), label="large")
        prepared = [operations.prepare(definition(), request(f"request-{index}"), producer) for index in range(2)]
        with pytest.raises(LimitExceededError, match="metadata publication unit exceeds"):
            operations.publish(prepared)
        for item in prepared:
            assert progress(ledger, item.execution.execution_id)[-1]["status"] == "incomplete"
            assert next(ledger.read_records([("result", item.result.result_id)]))[0] is None
        # The completed producers remain publishable individually; they do not
        # need another attempt merely because their combined batch was large.
        assert all(operations.publish((item,))[0] == item.result for item in prepared)


def test_journal_storage_failure_records_interruption_and_preserves_the_error(tmp_path, monkeypatch):
    error = OSError("journal storage interrupted")
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        item = operations.prepare(definition(), request(), lambda context: None)
        with operations.publisher.session() as session:
            def fail(*args, **kwargs):
                raise error
            monkeypatch.setattr(session, "retain_value", fail)
            with pytest.raises(OSError) as caught:
                operations.publish((item,), session=session)
            assert caught.value is error
        assert progress(ledger, item.execution.execution_id)[-1]["status"] == "incomplete"
        assert operations.publish((item,))[0] == item.result


def test_oversized_added_metadata_cannot_hide_the_actual_failed_prefix(tmp_path):
    seen = []
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        def producer(context):
            seen.append(context.execution.execution_id)
            context.generate(core.InlineValue(value="prefix"), label="prefix", entity_id="prefix")
            # Composition attaches records through this same context collection.
            # Failure accounting must not depend on all attached payloads fitting.
            context.records.extend(core.Entity(format_version=1, entity_id=f"extra-{index}", entity_type="artifact",
                                                value=core.InlineValue(value="x" * (5 * 1024**2))) for index in range(2))
        with pytest.raises(LimitExceededError, match="publication budget"):
            operations.run(definition(), request(), producer)
        failure = next(ledger.read_records([("result", seen[0] + ":result")]))[0]
        assert failure.value.outcome.status == "failed" and not failure.retained
        assert [event.entity_id for event in failure.value.generations] == ["prefix"]
        assert [output.entity_id for output in failure.value.outcome.outputs] == ["prefix"]
        assert progress(ledger, seen[0])[-1]["status"] == "failed"


def test_continuation_accounts_for_events_retained_by_every_suspension(tmp_path, monkeypatch):
    """Usage events retained by each suspension count toward the control budget, so a suspension chain ends failed."""
    monkeypatch.setattr("docspec.application.core_execution.BATCH_BYTES", 1000)
    monkeypatch.setattr("docspec.application.core_execution._CONTROL_RESERVE", 128)
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        accepted = []
        def pause(context, state=None):
            event = context.use("actually-used")
            accepted.append(event)
            context.suspend({})
        suspended = operations.run(definition(), request(), pause)
        for _ in range(20):
            try:
                operations.resume(suspended.execution_id, pause, definition=definition(), verify=lambda _: True)
            except LimitExceededError:
                break
        else:
            pytest.fail("successive suspension forgot retained event metadata")
        failure = next(ledger.read_records([("result", suspended.execution_id + ":result")]))[0].value
        assert failure.outcome.status == "failed"
        assert failure.usages == tuple(accepted)
        assert len(accepted) < 20
        assert progress(ledger, suspended.execution_id)[-1]["status"] == "failed"
