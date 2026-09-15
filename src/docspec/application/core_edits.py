"""Partial-value changes use the common operation and publication lifecycle."""

from docspec.application.core_execution import _identity
from docspec.domain import core
from docspec.domain.core_admission import admit_record, encode_record, record_value, validate_patch
from docspec.domain.identity import canonical_value_bytes, snapshot_json_value, sha256_digest
from docspec.domain.json_values import apply_patch
from docspec.domain.streams import bounded_items
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.record_storage import BATCH_BYTES


VALUE_EDIT_BATCH_ROWS = 256


def prepare_value_edits(operations, edits, *, upstream=(), session=None):
    """Prepare bounded independent replacements in one recorded activity.

    Each (source occurrence ID, ordered JSON Patch) pair reads its original
    source independently. Grouping changes no patch sequence or derivation;
    every replacement has its own usage, generation and qualified edge.
    """
    edits = bounded_items(edits, limit=VALUE_EDIT_BATCH_ROWS)
    if not edits:
        raise IntegrityError("value edit batch must contain at least one edit")
    sources, patches, patch_bytes = [], [], 0
    for source, instructions in edits:
        patch = snapshot_json_value(instructions, label="value edit")
        validate_patch(patch)
        patch_bytes += len(canonical_value_bytes(patch))
        if patch_bytes > BATCH_BYTES:
            raise LimitExceededError("value edit instructions exceed their byte budget")
        sources.append(source)
        patches.append(patch)
    single = len(edits) == 1
    configuration = {"patch": patches[0]} if single else {"patches": patches}
    definition = core.OperationDefinition(
        format_version=1, definition_id="urn:docspec:json-patch:" + sha256_digest(
            canonical_value_bytes(patches[0] if single else configuration)),
        implementation_id="docspec.json-patch", implementation_version="1", operation_kind="transformation",
        configuration=configuration,
    )
    inputs = tuple(core.WholeInput(label="source" if single else f"source:{index}", entity_id=source)
        for index, source in enumerate(sources))
    request = core.Request(format_version=1, request_id=_identity("request"), definition_id=definition.definition_id,
        inputs=inputs, dependencies=tuple(core.Dependency(label=item.label, binding_label=item.label, selection=core.Whole()) for item in inputs))

    def transform(context):
        context.prefetch_entities(sources)
        values, total = [], 0
        for source, patch in zip(sources, patches, strict=True):
            if context._entity(source).entity_type != "occurrence":
                raise IntegrityError("value edit source must be an occurrence")
            value = apply_patch(context.read_value(source), patch)
            total += len(canonical_value_bytes(value))
            if total > BATCH_BYTES:
                raise LimitExceededError("value edit batch output exceeds its byte budget")
            values.append((source, value, context.usages[-1].event_id))
        # Failed patch preconditions generate no partial batch outputs. The
        # common context still enforces the final records/event byte budget.
        for index, (source, value, usage_id) in enumerate(values):
            result = context.generate(core.InlineValue(value=value), label="edited" if single else f"edited:{index}", entity_type="occurrence")
            context.derive(result.entity_id, source,
                generation_event_id=context.generations[-1].event_id, usage_event_id=usage_id)

    prepared = operations.prepare(definition, request, transform, upstream=upstream, session=session)
    evidence = tuple(core.ValueEdit(source_occurrence_id=source, result_occurrence_id=output.entity_id,
        execution_id=prepared.execution.execution_id, patch=tuple(patch))
        for source, patch, output in zip(sources, patches, prepared.result.outcome.outputs, strict=True))
    return prepared, evidence


def prepare_value_edit(operations, source_occurrence_id, patch, *, upstream=(), session=None):
    """Prepare one immutable replacement through the shared batch owner."""
    prepared, evidence = prepare_value_edits(operations, ((source_occurrence_id, patch),), upstream=upstream, session=session)
    return prepared, evidence[0]


def prepare_revision(operations, revision, *, session=None):
    """Compose one explicit base and ordered edits through the common lifecycle.

    Referenced occurrences must already be retained, including the results of
    preceding value edits. A branch has one explicit base; no merge is inferred.
    """
    revision = admit_record(encode_record(revision))
    if not isinstance(revision, core.Revision):
        raise IntegrityError("state transformation requires a revision")
    with operations._session(session) as active:
        if active.states is None:
            raise IntegrityError("state transformation requires state storage")
        edits = active.states._edits(active, revision)
        identities = sorted({edit.occurrence_id for edit in edits if isinstance(edit, core.Put)})
        # A state binds a potentially large set through two metadata records;
        # per-occurrence request bindings would exceed publication's row budget.
        inputs_id = "urn:docspec:revision-inputs:" + sha256_digest(canonical_value_bytes(identities))
        active.states.from_occurrences(active, occurrence_ids=identities, state_id=inputs_id,
                                       representation_id=inputs_id + ":representation", unit_id=inputs_id + ":import")
        configuration = record_value(revision)
        definition = core.OperationDefinition(
            format_version=1, definition_id="urn:docspec:revision:" + sha256_digest(canonical_value_bytes(configuration)),
            implementation_id="docspec.keyed-revision", implementation_version="1", operation_kind="transformation",
            configuration=configuration,
        )
        inputs = (core.StateInput(label="base", state_id=revision.base_state_id), core.StateInput(label="puts", state_id=inputs_id))
        request = core.Request(format_version=1, request_id=_identity("request"), definition_id=definition.definition_id,
                               inputs=inputs, dependencies=tuple(core.Dependency(label=item.label, binding_label=item.label, selection=core.Whole()) for item in inputs))

        return operations.prepare(definition, request,
            lambda context: compose_revision(operations, context, revision, inputs_id), session=active)


def compose_revision(operations, context, revision, inputs_id):
    """Resolve and record a revision inside an existing operation attempt."""
    active = context.session
    previous_execution = None
    derivation_pairs, generated_ids, used_ids = set(), set(), set()
    def check_group(group):
        nonlocal previous_execution, derivation_pairs, generated_ids, used_ids
        prefetched = True
        try:
            context.prefetch_entities(identity for edit in group for identity in (edit.source_occurrence_id, edit.result_occurrence_id))
        except LimitExceededError:
            if len(group) > 1:
                middle = len(group) // 2
                check_group(group[:middle])
                check_group(group[middle:])
                return
            prefetched = False
        for edit in group:
            if edit.execution_id != previous_execution:
                result = operations._record(("result", edit.execution_id + ":result"))
                if result.outcome.status != "success":
                    raise IntegrityError("value edit lacks its actual transformation provenance")
                derivation_pairs = {(edge.generated_entity_id, edge.used_entity_id) for edge in result.derivations}
                generated_ids = {event.entity_id for event in result.generations}
                used_ids = {event.entity_id for event in result.usages}
                previous_execution = edit.execution_id
            if ((edit.result_occurrence_id, edit.source_occurrence_id) not in derivation_pairs
                    or edit.result_occurrence_id not in generated_ids or edit.source_occurrence_id not in used_ids):
                raise IntegrityError("value edit lacks its actual transformation provenance")
            if not prefetched:
                context.prefetch_entities((edit.source_occurrence_id,))
            expected = apply_patch(context.read_value(edit.source_occurrence_id), edit.patch)
            if not prefetched:
                context.prefetch_entities((edit.result_occurrence_id,))
            if canonical_value_bytes(expected) != canonical_value_bytes(context.read_value(edit.result_occurrence_id)):
                raise IntegrityError("value edit differs from its retained result value")
    for offset in range(0, len(revision.value_edits), VALUE_EDIT_BATCH_ROWS):
        check_group(revision.value_edits[offset:offset + VALUE_EDIT_BATCH_ROWS])
    representation = active.states.revision_representation(active, revision, representation_id=_identity("representation"),
                                                           occurrences_state_id=inputs_id)
    usages = [context.use(identity) for identity in (revision.base_state_id, inputs_id)]
    context.generate_record(core.State(format_version=1, state_id=revision.result_state_id), label="state")
    for used in usages:
        context.derive(revision.result_state_id, used.entity_id,
                       generation_event_id=context.generations[-1].event_id, usage_event_id=used.event_id)
    context.records.extend((revision, representation))
