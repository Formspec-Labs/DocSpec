"""Admit a producer generation as a table-shaped state in one metadata unit (decision 0007)."""

from dataclasses import dataclass

from docspec.application.table_units import advance, table_state_unit
from docspec.domain import core
from docspec.domain.identity import require_text, stable_urn
from docspec.domain.table_rows import TableIdentity, volatile_columns
from docspec.errors import IntegrityError


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
    generated here from those the dataset's index already held. A table
    published as several members is one state over all of them, with the same
    identity rules and occurrences as its rows published as one file. No
    per-row ledger record is written.
    """
    for value, label in ((family, "table family"), (table, "logical table")):
        require_text(value, label)
    if dataset is not None:
        require_text(dataset, "dataset")
    volatile = volatile_columns(name for name, _ in generation.columns)
    if volatile:
        raise IntegrityError(f"table carries observation-time columns {list(volatile)}: ruling R5 admits it only "
                             "through a declared projection without them, which does not exist yet")
    try:
        identity = TableIdentity(family, table, generation.key, generation.columns)
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
            advance(operations, dataset, state_id, report["base"], kind="generation-current")
            return GenerationAdmission(state_id, report)
        current = None if dataset is None else operations.ledger.current(dataset)
        if current is not None and current[0] != "state":
            raise IntegrityError("the dataset's current target is not a state")
        base = None if current is None else current[1]
        with session.states.admit_table(session, [(path, member.sha256) for path, member in generation.members],
                                        identity, generation.columns, state_id=state_id,
                                        base_state_id=base) as (content, counts, identity_check):
            members = [{"objectKey": member.object_key, "sha256": member.sha256, "byteSize": member.byte_size,
                        "recordCount": member.record_count} for _, member in generation.members]
            # A single-file table's report is unchanged; a split table names every member and its partitioning.
            shape = ({"members": members, "partitionColumns": list(generation.partition_columns)}
                     if generation.partition_columns else {"member": members[0]})
            receipt_members = [{"objectKey": member.object_key, "sha256": member.sha256, "byteSize": member.byte_size,
                                "recordCount": member.record_count} for _, member in generation.receipts]
            report = {"format": REPORT_FORMAT, "version": 1,
                      "pin": {"logicalId": pin.logical_id, "artifactDigest": pin.artifact_digest},
                      **shape, **({"etlReceipts": receipt_members} if receipt_members else {}), "dataset": dataset, "base": base, "counts": counts}
            session.publish(_unit(session, identity, generation, state_id, report, content), identity_check=identity_check)
    advance(operations, dataset, state_id, base, kind="generation-current")
    return GenerationAdmission(state_id, report)


def _unit(session, identity, generation, state_id, report, content):
    """The one metadata unit of an admission; every identity is a digest of the admitted evidence."""
    evidence = []
    for label, payload in (("root", generation.root_bytes), ("members", generation.manifest_bytes)):
        stored = session.retain_bytes([payload], media_type="application/json")
        evidence.append(core.Entity(format_version=1, entity_id=stable_urn("generation-" + label, stored.digest),
                                    entity_type="artifact", value=stored))
    labels = ["root", "members"]
    for index, (path, member) in enumerate(generation.receipts):
        with path.open("rb") as stream:
            stored = session.retain_bytes(iter(lambda: stream.read(1024 * 1024), b""), media_type=member.media_type)
        if stored.digest != member.sha256:
            raise IntegrityError("Receipt bytes changed after generation admission")
        evidence.append(core.Entity(format_version=1, entity_id=stable_urn("generation-etl-receipts", stored.digest),
                                    entity_type="artifact", value=stored))
        labels.append(f"etl-receipts-{index}")
    configuration = {"family": identity.family, "table": identity.table, "rules": identity.to_dict()}
    definition = core.OperationDefinition(
        format_version=1, definition_id=stable_urn("generation-admission-definition", configuration),
        implementation_id="docspec.admit-generation", implementation_version="1", operation_kind="transformation",
        configuration=configuration)
    inputs = tuple(core.WholeInput(label=label, entity_id=entity.entity_id)
                   for label, entity in zip(labels, evidence, strict=True))
    return table_state_unit(session, state_id=state_id, unit="admission", definition=definition, inputs=inputs,
                            report=report, content=content, evidence=evidence)
