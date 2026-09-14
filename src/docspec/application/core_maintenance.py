"""Guarded selection and explicit, resumable removal of Core retained bytes."""

from contextlib import closing
import sqlite3
import tempfile

from docspec.domain import core
from docspec.domain.core_admission import admit_record, record_value
from docspec.domain.core_recovery import recovery_document
from docspec.domain.identity import canonical_value_bytes, decode_canonical_json_value
from docspec.domain.references import BlobRef
from docspec.domain.streams import bounded_items, owned_iterator
from docspec.errors import IntegrityError
from docspec.ports.core_ledger import MetadataBatch, RemovalContent, RemovalOutcome
from docspec.ports.record_storage import BATCH_BYTES, BATCH_ROWS


def _blob(content):
    return BlobRef(content["locator"], content["digest"], content["byte_size"], content["media_type"])


class _PhysicalIndex:
    """Disposable physical reachability, never a second logical metadata graph."""

    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="docspec-removal-")
        self.connection = sqlite3.connect(self.directory.name + "/references.sqlite")
        self.connection.execute("CREATE TABLE refs(store TEXT,locator TEXT,reference BLOB,candidate INTEGER,protected INTEGER,PRIMARY KEY(store,locator)) WITHOUT ROWID")

    def close(self):
        self.connection.close()
        self.directory.cleanup()

    def add(self, content, *, protected):
        payload = canonical_value_bytes(content.reference.to_dict())
        previous = self.connection.execute("SELECT reference FROM refs WHERE store=? AND locator=?", (content.store, content.reference.locator)).fetchone()
        if previous is not None:
            old = BlobRef.from_dict(decode_canonical_json_value(previous[0], label="physical reference"))
            if (old.digest, old.byte_size) != (content.reference.digest, content.reference.byte_size):
                raise IntegrityError("physical references disagree about immutable bytes")
        self.connection.execute("INSERT INTO refs VALUES (?,?,?,?,?) ON CONFLICT(store,locator) DO UPDATE SET candidate=max(candidate,excluded.candidate),protected=max(protected,excluded.protected)",
                                (content.store, content.reference.locator, payload, int(not protected), int(protected)))

    def protected(self, content):
        row = self.connection.execute("SELECT protected FROM refs WHERE store=? AND locator=?", (content.store, content.reference.locator)).fetchone()
        return row is not None and bool(row[0])

    def candidates(self):
        for store, payload in self.connection.execute("SELECT store,reference FROM refs WHERE candidate=1 ORDER BY store,locator"):
            yield RemovalContent(store, BlobRef.from_dict(decode_canonical_json_value(payload, label="physical reference")))


class CoreMaintenance:
    def __init__(self, publisher, records):
        self.publisher, self.records = publisher, records
        self.ledger, self.blobs = publisher.ledger, publisher.blobs

    def select_current(self, update_id, dataset, target, expected_current):
        with self.publisher.session() as session:
            if not self.ledger.is_committed(update_id):
                session.validate(MetadataBatch(update_id, retained=(target,)))
            return self.ledger.select_current(update_id, dataset, target, expected_current)

    def _policy(self, policy_id, keys, *, orphan_content):
        with owned_iterator(self.ledger.read_records([("retention_policy", policy_id)])) as batches:
            policy = next(batches)[0]
        if policy is None or not policy.available or not isinstance(policy.value, core.RetentionPolicy):
            raise IntegrityError("removal requires an available retained policy")
        description = policy.value.description
        if (set(description) != {"remove", "collect_unreferenced"} or not isinstance(description["remove"], list)
                or type(description["collect_unreferenced"]) is not bool):
            raise IntegrityError("unsupported retention policy: expected remove keys and collect_unreferenced flag")
        try:
            allowed = {tuple(key) for key in description["remove"]}
        except TypeError as error:
            raise IntegrityError("retention policy has invalid removal keys") from error
        if any(len(key) != 2 or key[0] not in core.RECORD_ID_FIELDS or not isinstance(key[1], str) or not key[1] for key in allowed):
            raise IntegrityError("retention policy has invalid removal keys")
        if not set(keys) <= allowed or (orphan_content and not description["collect_unreferenced"]):
            raise IntegrityError("retention policy does not authorize the requested scope")
        if ("retention_policy", policy_id) in keys:
            raise IntegrityError("removal must preserve its authorizing policy")

    def _json(self, reference):
        with owned_iterator(self.blobs.read(reference, max_bytes=BATCH_BYTES)) as chunks:
            return decode_canonical_json_value(b"".join(chunks), label="retained physical manifest")

    def inventory_layer(self, index, reference, *, protected, scanned):
        identity = reference.layer_id, protected
        if identity in scanned:
            return
        scanned.add(identity)
        for item in self.records.physical_references(reference):
            index.add(RemovalContent("records", item), protected=protected)

    def inventory_record(self, index, value, *, protected, scanned, entity_targets):
        raw = record_value(value)
        content = raw.get("membership" if isinstance(value, core.StateRepresentation) else "edits" if isinstance(value, core.Revision) else "value")
        if not isinstance(content, dict) or content.get("kind") != "content":
            if isinstance(value, core.StateRepresentation) and protected and any(member.occurrence_id in entity_targets for member in value.membership):
                raise IntegrityError("retained state membership requires an occurrence in the removal scope")
            return
        reference = _blob(content)
        index.add(RemovalContent("blobs", reference), protected=protected)
        if isinstance(value, core.StateRepresentation):
            manifest = self._json(reference)
            layers = self.publisher.states._references(manifest)
            if protected and entity_targets:
                import pyarrow as pa
                targets = pa.table({"target_id": sorted(entity_targets)})
                with self.records.relations({"members": layers["membership"]}, tables={"targets": targets}) as relations:
                    members = relations["members"].project("json_extract_string(decode(record_json), '/occurrence_id') AS occurrence_id")
                    if members.join(relations["targets"], "occurrence_id=target_id", how="semi").limit(1).fetchone():
                        raise IntegrityError("retained state membership requires an occurrence in the removal scope")
            for layer in layers.values():
                self.inventory_layer(index, layer, protected=protected, scanned=scanned)
        elif isinstance(value, core.SelectedValue) and isinstance(value.definition, core.StateMembers):
            manifest = self._json(reference)
            layer = self.publisher.selections._reference(manifest)
            self.inventory_layer(index, layer, protected=protected, scanned=scanned)
            # The selected-value owner supplies the same blob obligations as publication.
            with closing(self.publisher.selections.content_references(layer)) as contents:
                for content in contents:
                    index.add(RemovalContent("blobs", _blob(record_value(content, core.ContentRef))), protected=protected)

    def _inventory(self, index, *, exclude=(), candidates=False):
        excluded, scanned = set(exclude), set()
        entity_targets = {key[1] for key in excluded if key[0] == "entity"}
        if candidates:
            with owned_iterator(self.ledger.source_layers(include=excluded)) as batches:
                for batch in batches:
                    for layer in batch:
                        self.inventory_layer(index, layer, protected=False, scanned=scanned)
        with owned_iterator(self.ledger.source_layers(exclude=excluded)) as batches:
            for batch in batches:
                for layer in batch:
                    self.inventory_layer(index, layer, protected=True, scanned=scanned)
        with owned_iterator(self.ledger.retained_records()) as batches:
            for batch in batches:
                for row in batch:
                    if row.available and (candidates or row.key not in excluded):
                        self.inventory_record(index, row.value, protected=row.key not in excluded,
                                             scanned=scanned, entity_targets=entity_targets)
        self.inventory_recovery(index, exclude=excluded, scanned=scanned)

    def inventory_recovery(self, index, *, exclude=(), scanned=None, execution_ids=None):
        """Add only journals/frontiers that their execution owner can recover."""
        excluded = set(exclude)
        scanned = set() if scanned is None else scanned
        entity_targets = {key[1] for key in excluded if key[0] == "entity"}
        execution_ids = None if execution_ids is None else set(execution_ids)
        with self.publisher.session() as session, owned_iterator(self.ledger.recovery_progress(exclude=excluded)) as batches:
            seen = set()
            for batch in batches:
                for kind, payload in batch:
                    progress = decode_canonical_json_value(payload, label="operation progress")
                    if execution_ids is not None and progress["execution_id"] not in execution_ids:
                        continue
                    content = record_value(progress["description"][kind], core.ContentRef)
                    reference = _blob(content)
                    index.add(RemovalContent("blobs", reference), protected=True)
                    if (kind, reference.digest) in seen:
                        continue
                    seen.add((kind, reference.digest))
                    document = recovery_document(session.read_json(content, label="operation " + kind), kind, progress["execution_id"])
                    staged = tuple(admit_record(canonical_value_bytes(record)) for record in document.get("records", ()))
                    required = session.validate(MetadataBatch("recovery-inventory", records=staged,
                                                          retained=tuple(tuple(key) for key in document["roots"])))
                    if excluded.intersection(required):
                        raise IntegrityError("recoverable operation requires records in the removal scope")
                    for record in staged:
                        self.inventory_record(index, record, protected=True, scanned=scanned, entity_targets=entity_targets)

    def remove_under_policy(self, update_id, policy_id, keys=(), *, orphan_content=()):
        """Authorize exact keys; retain shared bytes, journal each physical outcome.

        Policy description is {remove: [[kind, id], ...], collect_unreferenced:
        bool}. Orphans are explicit backend references, never an implicit scan.
        Current states must be switched before their removal. Call resume after
        interruption; metadata, policy and per-file outcomes remain inspectable.
        """
        keys = tuple(sorted(set(bounded_items(keys, limit=BATCH_ROWS))))
        orphans = bounded_items(orphan_content, limit=BATCH_ROWS)
        with self.ledger.content_guard(exclusive=True):
            existing = self.ledger.removal(update_id)
            if existing is not None:
                if existing[:2] != (policy_id, keys):
                    raise IntegrityError("removal retry differs from its retained authorization")
                if orphans:
                    with owned_iterator(self.ledger.removal_outcomes(update_id)) as batches:
                        saved = {item.content for batch in batches for item in batch}
                    if not set(orphans) <= saved:
                        raise IntegrityError("removal retry changes its physical targets")
                return self.resume(update_id)
            self._policy(policy_id, keys, orphan_content=bool(orphans))
            with owned_iterator(self.ledger.read_records(keys)) as batches:
                if any(row is None or not row.retained for batch in batches for row in batch):
                    raise IntegrityError("removal targets must have retained historical records")
            with owned_iterator(self.ledger.removal_blockers(keys)) as batches:
                if any(batch for batch in batches):
                    raise IntegrityError("current selection or retained commitments outside the policy scope require these records")
            with closing(_PhysicalIndex()) as index:
                self._inventory(index, exclude=keys, candidates=True)
                for content in orphans:
                    if content.store not in {"blobs", "records"}:
                        raise IntegrityError("orphan content names an unsupported store")
                    if index.protected(content):
                        raise IntegrityError("explicit orphan target is required by a retained commitment")
                    index.add(content, protected=False)
                self.ledger.begin_removal(update_id, policy_id, keys, content=index.candidates())
                return self._delete(update_id, index)

    def resume(self, update_id):
        with self.ledger.content_guard(exclusive=True):
            intent = self.ledger.removal(update_id)
            if intent is None:
                raise IntegrityError("removal has no retained policy intent")
            if not intent[2]:
                with closing(_PhysicalIndex()) as index:
                    self._inventory(index)
                    return self._delete(update_id, index)
            return self._counts(update_id)

    def _delete(self, update_id, index):
        with owned_iterator(self.ledger.removal_outcomes(update_id, pending_only=True)) as batches:
            for batch in batches:
                for item in batch:
                    try:
                        if index.protected(item.content):
                            status = "retained"
                        else:
                            store = self.blobs if item.content.store == "blobs" else self.records
                            status = "deleted" if store.delete(item.content.reference) else "absent"
                    except Exception as error:
                        self.ledger.record_removal_outcome(update_id, RemovalOutcome(item.content, "failed", str(error) or type(error).__name__))
                        raise
                    self.ledger.record_removal_outcome(update_id, RemovalOutcome(item.content, status))
        self.ledger.finish_removal(update_id)
        return self._counts(update_id)

    def _counts(self, update_id):
        counts = {}
        with owned_iterator(self.ledger.removal_outcomes(update_id)) as batches:
            for batch in batches:
                for outcome in batch:
                    counts[outcome.status] = counts.get(outcome.status, 0) + 1
        return counts
