"""Admit a producer generation as a table-shaped state in one metadata unit (decision 0007)."""

from dataclasses import dataclass

from docspec.application.table_units import advance, state_request, table_state_unit
from docspec.domain import core
from docspec.domain.identity import require_sha256, require_text, sha256_digest, stable_urn
from docspec.domain.references import BlobRef
from docspec.domain.table_rows import TableIdentity, volatile_columns
from docspec.errors import IntegrityError, StaleBaseError


REPORT_FORMAT = "docspec-generation-admission"
REPORT_VERSION = 2
_RETRY = "generation appeared during admission; retry from source metadata"


@dataclass(frozen=True, slots=True)
class GenerationAdmission:
    """The admitted state and its admission's report: pin, member, base and counts (ruling R1(b))."""

    state_id: str
    report: dict


def _published(session, state_id):
    return next(session.read_records([("state", state_id)]))[0] is not None


def _identity(generation, family, table):
    return TableIdentity(family, table, generation.key, generation.columns)


def _state_id(generation, identity):
    return stable_urn("generation-admission", [generation.pin.logical_id, generation.pin.artifact_digest,
                      identity.family, identity.table, identity.to_dict()])


def _definition(identity):
    configuration = {"family": identity.family, "table": identity.table, "rules": identity.to_dict()}
    return core.OperationDefinition(
        format_version=1, definition_id=stable_urn("generation-admission-definition", configuration),
        implementation_id="docspec.admit-generation", implementation_version="1", operation_kind="transformation",
        configuration=configuration)


def retained_generation(operations, generation, *, family, table, dataset, validator, verify_state):
    """Reuse only this workspace's complete owner admission over freshly checked retained bytes.

    Unsupported reports and changed validators refuse; a fresh workspace
    destination is required. Immutable reports are never upgraded. Source metadata remains
    untrusted until its exact bytes match the retained request inputs.
    """
    identity = _identity(generation, family, table)
    state_id = _state_id(generation, identity)
    with operations.publisher.session() as session:
        def available(kind, key):
            row = next(session.read_records([(kind, key)]))[0]
            if row is None or not row.retained or not row.available:
                if (kind, key) in (("state", state_id), ("entity", state_id + ":report")):
                    raise IntegrityError("this generation's state was removed; it cannot be admitted again")
                raise IntegrityError("retained generation admission metadata is unavailable")
            return row.value

        if not _published(session, state_id):
            return None
        available("state", state_id)
        stored = available("entity", state_id + ":report").value
        if not isinstance(stored, core.InlineValue):
            raise IntegrityError("retained generation admission report differs")
        report = stored.value
        if (not isinstance(report, dict) or report.get("format") != REPORT_FORMAT
                or report.get("version") != REPORT_VERSION or report.get("validator") != validator):
            raise IntegrityError("unsupported retained generation admission format or validator; "
                                 "admit into a new workspace destination")
        definition = _definition(identity)
        if available("operation_definition", definition.definition_id) != definition:
            raise IntegrityError("retained generation admission definition differs")
        request = available("request", state_id + ":request")
        inputs = []
        expected = [("root", "generation-root", sha256_digest(generation.root_bytes), generation.root_bytes),
                    ("members", "generation-members", sha256_digest(generation.manifest_bytes), generation.manifest_bytes)]
        expected.extend((f"etl-receipts-{index}", "generation-etl-receipts", member.sha256, None)
                        for index, (_, member) in enumerate(generation.receipts))
        for label, kind, digest, payload in expected:
            entity_id = stable_urn(kind, digest)
            entity = available("entity", entity_id)
            if not isinstance(entity.value, core.ContentRef) or entity.value.digest != digest:
                raise IntegrityError("retained generation evidence differs")
            content = entity.value
            chunks = session.blobs.read(BlobRef(content.locator, content.digest, content.byte_size, content.media_type))
            if payload is None:
                for _ in chunks:
                    pass
            elif b"".join(chunks) != payload:
                raise IntegrityError("generation metadata differs from retained admission evidence")
            inputs.append(core.WholeInput(label=label, entity_id=entity_id))
        if request != state_request(state_id, definition, tuple(inputs)):
            raise IntegrityError("retained generation admission request differs")
        execution_id = state_id + ":execution"
        execution = available("execution", execution_id)
        result_id = execution_id + ":result"
        result = available("result", result_id)
        outputs = tuple(core.ResultBinding(label=label, entity_id=key, role="derived", production="new")
                        for label, key in (("state", state_id), ("report", state_id + ":report")))
        if (execution.request_id != request.request_id or result.execution_id != execution_id
                or result.outcome != core.Outcome(status="success", value="outputs", outputs=outputs)
                or operations.ledger.producers(("state", state_id)) != (result_id,)
                or operations.ledger.producers(("entity", state_id + ":report")) != (result_id,)):
            raise IntegrityError("retained generation requires its completed owner admission")
        pin = generation.pin
        if report.get("pin") != {"logicalId": pin.logical_id, "artifactDigest": pin.artifact_digest}:
            raise IntegrityError("retained generation report binding differs")
        verify_state(state_id)
        advance(operations, dataset, state_id, report["base"], kind="generation-current")
        return GenerationAdmission(state_id, report)


def admit_generation(operations, generation, *, family, table, validator, dataset=None):
    """Publish a staged, Rulespec-admitted generation as a table-shaped state.

    The state ID digests the pin, family, logical table and identity rules.
    Another admission of the same state may commit first: a state that exists
    when this session opens raises ``StaleBaseError``, and so does a failure of
    the publish itself while the state now exists (a collision with that
    competitor; its staged record files stay behind unreferenced, as for any
    failed publish). Either way the caller retries from source metadata, and
    ``CoreWorkspace.admit_generation`` then reuses the committed admission.
    Every other refusal, including one raised before publishing, stands even
    if the state exists afterwards, so a retry loop cannot mask it.
    With ``dataset`` the dataset's current state is the base: unchanged rows
    keep their occurrences, and the current pointer advances with the
    stale-base check.
    One unit publishes the state, its representation and the admission's
    definition, request, execution and result. The request binds the
    generation's root and member manifest, retained as exact bytes; the result
    generates the state and a report whose counts separate occurrences
    generated here from those the dataset's index already held. A table
    published as several members is one state over all of them, with the same
    identity rules and occurrences as its rows published as one file. No
    per-row ledger record is written.
    """
    require_sha256(validator)
    for value, label in ((family, "table family"), (table, "logical table")):
        require_text(value, label)
    if dataset is not None:
        require_text(dataset, "dataset")
    volatile = volatile_columns(name for name, _ in generation.columns)
    if volatile:
        raise IntegrityError(f"table carries observation-time columns {list(volatile)}: ruling R5 admits it only "
                             "through a declared projection without them, which does not exist yet")
    try:
        identity = _identity(generation, family, table)
    except ValueError as error:
        raise IntegrityError(f"generation table cannot take table-row identity: {error}") from error
    pin = generation.pin
    state_id = _state_id(generation, identity)
    publishing = False
    try:
        with operations.publisher.session() as session:
            if _published(session, state_id):
                raise StaleBaseError(_RETRY)
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
                report = {"format": REPORT_FORMAT, "version": REPORT_VERSION,
                          "pin": {"logicalId": pin.logical_id, "artifactDigest": pin.artifact_digest},
                          **shape, **({"etlReceipts": receipt_members} if receipt_members else {}), "dataset": dataset,
                          "base": base, "counts": counts, "validator": validator}
                unit = _unit(session, identity, generation, state_id, report, content)
                publishing = True
                session.publish(unit, identity_check=identity_check)
                publishing = False
    except IntegrityError as error:
        # Only a failed publish is a collision rather than a verdict, and only when a competing admission committed this state.
        if publishing:
            with operations.publisher.session() as session:
                if _published(session, state_id):
                    raise StaleBaseError(_RETRY) from error
        raise
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
    definition = _definition(identity)
    inputs = tuple(core.WholeInput(label=label, entity_id=entity.entity_id)
                   for label, entity in zip(labels, evidence, strict=True))
    return table_state_unit(session, state_id=state_id, unit="admission", definition=definition, inputs=inputs,
                            report=report, content=content, evidence=evidence)
