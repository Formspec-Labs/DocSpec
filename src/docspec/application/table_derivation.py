"""Derive a typed table-shaped state from caller rows in one metadata unit (C29, decision 0007 item 9)."""

from dataclasses import dataclass

from docspec.application.core_dependencies import binding_key
from docspec.application.table_units import advance, state_request, table_state_unit
from docspec.domain import core
from docspec.domain.core_admission import admit_record, encode_record
from docspec.domain.identity import canonical_value_bytes, require_text, sha256_digest, stable_urn
from docspec.domain.storage import TableSchema
from docspec.domain.streams import bounded_items, owned_iterator
from docspec.domain.table_rows import TableIdentity
from docspec.errors import IntegrityError, StaleBaseError
from docspec.ports.record_storage import BATCH_ROWS


REPORT_FORMAT = "docspec-table-derivation"


@dataclass(frozen=True, slots=True)
class TableDerivation:
    """The derived state and its derivation's report: identity rules, base, dataset and counts."""

    state_id: str
    report: dict


def derive_table(operations, batches, *, schema, batch_id, definition, inputs, base_state_id=None, removals=(),
                 dataset=None):
    """Publish one typed table-shaped state derived from source states and retained lookups (C29).

    ``batches`` are Arrow record batches in ``schema``: member_key, the
    source_occurrence_id each row derives from (a list for a fusion), and for
    a one-to-many layer segment_index. The rows are spilled and minted once,
    natively, under the definition's identity scope; the state ID digests the
    batch ID alone. A retry with the same definition, inputs, base, removals
    and rows, in any order, returns the published state without writing; any
    change under the batch ID refuses. With a base, the rows replace every row
    of the source members they name, and ``removals`` (source member keys)
    drop theirs; only rows whose occurrence changed are written, sharing the
    base's files. The base must share the definition and schema, and is
    recorded as used without being bound, so it stays removable.
    ``dataset=`` advances the current pointer from the base, or from none,
    with the stale-base check. One unit publishes the definition, a rows
    entity pinning the digest, request, execution, result, report, state and
    representation; nothing per row.
    """
    require_text(batch_id, "batch identity")
    for value, label in ((dataset, "dataset"), (base_state_id, "base state identity")):
        if value is not None:
            require_text(value, label)
    removals = tuple(removals)
    if any(not isinstance(key, str) or not key for key in removals) or len(set(removals)) != len(removals):
        raise IntegrityError("derive removals must be distinct member keys")
    if removals and base_state_id is None:
        raise IntegrityError("derive removals require a base state")
    definition = admit_record(encode_record(definition))
    if not isinstance(definition, core.OperationDefinition):
        raise IntegrityError("derive requires an operation definition")
    if not isinstance(schema, TableSchema):
        raise IntegrityError("a typed derive declares its TableSchema")
    inputs = tuple(bounded_items(inputs, limit=BATCH_ROWS))
    if any(not isinstance(item, (core.StateInput, core.WholeInput)) for item in inputs):
        raise IntegrityError("derive inputs must be state or whole-input bindings")
    labels = {item.label for item in inputs}
    if len(labels) != len(inputs) or labels & {"base", "rows"}:
        raise IntegrityError("derive input labels must be distinct and avoid base and rows")
    try:
        identity = TableIdentity.derived(definition.definition_id, schema)
    except ValueError as error:
        raise IntegrityError(f"derived rows cannot take table-row identity: {error}") from error
    state_id = stable_urn("core-derive-table", batch_id)
    request_id = state_id + ":request"
    with operations.ledger.request_guard(request_id), operations.publisher.session() as session, \
            session.states.stage_rows(batches, schema, identity) as staged:
        if not staged.count and not removals:
            raise IntegrityError("derive batch must contain at least one row or removal")
        rows = _rows_entity(batch_id, identity, staged, base_state_id, removals)
        request_inputs = (*inputs, core.WholeInput(label="rows", entity_id=rows.entity_id))
        request, stored_definition = _read(session, ("request", request_id), ("operation_definition", definition.definition_id))
        if request is not None:
            # Only the unit that published the state wrote its request.
            if (encode_record(request.value) != encode_record(state_request(state_id, definition, request_inputs))
                    or encode_record(stored_definition.value) != encode_record(definition)):
                raise IntegrityError("batch ID already names different input, base, or configuration")
            state, report = _read(session, ("state", state_id), ("entity", state_id + ":report"))
            if any(row is None or not row.available for row in (state, report)):
                raise IntegrityError("this batch's derived state was removed; it cannot be derived again")
            report = report.value.value.value
        else:
            if stored_definition is not None and encode_record(stored_definition.value) != encode_record(definition):
                raise IntegrityError("operation definition identity already names another definition")
            _check_inputs(session, inputs, operations, dataset, base_state_id)
            content, counts, identity_check = session.states.derive_table(session, staged, state_id=state_id,
                                                                          base_state_id=base_state_id, removals=removals)
            report = {"format": REPORT_FORMAT, "version": 1, "rules": identity.to_dict(), "base": base_state_id,
                      "dataset": dataset, "counts": counts}
            unit = table_state_unit(session, state_id=state_id, unit="derivation", definition=definition,
                                    inputs=request_inputs, report=report, content=content, evidence=(rows,),
                                    used=() if base_state_id is None else (("base", base_state_id),))
            session.publish(unit, identity_check=identity_check)
    advance(operations, dataset, state_id, base_state_id, kind="derive-table-current")
    return TableDerivation(state_id, report)


def _read(session, *keys):
    """Read a few ledger rows by key, None for each one absent."""
    with owned_iterator(session.read_records(keys)) as batches:
        return next(batches)


def _rows_entity(batch_id, identity, staged, base_state_id, removals):
    """The retained entity the request binds as ``rows``: every row by digest, the removals and the base."""
    value = {"format": "docspec-derived-rows", "version": 1, "rules": identity.to_dict(), "base": base_state_id,
             "rowCount": staged.count, "rowsDigest": staged.digest, "removalCount": len(removals),
             "removalsDigest": sha256_digest(canonical_value_bytes(sorted(removals)))}
    return core.Entity(format_version=1, entity_id=stable_urn("core-derive-table-rows", [batch_id, value]),
                       entity_type="artifact", value=core.InlineValue(value=value))


def _check_inputs(session, inputs, operations, dataset, base_state_id):
    """Refuse an unavailable input or base, and a dataset whose current state is not the base."""
    wanted = [binding_key(item) for item in inputs] + ([("state", base_state_id)] if base_state_id is not None else [])
    with owned_iterator(session.read_records(wanted)) as batches:
        if any(row is None or not row.available for batch in batches for row in batch):
            raise IntegrityError("derive input is unavailable")
    if dataset is not None and operations.ledger.current(dataset) != (None if base_state_id is None else ("state", base_state_id)):
        raise StaleBaseError("dataset current state differs from the supplied base")
