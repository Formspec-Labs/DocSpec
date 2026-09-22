"""Protected reads of one exact existing Core state, without new data files."""

from contextlib import closing, contextmanager

from docspec.domain import core
from docspec.domain.core_admission import admit_record, record_value
from docspec.domain.identity import canonical_value_bytes, require_text, sha256_digest
from docspec.domain.references import BlobRef
from docspec.domain.streams import owned_iterator
from docspec.errors import IntegrityError, StateValueRelationUnavailable
from docspec.ports.record_storage import BATCH_BYTES, BATCH_ROWS, bounded_batches


class CoreStateReader:
    """Use through ``CoreWorkspace.open_state`` and close child streams first.

    Construction verifies the pinned member files and refuses a pin mismatch
    with IntegrityError.
    """

    def __init__(self, session, states, state_id, *, expected_pin=None):
        require_text(state_id, "state identity")
        self._session, self._states, self._state_id = session, states, state_id
        representation = states.representation(session, state_id, materialize=False)
        keys = (("state", state_id), ("state_representation", representation.representation_id))
        for batch in session.read_records(keys):
            if any(row is None or not row.retained or not row.available for row in batch):
                raise IntegrityError("state reader requires retained available state and representation")
        layers = states._layers(session.ready_states[representation.membership.digest])
        self._representation_id = representation.representation_id
        self._pin = sha256_digest(canonical_value_bytes({
            "representation": record_value(representation),
            "layers": {name: layer.reference.to_dict() for name, layer in layers.items()},
        }))
        if expected_pin is not None and expected_pin != self._pin:
            raise IntegrityError("state differs from the expected read pin")
        # Publication checked the logical rows and complete membership before
        # retaining this representation. Bind that verdict to its unchanged bytes
        # on every reopen; a full logical audit remains records.verify/admit.
        for layer in layers.values():
            states.records.verify_members(layer.reference)
        self._layers = {name: states.records.admitted(layer.reference) for name, layer in layers.items()}

    @property
    def pin(self):
        """Bind state identity, selected representation and exact layer references."""
        return self._pin

    @property
    def state_id(self):
        return self._state_id

    @property
    def representation_id(self):
        return self._representation_id

    @property
    def record_count(self):
        return self._layers["membership"].reference.record_count

    @contextmanager
    def relation(self, *, member_keys=None):
        """Yield the admitted membership/occurrence join in the owning process.

        Columns are member_key, occurrence_id and canonical occurrence_record
        bytes. Native SQL preserves snapshot versions and Iceberg deletes;
        generated SQL is meaningful only while its input protection is held.
        """
        self._session._active()
        if member_keys is not None:
            if (not isinstance(member_keys, (tuple, list)) or len(member_keys) > BATCH_ROWS
                    or any(not isinstance(key, str) or not key for key in member_keys)
                    or len(set(member_keys)) != len(member_keys)):
                raise ValueError("member keys require a bounded sequence of distinct nonempty names")
            if sum(map(len, member_keys)) > BATCH_BYTES or len(canonical_value_bytes(member_keys)) > BATCH_BYTES:
                raise ValueError("member keys exceed the batch byte limit")
            member_keys = tuple(member_keys)
        with self._states.relation(self._session, self._state_id, scope=member_keys, layers=self._layers) as relation:
            yield relation

    def batches(self, *, member_keys=None):
        """Stream the same columns in deterministic order within shared bounds."""
        with self.relation(member_keys=member_keys) as relation, closing(relation.order("member_key").to_arrow_reader(BATCH_ROWS)) as batches:
            yield from bounded_batches(batches, byte_column="occurrence_record", allow_null=member_keys is not None)

    @contextmanager
    def value_relation(self):
        """Yield member_key, occurrence_id and JSON value for inline-only states.

        The native check scans value kinds without decoding rows into Python.
        Content references require ``values()`` and raise a distinct exception;
        corruption and other admission failures keep their original errors.
        """
        with self.relation() as relation:
            if relation.filter("json_extract_string(decode(occurrence_record), '/value/kind') "
                               "IS DISTINCT FROM 'inline'").limit(1).fetchone():
                raise StateValueRelationUnavailable("state contains retained content; use the decoded values stream")
            yield relation.project("member_key, occurrence_id, "
                                   "json_extract(decode(occurrence_record), '/value/value') AS value")

    def rows(self, *, member_keys=None):
        """Stream detached (member key, Entity) pairs in deterministic order."""
        with closing(self.batches(member_keys=member_keys)) as batches:
            for batch in batches:
                for key, identity, payload in zip(batch.column("member_key").to_pylist(),
                                        batch.column("occurrence_id").to_pylist(),
                                        batch.column("occurrence_record").to_pylist(), strict=True):
                    self._session._active()
                    if payload is None:
                        if identity is not None:
                            raise IntegrityError("state member occurrence payload is unavailable")
                        raise LookupError("state member does not exist: " + key)
                    yield key, admit_record(payload)

    def values(self, *, member_keys=None):
        """Stream values, optionally selecting one bounded group of exact members.

        An empty scope yields no rows. Missing requested members raise LookupError.
        The source pin still describes the complete immutable state.
        """
        with closing(self.rows(member_keys=member_keys)) as rows:
            for key, entity in rows:
                yield key, entity.entity_id, self._read_value(entity)

    def changes(self, older):
        """Stream (member key, occurrence ID, value) where this state differs from ``older``.

        Rows arrive in key order. An added or rewritten member carries its new
        occurrence and value; a removed member yields ``(key, None, None)``.
        Both readers must stay open. Cost follows the changed members when this
        state descends from ``older`` through recorded revisions, and one
        native pass over both memberships otherwise.
        """
        if not isinstance(older, CoreStateReader):
            raise TypeError("changes require another open state reader")
        self._session._active()
        older._session._active()
        with self._states.changes(self._session, older.state_id, self._state_id, older_layers=older._layers,
                                  newer_layers=self._layers) as relation, \
                closing(relation.order("member_key").to_arrow_reader(BATCH_ROWS)) as batches:
            for batch in bounded_batches(batches, byte_column="occurrence_record", allow_null=True):
                for key, identity, payload in zip(batch.column("member_key").to_pylist(),
                                                  batch.column("occurrence_id").to_pylist(),
                                                  batch.column("occurrence_record").to_pylist(), strict=True):
                    self._session._active()
                    if identity is None:
                        yield key, None, None
                    elif payload is None:
                        raise IntegrityError("state member occurrence payload is unavailable")
                    else:
                        entity = admit_record(payload)
                        yield key, entity.entity_id, self._read_value(entity)

    def lookup(self, member_key, *, occurrence_id=None):
        """Read one current member; an expected occurrence must match exactly."""
        self._session._active()
        with self._states.relation(self._session, self._state_id, scope=(member_key,), layers=self._layers) as relation:
            with closing(relation.to_arrow_reader(1)) as batches:
                with closing(bounded_batches(batches, byte_column="occurrence_record", allow_null=True)) as bounded:
                    row = next(bounded).to_pylist()[0]
        if occurrence_id is not None and occurrence_id != row["occurrence_id"]:
            raise IntegrityError("state member differs from the expected occurrence identity")
        return None if row["occurrence_id"] is None else admit_record(row["occurrence_record"])

    def read_value(self, member_key, *, occurrence_id=None):
        """Read the member's JSON value or opaque bytes, bounded to 8 MiB.

        A missing member raises LookupError, distinct from a JSON null value.
        Content references are resolved by the existing blob/codec readers.
        """
        entity = self.lookup(member_key, occurrence_id=occurrence_id)
        if entity is None:
            raise LookupError("state member does not exist")
        return self._read_value(entity)

    def _read_value(self, entity):
        self._session._active()
        value = entity.value
        if isinstance(value, core.InlineValue):
            return value.value
        if value.codec == "json-v1":
            return self._session.read_json(value, label="state member JSON value")
        reference = BlobRef(value.locator, value.digest, value.byte_size, value.media_type)
        with owned_iterator(self._session.blobs.read(reference, max_bytes=BATCH_BYTES)) as chunks:
            return b"".join(chunks)
