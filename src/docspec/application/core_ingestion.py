"""Incremental imports through the shared revision and publication owners."""

from hashlib import sha256
import struct
from tempfile import TemporaryFile

from docspec.application.core_dependencies import binding_key
from docspec.application.core_edits import compose_revision
from docspec.domain import core
from docspec.domain.core_admission import admit_record, encode_record, inline_occurrence_payload, record_value
from docspec.domain.identity import canonical_value_bytes, decode_canonical_json_value, require_text, stable_urn
from docspec.domain.streams import bounded_items, owned_iterator
from docspec.errors import IntegrityError, LimitExceededError, StaleBaseError
from docspec.ports.record_storage import BATCH_BYTES, BATCH_ROWS


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
    # Freeze a one-shot batch once to name its definition before delegating to
    # derive. Only the framing digest and spool stay; derive re-checks rows.
    with owned_iterator(rows) as source, TemporaryFile() as spool:
        digest, keys = sha256(), set()
        count = 0
        for key, value in source:
            if key in keys:
                raise IntegrityError("upsert batch contains a duplicate member key")
            keys.add(key)
            count += 1
            try:
                framed = canonical_value_bytes([key, value]) + b"\n"
            except (TypeError, ValueError) as error:
                raise IntegrityError("upsert rows are outside their JSON codec") from error
            digest.update(framed)
            spool.write(framed)
        if not count:
            raise IntegrityError("upsert batch must contain at least one row")
        configuration = {"batch_id": batch_id, "rows_digest": "sha256:" + digest.hexdigest(),
                         "row_count": count, "dataset": dataset}
        definition = core.OperationDefinition(format_version=1,
            definition_id=stable_urn("core-upsert-definition", configuration), implementation_id="docspec.upsert",
            implementation_version="1", operation_kind="transformation", configuration=configuration)

        def spooled():
            spool.seek(0)
            for line in spool:
                yield decode_canonical_json_value(line.rstrip(b"\n"))

        return derive(operations, spooled(), batch_id=batch_id, definition=definition, inputs=(),
                      base_state_id=base_state_id, dataset=dataset, identity_kind="core-upsert")


def derive(operations, rows, *, batch_id, definition, inputs, base_state_id=None, removals=(), dataset=None,
           identity_kind="core-derive"):
    """Publish one keyed state derived from source states and retained lookups.

    With a base, the rows and removals become one Revision of Put/Remove edits
    that shares the base's files, within 8 MiB of edit metadata. Without one,
    the rows are written once into a fresh keyed state with no edit bound.
    A batch ID pins the ordered rows, removals, base, optional
    dataset and caller inputs; retrying it returns the same published state,
    while changed rows or inputs refuse. Dataset promotion expects its current
    state to be the base. Lookup values are retained once by the caller and
    bind as whole inputs; source states and the base bind as state inputs.
    """
    require_text(batch_id, "batch identity")
    if dataset is not None:
        require_text(dataset, "dataset")
    if base_state_id is not None:
        require_text(base_state_id, "base state identity")
    elif dataset is not None or removals:
        raise IntegrityError("derive dataset promotion and removals require a base state")
    definition = admit_record(encode_record(definition))
    if not isinstance(definition, core.OperationDefinition):
        raise IntegrityError("derive requires an operation definition")
    inputs = tuple(bounded_items(inputs, limit=BATCH_ROWS))
    if any(not isinstance(item, (core.StateInput, core.WholeInput)) for item in inputs):
        raise IntegrityError("derive inputs must be state or whole-input bindings")
    labels = {item.label for item in inputs}
    if len(labels) != len(inputs) or labels & {"base", "rows"}:
        raise IntegrityError("derive input labels must be distinct and avoid base and rows")
    identity = stable_urn(identity_kind, batch_id)

    def occurrence(key):
        return stable_urn(identity_kind + "-occurrence", [batch_id, key])

    # Freeze a one-shot batch before binding its retry identity. Payloads spool
    # to disk and stream to Iceberg. Only a base's Put/Remove edits stay in
    # memory; their 8 MiB bound is checked as rows arrive, so an oversized
    # change fails before the rest is prepared. A state without a base keeps
    # only its member keys and has no edit bound. Removals are consumed after
    # the rows, so a caller may discover them while streaming prepared values.
    # Each value is encoded once: its canonical bytes frame the retry digest
    # ([key, value]) and complete the stored occurrence record, and the spool
    # keeps that record so the writer never decodes or re-encodes it.
    with owned_iterator(rows) as source, TemporaryFile() as spool:
        digest, keys, edits, edit_bytes = sha256(), set(), [], 2

        def edit(value, record_type):
            nonlocal edit_bytes
            edit_bytes += len(canonical_value_bytes(record_value(value, record_type))) + 1
            if edit_bytes > BATCH_BYTES:
                raise LimitExceededError("derive edit metadata exceeds 8 MiB; split the changes into separate batches")
            edits.append(value)

        for key, value in source:
            if not isinstance(key, str):
                raise IntegrityError("derive member keys must be strings")
            if key in keys:
                raise IntegrityError("derive batch contains a duplicate member key")
            entity_id = occurrence(key)
            try:
                value_bytes = canonical_value_bytes(value)
            except (TypeError, ValueError) as error:
                raise IntegrityError("derive rows are outside their JSON codec") from error
            payload = inline_occurrence_payload(entity_id, value_bytes)
            if len(payload) > BATCH_BYTES:
                raise LimitExceededError("derive occurrence exceeds the 8 MiB record limit")
            # Canonical arrays are their items' canonical bytes, comma-joined.
            digest.update(b"[" + canonical_value_bytes(key) + b"," + value_bytes + b"]\n")
            encoded_key = key.encode("utf-8")
            spool.write(struct.pack(">II", len(encoded_key), len(payload)) + encoded_key + payload)
            keys.add(key)
            if base_state_id is not None:
                edit(core.Put(sequence=len(edits), member_key=key, occurrence_id=entity_id), core.Put)
        removals = tuple(removals)
        if any(not isinstance(key, str) for key in removals) or len(set(removals)) != len(removals):
            raise IntegrityError("derive removals must be distinct member keys")
        if keys & set(removals):
            raise IntegrityError("derive removal repeats a put member key")
        digest.update(canonical_value_bytes(["removals", sorted(removals)]) + b"\n")
        for key in removals:
            edit(core.Remove(sequence=len(edits), member_key=key), core.Remove)
        if not keys and not removals:
            raise IntegrityError("derive batch must contain at least one row or removal")
        rows_state_id = stable_urn(identity_kind + "-rows", [batch_id, digest.hexdigest()])
        request_inputs = (core.StateInput(label="base", state_id=base_state_id), *inputs,
                          core.StateInput(label="rows", state_id=rows_state_id)) if base_state_id is not None else \
                         (*inputs, core.StateInput(label="rows", state_id=rows_state_id))
        request = core.Request(format_version=1, request_id=identity + ":request", definition_id=definition.definition_id,
            inputs=request_inputs, dependencies=tuple(core.Dependency(label=item.label, binding_label=item.label,
                selection=core.Whole()) for item in request_inputs))

        def entities():
            spool.seek(0)
            while header := spool.read(8):
                key_size, payload_size = struct.unpack(">II", header)
                key = spool.read(key_size).decode("utf-8")
                yield key, occurrence(key), spool.read(payload_size)

        def produce(context):
            session = context.session
            if dataset is not None and operations.ledger.current(dataset) != ("state", base_state_id):
                raise StaleBaseError("dataset current state differs from the supplied base")
            wanted = [binding_key(item) for item in request_inputs if item.label != "rows"]
            if wanted:
                with owned_iterator(session.read_records(wanted)) as batches:
                    for batch in batches:
                        for row in batch:
                            if row is None or not row.available:
                                raise IntegrityError("derive input is unavailable")
            with owned_iterator(session.read_records([("state", rows_state_id)])) as batches:
                existing = next(batches)[0]
            if existing is None:
                session.mint(rows_state_id)
                session.states.create_keyed(session, state_id=rows_state_id, representation_id=rows_state_id + ":physical",
                                            unit_id=rows_state_id + ":import", rows=entities(), encoded=True)
            elif not existing.available:
                raise IntegrityError("derive rows state is unavailable")
            caller_usages = [context.use(binding_key(item)[1]) for item in inputs]
            session.mint(context.execution.execution_id + ":state")
            if base_state_id is not None:
                revision = core.Revision(format_version=1, revision_id=context.execution.execution_id + ":revision",
                    base_state_id=base_state_id, result_state_id=context.execution.execution_id + ":state",
                    edits=session.retain_value([record_value(item, core.Put if isinstance(item, core.Put) else core.Remove)
                                                for item in edits]))
                compose_revision(operations, context, revision, rows_state_id)
                for used in caller_usages:
                    context.derive(revision.result_state_id, used.entity_id,
                        generation_event_id=context.generations[-1].event_id, usage_event_id=used.event_id)
            else:
                result_state_id = context.execution.execution_id + ":state"
                usages = [context.use(rows_state_id), *caller_usages]
                # The result has exactly the rows' membership. Present the rows
                # state's retained files under the result rather than writing
                # and receipting every payload a second time.
                rows_representation = session.states.representation(session, rows_state_id)
                context.generate_record(core.State(format_version=1, state_id=result_state_id), label="state")
                context.records.append(core.StateRepresentation(format_version=1,
                    representation_id=result_state_id + ":physical", state_id=result_state_id,
                    membership=rows_representation.membership))
                for used in usages:
                    context.derive(result_state_id, used.entity_id,
                        generation_event_id=context.generations[-1].event_id, usage_event_id=used.event_id)

        with operations.publisher.session() as session:
            result = operations.run_once(definition, request, produce, session=session)
            state_id = result.outcome.outputs[0].entity_id
            with owned_iterator(session.read_records([("state", state_id)])) as batches:
                state = next(batches)[0]
            if state is None or not state.available:
                raise IntegrityError("derive result state is unavailable")
            if dataset is not None:
                operations.ledger.select_current(identity + ":current", dataset, ("state", state_id), ("state", base_state_id))
            return state.value
