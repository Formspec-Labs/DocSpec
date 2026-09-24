"""Common attempts and recoverable publication for direct Core operations."""

from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from uuid import uuid4

from docspec.application.core_dependencies import CoreDependencies, binding_key
from docspec.application.core_publication import CorePublisher
from docspec.application.core_reuse import CoreReuse, Resolution, ReuseRequest, selection_for
from docspec.domain import core
from docspec.domain.core_recovery import recovery_document
from docspec.domain.core_admission import admit_record, encode_record, record_value
from docspec.domain.graphs import operation_order
from docspec.domain.identity import canonical_value_bytes, decode_canonical_json_value, sha256_digest, snapshot_json_value
from docspec.domain.references import BlobRef
from docspec.domain.streams import bounded_items, owned_iterator
from docspec.errors import IntegrityError, LimitExceededError, StateTransitionError
from docspec.ports.core_ledger import MetadataBatch
from docspec.ports.record_storage import BATCH_BYTES, BATCH_ROWS, bounded_rows


def _identity(kind):
    return f"urn:docspec:{kind}:{uuid4()}"


def _snapshot_size(record):
    payload = encode_record(record)
    return admit_record(payload), len(payload)


def _snapshot(record):
    return _snapshot_size(record)[0]


def publication_records(session, journal):
    """Read exact staged records; older journals contain their own record values."""
    if journal["version"] == 1:
        return tuple(_snapshot(record) for record in journal["records"])
    keys = bounded_items((tuple(key) for key in journal["record_keys"]), limit=BATCH_ROWS)
    records, size = [], 0
    with owned_iterator(session.read_records(keys, include_unavailable_values=True)) as batches:
        for batch in batches:
            for row in batch:
                if row is None or row.value is None:
                    raise IntegrityError("publication journal is missing a staged record")
                size += len(encode_record(row.value))
                if size > BATCH_BYTES:
                    raise LimitExceededError("publication recovery records exceed the 8 MiB limit")
                records.append(row.value)
    if len(records) != len(keys):
        raise IntegrityError("publication journal is missing a staged record")
    return tuple(records)


# Leave space for the final Result, journal framing and a bounded diagnostic.
_CONTROL_RESERVE = 64 * 1024


def _diagnostic(error):
    try:
        message = str(error)
    except Exception:
        message = "error description unavailable"
    # Escape invalid Unicode as text before canonical encoding; never let a
    # diagnostic itself prevent the authoritative failure write.
    name = type(error).__name__
    truncated = len(name) + len(message) + 2 > 4096
    message = f"{name[:4096]}: {message[:4096]}"[:4096].encode("utf-8", "backslashreplace").decode("utf-8")
    return message[:4096] + (" [truncated]" if truncated or len(message) > 4096 else "")


@dataclass(frozen=True)
class ResolveCall:
    """One reuse-or-run request: definition, request, producer, selection identity and reuse policy."""
    definition: core.OperationDefinition
    request: core.Request
    producer: object
    selection_id: str
    target: core.Origin
    reuse_policy: object
    output_labels: tuple[str, ...] | None = None
    fresh: bool = False
    input_records: tuple = ()
    capture_origin: core.Origin | None = None


@dataclass(frozen=True, slots=True)
class PreparedOperation:
    """A completed producer's records and result, held in memory until publication."""
    execution: core.Execution
    result: core.Result
    records: tuple[core.CoreRecord, ...]
    upstream: tuple["PreparedOperation", ...] = ()


@dataclass(frozen=True, slots=True)
class SuspendedOperation:
    """An explicitly suspended attempt's execution identity and retained checkpoint."""
    execution_id: str
    checkpoint: core.ContentRef


class _Suspend(BaseException):
    """Internal signal that a producer stopped at an explicit recoverable boundary."""
    def __init__(self, state):
        self.state = state


class OperationContext:
    """Record actual work; declared input dependencies do not fabricate events."""

    def __init__(self, session, execution, input_records=()):
        self.session, self.execution = session, execution
        self.records = list(input_records)
        self.outputs, self.generations, self.usages, self.derivations = [], [], [], []
        self.active = True
        self._prefetched_entities = {}
        self._prefetched_bytes = 0
        self.selection = None
        self._control_bytes = sum(len(encode_record(record)) for record in self.records)

    def _reserve(self, byte_size, *, records=0):
        if len(self.records) + records > BATCH_ROWS - 4 or self._control_bytes + byte_size > BATCH_BYTES - _CONTROL_RESERVE:
            raise LimitExceededError("operation control metadata exceeds its budget; use bulk states or retained content")
        self._control_bytes += byte_size

    def _active(self):
        self.session._active()
        if not self.active:
            raise StateTransitionError("operation callback is complete")

    def generate(self, value, *, label, role="derived", entity_type="artifact", entity_id=None, happened_at=None):
        """Generate one new entity output and record its generation event and binding."""
        entity = core.Entity(format_version=1, entity_id=entity_id or _identity("entity"), entity_type=entity_type, value=value)
        if entity_id is None:
            self.session.mint(entity.entity_id)
        return self.generate_record(entity, label=label, role=role, happened_at=happened_at)

    def generate_record(self, record, *, label, role="derived", happened_at=None):
        """Generate an entity or state with one shared binding/event rule."""
        self._active()
        record, record_size = _snapshot_size(record)
        if not isinstance(record, (core.Entity, core.State)):
            raise IntegrityError("generation requires an entity or state")
        identity = record.entity_id if isinstance(record, core.Entity) else record.state_id
        binding = self._binding(identity, label, role, "new")
        event = core.EntityEvent(event_id=_identity("generation"), entity_id=identity, happened_at=happened_at)
        # Reuse Result admission for one new event before mutating the context.
        _, event_size = _snapshot_size(core.Result(format_version=1, result_id="event-check", execution_id=self.execution.execution_id,
                              outcome=core.Outcome(status="success", value="outputs", outputs=(binding,)), generations=(event,)))
        if any(item.entity_id == identity for item in self.generations):
            raise IntegrityError("duplicate generation of an entity within this operation")
        self._reserve(record_size + event_size, records=1)
        self.records.append(record)
        self.outputs.append(binding)
        self.generations.append(event)
        return record

    def adopt(self, entity_id, *, label, role="derived"):
        """Bind an already-retained entity as an adopted output without generating it."""
        self._active()
        binding = self._binding(entity_id, label, role, "adopted")
        _, size = _snapshot_size(core.Result(format_version=1, result_id="event-check", execution_id=self.execution.execution_id,
                                            outcome=core.Outcome(status="success", value="outputs", outputs=(binding,))))
        self._reserve(size)
        self.outputs.append(binding)

    def _binding(self, entity_id, label, role, production):
        """Build one output binding, refusing a label already used in this attempt."""
        value = core.ResultBinding(label=label, entity_id=entity_id, role=role, production=production)
        record_value(value, core.ResultBinding)
        if any(binding.label == label for binding in self.outputs):
            raise IntegrityError("duplicate output binding label")
        return value

    def use(self, entity_id, *, happened_at=None):
        """Record one actual usage event for a retained entity."""
        self._active()
        event = core.EntityEvent(event_id=_identity("usage"), entity_id=entity_id, happened_at=happened_at)
        _, size = _snapshot_size(core.Result(format_version=1, result_id="event-check", execution_id=self.execution.execution_id,
                              outcome=core.Outcome(status="success", value="empty"), usages=(event,)))
        self._reserve(size)
        self.usages.append(event)
        return event

    def derive(self, generated_entity_id, used_entity_id, *, generation_event_id=None, usage_event_id=None):
        """Record one derivation edge from a generated entity to a used entity."""
        self._active()
        edge = core.Derivation(generated_entity_id=generated_entity_id, used_entity_id=used_entity_id,
                               generation_event_id=generation_event_id, usage_event_id=usage_event_id)
        _, size = _snapshot_size(core.Result(format_version=1, result_id="event-check", execution_id=self.execution.execution_id,
                              outcome=core.Outcome(status="success", value="empty"), derivations=(edge,),
                              generations=tuple(event for event in self.generations if event.event_id == generation_event_id),
                              usages=tuple(event for event in self.usages if event.event_id == usage_event_id)))
        self._reserve(size)
        self.derivations.append(edge)

    def prefetch_entities(self, entity_ids):
        """Replace the bounded input read window without republishing inputs.

        Prefetching metadata does not assert usage. read_value/read_chunks keep
        recording actual reads and use the same availability and codec rules.
        """
        self._active()
        identities = tuple(dict.fromkeys(bounded_items(entity_ids, limit=BATCH_ROWS)))
        self._prefetched_entities.clear()
        self._prefetched_bytes = 0
        local = {record.entity_id for record in self.records if isinstance(record, core.Entity)}
        wanted = (identity for identity in identities if identity not in local)
        with owned_iterator(self.session.read_records(("entity", identity) for identity in wanted)) as batches:
            for batch in batches:
                for row in batch:
                    if row is None or not row.available:
                        raise IntegrityError("operation input entity is unavailable")
                    size = len(encode_record(row.value))
                    if len(self._prefetched_entities) >= BATCH_ROWS or self._prefetched_bytes + size > BATCH_BYTES:
                        raise LimitExceededError("operation input prefetch exceeds its record or byte budget")
                    self._prefetched_entities[row.value.entity_id] = row.value
                    self._prefetched_bytes += size

    def _entity(self, entity_id):
        """Return an input entity from local records, the prefetch window or the ledger, refusing unavailability."""
        self._active()
        entity = next((record for record in self.records if isinstance(record, core.Entity) and record.entity_id == entity_id), None)
        if entity is None:
            entity = self._prefetched_entities.get(entity_id)
        if entity is None:
            row = next(self.session.read_records([("entity", entity_id)]))[0]
            if row is None or not row.available:
                raise IntegrityError("operation input entity is unavailable")
            entity = row.value
        return entity

    def read_chunks(self, entity_id, *, max_bytes=None):
        """Consume opaque/JSON bytes as a closable stream with actual usage."""
        value = self._entity(entity_id).value
        if not isinstance(value, core.ContentRef):
            raise IntegrityError("streamed bytes require a retained content reference")
        reference = BlobRef(value.locator, value.digest, value.byte_size, value.media_type)
        used = False
        with owned_iterator(self.session.blobs.read(reference, max_bytes=max_bytes)) as chunks:
            for chunk in chunks:
                self._active()
                if not used:
                    self.use(entity_id)
                    used = True
                yield chunk
            if not used:
                self.use(entity_id)

    def read_value(self, entity_id):
        """Read an entity's value, recording actual use and decoding retained JSON when required."""
        entity = self._entity(entity_id)
        value = entity.value
        if isinstance(value, core.InlineValue):
            result = deepcopy(value.value)
            self.use(entity_id)
        else:
            payload = b"".join(self.read_chunks(entity_id, max_bytes=BATCH_BYTES))
            result = decode_canonical_json_value(payload, label="operation JSON input") if value.codec == "json-v1" else payload
        return result

    def suspend(self, state):
        """Stop at an explicit recoverable boundary with a JSON continuation state."""
        self._active()
        state = snapshot_json_value(state, label="continuation state")
        raise _Suspend(state)

    def result(self, outcome, *, result_id):
        """Snapshot the attempt's result record from its accumulated events."""
        return _snapshot(core.Result(format_version=1, result_id=result_id, execution_id=self.execution.execution_id,
                                     outcome=outcome, generations=tuple(self.generations), usages=tuple(self.usages),
                                     derivations=tuple(self.derivations)))


class CoreOperations:
    """Prepare, run, resume and publish direct Core operations through one publisher."""
    def __init__(self, publisher: CorePublisher):
        self.publisher, self.ledger = publisher, publisher.ledger

    @contextmanager
    def _session(self, session):
        """Enter the owning publisher session, refusing a session from another metadata owner."""
        with (self.publisher.session() if session is None else nullcontext(session)) as active:
            active._active()
            if active.ledger is not self.ledger:
                raise IntegrityError("operation session belongs to another metadata owner")
            yield active

    def prepare(self, definition, request, producer, *, input_records=(), upstream=(), capture_origin=None, session=None, selection=None, start_unit_id=None):
        """Run a fresh attempt and return its output before a publication barrier.

        Several prepared operations can pass values directly and publish together.
        Calling the producer again always receives another execution identity.
        Prepared outputs remain in memory until publish journals them.
        """
        definition, request = _snapshot(definition), _snapshot(request)
        if not isinstance(definition, core.OperationDefinition) or not isinstance(request, core.Request) or request.definition_id != definition.definition_id:
            raise IntegrityError("operation request must name its supplied definition")
        if definition.operation_kind == "capture" and capture_origin is None:
            raise IntegrityError("capture attempt requires its originating occurrence")
        execution = _snapshot(core.Execution(format_version=1, execution_id=_identity("execution"), request_id=request.request_id,
                                              capture_origin=capture_origin))
        upstream = bounded_items(upstream, limit=BATCH_ROWS)
        if any(not isinstance(item, PreparedOperation) for item in upstream):
            raise StateTransitionError("operation prerequisites must have completed their producers")
        inputs = bounded_items((record for item in upstream for record in item.records), limit=BATCH_ROWS) + bounded_items(input_records, limit=BATCH_ROWS)
        inputs = tuple(_snapshot(record) for record in bounded_items(inputs, limit=BATCH_ROWS))
        with self._session(session) as active:
            active.stage(MetadataBatch(start_unit_id or execution.execution_id + ":start", records=(definition, request, execution, *inputs),
                                       retained=(("operation_definition", definition.definition_id), ("request", request.request_id), ("execution", execution.execution_id))))
            self.ledger.record_progress(execution.execution_id + ":started", execution.execution_id, "started", {})
            context = OperationContext(active, execution, inputs)
            context.selection = selection
            return self._perform(context, producer, upstream)

    def _perform(self, context, producer, upstream=()):
        """Run one producer, returning a PreparedOperation or SuspendedOperation, or record its failure."""
        execution = context.execution
        try:
            try:
                context._reserve(0)
                outcome = producer(context)
            except _Suspend as pause:
                return self._save_checkpoint(context, upstream, pause.state)
            if outcome is None:
                outcome = core.Outcome(status="success", value="outputs" if context.outputs else "empty", outputs=tuple(context.outputs))
            if not isinstance(outcome, core.Outcome) or outcome.status != "success":
                raise IntegrityError("producer must return a successful Outcome or complete its context outputs")
            if outcome.outputs != tuple(context.outputs):
                raise IntegrityError("producer outcome differs from its recorded output bindings")
            result = context.result(outcome, result_id=execution.execution_id + ":result")
            if context.selection is not None:
                context.records.append(selection_for(result, execution.request_id, **context.selection))
            snapshots = tuple(_snapshot_size(record) for record in bounded_items(context.records, limit=BATCH_ROWS - 1))
            if sum(size for _, size in snapshots) + len(encode_record(result)) > BATCH_BYTES - _CONTROL_RESERVE:
                raise LimitExceededError("operation control metadata exceeds its publication budget")
            return PreparedOperation(execution, result, tuple(record for record, _ in snapshots), upstream)
        except BaseException as error:
            status = "failed" if isinstance(error, Exception) else "interrupted"
            try:
                failure = context.result(core.Outcome(status=status, error=_diagnostic(error), outputs=tuple(context.outputs)),
                                         result_id=execution.execution_id + ":result")
                try:
                    context.session.stage(MetadataBatch(execution.execution_id + ":failure", records=(*context.records, failure)), final=True)
                except (LimitExceededError, IntegrityError):
                    # Output payloads are not a prerequisite for recording a failed
                    # attempt, and outputs refused for their identity are not kept.
                    # The Result retains every actual event.
                    self.ledger.commit(MetadataBatch(execution.execution_id + ":failure", records=(failure,)))
                self.ledger.record_progress(execution.execution_id + ":failed", execution.execution_id, status,
                                            {"result_id": failure.result_id, "error": failure.outcome.error})
            except BaseException as recording_error:
                raise BaseExceptionGroup("Operation failed and its failure could not be recorded", [error, recording_error]) from None
            raise
        finally:
            context.active = False

    def _save_checkpoint(self, context, upstream, state):
        """Retain completed prerequisites and an explicit JSON continuation checkpoint."""
        # An explicit checkpoint retains its completed prerequisites. Ordinary
        # fused preparation still imposes no intermediate publication barrier.
        self.publish(upstream, session=context.session)
        identity = _identity("checkpoint")
        partial = context.result(core.Outcome(status="incomplete", outputs=tuple(context.outputs)), result_id=identity + ":result")
        keys = [(value["kind"], value[core.RECORD_ID_FIELDS[value["kind"]]])
                for record in context.records for value in (record_value(record),)]
        data_kinds = {"entity", "state", "state_representation", "selected_value"}
        roots = tuple(dict.fromkeys([("request", context.execution.request_id), *(key for key in keys if key[0] in data_kinds)]))
        context.session.publish(MetadataBatch(identity + ":data", records=(*context.records, partial), retained=roots))
        checkpoint = context.session.retain_value({
            "format": "docspec-operation-checkpoint", "version": 1, "execution_id": context.execution.execution_id,
            "partial_result_id": partial.result_id, "record_keys": [list(key) for key in keys],
            "roots": [list(key) for key in roots], "state": state,
            "selection": context.selection,
        })
        self.ledger.record_progress(identity, context.execution.execution_id, "incomplete", {"checkpoint": record_value(checkpoint, core.ContentRef)})
        return SuspendedOperation(context.execution.execution_id, checkpoint)

    def _record(self, key):
        with owned_iterator(self.ledger.read_records([key])) as batches:
            row = next(batches)[0]
        if row is None:
            raise IntegrityError("operation recovery requires its retained descriptions")
        return row.value

    def _read_json(self, session, content, label):
        return session.read_json(content, label=label)

    def resume(self, execution_id, continuation, *, definition, verify, session=None):
        """Continue only an explicitly suspended and verified operation frontier.

        The verifier checks the meaning of the saved continuation state. Core
        checks the pinned definition, data, provenance and exclusive progress
        claim. An uncheckpointed interruption still requires a fresh attempt.
        """
        with self._session(session) as active:
            latest = None
            for batch in self.ledger.read_progress(execution_id):
                for payload in batch:
                    latest = decode_canonical_json_value(payload, label="operation progress")
            if latest is None or latest["status"] != "incomplete" or "checkpoint" not in latest["description"]:
                raise StateTransitionError("attempt is not at an explicit suspension checkpoint")
            checkpoint = self._read_json(active, latest["description"]["checkpoint"], "operation checkpoint")
            checkpoint = recovery_document(checkpoint, "checkpoint", execution_id)
            execution = self._record(("execution", execution_id))
            request = self._record(("request", execution.request_id))
            original_definition = self._record(("operation_definition", request.definition_id))
            if encode_record(definition) != encode_record(original_definition):
                raise IntegrityError("continuation definition differs from its original operation")
            partial = self._record(("result", checkpoint["partial_result_id"]))
            if partial.execution_id != execution_id or partial.outcome.status != "incomplete":
                raise IntegrityError("checkpoint does not name its unfinished operation result")
            active.validate(MetadataBatch(_identity("checkpoint-check"), retained=tuple(tuple(key) for key in checkpoint["roots"])))
            if verify(deepcopy(checkpoint["state"])) is not True:
                raise IntegrityError("continuation state failed its required verification")
            records = [row.value for batch in self.ledger.read_records(tuple(tuple(key) for key in checkpoint["record_keys"])) for row in batch if row is not None]
            if len(records) != len(checkpoint["record_keys"]):
                raise IntegrityError("checkpoint is missing an operation record")
            self.ledger.record_progress(_identity("continuation"), execution_id, "started", {"checkpoint_update": latest["update_id"]},
                                        expected_update_id=latest["update_id"])
            context = OperationContext(active, execution, records)
            context.selection = checkpoint["selection"]
            context.outputs, context.generations = list(partial.outcome.outputs), list(partial.generations)
            context.usages, context.derivations = list(partial.usages), list(partial.derivations)
            context._control_bytes += len(encode_record(partial))
            pending = self._perform(context, lambda restored: continuation(restored, deepcopy(checkpoint["state"])))
            return pending if isinstance(pending, SuspendedOperation) else self.publish((pending,), session=active)[0]

    def publish(self, prepared, *, session=None):
        """Store completed outputs once; journal their keys before publication."""
        requested = bounded_items(prepared, limit=BATCH_ROWS)
        if not requested:
            return ()
        operations, pending = {}, list(requested)
        while pending:
            item = pending.pop()
            if not isinstance(item, PreparedOperation):
                raise StateTransitionError("only completed producers can publish results")
            identity = item.execution.execution_id
            if identity in operations:
                if operations[identity] != item:
                    raise IntegrityError("publication repeats a conflicting operation attempt")
                continue
            operations[identity] = item
            if len(operations) > BATCH_ROWS:
                raise LimitExceededError("operation publication graph exceeds 2048 attempts")
            pending.extend(item.upstream)
        executions = operation_order({identity: [item.execution.execution_id for item in operation.upstream] for identity, operation in operations.items()})
        prepared = tuple(operations[identity] for identity in executions)
        unique = {}
        for item in prepared:
            for record in (*item.records, item.result):
                value = record_value(record)
                key = value["kind"], value[core.RECORD_ID_FIELDS[value["kind"]]]
                if key in unique and canonical_value_bytes(unique[key]) != canonical_value_bytes(value):
                    raise IntegrityError("operation graph contains conflicting immutable records")
                unique[key] = value
        records = list(unique.values())
        roots = [["result", item.result.result_id] for item in prepared]
        roots.extend(["selection", value["selection_id"]] for value in records if value["kind"] == "selection")
        unit_id = "operations:" + sha256_digest(canonical_value_bytes(sorted(executions)))
        journal = {"format": "docspec-operation-publication", "version": 2, "unit_id": unit_id,
                   "executions": list(executions), "record_keys": [list(key) for key in unique], "roots": roots}
        with self._session(session) as active:
            try:
                # Staging records does not claim successful retention. The same
                # immutable records are checked and retained by the publisher.
                active.stage(MetadataBatch("prepared:" + unit_id, records=tuple(records)))
                content = active.retain_value(journal)
                for identity in executions:
                    self.ledger.record_progress(unit_id + ":prepared:" + identity, identity, "progress", {"publication": record_value(content, core.ContentRef)})
            except BaseException as error:
                self._publication_interrupted(journal, error)
                raise
            self._publish_journal(active, journal, records=records)
        return tuple(item.result for item in requested)

    def _publish_journal(self, session, journal, *, records=None):
        """Publish one completed journal, then index dependency evidence for its selected results."""
        try:
            records = publication_records(session, journal) if records is None else records
            batch = MetadataBatch(journal["unit_id"], records=tuple(records), retained=tuple(tuple(key) for key in journal["roots"]))
            session.publish(batch)
            dependencies = CoreDependencies()
            values = [record_value(record) for record in records]
            selected_results = {record["selected_result_id"] for record in values if record["kind"] == "selection"}
            dependencies.index_results(session, (record["result_id"] for record in values
                if record["kind"] == "result" and record["result_id"] in selected_results))
        except BaseException as error:
            self._publication_interrupted(journal, error)
            raise

    def _publication_interrupted(self, journal, error):
        """Record an incomplete progress entry for every execution of an interrupted publication."""
        try:
            for identity in journal["executions"]:
                self.ledger.record_progress(_identity("publication-interruption"), identity, "incomplete",
                                            {"publication_unit": journal["unit_id"], "error": _diagnostic(error)})
        except BaseException as recording_error:
            raise BaseExceptionGroup("Publication failed and its interruption could not be recorded", [error, recording_error]) from None

    def recover(self, execution_id, *, session=None):
        """Reconcile a completed producer's journal without invoking it again."""
        result = next(self.ledger.read_records([("result", execution_id + ":result")]))[0]
        if result is not None and result.retained:
            return result.value
        content = None
        for batch in self.ledger.read_progress(execution_id):
            for payload in batch:
                progress = decode_canonical_json_value(payload, label="operation progress")
                content = progress["description"].get("publication", content)
        if content is None:
            raise StateTransitionError("attempt has no completed output journal; use resume for an explicit checkpoint or start a fresh attempt")
        with self._session(session) as active:
            journal = self._read_json(active, content, "operation publication")
            journal = recovery_document(journal, "publication", execution_id)
            records = publication_records(active, journal)
            results = [record for record in records if isinstance(record, core.Result) and record.execution_id == execution_id]
            if len(results) != 1:
                raise IntegrityError("publication journal must identify one result for the requested attempt")
            self._publish_journal(active, journal, records=records)
            return results[0]

    def run(self, definition, request, producer, *, input_records=(), capture_origin=None, session=None):
        """Prepare and publish one operation inside a single session."""
        with self._session(session) as active:
            pending = self.prepare(definition, request, producer, input_records=input_records, capture_origin=capture_origin, session=active)
            return pending if isinstance(pending, SuspendedOperation) else self.publish((pending,), session=active)[0]

    def run_once(self, definition, request, producer, *, session=None):
        """Retry an exact request, recovering completed publication before work.

        This is for repeatable local producers. Work without a completed journal
        may run again in a fresh attempt; external side effects need their own
        idempotency. Failed attempts remain recorded.
        """
        definition, request = _snapshot(definition), _snapshot(request)
        with self.ledger.request_guard(request.request_id), self._session(session) as active:
            with owned_iterator(active.read_records([("request", request.request_id),
                    ("operation_definition", definition.definition_id)])) as batches:
                previous, stored_definition = next(batches)
            for stored, supplied in ((previous, request), (stored_definition, definition)):
                if stored is not None and encode_record(stored.value) != encode_record(supplied):
                    raise IntegrityError("batch ID already names different input, base, or configuration")
            with owned_iterator(self.ledger.executions(request.request_id)) as batches:
                for batch in batches:
                    for execution_id in batch:
                        with owned_iterator(active.read_records([("result", execution_id + ":result")])) as rows:
                            result = next(rows)[0]
                        if result is not None and result.retained:
                            if not result.available:
                                raise IntegrityError("prior request result is unavailable")
                            return self.recover(execution_id, session=active)
                        with owned_iterator(self.ledger.read_progress(execution_id)) as progress:
                            journaled = any("publication" in decode_canonical_json_value(payload)["description"]
                                            for group in progress for payload in group)
                        if journaled:
                            return self.recover(execution_id, session=active)
            return self.run(definition, request, producer, session=active)

    def resolve(self, definition, request, producer, *, selection_id, target, reuse_policy, output_labels=None,
                fresh=False, input_records=(), capture_origin=None, session=None):
        """Resolve one request through the same bounded bulk path."""
        call = ResolveCall(definition, request, producer, selection_id, target, reuse_policy,
                           output_labels, fresh, input_records, capture_origin)
        with owned_iterator(self.resolve_many((call,), session=session)) as results:
            return next(results)

    def resolve_many(self, calls, *, session=None):
        """Batch candidate lookup and durable publication, preserving input order.

        Producers still use prepare/publish. Completed siblings publish before a
        later producer failure escapes, so retry can reuse their exact results.
        """
        def described():
            with owned_iterator(calls) as source:
                for call in source:
                    if not isinstance(call, ResolveCall):
                        raise TypeError("bulk resolution requires ResolveCall values")
                    definition, definition_size = _snapshot_size(call.definition)
                    request, request_size = _snapshot_size(call.request)
                    reuse = ReuseRequest(definition, request, call.selection_id, call.target, call.reuse_policy, call.output_labels)
                    yield call, reuse, definition_size + request_size
        # Amortize Parquet writes across a bounded group; the byte budget and
        # generated-record limits still split unusually large operations.
        with self._session(session) as active, owned_iterator(bounded_rows(described(), size=lambda item: item[2], max_rows=128)) as groups:
            for group in groups:
                keys = (binding_key(binding) for _, wanted, _ in group for binding in wanted.request.inputs)
                with active.record_window(keys):
                    reuse = CoreReuse()
                    if len({call.selection_id for call, _, _ in group}) != len(group):
                        raise IntegrityError("bulk resolution requires distinct selection identities within a batch")
                    for call, wanted, _ in group:
                        if call.input_records:
                            active.publish(MetadataBatch("inputs:" + call.selection_id,
                                records=(wanted.definition, wanted.request, *bounded_items(call.input_records, limit=BATCH_ROWS - 2)),
                                retained=(("request", wanted.request.request_id),)))
                    eligible = tuple(wanted for call, wanted, _ in group if not call.fresh)
                    found = iter(reuse.choose_many(active, eligible)) if eligible else iter(())
                    choices = [reuse.existing(active, wanted) if call.fresh else next(found) for call, wanted, _ in group]
                    pending, pending_bytes, pending_records = [], 0, 0
                    def flush():
                        nonlocal pending_bytes, pending_records
                        if pending:
                            ready = tuple(pending)
                            pending.clear()
                            pending_bytes, pending_records = 0, 0
                            self.publish(ready, session=active)
                    try:
                        for ordinal, (call, wanted, _) in enumerate(group):
                            if choices[ordinal] is not None:
                                continue
                            intent = snapshot_json_value({"selection_id": call.selection_id,
                                "target": record_value(call.target, core.Origin),
                                "output_labels": None if call.output_labels is None else list(call.output_labels)})
                            prepared = self.prepare(wanted.definition, wanted.request, call.producer,
                                capture_origin=call.capture_origin, session=active, selection=intent)
                            if isinstance(prepared, SuspendedOperation):
                                choices[ordinal] = prepared
                                continue
                            count = len(prepared.records) + 1
                            size = sum(len(encode_record(record)) for record in (*prepared.records, prepared.result))
                            if pending and (pending_records + count > BATCH_ROWS // 2 or pending_bytes + size > BATCH_BYTES - 2 * _CONTROL_RESERVE):
                                flush()
                            pending.append(prepared)
                            pending_bytes += size
                            pending_records += count
                            choices[ordinal] = Resolution(next(record for record in prepared.records if isinstance(record, core.Selection)), prepared.result)
                        flush()
                    except BaseException:
                        # No completed producer is lost merely because a sibling failed.
                        flush()
                        raise
                    yield from choices

    def run_many(self, calls, *, map_operations=map):
        """Stream direct calls through the configured bounded worker owner."""
        with owned_iterator(calls) as source:
            with owned_iterator(map_operations(lambda arguments: self.run(**arguments), (call for call in source))) as results:
                yield from results
