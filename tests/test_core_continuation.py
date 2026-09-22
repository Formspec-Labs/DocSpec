"""Explicit, verified checkpoint continuation keeps one attempt and its provenance."""

from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from docspec.application.core_execution import SuspendedOperation
from docspec.domain import core
from docspec.errors import IntegrityError, StaleBaseError, StateTransitionError
from tests.test_core_execution import setup, definition, request, progress


def start(context):
    """Suspend a first attempt after generating one value, recording `next` and the entity id."""
    entity = context.generate(core.InlineValue(value=2), label="first")
    context.suspend({"next": 1, "first": entity.entity_id})


def finish(context, state):
    """Resume by reading the suspended value, generating its triple, and deriving from the original."""
    value = context.read_value(state["first"])
    entity = context.generate(core.InlineValue(value=value * 3), label="last")
    context.derive(entity.entity_id, state["first"])


def test_checkpoint_continues_after_reopen_with_original_attempt_and_events(tmp_path):
    """Continuation after reopen keeps the original execution id with one execution and one event per generation."""
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        suspended = operations.run(definition(), request(), start)
        assert isinstance(suspended, SuspendedOperation)
        assert next(ledger.read_records([("result", suspended.execution_id + ":result")]))[0] is None
        assert progress(ledger, suspended.execution_id)[-1]["status"] == "incomplete"
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        result = operations.resume(suspended.execution_id, finish, definition=definition(), verify=lambda state: state["next"] == 1)
        assert result.execution_id == suspended.execution_id
        assert [binding.label for binding in result.outcome.outputs] == ["first", "last"]
        assert len(result.generations) == 2
        with ledger._transaction() as connection:
            assert connection.execute("SELECT count(*) FROM records WHERE kind='execution'").fetchone() == (1,)
            assert connection.execute("SELECT count(*) FROM provenance_events WHERE event_kind='generation'").fetchone() == (2,)
        assert operations.recover(suspended.execution_id) == result
        with pytest.raises(StateTransitionError, match="explicit suspension"):
            operations.resume(suspended.execution_id, finish, definition=definition(), verify=lambda state: True)


def test_verifier_and_definition_refusals_do_not_claim_or_invoke_continuation(tmp_path):
    """A failed verifier or changed definition refuses before the continuation runs and leaves the attempt incomplete."""
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        suspended = operations.run(definition(), request(), start)
        calls = []
        with pytest.raises(IntegrityError, match="verification"):
            operations.resume(suspended.execution_id, lambda *args: calls.append(True), definition=definition(), verify=lambda state: False)
        changed = core.OperationDefinition(format_version=1, definition_id="definition", implementation_id="test-operation", implementation_version="2",
                                           operation_kind="transformation", configuration={})
        with pytest.raises(IntegrityError, match="definition"):
            operations.resume(suspended.execution_id, lambda *args: calls.append(True), definition=changed, verify=lambda state: True)
        assert calls == []
        assert progress(ledger, suspended.execution_id)[-1]["status"] == "incomplete"
        assert operations.resume(suspended.execution_id, finish, definition=definition(), verify=lambda state: True).outcome.status == "success"


def test_only_one_concurrent_continuation_claim_can_run(tmp_path):
    """Two concurrent resumes produce exactly one winner, and only the winner's continuation runs."""
    with ExitStack() as stack:
        _, operations = setup(stack, tmp_path)
        suspended = operations.run(definition(), request(), start)
        barrier, calls = Barrier(2), []
        def verify(state):
            barrier.wait(timeout=5)
            return True
        def continuation(context, state):
            calls.append(context.execution.execution_id)
            finish(context, state)
        def resume():
            try:
                return operations.resume(suspended.execution_id, continuation, definition=definition(), verify=verify)
            except StaleBaseError:
                return None
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: resume(), range(2)))
        assert sum(result is not None for result in results) == 1
        assert calls == [suspended.execution_id]


def test_repeated_suspension_preserves_the_complete_prefix(tmp_path):
    """Suspending twice keeps every earlier generation and the full ordered output prefix."""
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        suspended = operations.run(definition(), request(), start)
        def pause_again(context, state):
            context.generate(core.InlineValue(value=3), label="middle")
            context.suspend({**state, "next": 2})
        again = operations.resume(suspended.execution_id, pause_again, definition=definition(), verify=lambda state: state["next"] == 1)
        assert isinstance(again, SuspendedOperation) and again.execution_id == suspended.execution_id
        result = operations.resume(again.execution_id, finish, definition=definition(), verify=lambda state: state["next"] == 2)
        assert [binding.label for binding in result.outcome.outputs] == ["first", "middle", "last"]
        assert len(result.generations) == 3
        with ledger._transaction() as connection:
            assert connection.execute("SELECT count(*) FROM provenance_events WHERE event_kind='generation'").fetchone() == (3,)


def test_checkpoint_retains_prepared_prerequisites_before_suspending(tmp_path):
    """A suspended operation retains its prepared prerequisite result, which the continuation adopts without a new generation."""
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        with operations.publisher.session() as session:
            def first(context):
                context.generate(core.InlineValue(value=10), label="source")
            upstream = operations.prepare(definition(), request("upstream"), first, session=session)
            source = upstream.result.outcome.outputs[0].entity_id
            def pause(context):
                assert context.read_value(source) == 10
                context.suspend({"source": source})
            suspended = operations.prepare(definition(), request("downstream", (core.WholeInput(label="source", entity_id=source),)), pause,
                                           upstream=(upstream,), session=session)
            assert next(ledger.read_records([("result", upstream.result.result_id)]))[0].available
        result = operations.resume(suspended.execution_id, lambda context, state: context.adopt(state["source"], label="kept"),
                                   definition=definition(), verify=lambda state: True)
        assert result.outcome.outputs[0].production == "adopted"
        assert result.generations == ()


def test_missing_checkpoint_content_refuses_before_claiming_the_attempt(tmp_path):
    """Deleted checkpoint bytes refuse before the continuation runs and leave the attempt incomplete."""
    with ExitStack() as stack:
        ledger, operations = setup(stack, tmp_path)
        suspended = operations.run(definition(), request(), start)
        from docspec.domain.references import BlobRef
        content = suspended.checkpoint
        operations.publisher.blobs.delete(BlobRef(content.locator, content.digest, content.byte_size, content.media_type))
        calls = []
        with pytest.raises((IntegrityError, FileNotFoundError)):
            operations.resume(suspended.execution_id, lambda *args: calls.append(True), definition=definition(), verify=lambda state: True)
        assert calls == []
        assert progress(ledger, suspended.execution_id)[-1]["status"] == "incomplete"
