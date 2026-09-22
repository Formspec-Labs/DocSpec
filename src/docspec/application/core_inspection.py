"""Read retained meaning from the Core ledger without executing work."""

from collections import deque

from docspec.domain import core
from docspec.domain.core_admission import record_value
from docspec.domain.identity import decode_canonical_json_value
from docspec.domain.streams import owned_iterator
from docspec.errors import IntegrityError


def inspect_record(session, key, *, progress_limit=20):
    """Assemble one record's retained meaning, execution, outputs and recent progress.

    Raises IntegrityError when the record does not exist; ``progress_limit``
    must be an integer between 0 and 2048.
    """
    if type(progress_limit) is not int or not 0 <= progress_limit <= 2048:
        raise ValueError("progress limit must be between 0 and 2048")

    def read(target):
        with owned_iterator(session.read_records([target])) as batches:
            return next(batches)[0]

    row = read(key)
    if row is None:
        raise IntegrityError("record does not exist")

    def describe(stored, *, include_record=True):
        if stored is None:
            return None
        return {"key": list(stored.key), "retained": stored.retained, "available": stored.available,
                "evidence_version": stored.evidence_version,
                "record": None if not include_record or stored.value is None else record_value(stored.value)}

    report = describe(row)
    result = row
    if isinstance(row.value, core.Selection):
        report["requested"] = describe(read(("request", row.value.request_id)))
        result = read(("result", row.value.selected_result_id))
        report["selected_result"] = describe(result)
    if result is not None and isinstance(result.value, core.Result):
        execution = read(("execution", result.value.execution_id))
        report["execution"] = describe(execution)
        if execution is not None:
            report["executed_request"] = describe(read(("request", execution.value.request_id)))
        keys = ((kind, item.entity_id) for item in result.value.outcome.outputs for kind in ("entity", "state"))
        with owned_iterator(session.read_records(keys, include_values=False)) as batches:
            report["outputs"] = [describe(output, include_record=False) for batch in batches for output in batch if output is not None]
    execution_id = row.value.execution_id if isinstance(row.value, core.Execution) else (
        result.value.execution_id if result is not None and isinstance(result.value, core.Result) else None)
    if execution_id is not None:
        with owned_iterator(session.ledger.read_progress(execution_id)) as batches:
            report["progress"] = list(deque((decode_canonical_json_value(payload, label="execution progress")
                                           for batch in batches for payload in batch), maxlen=progress_limit))
    return report
