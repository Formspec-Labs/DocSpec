"""Admit a producer generation as a table-shaped state in one metadata unit (decision 0007)."""

from dataclasses import dataclass

from docspec.domain import core
from docspec.domain.identity import require_text, stable_urn
from docspec.domain.table_rows import KeySpelling, TableIdentity, table_type
from docspec.errors import IntegrityError
from docspec.ports.core_ledger import MetadataBatch


REPORT_FORMAT = "docspec-generation-admission"


@dataclass(frozen=True, slots=True)
class GenerationAdmission:
    """The admitted state and its admission's report: pin, member, base and counts (ruling R1(b))."""

    state_id: str
    report: dict


def admit_generation(operations, generation, *, family, table, dataset=None):
    """Publish a staged, Rulespec-admitted generation as a table-shaped state.

    The state ID digests the pin, family, logical table and identity rules, so
    re-admitting a pin returns its state without new work. With ``dataset``
    the dataset's current state is the base: unchanged rows keep their
    occurrences, and the current pointer advances with the stale-base check.
    One unit publishes the state, its representation and the admission's
    definition, request, execution and result. The request binds the
    generation's root and member manifest, retained as exact bytes; the result
    generates the state and a report whose counts separate occurrences
    generated here from those the dataset's index already held. No per-row
    ledger record is written.
    """
    for value, label in ((family, "table family"), (table, "logical table")):
        require_text(value, label)
    if dataset is not None:
        require_text(dataset, "dataset")
    try:
        identity = TableIdentity(family, table, KeySpelling(generation.key_spelling_id, generation.key_spelling_version,
                                                            generation.key_fields),
                                 tuple((name, table_type(kind)) for name, kind in generation.columns))
    except ValueError as error:
        raise IntegrityError(f"generation table cannot take table-row identity: {error}") from error
    pin = generation.pin
    rules = identity.to_dict()
    state_id = stable_urn("generation-admission", [pin.logical_id, pin.artifact_digest, family, table, rules])
    report_id = state_id + ":report"
    with operations.publisher.session() as session:
        if next(session.read_records([("state", state_id)]))[0] is not None:
            existing = next(session.read_records([("state", state_id), ("entity", report_id)]))
            if any(row is None or not row.available for row in existing):
                raise IntegrityError("this generation's state was removed; it cannot be admitted again")
            report = existing[1].value.value.value
            _advance(operations, dataset, state_id, report["base"])
            return GenerationAdmission(state_id, report)
        current = None if dataset is None else operations.ledger.current(dataset)
        if current is not None and current[0] != "state":
            raise IntegrityError("the dataset's current target is not a state")
        base = None if current is None else current[1]
        content, counts = session.states.admit_table(session, generation.path, identity, generation.columns,
                                                     member_digest=generation.member.sha256, state_id=state_id,
                                                     base_state_id=base)
        report = {"format": REPORT_FORMAT, "version": 1,
                  "pin": {"logicalId": pin.logical_id, "artifactDigest": pin.artifact_digest},
                  "member": {"objectKey": generation.member.object_key, "sha256": generation.member.sha256,
                             "byteSize": generation.member.byte_size, "recordCount": generation.record_count},
                  "dataset": dataset, "base": base, "counts": counts}
        session.publish(_unit(session, identity, generation, state_id, report, content),
                        identity_check=lambda: session.states.table_identity_check(session, content))
    _advance(operations, dataset, state_id, base)
    return GenerationAdmission(state_id, report)


def _unit(session, identity, generation, state_id, report, content):
    """The one metadata unit of an admission; every identity is a digest of the admitted evidence."""
    evidence = []
    for label, payload in (("root", generation.root_bytes), ("members", generation.manifest_bytes)):
        stored = session.retain_bytes([payload], media_type="application/json")
        evidence.append(core.Entity(format_version=1, entity_id=stable_urn("generation-" + label, stored.digest),
                                    entity_type="artifact", value=stored))
    configuration = {"family": identity.family, "table": identity.table, "rules": identity.to_dict()}
    definition = core.OperationDefinition(
        format_version=1, definition_id=stable_urn("generation-admission-definition", configuration),
        implementation_id="docspec.admit-generation", implementation_version="1", operation_kind="transformation",
        configuration=configuration)
    inputs = tuple(core.WholeInput(label=label, entity_id=entity.entity_id)
                   for label, entity in zip(("root", "members"), evidence, strict=True))
    request = core.Request(format_version=1, request_id=state_id + ":request", definition_id=definition.definition_id,
                           inputs=inputs, dependencies=tuple(core.Dependency(label=item.label, binding_label=item.label,
                                                                             selection=core.Whole()) for item in inputs))
    execution_id = state_id + ":execution"
    output = core.Entity(format_version=1, entity_id=state_id + ":report", entity_type="artifact",
                         value=core.InlineValue(value=report))
    generations = tuple(core.EntityEvent(event_id=f"{execution_id}:generation:{label}", entity_id=identity_id)
                        for label, identity_id in (("state", state_id), ("report", output.entity_id)))
    usages = tuple(core.EntityEvent(event_id=f"{execution_id}:usage:{item.label}", entity_id=item.entity_id)
                   for item in inputs)
    result = core.Result(
        format_version=1, result_id=execution_id + ":result", execution_id=execution_id,
        outcome=core.Outcome(status="success", value="outputs", outputs=(
            core.ResultBinding(label="state", entity_id=state_id, role="derived", production="new"),
            core.ResultBinding(label="report", entity_id=output.entity_id, role="derived", production="new"))),
        generations=generations, usages=usages,
        derivations=tuple(core.Derivation(generated_entity_id=state_id, used_entity_id=usage.entity_id,
                                          generation_event_id=generations[0].event_id, usage_event_id=usage.event_id)
                          for usage in usages))
    for identity_id in (state_id, output.entity_id, *(entity.entity_id for entity in evidence)):
        session.mint(identity_id)
    records = (definition, request, core.Execution(format_version=1, execution_id=execution_id, request_id=request.request_id),
               result, *evidence, output, core.State(format_version=1, state_id=state_id),
               core.StateRepresentation(format_version=1, representation_id=state_id + ":physical", state_id=state_id,
                                        membership=content))
    return MetadataBatch(state_id + ":admission", records=records, retained=(("result", result.result_id),))


def _advance(operations, dataset, state_id, base):
    """Make the state current for ``dataset`` when its admission's base still is; a retry already applied is a no-op."""
    if dataset is None or operations.ledger.current(dataset) == ("state", state_id):
        return
    update_id = stable_urn("generation-current", [dataset, state_id, base])
    operations.ledger.select_current(update_id, dataset, ("state", state_id), None if base is None else ("state", base))
