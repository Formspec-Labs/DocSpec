"""One metadata unit per table-shaped state, and the dataset pointer it advances (decision 0007)."""

from docspec.application.core_dependencies import binding_key
from docspec.domain import core
from docspec.domain.identity import stable_urn
from docspec.ports.core_ledger import MetadataBatch


def table_state_unit(session, *, state_id, unit, definition, inputs, report, content, evidence=(), used=()):
    """The one unit publishing a table-shaped state with its operation's records; no per-row record.

    The request binds ``inputs`` with whole dependencies; ``evidence`` are the
    entities this unit retains for them. The result generates the state and
    its ``report`` and records a usage of every input and of each extra
    (label, identity) in ``used``, from all of which the state derives. An
    extra usage is lineage the request does not bind, so its entity stays
    removable. Every identity is ``state_id`` or one the caller minted.
    """
    execution_id = state_id + ":execution"
    request = state_request(state_id, definition, inputs)
    output = core.Entity(format_version=1, entity_id=state_id + ":report", entity_type="artifact",
                         value=core.InlineValue(value=report))
    generations = tuple(core.EntityEvent(event_id=f"{execution_id}:generation:{label}", entity_id=identity)
                        for label, identity in (("state", state_id), ("report", output.entity_id)))
    usages = tuple(core.EntityEvent(event_id=f"{execution_id}:usage:{label}", entity_id=identity)
                   for label, identity in (*((item.label, binding_key(item)[1]) for item in inputs), *used))
    result = core.Result(
        format_version=1, result_id=execution_id + ":result", execution_id=execution_id,
        outcome=core.Outcome(status="success", value="outputs", outputs=(
            core.ResultBinding(label="state", entity_id=state_id, role="derived", production="new"),
            core.ResultBinding(label="report", entity_id=output.entity_id, role="derived", production="new"))),
        generations=generations, usages=usages,
        derivations=tuple(core.Derivation(generated_entity_id=state_id, used_entity_id=usage.entity_id,
                                          generation_event_id=generations[0].event_id, usage_event_id=usage.event_id)
                          for usage in usages))
    for identity in (state_id, output.entity_id, *(entity.entity_id for entity in evidence)):
        session.mint(identity)
    records = (definition, request, core.Execution(format_version=1, execution_id=execution_id, request_id=request.request_id),
               result, *evidence, output, core.State(format_version=1, state_id=state_id),
               core.StateRepresentation(format_version=1, representation_id=state_id + ":physical", state_id=state_id,
                                        membership=content))
    return MetadataBatch(f"{state_id}:{unit}", records=records, retained=(("result", result.result_id),))


def state_request(state_id, definition, inputs):
    """The request of a table-shaped state's operation: its inputs, each with a whole dependency."""
    return core.Request(format_version=1, request_id=state_id + ":request", definition_id=definition.definition_id,
                        inputs=tuple(inputs), dependencies=tuple(core.Dependency(
                            label=item.label, binding_label=item.label, selection=core.Whole()) for item in inputs))


def advance(operations, dataset, state_id, base, *, kind):
    """Make the state current for ``dataset`` when its base still is; a retry already applied is a no-op."""
    if dataset is None or operations.ledger.current(dataset) == ("state", state_id):
        return
    update_id = stable_urn(kind, [dataset, state_id, base])
    operations.ledger.select_current(update_id, dataset, ("state", state_id), None if base is None else ("state", base))
