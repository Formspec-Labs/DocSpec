"""Incremental imports through the shared revision and publication owners."""

from hashlib import sha256
from tempfile import TemporaryFile

from docspec.application.core_edits import compose_revision
from docspec.domain import core
from docspec.domain.core_admission import encode_record, record_value
from docspec.domain.identity import canonical_value_bytes, decode_canonical_json_value, require_text, stable_urn
from docspec.domain.streams import owned_iterator
from docspec.errors import IntegrityError, LimitExceededError, StaleBaseError
from docspec.ports.record_storage import BATCH_BYTES


def upsert(operations, base_state_id, rows, *, batch_id, dataset=None):
    """Add or replace supplied keys; preserve every unmentioned member.

    A batch ID pins the base, ordered input and optional dataset. Retry returns
    the same published state. A different batch imports fresh occurrences even
    for equal values. Dataset promotion expects its current state to be the base.
    """
    require_text(base_state_id, "base state identity")
    require_text(batch_id, "batch identity")
    if dataset is not None:
        require_text(dataset, "dataset")
    identity = stable_urn("core-upsert", batch_id)
    inputs_id = identity + ":inputs"
    # Freeze a one-shot batch before binding its retry identity. Only bounded
    # edit metadata stays in memory; payloads spool to disk and stream to Iceberg.
    with owned_iterator(rows) as source, TemporaryFile() as spool:
        digest, keys, edits, edit_bytes = sha256(), set(), [], 2
        for key, value in source:
            if not isinstance(key, str):
                raise IntegrityError("upsert member keys must be strings")
            if key in keys:
                raise IntegrityError("upsert batch contains a duplicate member key")
            entity = core.Entity(format_version=1, entity_id=stable_urn("core-upsert-occurrence", [batch_id, key]),
                                 entity_type="occurrence", value=core.InlineValue(value=value))
            payload = encode_record(entity)
            if len(payload) > BATCH_BYTES:
                raise LimitExceededError("upsert occurrence exceeds the 8 MiB record limit")
            edit = core.Put(sequence=len(edits), member_key=key, occurrence_id=entity.entity_id)
            edit_bytes += len(canonical_value_bytes(record_value(edit, core.Put))) + 1
            if edit_bytes > BATCH_BYTES:
                raise LimitExceededError("upsert edit metadata exceeds 8 MiB; split the input into separate batches")
            framed = canonical_value_bytes([key, value]) + b"\n"
            digest.update(framed)
            spool.write(framed)
            keys.add(key)
            edits.append(edit)
        if not edits:
            raise IntegrityError("upsert batch must contain at least one row")
        configuration = {"batch_id": batch_id, "rows_digest": "sha256:" + digest.hexdigest(),
                         "row_count": len(edits), "dataset": dataset}
        definition = core.OperationDefinition(format_version=1,
            definition_id=stable_urn("core-upsert-definition", configuration), implementation_id="docspec.upsert",
            implementation_version="1", operation_kind="transformation", configuration=configuration)
        inputs = (core.StateInput(label="base", state_id=base_state_id), core.StateInput(label="puts", state_id=inputs_id))
        request = core.Request(format_version=1, request_id=identity + ":request", definition_id=definition.definition_id,
            inputs=inputs, dependencies=tuple(core.Dependency(label=item.label, binding_label=item.label,
                selection=core.Whole()) for item in inputs))

        def produce(context):
            session = context.session
            if dataset is not None and operations.ledger.current(dataset) != ("state", base_state_id):
                raise StaleBaseError("dataset current state differs from the supplied base")
            with owned_iterator(session.read_records([("state", base_state_id), ("state", inputs_id)])) as batches:
                base, retained_input = next(batches)
            if base is None or not base.available:
                raise IntegrityError("upsert base state is unavailable")
            if retained_input is None:
                def entities():
                    spool.seek(0)
                    for edit, payload in zip(edits, spool, strict=True):
                        key, value = decode_canonical_json_value(payload.rstrip(b"\n"))
                        yield key, core.Entity(format_version=1,
                            entity_id=edit.occurrence_id,
                            entity_type="occurrence", value=core.InlineValue(value=value))
                session.states.create_keyed(session, state_id=inputs_id, representation_id=inputs_id + ":physical",
                                            unit_id=inputs_id + ":import", rows=entities())
            elif not retained_input.available:
                raise IntegrityError("upsert batch input is unavailable")
            revision = core.Revision(format_version=1, revision_id=context.execution.execution_id + ":revision",
                base_state_id=base_state_id, result_state_id=context.execution.execution_id + ":state",
                edits=session.retain_value([record_value(edit, core.Put) for edit in edits]))
            compose_revision(operations, context, revision, inputs_id)

        with operations.publisher.session() as session:
            result = operations.run_once(definition, request, produce, session=session)
            state_id = result.outcome.outputs[0].entity_id
            with owned_iterator(session.read_records([("state", state_id)])) as batches:
                state = next(batches)[0]
            if state is None or not state.available:
                raise IntegrityError("upsert result state is unavailable")
            if dataset is not None:
                operations.ledger.select_current(identity + ":current", dataset, ("state", state_id), ("state", base_state_id))
            return state.value
