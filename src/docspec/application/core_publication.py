"""One protected publisher for Core results, associations and imported data."""

from contextlib import contextmanager
from dataclasses import replace
import re

from docspec.application.core_dependencies import CoreDependencies, corresponds
from docspec.domain import core
from docspec.domain.core_admission import AdmittedRecord, admit_record, record_parts, record_value
from docspec.domain.identity import canonical_value_bytes, decode_canonical_json_value, sha256_digest
from docspec.domain.references import BlobRef
from docspec.domain.selected_values import validate_fields_value
from docspec.domain.streams import bounded_items, owned_iterator
from docspec.errors import IntegrityError, LimitExceededError, StaleBaseError, StateTransitionError
from docspec.ports.blob_store import BlobStore
from docspec.ports.core_ledger import CoreLedger, MetadataBatch, MetadataLink, StoredRecord
from docspec.ports.record_storage import BATCH_BYTES, BATCH_ROWS, bounded_rows


_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
# Identities DocSpec mints for its own outputs: fresh generated entities and
# execution-scoped states (core_execution._identity), and digest-scoped derive
# rows and revision inputs. Publishing one never searches the bulk layers for a
# colliding caller identity, so document runs and derive stay flat as layers
# accumulate; every other entity or state identity is checked.
_MINTED = re.compile(rf"urn:docspec:(?:entity:{_UUID}|execution:{_UUID}:.+|core-(?:derive|upsert)-rows:v1:[0-9a-f]{{64}}"
                     r"|revision-inputs:sha256:[0-9a-f]{64})")


class CorePublisher:
    """Own the content guard and create one disposable Publication session per consumer."""
    def __init__(self, ledger: CoreLedger, blobs: BlobStore, *, states=None, selections=None) -> None:
        self.ledger, self.blobs = ledger, blobs
        self.states = states
        self.selections = selections

    @contextmanager
    def session(self):
        """Enter before producing new content; keep protection through publication."""
        with self.ledger.content_guard():
            session = Publication(self.ledger, self.blobs, states=self.states, selections=self.selections)
            try:
                yield session
            finally:
                session.active = False
                for plan in session.member_selections.values():
                    plan.close()


class Publication:
    """One guarded session that retains, validates and commits Core records and bytes."""
    pending_keys = frozenset()

    def __init__(self, ledger: CoreLedger, blobs: BlobStore, *, states=None, selections=None) -> None:
        self.ledger, self.blobs = ledger, blobs
        self.active = True
        self.ready = {}
        self.states, self.ready_states = states, {}
        self.selections, self.ready_selections = selections, {}
        self.computed_records = {}
        self.member_selections = {}
        self.membership_equivalence = set()
        # Bulk members read through their layers (identity -> admitted bytes and
        # layer) and identities searched without a match, both bounded like the
        # record window, and the admitted layers searched (newest first).
        self.bulk_members, self.bulk_member_bytes, self.bulk_misses, self.entity_layers = {}, 0, {}, None
        self._generated_row_observer = None
        self._record_window = None

    @contextmanager
    def observe_generated_rows(self, observer):
        """Scope a producer's row accounting without charging retained reads."""
        self._active()
        previous = self._generated_row_observer
        self._generated_row_observer = observer
        try:
            yield
        finally:
            self._generated_row_observer = previous

    def generated_row(self):
        if self._generated_row_observer is not None:
            self._generated_row_observer()

    def _active(self):
        if not self.active:
            raise StateTransitionError("publication session is closed")

    @contextmanager
    def record_window(self, keys=()):
        """Reuse bounded immutable payloads; availability and versions stay live.

        The window belongs to one operation batch, never the full session. Every
        read still consults the ledger metadata. Only admitted record bytes are
        reused, and detached values keep producer callbacks outside the cache.
        """
        self._active()
        previous = self._record_window
        self._record_window = _RecordWindow()
        try:
            with owned_iterator(self.read_records(keys)) as batches:
                for _ in batches:
                    pass
            yield
        finally:
            self._record_window = previous

    def read_records(self, keys, *, include_values=True, include_unavailable_values=False):
        """Read ledger rows; an entity with no row resolves through its bulk layer as retained and available.

        Bulk state members have a ledger row only once a publication pins them.
        A call that also names the same identity as a state resolves that
        identity itself; the fallback would only repeat the layer search.
        """
        keys = list(keys)
        states, position = {identity for kind, identity in keys if kind == "state"}, 0
        with owned_iterator(self._read_records(keys, include_values=include_values,
                                               include_unavailable_values=include_unavailable_values)) as batches:
            for rows in batches:
                wanted = keys[position:position + len(rows)]
                position += len(rows)
                missing = [index for index, (key, row) in enumerate(zip(wanted, rows, strict=True))
                           if row is None and key[0] == "entity" and key[1] not in states]
                if not missing:
                    yield rows
                    continue
                located = self.locate_members(wanted[index][1] for index in missing)
                members = {index for index in missing if wanted[index][1] in located}
                # Members found through layers join the output in byte-bounded
                # groups, as the ledger bounds the external rows it reads.
                with owned_iterator(bounded_rows(range(len(rows)), size=lambda index: located[wanted[index][1]][1]
                                                 if index in members else 0)) as groups:
                    for group in groups:
                        part = [index for index in group if index in members]
                        found = self.bulk_member_records((wanted[index][1] for index in part), located)
                        yield tuple(_member_row(wanted[index][1], found[wanted[index][1]][0])
                                    if index in members and wanted[index][1] in found else rows[index] for index in group)

    def locate_members(self, identities):
        """Find members with no ledger row in the retained layers: identity -> (admitted layer, byte size).

        Found members and misses stay known until this session publishes
        another bulk representation; the layers of other sessions' later
        states are not seen.
        """
        self._active()
        wanted = set(identities)
        located = {identity: (self.bulk_members[identity][1], len(self.bulk_members[identity][0].payload))
                   for identity in wanted if identity in self.bulk_members}
        search = wanted - located.keys() - self.bulk_misses.keys()
        if self.states is not None and search:
            found = self.states.find_members(self, search)
            located.update(found)
            self.bulk_misses.update(dict.fromkeys(search - found.keys()))
            while len(self.bulk_misses) > BATCH_ROWS:
                self.bulk_misses.pop(next(iter(self.bulk_misses)))
        return located

    def bulk_member_records(self, identities, located=None):
        """Read located members through their layers: identity -> (admitted record, admitted layer).

        Locating searches every available retained entity layer, so it costs
        one layer admission per layer; only by-identity reads of unpinned
        members pay it. Found bytes pass full admission, as ledger reads of
        external rows do, and stay cached within the record window's bounds.
        """
        self._active()
        wanted = set(identities)
        located = self.locate_members(wanted) if located is None else located
        found = {identity: self.bulk_members[identity] for identity in wanted if identity in self.bulk_members}
        layers = {}
        for identity in wanted - found.keys():
            if identity in located:
                layers.setdefault(located[identity][0].reference.layer_id, (located[identity][0], []))[1].append(identity)
        for layer, group in layers.values():
            for identity, payload in self.states.member_payloads(layer, group):
                record = AdmittedRecord(payload)
                value = record.record
                if not isinstance(value, core.Entity) or value.entity_id != identity or value.entity_type != "occurrence":
                    raise IntegrityError("bulk member row differs from its occurrence identity")
                found[identity] = self.bulk_members[identity] = record, layer
                self.bulk_member_bytes += len(payload)
        while self.bulk_members and (len(self.bulk_members) > BATCH_ROWS or self.bulk_member_bytes > BATCH_BYTES):
            self.bulk_member_bytes -= len(self.bulk_members.pop(next(iter(self.bulk_members)))[0].payload)
        return found

    def _read_records(self, keys, *, include_values=True, include_unavailable_values=False):
        self._active()
        if include_unavailable_values:
            yield from self.ledger.read_records(keys, include_values=include_values, include_unavailable_values=True)
            return
        if self._record_window is None or not include_values:
            yield from self.ledger.read_records(keys, include_values=include_values)
            return
        window = self._record_window
        with owned_iterator(self.ledger.read_records(keys, include_values=False)) as batches:
            for statuses in batches:
                missing = tuple(row.key for row in statuses if row is not None and not window.contains(row))
                fetched = {}
                if missing:
                    with owned_iterator(self.ledger.read_records(dict.fromkeys(missing))) as values:
                        for batch in values:
                            for row in batch:
                                if row is not None:
                                    fetched[row.key] = row
                                    window.remember(row)
                yield tuple(None if row is None else fetched[row.key] if row.key in fetched else
                            replace(row, value=window.value(row)) for row in statuses)

    def read_links(self, keys):
        self._active()
        yield from self.ledger.read_links(keys)

    def retain_bytes(self, chunks, *, media_type: str = "application/octet-stream") -> core.ContentRef:
        """Retain opaque bytes and mark them ready for this session."""
        self._active()
        reference = self.blobs.put_if_absent(chunks, media_type=media_type)
        self.ready.setdefault(reference, set()).add("bytes-v1")
        return core.ContentRef(digest=reference.digest, byte_size=reference.byte_size,
                               locator=reference.locator, media_type=reference.media_type)

    def retain_value(self, value, *, media_type="application/json") -> core.ContentRef:
        """Retain one canonical JSON value, refusing a payload above the 8 MiB limit."""
        self._active()
        payload = canonical_value_bytes(value)
        if len(payload) > BATCH_BYTES:
            raise LimitExceededError("JSON value exceeds the 8 MiB value limit; use bulk records or opaque bytes")
        reference = self.retain_bytes([payload], media_type=media_type)
        blob = BlobRef(reference.locator, reference.digest, reference.byte_size, reference.media_type)
        self.ready[blob].add("json-v1")
        return core.ContentRef(digest=reference.digest, byte_size=reference.byte_size, locator=reference.locator,
                               media_type=reference.media_type, codec="json-v1")

    def read_json(self, content, *, label="retained JSON value"):
        """Decode a retained JSON content reference, refusing any other codec."""
        self._active()
        value = record_value(content, core.ContentRef)
        if value["codec"] != "json-v1":
            raise IntegrityError("JSON reading requires the JSON value codec")
        reference = BlobRef(value["locator"], value["digest"], value["byte_size"], value["media_type"])
        with owned_iterator(self.blobs.read(reference, max_bytes=BATCH_BYTES)) as chunks:
            return decode_canonical_json_value(b"".join(chunks), label=label)

    def check_content(self, content, *, retained=False, validate_json=None):
        """One owner for byte readiness, retained availability and JSON checks."""
        self._active()
        value = record_value(content, core.ContentRef)
        reference = BlobRef(value["locator"], value["digest"], value["byte_size"], value["media_type"])
        admitted = self.ready.setdefault(reference, set())
        if value["codec"] in admitted and validate_json is None:
            return
        if retained:
            self.blobs.stat(reference)
        elif "bytes-v1" not in admitted:
            self.blobs.ensure_ready(reference)
        admitted.add("bytes-v1")
        if value["codec"] == "json-v1" and (not retained or validate_json is not None):
            decoded = self.read_json(value)
            if validate_json is not None:
                validate_json(decoded)
        admitted.add(value["codec"])

    def publish(self, batch: MetadataBatch) -> bool:
        """Retain the requested roots and their exact binding-specific obligations.

        Result roots retain all bound inputs and outputs. Association roots
        retain their requested inputs, selected outputs and original provenance.
        The publisher derives links; callers cannot inject substitute obligations.
        """
        self._active()
        if batch.links:
            raise IntegrityError("the publisher owns retention relationship construction")
        if not isinstance(batch.records, (tuple, list)) or len(batch.records) > BATCH_ROWS:
            raise LimitExceededError("publication requires a bounded collection of at most 2048 records")
        check = _PublicationCheck(self, batch)
        result = check.publish()
        for key in check.incoming & check.required:
            self.computed_records.pop(key, None)
            if key[0] == "state_representation" and isinstance(check.values[key]["membership"], dict):
                # A new bulk representation may bring another entity layer and
                # another copy of a cached member: search again, with that layer.
                self.bulk_members, self.bulk_member_bytes, self.bulk_misses = {}, 0, {}
                self.states.add_entity_layer(self, check.values[key]["membership"])
        return result

    def retention_scope(self, batch: MetadataBatch) -> tuple[tuple, tuple]:
        """Check retained or staged roots and return their complete obligations."""
        self._active()
        if batch.links:
            raise IntegrityError("the publisher owns retention relationship construction")
        if not isinstance(batch.records, (tuple, list)) or len(batch.records) > BATCH_ROWS:
            raise LimitExceededError("publication requires a bounded collection of at most 2048 records")
        check = _PublicationCheck(self, batch)
        check.publish(validate_only=True)
        return tuple(sorted(check.required)), tuple(sorted(check.full))

    def validate(self, batch: MetadataBatch) -> tuple:
        """Check roots using the same exact retention and description scope."""
        return self.retention_scope(batch)[0]


class _RecordWindow:
    """A bounded cache of checked bytes, without mutable retention metadata."""
    def __init__(self):
        self.payloads = {}
        self.byte_size = 0

    def contains(self, row):
        return row.available and (row.key, row.row_digest) in self.payloads

    def remember(self, row):
        if row.value is None or not row.available or self.contains(row) or len(self.payloads) >= BATCH_ROWS:
            return
        snapshot = AdmittedRecord(row.value)
        if self.byte_size + len(snapshot.payload) <= BATCH_BYTES:
            self.payloads[row.key, row.row_digest] = snapshot
            self.byte_size += len(snapshot.payload)

    def value(self, row):
        # These bytes already passed ordinary Core admission; each read is detached.
        return self.payloads[row.key, row.row_digest].record


def _member_row(identity, record):
    """A bulk member read through its layer: retained by its state, available, never versioned."""
    return StoredRecord(("entity", identity), record.record, True, True, 0, sha256_digest(record.payload))


class _PublicationCheck:
    """Compute one batch's exact retention closure before the ledger commits it."""
    def __init__(self, session: Publication, batch: MetadataBatch):
        self.session, self.ledger, self.batch = session, session.ledger, batch
        self.values, self.stored, self.representations = {}, {}, {}
        self.members = {}
        self.metadata_bytes = 0
        self.loaded, self.state_links_loaded = set(), set()
        records = []
        for record in batch.records:
            record = record if type(record) is AdmittedRecord else AdmittedRecord(record)
            value, payload = record_parts(record)
            key = value["kind"], value[core.RECORD_ID_FIELDS[value["kind"]]]
            if key in self.values and self.values[key] != value:
                raise IntegrityError("publication repeats a conflicting record identity")
            self._cache(key, value, payload)
            records.append(record)
        # Commit the same immutable bytes that this check admitted. The ledger
        # still owns cross-record checks and transactional publication.
        self.batch = replace(batch, records=tuple(records))
        self.incoming = set(self.values)
        self.roots = set(batch.retained)
        self.links, self.required, self.full = set(), set(), set()

    def _cache(self, key, value, payload=None):
        if key not in self.values:
            self.metadata_bytes += len(canonical_value_bytes(value) if payload is None else payload)
            if self.metadata_bytes > BATCH_BYTES:
                raise LimitExceededError("publication metadata closure exceeds the 8 MiB limit")
        self.values[key] = value
        if key[0] == "state_representation":
            self.representations.setdefault(value["state_id"], {})[key] = value

    def _load(self, keys):
        """Load required existing records and follow state representation links.

        A bulk state member has no ledger row until a publication pins it. A
        referenced entity or data key found in neither form resolves through its
        retained layer and is pinned at commit. An incoming entity must match
        any retained copy of its identity, and an incoming state cannot share a
        member's identity; identities DocSpec mints for its own outputs are not
        searched for.
        """
        keys, wanted = list(keys), set()
        for kind, identity in keys:
            wanted.update((("entity", identity), ("state", identity)) if kind in {"data", "entity", "state"} else ((kind, identity),))
        missing = wanted - self.loaded
        if missing:
            # Incoming values have already crossed canonical admission. Compare
            # their immutable digests against metadata without rereading the old
            # payloads (especially every batch of a physical checkpoint).
            groups = ((missing & self.incoming, False), (missing - self.incoming, True))
            for pending, include_values in groups:
                if not pending:
                    continue
                for rows in self.session._read_records(sorted(pending), include_values=include_values):
                    for row in rows:
                        if row is not None:
                            self.stored[row.key] = row
                            if row.value is None:
                                # Cleanup keeps identity and history after external
                                # payload removal. Incoming restoration is checked
                                # against the ledger's immutable digest at commit.
                                if row.key in self.values and row.row_digest != sha256_digest(canonical_value_bytes(self.values[row.key])):
                                    raise IntegrityError("publication conflicts with an immutable retained record")
                                continue
                            value = record_value(row.value)
                            if row.key in self.values and self.values[row.key] != value:
                                raise IntegrityError("publication conflicts with an immutable retained record")
                            self._cache(row.key, value)
            self.loaded.update(missing)
            referenced = {identity for kind, identity in keys if kind in {"data", "entity"} and ("entity", identity) in missing
                          and not any((form, identity) in self.values or (form, identity) in self.stored for form in ("entity", "state"))}
            chosen = {key[1] for key in missing & self.incoming if key[0] in {"entity", "state"} and key not in self.stored
                      and _MINTED.fullmatch(key[1]) is None}
            located = self.session.locate_members(referenced | chosen) if referenced or chosen else {}
            if self.metadata_bytes + sum(located[identity][1] for identity in referenced & located.keys()) > BATCH_BYTES:
                raise LimitExceededError("publication metadata closure exceeds the 8 MiB limit")
            for identity, (record, layer) in self.session.bulk_member_records(located, located).items():
                key = "entity", identity
                if key in self.incoming:
                    if canonical_value_bytes(self.values[key]) != record.payload:
                        raise IntegrityError("publication conflicts with an immutable retained record")
                    continue
                self.stored[key] = _member_row(identity, record)
                self._cache(key, record.value, record.payload)
                self.members[key] = record, layer
        states = {key for key in wanted if key[0] == "state" and key in self.values} - self.state_links_loaded
        if states:
            targets = set()
            for links in self.ledger.read_links(sorted(states)):
                targets.update(link.target for link in links if link.relation == "representation")
            self.state_links_loaded.update(states)
            if targets:
                self._load(targets)

    def _key(self, key):
        """Resolve a data output key to its unique entity or state, refusing the ambiguity."""
        if key[0] == "data":
            found = [candidate for candidate in (("entity", key[1]), ("state", key[1])) if candidate in self.values]
            if len(found) != 1:
                raise IntegrityError("output identity must resolve to exactly one entity or state")
            return found[0]
        if key not in self.values:
            raise IntegrityError(f"publication lacks required {key[0]} record: {key[1]}")
        return key

    def _state(self, identity):
        """Choose a complete available state representation and check membership agreement."""
        choices = list(self.representations.get(identity, {}).items())
        if not choices:
            raise IntegrityError("complete state retention requires its membership representation")
        # Previously admitted representations already agree. Validate incoming
        # data, and compare only when adding a new physical representation.
        comparable = [(key, value) for key, value in choices if key in self.incoming or
                      (key in self.stored and self.stored[key].available) or isinstance(value["membership"], list)]
        known = [key for key, _ in comparable if key in self.stored]
        if any(key in self.stored for key, _ in choices) and not known:
            raise IntegrityError("state membership restoration requires a previously admitted representation")
        memberships = []
        for key, value in comparable:
            if not isinstance(value["membership"], list):
                if self.session.states is None:
                    raise IntegrityError("external state membership requires state-storage admission")
                membership = self.session.states.check_representation(
                    self.session, value, retained=key in self.stored and self.stored[key].available)
            else:
                membership = sorted(value["membership"], key=lambda row: row["member_key"])
            memberships.append((key, membership))
        if memberships:
            baseline_key, baseline = next(((key, rows) for key, rows in memberships if key in self.stored), memberships[0])
            for key, rows in memberships:
                if key == baseline_key or (key in self.stored and baseline_key in self.stored):
                    continue
                same = self.session.states.same_membership(self.session, baseline, rows) if self.session.states else baseline == rows
                if not same:
                    raise IntegrityError("state representations disagree about the immutable membership")
        eligible = sorted(key for key, _ in choices if key in self.incoming)
        if not eligible:
            eligible = sorted(key for key, _ in choices if key in self.stored and self.stored[key].available)
        if not eligible:
            raise IntegrityError("state has no available complete representation")
        return [("representation", eligible[0], True)]

    def _requirements(self, key, full):
        """Return the retention edges one record kind requires, full or descriptive."""
        value = self.values[key]
        kind = key[0]
        requirements = []
        if kind == "result":
            if value["outcome"]["status"] != "success":
                raise IntegrityError("unsuccessful result cannot be successfully retained")
            requirements.append(("execution", ("execution", value["execution_id"]), full))
            if full:
                requirements.extend(("output:" + output["label"], ("data", output["entity_id"]), True) for output in value["outcome"]["outputs"])
        elif kind == "execution":
            requirements.append(("request", ("request", value["request_id"]), full))
        elif kind == "request":
            requirements.append(("definition", ("operation_definition", value["definition_id"]), False))
            if full:
                fields = {"whole_value": ("entity", "entity_id"), "dataset_state": ("state", "state_id"), "selected_value": ("selected_value", "selected_value_id")}
                for binding in value["inputs"]:
                    target_kind, field = fields[binding["kind"]]
                    requirements.append(("input:" + binding["label"], (target_kind, binding[field]), True))
        elif kind == "selection":
            requirements.extend([
                ("request", ("request", value["request_id"]), True),
                ("selected_result", ("result", value["selected_result_id"]), False),
            ])
            selected_key = ("result", value["selected_result_id"])
            selected = self.values[self._key(selected_key)]
            if selected_key not in self.roots and (selected_key not in self.stored or not self.stored[selected_key].retained):
                raise IntegrityError("association requires an already retained result or a result published in this unit")
            existing = key in self.stored and self.stored[key].retained
            if not existing and selected_key not in self.roots and selected_key not in dict(self.batch.expected_versions):
                raise IntegrityError("reuse publication requires the checked result evidence version")
            if existing:
                requirements.extend(("comparison_evidence", link.target, True) for batch in self.ledger.read_links([key])
                                    for link in batch if link.label == "comparison_evidence" and link.relation == "requires")
            outputs = {output["label"]: output for output in selected["outcome"]["outputs"]}
            for label in value["output_labels"]:
                if label not in outputs:
                    raise IntegrityError("association selects an output absent from the chosen result")
                requirements.append(("selected_output:" + label, ("data", outputs[label]["entity_id"]), True))
        elif kind == "state" and full:
            requirements.extend(self._state(key[1]))
        elif kind == "state_representation" and full:
            requirements.append(("state", ("state", value["state_id"]), True))
            if value["revision_id"] is not None:
                requirements.append(("revision", ("revision", value["revision_id"]), True))
            if isinstance(value["membership"], list):
                requirements.extend(("member:" + member["member_key"], ("entity", member["occurrence_id"]), True) for member in value["membership"])
        elif kind == "selected_value" and full:
            if value["value"]["kind"] == "from_parent":
                target_kind = "state" if value["definition"]["kind"] == "state_members" else "entity"
                requirements.append(("recovery_parent", (target_kind, value["origin"]["parent_entity_id"]), True))
            if value["definition"].get("sort_rule"):
                requirements.append(("sort_rule", ("operation_definition", value["definition"]["sort_rule"]), False))
        elif kind == "revision":
            requirements.append(("base_description", ("state", value["base_state_id"]), False))
            for edit in value["value_edits"]:
                requirements.append(("value_edit_execution", ("execution", edit["execution_id"]), False))
        return requirements

    def _validate(self):
        """Check cross-record invariants of the computed closure before commit."""
        if {identity for kind, identity in self.values if kind == "entity"} & {identity for kind, identity in self.values if kind == "state"}:
            raise IntegrityError("data identity is ambiguous between an entity and a state")
        for key in self.required:
            value = self.values[key]
            if key[0] == "execution":
                request = self.values[("request", value["request_id"])]
                definition = self.values[("operation_definition", request["definition_id"])]
                if definition["operation_kind"] == "capture" and value["capture_origin"] is None:
                    raise IntegrityError("retained capture result requires its originating occurrence")
            elif key[0] == "state_representation" and isinstance(value["membership"], list):
                for member in value["membership"]:
                    if self.values[("entity", member["occurrence_id"])]["entity_type"] != "occurrence":
                        raise IntegrityError("state membership must identify an occurrence entity")
            if key[0] == "state_representation" and value["revision_id"] is not None:
                revision = self.values[("revision", value["revision_id"])]
                if revision["result_state_id"] != value["state_id"]:
                    raise IntegrityError("state representation names another revision result")

    def _validate_selections(self):
        """Check that each newly retained selection's dependency evidence still corresponds."""
        if not any(key[0] == "selection" and (key not in self.stored or not self.stored[key].retained) for key in self.required):
            return
        dependencies = CoreDependencies()
        supports = set()
        staged = {key: admit_record(canonical_value_bytes(self.values[key])) for key in self.incoming
                  if key not in self.stored or not self.stored[key].retained}
        view = _PublicationReadView(self.session, staged, self.links)
        for key in self.required:
            if key[0] != "selection" or (key in self.stored and self.stored[key].retained):
                continue
            value = self.values[key]
            original_key = "result", value["selected_result_id"]
            execution = self.values[("execution", self.values[original_key]["execution_id"])]
            if original_key in self.roots and execution["request_id"] == value["request_id"]:
                # This associates the result of this exact fresh request, not a
                # reuse decision. Unknown external resources may be observed.
                continue
            original = self.stored.get(original_key)
            if original is None or dict(self.batch.expected_versions).get(original_key) != original.evidence_version:
                raise StaleBaseError("selected result dependency evidence changed")
            requested = admit_record(canonical_value_bytes(self.values[("request", value["request_id"])]))
            definition = admit_record(canonical_value_bytes(self.values[("operation_definition", requested.definition_id)]))
            current = dependencies.assess(view, requested, definition)
            prior = dependencies.assess_result(view, original_key[1])
            if not corresponds(current, prior):
                raise IntegrityError("selected result does not correspond to the adequately described request")
            self.selection_versions.update(current.expected_versions)
            self.selection_versions.update(prior.expected_versions)
            for support in prior.support_keys:
                self.links.add(MetadataLink(key, "requires", "comparison_evidence", support))
                supports.add(support)
        self._load(supports)
        self.required.update(supports)
        self.full.update(supports)

    def _content(self, key):
        """Check retained bytes and JSON validity for one content-bearing record."""
        record = self.values[key]
        if (key[0] == "selected_value" and self.session.selections is None
                and (record["definition"]["kind"] == "state_members" or record["value"]["kind"] == "from_parent")):
            raise IntegrityError("selected members and parent recovery require the selected-value evaluator")
        if key[0] == "selected_value" and self.session.selections is not None:
            from docspec.domain.core_admission import admit_record
            selected = admit_record(canonical_value_bytes(record))
            self.session.selections.check_selection(self.session, selected,
                retained=key in self.stored and self.stored[key].retained and self.stored[key].available)
        value = record.get("edits" if key[0] == "revision" else "value")
        if not isinstance(value, dict) or value["kind"] != "content":
            return
        retained = key in self.stored and self.stored[key].retained and self.stored[key].available
        field_check = key[0] == "selected_value" and record["definition"]["kind"] == "json_fields" and not retained
        validator = None
        if field_check:
            selectors = tuple(core.Field(label=field["label"], pointer=field["pointer"]) for field in record["definition"]["selectors"])
            def validator(decoded):
                validate_fields_value(selectors, decoded)
        self.session.check_content(value, retained=retained, validate_json=validator)

    def publish(self, *, validate_only=False):
        """Walk the retention closure, validate content and commit it (or check only)."""
        pending = [(key, True) for key in self.roots]
        while pending:
            self._load(key for key, _ in pending)
            pending = [(self._key(key), full) for key, full in pending]
            self._load(("result", self.values[key]["selected_result_id"]) for key, _ in pending if key[0] == "selection")
            edges = []
            for raw_key, full in pending:
                key = self._key(raw_key)
                if key in self.full or (key in self.required and not full):
                    continue
                self.required.add(key)
                if full:
                    self.full.add(key)
                edges.extend((key, label, target, target_full) for label, target, target_full in self._requirements(key, full))
            self._load(target for _, _, target, _ in edges)
            pending = []
            for key, label, raw_target, target_full in edges:
                target = self._key(raw_target)
                relation = "representation" if label == "representation" else "requires" if target_full else "describes"
                self.links.add(MetadataLink(key, relation, label, target))
                pending.append((target, target_full))
        self._validate()
        self.selection_versions = {}
        committed = not validate_only and self.ledger.is_committed(self.batch.unit_id)
        if not validate_only and not committed:
            self._validate_selections()
        versions = dict(self.batch.expected_versions)
        for key, version in self.selection_versions.items():
            versions.setdefault(key, version)
        for key in self.required - self.incoming:
            if key not in self.stored or not self.stored[key].retained:
                raise IntegrityError("required existing data or descriptions were not retained")
            versions.setdefault(key, self.stored[key].evidence_version)
        # Only an actual owner computation can supply this session witness.
        # Supplied records cannot assert evaluator or resolver certificates.
        for key, (payload, link) in self.session.computed_records.items():
            if key in self.required and key in self.incoming:
                if canonical_value_bytes(self.values[key]) != payload:
                    raise IntegrityError("computed record differs from its publication witness")
                self.links.add(link)
        prepared = replace(self.batch, retained=tuple(sorted(self.required)), links=tuple(sorted(self.links, key=lambda link: (link.owner, link.relation, link.label, link.target))),
                           expected_versions=tuple(sorted(versions.items())))
        if committed:
            return self.ledger.commit(prepared)
        if validate_only and any(not self.stored[key].available for key in self.required - self.incoming):
            raise IntegrityError("checkpoint requires data that is no longer available")
        for key in self.full:
            self._content(key)
        if validate_only:
            return None
        # Members this unit retains are pinned, each at its layer's bytes, with
        # the parents that new direct selections name.
        members = {key: self.members[key] for key in self.members.keys() & self.required}
        members.update(self._origin_members())
        return self.ledger.commit(replace(prepared, members=tuple((members[key][0], members[key][1].reference)
                                                                  for key in sorted(members))))

    def _origin_members(self):
        """Resolve the unpinned bulk parents that new direct selections name, to pin with this unit.

        A direct selection describes its parent without requiring it, so no
        link changes the unit's receipt; the pin keeps the parent's identity,
        digest and availability history after its state is removed (Core §5.3).
        The selection owner has just read the parent, so its layer is known.
        """
        parents = {value["origin"]["parent_entity_id"] for key, value in self.values.items()
                   if key[0] == "selected_value" and key in self.incoming and value["definition"]["kind"] != "state_members"}
        parents -= {identity for kind, identity in self.values if kind in {"entity", "state"}}
        if not parents:
            return {}
        with owned_iterator(self.session._read_records([(kind, identity) for identity in sorted(parents) for kind in ("entity", "state")],
                                                       include_values=False)) as batches:
            parents -= {row.key[1] for batch in batches for row in batch if row is not None}
        return {("entity", identity): member for identity, member in self.session.bulk_member_records(parents).items()}


class _PublicationReadView:
    """Read checked incoming records before their single atomic publication."""

    def __init__(self, session, records, links):
        self._session, self._records, self._links = session, records, tuple(links)
        self.pending_keys = frozenset(records)

    def __getattr__(self, name):
        return getattr(self._session, name)

    def read_records(self, keys):
        keys = bounded_items(keys, limit=BATCH_ROWS)
        missing = tuple(dict.fromkeys(key for key in keys if key not in self._records))
        found = {row.key: row for batch in self._session.read_records(missing) for row in batch if row is not None}
        yield tuple(StoredRecord(key, self._records[key], False, True, 0) if key in self._records else found.get(key) for key in keys)

    def read_links(self, keys):
        keys = bounded_items(keys, limit=BATCH_ROWS)
        incoming = [link for link in self._links if link.owner in keys]
        preferred = {link.owner for link in incoming if link.relation == "representation"}
        existing = [link for batch in self._session.read_links(keys) for link in batch
                    if not (link.owner in preferred and link.relation == "representation")]
        yield tuple(dict.fromkeys((*existing, *incoming)))
