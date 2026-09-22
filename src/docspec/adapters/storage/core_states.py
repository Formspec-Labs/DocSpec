"""General Core roots over the existing record writer and metadata publisher."""

from contextlib import closing, contextmanager
from tempfile import TemporaryFile

import pyarrow as pa

from docspec.ports.record_storage import bounded_batches, bounded_rows
from docspec.adapters.storage.batches import ENCODED_RECORD_SCHEMA, encoded_batches
from docspec.adapters.streams import owned_iterator
from docspec.domain import core
from docspec.domain.core_admission import AdmittedRecord, admit_record, encode_record, record_value
from docspec.domain.identity import canonical_value_bytes, sha256_digest, decode_canonical_json_value
from docspec.domain.references import BlobRef, LayerRef
from docspec.domain.storage import PartitionPolicy, RecordSchema, partition_bucket
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.core_ledger import MetadataBatch, MetadataLink
from docspec.ports.record_storage import BATCH_BYTES, BATCH_ROWS


_ENTITIES = RecordSchema("core-entities:1", ("kind", *core.Entity.__struct_fields__), "entity_id", "entity_id")
_MEMBERS = RecordSchema("core-membership:1", ("kind", *core.Membership.__struct_fields__), "member_key", "member_key")
_SCHEMAS = {"entities": _ENTITIES, "membership": _MEMBERS}
_POLICY = PartitionPolicy("core-keys:1", 1)
_ENTITY_POLICY = PartitionPolicy("core-values:1", 1)
_READY_STATE_LIMIT = 4


def _entity_rows(entities, observe=None):
    for entity in entities:
        if observe is not None:
            observe()
        value = record_value(entity, core.Entity)
        if value["entity_type"] != "occurrence":
            raise IntegrityError("state values must be occurrence entities")
        yield value["entity_id"], value["entity_id"], canonical_value_bytes(value)


def _existing_entities(session, identities):
    with owned_iterator(session.read_records(("entity", identity) for identity in identities)) as batches:
        for batch in batches:
            for row in batch:
                if row is None or not row.available:
                    raise IntegrityError("membership put requires an available occurrence")
                yield row.value


class CoreStateStorage:
    """Write, verify and resolve Core states, representations and membership revisions."""

    def __init__(self, records):
        self.records = records

    def create_keyed(self, session, *, state_id, representation_id, unit_id, rows):
        """Import one-shot keyed entities through the shared state writer.

        Only membership addresses spool to disk; entity payloads pass directly
        to the existing bounded writer and are admitted there once.
        """
        schema = pa.schema([("member_key", pa.string()), ("occurrence_id", pa.string())])
        with owned_iterator(rows) as source, TemporaryFile() as spool:
            def entities():
                pending, size = [], 0
                with pa.ipc.new_stream(spool, schema) as writer:
                    for key, entity in source:
                        if not isinstance(entity, core.Entity):
                            raise IntegrityError("keyed state values must be occurrence entities")
                        member = record_value(core.Membership(member_key=key, occurrence_id=entity.entity_id), core.Membership)
                        row_size = len(key.encode("utf-8")) + len(entity.entity_id.encode("utf-8"))
                        if row_size > BATCH_BYTES:
                            raise LimitExceededError("membership addresses exceed the batch byte limit")
                        if pending and (len(pending) == BATCH_ROWS or size + row_size > BATCH_BYTES):
                            writer.write_batch(pa.RecordBatch.from_pylist(pending, schema=schema))
                            pending, size = [], 0
                        pending.append(member)
                        size += row_size
                        yield entity
                    if pending:
                        writer.write_batch(pa.RecordBatch.from_pylist(pending, schema=schema))

            def members():
                spool.seek(0)
                with pa.ipc.open_stream(spool) as reader:
                    for batch in reader:
                        for value in batch.to_pylist():
                            yield core.Membership(**value)

            return self.create(session, state_id=state_id, representation_id=representation_id,
                               unit_id=unit_id, entities=entities(), members=members())

    def create(self, session, *, state_id: str, representation_id: str, unit_id: str, entities, members):
        """Import occurrence and membership streams under publication protection.

        Equal values need not share occurrence identities; several member keys
        may address the same occurrence. Input iterators close on every exit.
        """
        session._active()
        with owned_iterator(entities) as entity_source, owned_iterator(members) as member_source:
            entity_layer = self.records.retain_batches(
                encoded_batches(_entity_rows(entity_source, session.generated_row), ENCODED_RECORD_SCHEMA, byte_column=2), layer_kind="core-entities",
                schema=_ENTITIES, partition_policy=_ENTITY_POLICY, ordered=False,
            )
            def member_rows():
                for member in member_source:
                    value = record_value(member, core.Membership)
                    payload = canonical_value_bytes(value)
                    yield value["member_key"], value["member_key"], payload
            member_layer = self.records.retain_batches(
                encoded_batches(member_rows(), ENCODED_RECORD_SCHEMA, byte_column=2), layer_kind="core-membership",
                schema=_MEMBERS, partition_policy=_POLICY, ordered=False,
            )
            self._match_members({"entities": entity_layer, "membership": member_layer})
            content = self._state_content(session, entity_layer, member_layer)
            self._retain_entities(session, entity_layer)
            state = core.State(format_version=1, state_id=state_id)
            representation = core.StateRepresentation(format_version=1, representation_id=representation_id,
                                                       state_id=state_id, membership=content)
            existing = next(session.read_records([("state_representation", representation_id)]))[0]
            if existing is not None:
                if not existing.available or existing.value.state_id != state_id:
                    raise IntegrityError("existing representation is unavailable or belongs to another state")
                retained = self.check_representation(session, record_value(existing.value), retained=True)
                if not self.same_membership(session, retained, member_layer.reference):
                    raise IntegrityError("retry changes immutable state membership")
                representation = existing.value
            session.publish(MetadataBatch(unit_id, records=(state, representation), retained=(("state", state_id),)))
            return state

    def _state_content(self, session, entities, members):
        """Retain a state after its caller establishes complete membership."""
        manifest = {"format": "docspec-core-state", "version": 2, "entities": entities.reference.to_dict(),
                    "membership": members.reference.to_dict()}
        content = session.retain_value(manifest)
        self._remember(session, content.digest, manifest)
        return content

    @staticmethod
    def _remember(session, digest, manifest):
        # Only small state manifests live here. The record store owns scoped
        # descriptor reuse for all readers, including ledger entity lookups.
        session.ready_states.pop(digest, None)
        session.ready_states[digest] = manifest
        while len(session.ready_states) > _READY_STATE_LIMIT:
            session.ready_states.pop(next(iter(session.ready_states)))

    def _retain_entities(self, session, admitted, *, publish=True):
        def entities():
            with closing(admitted.batches()) as batches:
                for batch in batches:
                    for payload in batch.column("record_json").to_pylist():
                        record = AdmittedRecord(payload)
                        value = record.value
                        if value["kind"] != "entity" or value["entity_type"] != "occurrence":
                            raise IntegrityError("root values must be occurrence entities")
                        if publish:
                            # The ledger receipt repeats the identity twice and
                            # adds its digest and framing. Reserve that space;
                            # keep the admitted entity bytes unchanged.
                            # A near-limit singleton still reaches the ledger's
                            # exact framing check; this estimate only groups rows.
                            size = min(BATCH_BYTES, len(payload) + 2 * len(canonical_value_bytes(value["entity_id"])) + 160)
                            yield record, value["entity_id"], size
                            continue
                        if value["value"]["kind"] == "content":
                            session.check_content(value["value"])

        with closing(bounded_rows(entities(), size=lambda row: row[2])) as groups:
            for index, group in enumerate(groups):
                session.publish(MetadataBatch(
                    f"{admitted.reference.layer_id}:entities:{index}", records=tuple(row[0] for row in group),
                    retained=tuple(("entity", row[1]) for row in group), record_layer=admitted.reference,
                ))

    def _references(self, manifest):
        if (not isinstance(manifest, dict) or set(manifest) != {"format", "version", "entities", "membership"}
                or manifest["format"] != "docspec-core-state" or type(manifest["version"]) is not int or manifest["version"] != 2):
            raise IntegrityError("invalid Core state storage manifest")
        try:
            references = {name: LayerRef.from_dict(manifest[name]) for name in ("entities", "membership")}
        except (TypeError, ValueError, KeyError) as error:
            raise IntegrityError("invalid Core state layer reference") from error
        for name, schema in _SCHEMAS.items():
            if references[name].layer_kind != "core-" + name or references[name].schema_id != schema.schema_id:
                raise IntegrityError("state layer differs from its required schema")
        return references

    def _layers(self, manifest, *, retained=True):
        admit = self.records.admitted if retained else self.records.admit
        layers = {name: admit(reference) for name, reference in self._references(manifest).items()}
        if {name: layer.schema for name, layer in layers.items()} != _SCHEMAS:
            raise IntegrityError("state layer differs from its required schema")
        return layers

    def _match_members(self, layers):
        with self.records.relations(layers) as relations:
            members = relations["membership"].project("record_identity AS member_key, json_extract_string(decode(record_json), '/occurrence_id') AS occurrence_id, json_type(decode(record_json), '/occurrence_id') AS identity_type")
            entities = relations["entities"].project("record_identity AS entity_id")
            if members.filter("identity_type IS DISTINCT FROM 'VARCHAR' OR length(occurrence_id)=0").limit(1).fetchone():
                raise IntegrityError("membership requires an occurrence identity string")
            if members.join(entities, "occurrence_id = entity_id", how="anti").limit(1).fetchone():
                raise IntegrityError("complete root membership refers to a missing occurrence")

    def check_representation(self, session, value, *, retained=False, publish_entities=True):
        """Check a representation's manifest and layers and return its membership layer."""

        content = value["membership"]
        if content["kind"] != "content" or content["codec"] != "json-v1":
            raise IntegrityError("Core state storage requires its JSON manifest")
        manifest = session.ready_states.get(content["digest"])
        if manifest is None:
            reference = BlobRef(content["locator"], content["digest"], content["byte_size"], content["media_type"])
            manifest = decode_canonical_json_value(b"".join(session.blobs.read(reference, max_bytes=BATCH_BYTES)), label="Core state manifest")
            if not retained:
                session.blobs.ensure_ready(reference)
            layers = self._layers(manifest, retained=retained)
            if not retained:
                self._match_members(layers)
                self._retain_entities(session, layers["entities"], publish=publish_entities)
            self._remember(session, content["digest"], manifest)
        return self._references(manifest)["membership"]

    def same_membership(self, session, left, right):
        """Compare another representation exactly, only at admission.

        Inputs are small inline memberships or admitted bulk references. Equal
        physical roots and compaction's exact proof avoid another row scan.
        """
        if left == right:
            return True
        if isinstance(left, LayerRef) and isinstance(right, LayerRef):
            if tuple(sorted((left.digest, right.digest))) in session.membership_equivalence:
                return True
        references, tables = {}, {}
        for name, value in (("left_members", left), ("right_members", right)):
            if isinstance(value, LayerRef):
                references[name] = value
            else:
                tables[name] = pa.table({"record_identity": pa.array([row["member_key"] for row in value], type=pa.string()),
                    "record_json": pa.array([canonical_value_bytes(row) for row in value], type=pa.binary())})
        with self.records.relations(references, tables=tables) as relations:
            before = relations["left_members"].project("record_identity AS old_key, record_json AS old_value")
            after = relations["right_members"].project("record_identity AS new_key, record_json AS new_value")
            return not before.join(after, "old_key = new_key", how="outer").filter(
                "old_key IS NULL OR new_key IS NULL OR old_value IS DISTINCT FROM new_value").limit(1).fetchone()

    def representation(self, session, state_id, *, materialize=True):
        """Find the available bulk representation within publication protection."""
        session._active()
        row = next(session.read_records([("state", state_id)]))[0]
        if row is None or not row.available:
            raise IntegrityError("state is not available")
        keys = [link.target for batch in session.read_links([("state", state_id)]) for link in batch if link.relation == "representation"]
        representations = [row for batch in session.read_records(keys) for row in batch if row is not None and row.available]
        for representation in representations:
            value = record_value(representation.value)
            if isinstance(value["membership"], dict) and value["membership"].get("kind") == "content":
                self.check_representation(session, value, retained=True)
                return representation.value
        if not materialize:
            raise IntegrityError("state has no existing bulk representation")
        for representation in representations:
            members = representation.value.membership
            if isinstance(members, tuple):
                # One physical writer also materializes small inline states.
                # Their logical identity and provenance remain unchanged.
                identity = f"{state_id}:bulk:{representation.value.representation_id}"
                self.create(session, state_id=state_id, representation_id=identity, unit_id=identity + ":import",
                            entities=_existing_entities(session, sorted({member.occurrence_id for member in members})), members=members)
                return self.representation(session, state_id)
        raise IntegrityError("state has no available bulk representation")

    def manifest(self, session, state_id):
        """Return the stored manifest of a state's retained bulk representation."""

        representation = self.representation(session, state_id)
        return session.ready_states[representation.membership.digest]

    def layers(self, session, state_id):
        """Reuse admitted descriptors only within this protected session."""
        return self._layers(self.manifest(session, state_id))

    def checkpoint(self, session, state_id, *, representation_id, unit_id):
        """Repack complete state values without generating another logical state.

        Every revision already checkpoints membership, so replay depth is zero.
        This physical maintenance also drops unreferenced values from this
        representation; other retained roots keep their own recovery paths.
        """
        session._active()
        existing = next(session.read_records([("state_representation", representation_id)]))[0]
        if existing is not None:
            if not existing.available or existing.value.state_id != state_id:
                raise IntegrityError("existing checkpoint is unavailable or belongs to another state")
            self.check_representation(session, record_value(existing.value), retained=True)
            session.publish(MetadataBatch(unit_id, records=(existing.value,), retained=(("state", state_id),)))
            return existing.value
        source = self.representation(session, state_id)
        manifest = session.ready_states[source.membership.digest]
        references = self._layers(manifest)
        members = self.records.compact(references["membership"])
        with self.records.relations(references) as relations:
            used = relations["membership"].project("record_identity AS member_key, json_extract_string(decode(record_json), '/occurrence_id') AS used_id").aggregate("used_id, min(member_key) AS first_key")
            entities = relations["entities"].join(used, "record_identity = used_id").order("first_key, record_identity").project("record_identity, partition_value, record_json")
            with closing(entities.to_arrow_reader(256)) as reader:
                values = self.records.retain_batches(reader, layer_kind="core-entities", schema=_ENTITIES,
                                                     partition_policy=_ENTITY_POLICY, ordered=False)
        self._match_members({"entities": values, "membership": members})
        content = self._state_content(session, values, members)
        # compact() already compared exact canonical rows. Transfer that scoped
        # proof to publication instead of hashing/comparing membership again.
        session.membership_equivalence.add(tuple(sorted((members.reference.digest, references["membership"].reference.digest))))
        # This same metadata path compares every immutable occurrence digest
        # before updating its physical location. No generation is asserted.
        self._retain_entities(session, values)
        result = core.StateRepresentation(format_version=1, representation_id=representation_id, state_id=state_id,
                                          membership=content, revision_id=source.revision_id)
        session.publish(MetadataBatch(unit_id, records=(result,), retained=(("state", state_id),)))
        return result

    @contextmanager
    def relation(self, session, state_id, *, scope=None, addresses=None, cursor=None, layers=None):
        """Recover unordered keyed values within the caller's protection scope."""
        tables, partitions = {}, None
        if scope is not None:
            scope = tuple(scope)
            record_value(core.StateMembers(member_selector=core.Whole(), scope=scope), core.Selector)
            tables["wanted"] = pa.table({"wanted_key": pa.array(scope, type=pa.string())})
        references = self.layers(session, state_id) if layers is None else layers
        if addresses is not None:
            if scope is not None or cursor is None:
                raise ValueError("native addresses require their owning cursor and no named scope")
            with self.records.relations(references, cursor=cursor) as relations:
                members = relations["membership"].project("record_identity AS member_key, json_extract_string(decode(record_json), '/occurrence_id') AS occurrence_id")
                members = addresses.join(members, "wanted_key = member_key", how="left").project("wanted_key AS member_key, occurrence_id")
                # Materialize compact addresses once before the payload join.
                members.create_view("selection_address_input")
                cursor.execute("CREATE TEMP TABLE selection_addresses AS SELECT * FROM selection_address_input")
                try:
                    yield self._keyed_rows(cursor.table("selection_addresses"), relations["entities"])
                finally:
                    cursor.execute("DROP TABLE selection_addresses")
                    cursor.execute("DROP VIEW selection_address_input")
            return
        if scope is not None:
            bucket_count = references["membership"].partition_policy.bucket_count
            partitions = {"membership": frozenset(partition_bucket(key, bucket_count) for key in scope)}
        if scope is not None and len(scope) <= BATCH_ROWS:
            # Recover only compact named addresses before selecting payload files.
            # The table also retains absent keys and aliases of one occurrence.
            with self.records.relations({"membership": references["membership"]}, partitions=partitions,
                                        tables=tables, identities={"membership": list(scope)}) as relations:
                members = relations["membership"].project("record_identity AS member_key, json_extract_string(decode(record_json), '/occurrence_id') AS occurrence_id")
                members = relations["wanted"].join(members, "wanted_key = member_key", how="left").project("wanted_key AS member_key, occurrence_id")
                addresses = members.to_arrow_table()
            if addresses.nbytes > BATCH_BYTES:
                raise LimitExceededError("named membership addresses exceed the batch byte limit")
            identities = list({identity for identity in addresses.column("occurrence_id").to_pylist() if identity is not None})
            with self.records.relations({"entities": references["entities"]}, tables={"members": addresses},
                                        identities={"entities": identities}) as relations:
                yield self._keyed_rows(relations["members"], relations["entities"])
            return
        with self.records.relations(references, partitions=partitions, tables=tables) as relations:
            members = relations["membership"].project("record_identity AS member_key, json_extract_string(decode(record_json), '/occurrence_id') AS occurrence_id")
            if scope is not None:
                members = relations["wanted"].join(members, "wanted_key = member_key", how="left").project("wanted_key AS member_key, occurrence_id")
            yield self._keyed_rows(members, relations["entities"])

    @staticmethod
    def _keyed_rows(members, entities):
        values = entities.project("record_identity AS entity_id, record_json AS occurrence_record")
        return members.join(values, "occurrence_id = entity_id", how="left").project("member_key, occurrence_id, occurrence_record")

    def rows(self, session, state_id):
        """The row convenience path shares native reads and byte-bounded decoding."""
        with self.relation(session, state_id) as relation, closing(relation.order("member_key").to_arrow_reader(BATCH_ROWS)) as batches:
            with closing(bounded_batches(batches, byte_column="occurrence_record")) as bounded:
                for batch in bounded:
                    for key, payload in zip(batch.column("member_key").to_pylist(), batch.column("occurrence_record").to_pylist(), strict=True):
                        yield key, admit_record(payload)

    def compare(self, session, older, newer, *, sample_limit=20):
        """Compare complete keyed states natively; return only bounded samples."""
        if type(sample_limit) is not int or not 0 <= sample_limit <= BATCH_ROWS:
            raise ValueError("comparison sample limit must be between 0 and 2048")
        layers = {}
        for prefix, identity in (("old", older), ("new", newer)):
            layers.update({prefix + "_" + name: ref for name, ref in self.layers(session, identity).items()})
        memberships = {name: layer for name, layer in layers.items() if name.endswith("_membership")}
        with self.records._cursor() as cursor, self.records.relations(memberships, cursor=cursor) as relations:
            sides = []
            for prefix in ("old", "new"):
                sides.append(relations[prefix + "_membership"].project(
                    f"record_identity AS {prefix}_key, record_json AS {prefix}_member"))
            # Membership has exactly a key and an occurrence ID, canonically
            # encoded. Compare those bytes before decoding the changed addresses.
            changes = sides[0].join(sides[1], "old_key = new_key", how="outer").filter("old_member IS DISTINCT FROM new_member").project(
                "coalesce(old_key,new_key) AS member_key, "
                "json_extract_string(decode(old_member), '/occurrence_id') AS old_id, "
                "json_extract_string(decode(new_member), '/occurrence_id') AS new_id, "
                "CASE WHEN old_key IS NULL THEN 'added' WHEN new_key IS NULL THEN 'removed' ELSE 'changed' END AS change")
            # Materialize compact changes once. Counts need no occurrence values;
            # only the bounded sample crosses Python or opens payload columns.
            try:
                cursor.execute("CREATE TEMP TABLE comparison_changes AS " + changes.sql_query())
                changed = cursor.table("comparison_changes")
                counts = dict(changed.aggregate("change, count(*)", "change").fetchall())
                sample = changed.order("member_key").limit(sample_limit).to_arrow_table()
            finally:
                cursor.execute("DROP TABLE IF EXISTS comparison_changes")
            samples = []
            if sample.num_rows:
                entities = {prefix: layers[prefix + "_entities"] for prefix in ("old", "new")}
                identities = {prefix: list({identity for identity in sample.column(prefix + "_id").to_pylist()
                                           if identity is not None}) for prefix in entities}
                with self.records.relations(entities, identities=identities, tables={"sample": sample}, cursor=cursor) as values:
                    selected = values["sample"]
                    for prefix in entities:
                        records = values[prefix].project(f"record_identity AS {prefix}_entity_id, record_json AS {prefix}_record")
                        selected = selected.join(records, f"{prefix}_id = {prefix}_entity_id", how="left")
                    samples = selected.project(
                        "member_key, old_id, new_id, change, "
                        "json_extract(decode(old_record), '/value') IS DISTINCT FROM json_extract(decode(new_record), '/value') AS value_changed"
                    ).order("member_key").fetchall()
            return {"older": older, "newer": newer, "counts": {name: counts.get(name, 0) for name in ("added", "removed", "changed")},
                    "sample": [dict(zip(("member_key", "older_occurrence", "newer_occurrence", "change", "value_changed"), row, strict=True)) for row in samples]}

    def resolve_membership(self, session, revision, *, full=False):
        """Validate every edit before reducing keys; write only the changed rows.

        The full control and row-update path share ordered edit validation.
        They return physical membership, leaving logical publication and actual
        transformation provenance to the operation lifecycle.
        """
        revision = admit_record(encode_record(revision))
        if not isinstance(revision, core.Revision):
            raise IntegrityError("membership resolution requires a revision")
        base = self.layers(session, revision.base_state_id)["membership"]
        edits = self._edits(session, revision)
        if not edits:
            return base
        return self._resolve_edits(base, edits, full=full)

    @staticmethod
    def _edits(session, revision):
        edits = revision.edits
        if isinstance(edits, core.ContentRef):
            if edits.codec != "json-v1":
                raise IntegrityError("membership edits require their JSON array")
            blob = BlobRef(edits.locator, edits.digest, edits.byte_size, edits.media_type)
            with owned_iterator(session.blobs.read(blob, max_bytes=BATCH_BYTES)) as chunks:
                values = decode_canonical_json_value(b"".join(chunks), label="membership edits")
            value = record_value(revision)
            value["edits"] = values
            edits = admit_record(encode_record(value)).edits
        return edits

    def _resolve_edits(self, base, edits, *, full=False):
        reference = base.reference
        # Only edit metadata crosses Python. Existing canonical member payloads
        # pass through DuckDB and Arrow without decoding and encoding again.
        rows = []
        for edit in edits:
            put = isinstance(edit, core.Put)
            payload = canonical_value_bytes(record_value(core.Membership(member_key=edit.member_key, occurrence_id=edit.occurrence_id), core.Membership)) if put else None
            rows.append((edit.member_key, edit.sequence, "put" if put else "remove", payload))
        table = pa.Table.from_arrays([pa.array(column, type=kind) for column, kind in zip(zip(*rows, strict=True),
                                      (pa.string(), pa.int64(), pa.string(), pa.binary()), strict=True)],
                                     names=["member_key", "sequence", "action", "payload"])
        with self.records.relations({"base": base}, tables={"edits": table}) as relations:
            original = relations["base"].project("record_identity, partition_value, record_json")
            ordered = relations["edits"].project("*, lag(action) OVER (PARTITION BY member_key ORDER BY sequence) AS previous_action")
            checked = ordered.join(original.project("record_identity AS existing_key"), "member_key = existing_key", how="left")
            if checked.filter("action = 'remove' AND (previous_action = 'remove' OR (previous_action IS NULL AND existing_key IS NULL))").limit(1).fetchone():
                raise IntegrityError("membership removal requires a live key at that sequence")
            latest = ordered.project("*, row_number() OVER (PARTITION BY member_key ORDER BY sequence DESC) AS latest").filter("latest = 1")
            kept = original.join(latest.project("member_key"), "record_identity = member_key", how="anti")
            inserted = latest.filter("action = 'put'").project("member_key AS record_identity, member_key AS partition_value, payload AS record_json")
            if full:
                with closing(kept.union(inserted).order("record_identity").to_arrow_reader(256)) as reader:
                    return self.records.retain_batches(reader, layer_kind=reference.layer_kind, schema=base.schema,
                                                       partition_policy=base.partition_policy)
            changes = latest.project("member_key AS record_identity, member_key AS partition_value, payload AS record_json")
            with closing(changes.to_arrow_reader(256)) as reader:
                return self.records.apply_changes(base, reader)

    def from_occurrences(self, session, *, occurrence_ids, state_id, representation_id, unit_id):
        """Group existing retained occurrences through the ordinary root writer."""
        with owned_iterator(occurrence_ids) as source:
            occurrence_ids = tuple(source)
        return self.create(session, state_id=state_id, representation_id=representation_id, unit_id=unit_id,
                           entities=_existing_entities(session, occurrence_ids),
                           members=(core.Membership(member_key=identity, occurrence_id=identity) for identity in occurrence_ids))

    def revision_representation(self, session, revision, *, representation_id, occurrences_state_id):
        """Prepare complete retained bytes; the caller publishes the new state."""
        revision = admit_record(encode_record(revision))
        if not isinstance(revision, core.Revision):
            raise IntegrityError("state revision requires a revision record")
        edits = self._edits(session, revision)
        references = self.layers(session, revision.base_state_id)
        base_members = references["membership"]
        members = self._resolve_edits(base_members, edits) if edits else base_members
        delta = self.layers(session, occurrences_state_id)["entities"]
        # These occurrences already exist. Reusing them never invents generation;
        # changed values were produced by the preceding value-edit operation.
        base = references["entities"]
        self._check_puts(edits, base, delta)
        # Both layers already passed immutable entity admission in the ledger.
        # Repeated IDs therefore mean the same bytes. Exclude them by ID alone;
        # never scan or rewrite the base payloads to append new occurrences.
        entities = self.records.union_disjoint(base, delta, exclude_existing=True)
        # The admitted base is complete. Resolution keeps/removes its members
        # or inserts checked IDs; the union contains both sources. This proves
        # completeness without joining all unchanged members again.
        content = self._state_content(session, entities, members)
        representation = core.StateRepresentation(format_version=1, representation_id=representation_id, state_id=revision.result_state_id,
                                                   membership=content, revision_id=revision.revision_id)
        key = "state_representation", representation_id
        session.computed_records[key] = (encode_record(representation), MetadataLink(
            ("state", revision.result_state_id), "resolved_revision", sha256_digest(encode_record(revision)), key))
        return representation

    def changed_keys(self, session, state_id, base_id, cursor):
        """Locate certified edit keys without scanning complete memberships.

        Only publisher-created resolver links certify completeness. Their exact
        revision digest and immutable result bind the proof. The caller still
        diffs old/new addresses at these keys to discard canceled edits. Missing
        history and external ledgers fall back to a complete native comparison.
        """
        if getattr(session.ledger, "read_only", False):
            return None
        cursor.execute("CREATE TEMP TABLE revision_keys (changed_key VARCHAR)")
        seen = set()
        while state_id != base_id:
            if state_id in seen:
                raise IntegrityError("resolved revision history contains a cycle")
            seen.add(state_id)
            certified = None
            for links in session.read_links([("state", state_id)]):
                for link in links:
                    if link.relation != "resolved_revision":
                        continue
                    row = next(session.read_records([link.target]))[0]
                    if row is None or not row.retained or not row.available or not isinstance(row.value, core.StateRepresentation):
                        continue
                    representation = row.value
                    if representation.state_id != state_id or representation.revision_id is None:
                        raise IntegrityError("resolved revision certificate names another state")
                    row = next(session.read_records([("revision", representation.revision_id)]))[0]
                    if row is None or not row.retained or not row.available:
                        continue
                    revision = row.value
                    if not isinstance(revision, core.Revision) or sha256_digest(encode_record(revision)) != link.label or revision.result_state_id != state_id:
                        raise IntegrityError("resolved revision certificate differs from its revision")
                    certified = revision
                    break
                if certified is not None:
                    break
            if certified is None:
                cursor.execute("DROP TABLE revision_keys")
                return None
            keys = pa.table({"changed_key": pa.array([edit.member_key for edit in self._edits(session, certified)], type=pa.string())})
            cursor.register("revision_key_input", keys)
            cursor.execute("INSERT INTO revision_keys SELECT DISTINCT changed_key FROM revision_key_input")
            cursor.unregister("revision_key_input")
            state_id = certified.base_state_id
        return cursor.table("revision_keys").distinct()

    def _check_puts(self, edits, base, delta):
        identities = (edit.occurrence_id for edit in edits if isinstance(edit, core.Put))
        with owned_iterator(bounded_rows(identities, size=lambda identity: len(identity.encode("utf-8")))) as groups:
            for group in groups:
                wanted = sorted(set(group))
                for layer in (delta, base):
                    if not wanted:
                        break
                    table = pa.table({"wanted_id": pa.array(wanted, type=pa.string())})
                    with self.records.relations({"entities": layer}, tables={"wanted": table},
                                                identities={"entities": wanted}) as relations:
                        present = relations["entities"].project("record_identity AS present_id")
                        wanted = [row[0] for row in relations["wanted"].join(
                            present, "wanted_id = present_id", how="anti").fetchall()]
                if wanted:
                    raise IntegrityError("membership put requires an available occurrence in its revision inputs")
