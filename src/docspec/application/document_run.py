"""Durable document-run control through the ordinary Core operation lifecycle."""

from docspec.application.core_execution import SuspendedOperation
from docspec.domain import core
from docspec.domain.core_admission import encode_record
from docspec.domain.identity import decode_canonical_json_value, stable_urn
from docspec.domain.streams import owned_iterator
from docspec.errors import IntegrityError, StateTransitionError


def run_request_id(run_id):
    return stable_urn("core-document-run-request", run_id)


def _checkpoint(value):
    if not isinstance(value, dict) or set(value) != {"source_bytes", "generated_rows", "expected_current"}:
        return False
    if any(type(value[name]) is not int or value[name] < 0 for name in ("source_bytes", "generated_rows")):
        return False
    previous = value["expected_current"]
    return previous is None or (isinstance(previous, list) and len(previous) == 2
        and previous[0] in {"state", "result"} and isinstance(previous[1], str) and bool(previous[1]))


def run_document_operation(operations, *, run_id, source_state_id, configuration, produce):
    """Resume one pinned run, retaining counters before exposing handled errors.

    Missing checkpoint evidence after a hard kill never authorizes a reset. A
    different run ID deliberately starts another budget and can reuse old stages.
    """
    definition = core.OperationDefinition(format_version=1,
        definition_id=stable_urn("core-document-run-definition", configuration),
        implementation_id="docspec.document-run", implementation_version="1",
        operation_kind="transformation", configuration=configuration)
    request = core.Request(format_version=1, request_id=run_request_id(run_id), definition_id=definition.definition_id,
        inputs=(core.StateInput(label="source", state_id=source_state_id),),
        dependencies=(core.Dependency(label="source", binding_label="source", selection=core.Whole()),))
    error = []

    def perform(context, checkpoint):
        try:
            if not context.usages:
                context.use(source_state_id)
            state = produce(context, checkpoint)
            context.generate_record(state, label="state")
            context.generate(core.InlineValue(value=checkpoint), label="work")
        except BaseException as failure:
            error.append(failure)
            context.suspend(checkpoint)

    with operations.publisher.session() as session:
        with owned_iterator(session.read_records([("request", request.request_id)])) as rows:
            previous = next(rows)[0]
        if previous is not None and (previous.value is None or encode_record(previous.value) != encode_record(request)):
            raise IntegrityError("document run configuration or source differs from its original request")
        with owned_iterator(operations.ledger.executions(request.request_id)) as batches:
            attempts = [identity for batch in batches for identity in batch]
        if len(attempts) > 1:
            raise IntegrityError("document run has more than one controlling execution")
        if attempts:
            execution_id = attempts[0]
            with owned_iterator(session.read_records([("result", execution_id + ":result")])) as rows:
                retained = next(rows)[0]
            if retained is not None and retained.retained:
                result = operations.recover(execution_id, session=session)
            else:
                latest, publication = None, False
                with owned_iterator(operations.ledger.read_progress(execution_id)) as batches:
                    for batch in batches:
                        for payload in batch:
                            latest = decode_canonical_json_value(payload, label="document run progress")
                            publication |= "publication" in latest["description"]
                if latest and latest["status"] == "incomplete" and "checkpoint" in latest["description"]:
                    result = operations.resume(execution_id, perform, definition=definition, verify=_checkpoint, session=session)
                elif publication:
                    result = operations.recover(execution_id, session=session)
                else:
                    raise StateTransitionError("document run has uncheckpointed work; use a new run ID and budget")
        else:
            current = operations.ledger.current(configuration["dataset"]) if configuration["dataset"] is not None else None
            checkpoint = {"source_bytes": 0, "generated_rows": 0,
                          "expected_current": None if current is None else list(current)}
            prepared = operations.prepare(definition, request, lambda context: perform(context, checkpoint),
                session=session, start_unit_id=request.request_id + ":start")
            result = prepared if isinstance(prepared, SuspendedOperation) else operations.publish((prepared,), session=session)[0]
        if isinstance(result, SuspendedOperation):
            if error:
                raise error[0]
            raise StateTransitionError("document run remains suspended")
        outputs = {binding.label: binding.entity_id for binding in result.outcome.outputs}
        with owned_iterator(session.read_records([("state", outputs["state"]), ("entity", outputs["work"])])) as rows:
            state, work = next(rows)
        if state is None or not state.available or work is None or not work.available:
            raise IntegrityError("document run output or accounting is unavailable")
        checkpoint = work.value.value.value
        if not _checkpoint(checkpoint):
            raise IntegrityError("document run accounting has an invalid shape")
        if configuration["dataset"] is not None:
            previous = checkpoint["expected_current"]
            operations.ledger.select_current(run_id + ":current", configuration["dataset"], ("state", state.value.state_id),
                                            None if previous is None else tuple(previous))
        return state.value
