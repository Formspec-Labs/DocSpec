"""Typed selections, exact retained values, and recovery from immutable parents."""

from contextlib import closing
from dataclasses import dataclass

import pyarrow as pa
import msgspec

from docspec.adapters.storage.batches import ENCODED_RECORD_SCHEMA, encoded_batches
from docspec.adapters.storage.selection import extracted_rows, field_paths
from docspec.domain import core
from docspec.domain.core_admission import admit_record, record_value
from docspec.domain.core_encoding import ABSENT, content_evidence, json_evidence, member_bytes, member_stream_evidence, selector_value
from docspec.domain.identity import canonical_value_bytes, decode_canonical_json_value, sha256_digest
from docspec.domain.references import BlobRef, LayerRef
from docspec.domain.storage import RecordSchema, PartitionPolicy, partition_bucket
from docspec.domain.selected_values import validate_fields_value
from docspec.domain.streams import owned_iterator
from docspec.errors import IntegrityError
from docspec.ports.core_ledger import MetadataBatch, MetadataLink
from docspec.ports.record_storage import AdmittedRecordLayer, BATCH_BYTES, BATCH_ROWS


_MEDIA = "application/vnd.docspec.selected-members+json"
_SCHEMA = RecordSchema("core-selected-members:1", ("member_key", "occurrence_id", "comparison_json", "sort_key", "content"), "member_key", "member_key")
_SOURCE = pa.schema([("member_key", pa.string()), ("occurrence_id", pa.string()), ("payload", pa.string())])
_POLICY = PartitionPolicy("core-key-buckets:1", 64)
_COMPUTED = "computed_selection"
_DELTA = pa.schema([("member_key", pa.string()), ("payload", pa.binary())])


@dataclass(frozen=True)
class _Members:
    """An evaluator-certified retained layer plus bounded transient changes."""
    selected: core.SelectedValue
    layer: AdmittedRecordLayer
    changes: dict[str, bytes | None]
    metadata_bytes: int

    @property
    def byte_size(self):
        return self.metadata_bytes + sum(len(key.encode()) + (len(value) if value is not None else 0) + 16
                                         for key, value in self.changes.items())


def _selector_key(definition):
    return sha256_digest(canonical_value_bytes(selector_value(definition)))


def _member_record(key, entity, comparison, sort_key, content):
    return canonical_value_bytes({"member_key": key, "occurrence_id": entity, "comparison_json": comparison.decode(),
                                  "sort_key": sort_key, "content": content})



def _record(session, key):
    with owned_iterator(session.read_records([key])) as batches:
        row = next(batches)[0]
    if row is None or not row.available:
        raise IntegrityError("selected value requires its available record")
    return row.value


def _content_bytes(session, content):
    reference = BlobRef(content.locator, content.digest, content.byte_size, content.media_type)
    with owned_iterator(session.blobs.read(reference, max_bytes=BATCH_BYTES)) as chunks:
        return b"".join(chunks)


class CoreSelectionStorage:
    def __init__(self, records, states):
        self.records, self.states = records, states

    @staticmethod
    def _sort(session, definition):
        if definition.sort_rule is None:
            return (), False
        rule = _record(session, ("operation_definition", definition.sort_rule))
        config = rule.configuration
        if (rule.implementation_id != "docspec.sort.canonical-json" or rule.implementation_version != "1"
                or set(config) != {"pointers", "descending"} or not isinstance(config["pointers"], list)
                or type(config["descending"]) is not bool):
            raise IntegrityError("unsupported selected-member sorting rule")
        field_paths([(str(index), pointer) for index, pointer in enumerate(config["pointers"])])
        return tuple(config["pointers"]), config["descending"]

    def _member_values(self, session, parent_id, definition, *, sort_fields=()):
        selector = definition.member_selector
        fields = None if isinstance(selector, core.Whole) else [(field.label, field.pointer) for field in selector.selectors]
        with self.states.relation(session, parent_id, scope=definition.scope) as relation:
            inline = relation.filter("occurrence_id IS NULL OR json_extract_string(decode(occurrence_record), '/value/kind') = 'inline'")
            source = inline.project("member_key, occurrence_id, json_extract(decode(occurrence_record), '/value/value')::VARCHAR AS payload")
            with closing(extracted_rows(source, fields=fields, sort_fields=sort_fields)) as rows:
                for key, entity, value, sort_key in rows:
                    yield key, entity, value, sort_key, None
            external = relation.filter("json_extract_string(decode(occurrence_record), '/value/kind') = 'content'")
            json_values = external.filter("json_extract_string(decode(occurrence_record), '/value/codec') = 'json-v1'")
            def inputs():
                with closing(json_values.to_arrow_reader(256)) as reader:
                    for batch in reader:
                        for key, entity, payload in zip(*(column.to_pylist() for column in batch.columns), strict=True):
                            content = admit_record(payload).value
                            yield key, entity, _content_bytes(session, content).decode("utf-8")
            with self.records._cursor() as cursor, closing(inputs()) as source:
                for batch in encoded_batches(source, _SOURCE, byte_column=2):
                    with closing(extracted_rows(cursor.from_arrow(batch), fields=fields, sort_fields=sort_fields)) as rows:
                        for key, entity, value, sort_key in rows:
                            yield key, entity, value, sort_key, None
            opaque = external.filter("json_extract_string(decode(occurrence_record), '/value/codec') = 'bytes-v1'")
            with closing(opaque.to_arrow_reader(256)) as reader:
                for batch in reader:
                    if batch.num_rows and (fields is not None or sort_fields):
                        raise IntegrityError("JSON fields require JSON values, not opaque bytes")
                    for key, entity, payload in zip(*(column.to_pylist() for column in batch.columns), strict=True):
                        content = admit_record(payload).value
                        session.blobs.stat(BlobRef(content.locator, content.digest, content.byte_size, content.media_type))
                        yield key, entity, ["opaque", content.codec, content.digest, content.byte_size], "[]", record_value(content, core.ContentRef)

    def _computed_rows(self, session, parent_id, definition):
        sort_fields, descending = self._sort(session, definition)
        material_entities = definition.member_selector.comparison == "identity"
        with closing(self._member_values(session, parent_id, definition, sort_fields=sort_fields)) as values:
            for key, entity, value, sort_key, content in values:
                comparison = member_bytes(value, member_key=key if definition.material_keys else None,
                                          entity_id=entity if material_entities else None)
                yield key, entity, comparison, sort_key, content

    def _remember(self, session, parent_id, definition, plan):
        key = parent_id, _selector_key(definition)
        session.member_selections.pop(key, None)
        size = plan.byte_size
        if size > BATCH_BYTES:
            return
        while session.member_selections and (len(session.member_selections) >= BATCH_ROWS or
                sum(value.byte_size for value in session.member_selections.values()) + size > BATCH_BYTES):
            session.member_selections.pop(next(iter(session.member_selections)))
        session.member_selections[key] = plan

    def _certified(self, session, parent_id, definition):
        label = _selector_key(definition)
        cached = session.member_selections.get((parent_id, label))
        if cached is not None:
            row = next(session.read_records([("selected_value", cached.selected.selected_value_id)]))[0]
            if row is not None and row.retained and row.available:
                return cached
            session.member_selections.pop((parent_id, label), None)
        # Export admission cannot treat links supplied in an external snapshot
        # as evidence that this process's evaluator computed a parent value.
        if getattr(session.ledger, "read_only", False):
            return None
        for links in session.read_links([("state", parent_id)]):
            keys = [link.target for link in links if link.relation == _COMPUTED and link.label == label]
            for batch in session.read_records(keys):
                for row in batch:
                    if row is None or not row.retained or not row.available:
                        continue
                    selected = row.value
                    if (not isinstance(selected, core.SelectedValue) or selected.origin != core.Origin(parent_entity_id=parent_id)
                            or selector_value(selected.definition) != selector_value(definition)
                            or not isinstance(selected.value, core.ContentRef) or selected.member_origins != selected.value):
                        raise IntegrityError("computed selection certificate differs from its retained record")
                    manifest = self._manifest(session, selected)
                    plan = _Members(selected, self.records.available(self._reference(manifest)), {},
                                    len(canonical_value_bytes(record_value(selected))))
                    self._remember(session, parent_id, definition, plan)
                    return plan
        return None

    @staticmethod
    def _base_state(session, state_id):
        # Revision ancestry only locates a candidate. It never establishes which
        # addresses changed, and does not require old parent payloads to exist.
        for links in session.read_links([("state", state_id)]):
            keys = [link.target for link in links if link.relation == "representation"]
            for batch in session.read_records(keys):
                for row in batch:
                    if row is None or not isinstance(row.value, core.StateRepresentation):
                        continue
                    revision_id = row.value.revision_id
                    if revision_id is not None:
                        revision = next(session.read_records([("revision", revision_id)]))[0]
                        if revision is not None and isinstance(revision.value, core.Revision):
                            if revision.value.result_state_id != state_id:
                                raise IntegrityError("revision ancestry names another result state")
                            return revision.value.base_state_id
        return None

    def _changed_members(self, session, parent_id, definition, plan):
        membership = self.states.layers(session, parent_id)["membership"]
        tables = {"changes": self._changes(plan)}
        if definition.scope is not None:
            tables["wanted"] = pa.table({"wanted_key": pa.array(definition.scope, type=pa.string())})
        with self.records.relations({"membership": membership, "selected": plan.layer}, tables=tables) as relations:
            current = relations["membership"].project(
                "record_identity AS current_key, json_extract_string(decode(record_json), '/occurrence_id') AS current_id")
            if definition.scope is not None:
                current = current.join(relations["wanted"], "current_key = wanted_key", how="semi")
            prior = self._member_relation(relations, plan).project(
                "record_identity AS prior_key, json_extract_string(decode(record_json), '/occurrence_id') AS prior_id")
            changed = prior.join(current, "prior_key = current_key", how="outer").filter("prior_id IS DISTINCT FROM current_id")
            return [row[0] for row in changed.project("coalesce(prior_key, current_key) AS member_key").limit(BATCH_ROWS + 1).fetchall()]

    def _members(self, session, parent_id, definition):
        """Use certified values and compare actual compact member addresses.

        Revision metadata locates a retained candidate; it is not proof that
        its advertised edits are complete. Immutable occurrence IDs establish
        unchanged values only after a native diff of the actual memberships.
        No retained bytes or metadata are written by this evaluation path.
        """
        _record(session, ("state", parent_id))
        current, seen = parent_id, set()
        while current is not None and len(seen) < BATCH_ROWS:
            if current in seen:
                raise IntegrityError("computed selection revision history contains a cycle")
            seen.add(current)
            plan = self._certified(session, current, definition)
            if plan is not None:
                break
            current = self._base_state(session, current)
        else:
            return None
        if plan.selected.origin.parent_entity_id == parent_id and not plan.changes:
            return plan
        changed = self._changed_members(session, parent_id, definition, plan)
        if len(changed) > BATCH_ROWS or len(set(changed) | plan.changes.keys()) > BATCH_ROWS:
            return None
        if not changed:
            self._remember(session, parent_id, definition, plan)
            return plan
        updates = dict(plan.changes)
        scoped = msgspec.structs.replace(definition, scope=tuple(sorted(changed)))
        size = plan.byte_size
        with closing(self._computed_rows(session, parent_id, scoped)) as rows:
            for row in rows:
                key, entity = row[:2]
                payload = None if entity is None and definition.scope is None else _member_record(*row)
                previous = updates.get(key)
                size += (len(payload) if payload is not None else 0) - (len(previous) if previous is not None else 0)
                if key not in updates:
                    size += len(key.encode()) + 16
                if size > BATCH_BYTES:
                    return None
                updates[key] = payload
        result = _Members(plan.selected, plan.layer, updates, plan.metadata_bytes)
        self._remember(session, parent_id, definition, result)
        return result

    def _member_relation(self, relations, plan):
        original = relations["selected"].project("record_identity, partition_value, record_json")
        if not plan.changes:
            return original
        changes = relations["changes"]
        kept = original.join(changes.project("member_key"), "record_identity = member_key", how="anti")
        added = changes.filter("payload IS NOT NULL").project(
            "member_key AS record_identity, member_key AS partition_value, payload AS record_json")
        return kept.union(added)

    @staticmethod
    def _changes(plan):
        return pa.Table.from_arrays([pa.array(list(plan.changes), type=pa.string()),
                                     pa.array(list(plan.changes.values()), type=pa.binary())], schema=_DELTA)

    def _write_members(self, session, parent_id, definition):
        _, descending = self._sort(session, definition)
        plan = self._members(session, parent_id, definition)
        if plan is not None:
            admitted = plan.layer
            if plan.changes:
                touched = frozenset(partition_bucket(key, admitted.partition_policy.bucket_count) for key in plan.changes)
                with self.records.relations({"selected": admitted}, partitions={"selected": touched},
                                            tables={"changes": self._changes(plan)}) as relations:
                    output = self._member_relation(relations, plan).order("record_identity")
                    with closing(output.to_arrow_reader(256)) as rows:
                        admitted = self.records.retain_batches(rows, layer_kind="core-selected-members", schema=_SCHEMA,
                            partition_policy=admitted.partition_policy, base=admitted, replace_partitions=touched)
        else:
            def rows():
                with closing(self._computed_rows(session, parent_id, definition)) as values:
                    for row in values:
                        yield row[0], row[0], _member_record(*row)
            admitted = self.records.retain_batches(encoded_batches(rows(), ENCODED_RECORD_SCHEMA, byte_column=2),
                                                   layer_kind="core-selected-members", schema=_SCHEMA, partition_policy=_POLICY, ordered=False)
        manifest = {"format": "docspec-selected-members", "version": 1, "layer": admitted.reference.to_dict(),
                    "ordered": definition.sort_rule is not None, "descending": descending}
        return manifest, admitted

    def _recover_rows(self, session, parent_id, definition):
        """Sort an Arrow stream in the native engine without retaining new files."""
        _, descending = self._sort(session, definition)
        schema = pa.schema([("member_key", pa.string()), ("occurrence_id", pa.string()), ("encoded", pa.binary()), ("sort_key", pa.string())])
        with closing(self._computed_rows(session, parent_id, definition)) as values:
            batches = encoded_batches((row[:4] for row in values), schema, byte_column=2)
            with closing(batches), closing(pa.RecordBatchReader.from_batches(schema, batches)) as source, self.records._cursor() as cursor:
                cursor.register("computed", source.__arrow_c_stream__())
                order = self._ordering(definition.sort_rule is not None, descending)
                with closing(cursor.sql(f"SELECT member_key, occurrence_id, encoded FROM computed ORDER BY {order}").to_arrow_reader(256)) as reader:
                    for batch in reader:
                        yield from zip(*(column.to_pylist() for column in batch.columns), strict=True)

    @staticmethod
    def _ordering(ordered, descending):
        return ("sort_key DESC, member_key" if descending else "sort_key, member_key") if ordered else "encoded, member_key"

    def _comparison_rows(self, relation, *, ordered, descending):
        rows = relation.project(
            "record_identity AS member_key, json_extract_string(decode(record_json), '/occurrence_id') AS occurrence_id, "
            "encode(json_extract_string(decode(record_json), '/comparison_json')) AS encoded, "
            "json_extract_string(decode(record_json), '/sort_key') AS sort_key")
        with closing(rows.order(self._ordering(ordered, descending)).project("member_key, occurrence_id, encoded").to_arrow_reader(256)) as reader:
            for batch in reader:
                yield from zip(*(column.to_pylist() for column in batch.columns), strict=True)

    def _ordered_rows(self, manifest):
        with self.records.relations({"selected": self._reference(manifest)}) as relations:
            yield from self._comparison_rows(relations["selected"], ordered=manifest["ordered"], descending=manifest["descending"])

    def _planned_rows(self, session, definition, plan):
        _, descending = self._sort(session, definition)
        with self.records.relations({"selected": plan.layer}, tables={"changes": self._changes(plan)}) as relations:
            yield from self._comparison_rows(self._member_relation(relations, plan),
                                            ordered=definition.sort_rule is not None, descending=descending)

    def _reference(self, manifest):
        if (not isinstance(manifest, dict) or set(manifest) != {"format", "version", "layer", "ordered", "descending"}
                or manifest["format"] != "docspec-selected-members" or type(manifest["version"]) is not int or manifest["version"] != 1
                or type(manifest["ordered"]) is not bool or type(manifest["descending"]) is not bool):
            raise IntegrityError("invalid selected-member manifest")
        reference = LayerRef.from_dict(manifest["layer"])
        if reference.layer_kind != "core-selected-members" or self.records.schema(reference) != _SCHEMA:
            raise IntegrityError("invalid selected-member layer")
        return reference

    def _manifest(self, session, selected, *, retained=True):
        content = selected.value
        if not isinstance(content, core.ContentRef) or content.media_type != _MEDIA:
            raise IntegrityError("selected members require their retained manifest")
        manifest = session.ready_selections.get(content.digest)
        if manifest is None:
            manifest = decode_canonical_json_value(_content_bytes(session, content), label="selected-member manifest")
            reference = self._reference(manifest)
            if retained:
                self.records.available(reference)
            else:
                self._admit_rows(self.records.admit(reference), selected.definition)
            session.ready_selections[content.digest] = manifest
        _, descending = self._sort(session, selected.definition)
        if manifest["ordered"] != (selected.definition.sort_rule is not None) or manifest["descending"] != descending:
            raise IntegrityError("selected-member storage differs from its sorting definition")
        return manifest

    @staticmethod
    def _admit_rows(admitted, definition):
        """Check externally supplied retained rows without requiring their parent."""
        wanted = None if definition.scope is None else set(definition.scope)
        with closing(admitted.batches()) as batches:
            for batch in batches:
                for payload in batch.column("record_json").to_pylist():
                    row = decode_canonical_json_value(payload, label="selected-member row")
                    key, entity = row["member_key"], row["occurrence_id"]
                    if not isinstance(key, str) or (entity is not None and (not isinstance(entity, str) or not entity)):
                        raise IntegrityError("invalid selected-member origin")
                    if not isinstance(row["comparison_json"], str) or not isinstance(row["sort_key"], str):
                        raise IntegrityError("selected-member encodings must be canonical JSON strings")
                    if wanted is not None:
                        if key not in wanted:
                            raise IntegrityError("selected-member rows differ from their named scope")
                        wanted.remove(key)
                    encoded = row["comparison_json"].encode("utf-8")
                    value = decode_canonical_json_value(encoded, label="selected-member comparison")
                    if entity is None:
                        selected = ABSENT
                    else:
                        if not isinstance(value, list) or len(value) < 2 or value[0] != "present":
                            raise IntegrityError("invalid selected-member presence")
                        selected = value[1]
                        if isinstance(definition.member_selector, core.JsonFields):
                            validate_fields_value(definition.member_selector.selectors, selected)
                        elif not isinstance(selected, list) or not (
                            (len(selected) == 2 and selected[0] == "present") or (len(selected) == 4 and selected[0:2] == ["opaque", "bytes-v1"])):
                            raise IntegrityError("invalid whole-member selection")
                    expected = member_bytes(selected, member_key=key if definition.material_keys else None,
                                            entity_id=entity if definition.member_selector.comparison == "identity" else None)
                    if encoded != expected:
                        raise IntegrityError("selected-member comparison differs from its origin or materiality")
                    opaque = isinstance(selected, list) and len(selected) == 4 and selected[0] == "opaque"
                    if opaque != (row["content"] is not None):
                        raise IntegrityError("opaque selection requires its recoverable content reference")
                    if opaque:
                        content = record_value(row["content"], core.ContentRef)
                        if selected != ["opaque", content["codec"], content["digest"], content["byte_size"]]:
                            raise IntegrityError("opaque selection differs from its retained bytes")
                    sort_key = decode_canonical_json_value(row["sort_key"].encode(), label="selected-member sort key")
                    if not isinstance(sort_key, list):
                        raise IntegrityError("invalid selected-member sort key")
        if wanted:
            raise IntegrityError("selected-member rows omit a named member")

    def _single(self, session, definition, parent_id):
        entity = _record(session, ("entity", parent_id))
        return self._select_value(session, definition, entity.value, parent_id)

    def _select_value(self, session, definition, value, parent_id):
        if isinstance(definition, core.Whole):
            if isinstance(value, core.ContentRef):
                session.check_content(value, retained=True)
            return value
        if not isinstance(definition, core.JsonFields):
            raise IntegrityError("single-value selection requires whole or JSON fields")
        if isinstance(value, core.InlineValue):
            payload = canonical_value_bytes(value.value).decode()
        else:
            if value.codec != "json-v1":
                raise IntegrityError("JSON fields require a JSON value")
            payload = _content_bytes(session, value).decode()
        table = pa.table({"member_key": [""], "occurrence_id": [parent_id], "payload": [payload]}, schema=_SOURCE)
        fields = [(field.label, field.pointer) for field in definition.selectors]
        with self.records._cursor() as cursor, closing(extracted_rows(cursor.from_arrow(table), fields=fields)) as rows:
            return core.InlineValue(value=next(rows)[2])

    def retain(self, session, *, selected_value_id, definition, origin, from_parent=False, unit_id=None):
        session._active()
        if session.selections is not self:
            raise IntegrityError("publication must use the configured selected-value owner")
        record_value(definition, core.Selector)
        if isinstance(definition, core.StateMembers):
            if from_parent:
                value = core.FromParent()
            else:
                manifest, admitted = self._write_members(session, origin.parent_entity_id, definition)
                value = session.retain_value(manifest, media_type=_MEDIA)
                session.ready_selections[value.digest] = manifest
        else:
            value = core.FromParent() if from_parent else self._single(session, definition, origin.parent_entity_id)
        selected = core.SelectedValue(format_version=1, selected_value_id=selected_value_id, definition=definition, origin=origin,
                                      value=value, member_origins=() if from_parent or not isinstance(definition, core.StateMembers) else value)
        key = "selected_value", selected_value_id
        computed = isinstance(definition, core.StateMembers) and not from_parent and origin == core.Origin(parent_entity_id=origin.parent_entity_id)
        if computed:
            payload = canonical_value_bytes(record_value(selected))
            link = MetadataLink(("state", origin.parent_entity_id), _COMPUTED, _selector_key(definition), key)
            session.computed_selections[key] = payload, link
        try:
            session.publish(MetadataBatch(unit_id or selected_value_id + ":retain", records=(selected,), retained=(key,)))
        finally:
            session.computed_selections.pop(key, None)
        if computed:
            self._remember(session, origin.parent_entity_id, definition, _Members(selected, admitted, {}, len(payload)))
        return selected

    def rows(self, session, selected):
        """Yield exact member context and canonical comparison bytes, not a hash."""
        session._active()
        if not isinstance(selected.definition, core.StateMembers):
            raise IntegrityError("member rows require a state-member selection")
        if isinstance(selected.value, core.FromParent):
            yield from self._recover_rows(session, selected.origin.parent_entity_id, selected.definition)
        else:
            yield from self._ordered_rows(self._manifest(session, selected))

    def check_selection(self, session, selected, *, retained=False):
        """Use the same evaluator for new recovery promises and their readers."""
        if isinstance(selected.definition, core.StateMembers):
            if isinstance(selected.value, core.FromParent):
                if not retained:
                    with closing(self.rows(session, selected)) as rows:
                        for _ in rows:
                            pass
                return
            manifest = self._manifest(session, selected, retained=retained)
            if selected.member_origins != selected.value:
                raise IntegrityError("direct selected members require their complete retained origins")
            reference = self._reference(manifest)
            with self.records.relations({"rows": reference}) as relations:
                contents = relations["rows"].filter("json_type(decode(record_json), '/content') != 'NULL'").project("json_extract(decode(record_json), '/content')::VARCHAR AS content")
                with closing(contents.to_arrow_reader(256)) as reader:
                    for batch in reader:
                        for payload in batch.column(0).to_pylist():
                            # Content references are records, never new values
                            # extracted from the user's opaque bytes.
                            content = msgspec.convert(msgspec.json.decode(payload), type=core.ContentRef, strict=True)
                            session.check_content(content, retained=retained)
        elif isinstance(selected.value, core.FromParent) and not retained:
            self._single(session, selected.definition, selected.origin.parent_entity_id)

    def evidence(self, session, selected):
        session._active()
        if isinstance(selected.definition, core.StateMembers):
            with closing(self.rows(session, selected)) as rows:
                return member_stream_evidence((encoded for _, _, encoded in rows), ordered=selected.definition.sort_rule is not None)
        value = self._value_reference(session, selected)
        identity = selected.origin.parent_entity_id if selected.definition.comparison == "identity" else None
        return json_evidence(value.value, entity_id=identity) if isinstance(value, core.InlineValue) else content_evidence(value, entity_id=identity)

    def binding_evidence(self, session, binding, selector, *, guard=None):
        """Evaluate a declared binding with the same retained-value evaluator."""
        session._active()
        record_value(selector, core.Selector)
        if isinstance(binding, core.SelectedInput):
            selected = _record(session, ("selected_value", binding.selected_value_id))
            if isinstance(selector, core.Whole):
                if selector.comparison == "identity":
                    return selector, json_evidence(
                        [selected.selected_value_id, selector_value(selected.definition)], entity_id=selected.selected_value_id)
                return selected.definition, self.evidence(session, selected)
            if selector_value(selector) == selector_value(selected.definition):
                return selected.definition, self.evidence(session, selected)
            raise IntegrityError("a dependency on selected data must consume the whole selection or repeat its definition")
        if isinstance(binding, core.StateInput):
            parent_id = binding.state_id
            identity_selector = selector if isinstance(selector, core.Whole) and selector.comparison == "identity" else None
            if isinstance(selector, core.Whole):
                selector = core.StateMembers(member_selector=selector, material_keys=True)
            if not isinstance(selector, core.StateMembers):
                raise IntegrityError("a state dependency requires whole or state-member selection")
        elif isinstance(binding, core.WholeInput):
            parent_id = binding.entity_id
            identity_selector = None
            if isinstance(selector, core.StateMembers):
                raise IntegrityError("a value dependency cannot select state members")
        else:
            raise IntegrityError("unknown dependency input binding")
        selected = core.SelectedValue(format_version=1, selected_value_id="dependency-evaluation", definition=selector,
                                      origin=core.Origin(parent_entity_id=parent_id), value=core.FromParent())
        plan = self._members(session, parent_id, selector) if isinstance(selector, core.StateMembers) else None
        if plan is None:
            evidence = self.evidence(session, selected)
        else:
            if guard is not None:
                guard([("selected_value", plan.selected.selected_value_id)])
            with closing(self._planned_rows(session, selector, plan)) as rows:
                evidence = member_stream_evidence((encoded for _, _, encoded in rows), ordered=selector.sort_rule is not None)
        if identity_selector is not None:
            return identity_selector, json_evidence([evidence.codec, evidence.digest, evidence.byte_size], entity_id=parent_id)
        return selector, evidence

    def value(self, session, selected):
        value = self._value_reference(session, selected)
        if isinstance(value, core.InlineValue):
            return decode_canonical_json_value(canonical_value_bytes(value.value))
        payload = _content_bytes(session, value)
        return decode_canonical_json_value(payload) if value.codec == "json-v1" else payload

    def _value_reference(self, session, selected):
        session._active()
        if isinstance(selected.definition, core.StateMembers):
            raise IntegrityError("use the bounded row reader for selected members")
        return self._single(session, selected.definition, selected.origin.parent_entity_id) if isinstance(selected.value, core.FromParent) else selected.value

    def read_chunks(self, session, selected, *, max_bytes=None):
        """Read a whole opaque value without imposing the small JSON value limit."""
        value = self._value_reference(session, selected)
        if isinstance(value, core.InlineValue):
            payload = canonical_value_bytes(value.value)
            if max_bytes is not None and len(payload) > max_bytes:
                from docspec.errors import LimitExceededError
                raise LimitExceededError("selected value exceeds the requested byte limit")
            yield payload
        else:
            reference = BlobRef(value.locator, value.digest, value.byte_size, value.media_type)
            with owned_iterator(session.blobs.read(reference, max_bytes=max_bytes)) as chunks:
                for chunk in chunks:
                    session._active()
                    yield chunk
