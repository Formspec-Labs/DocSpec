"""Typed selections, exact retained values, and recovery from immutable parents."""

from contextlib import ExitStack, closing
from dataclasses import dataclass

import duckdb
import pyarrow as pa
import msgspec

from docspec.adapters.storage.batches import encoded_batches
from docspec.adapters.storage.selection import extracted_rows, field_paths
from docspec.domain import core
from docspec.domain.core_admission import record_value
from docspec.domain.core_encoding import ABSENT, content_evidence, json_evidence, member_bytes, member_stream_evidence, selector_value
from docspec.domain.identity import canonical_value_bytes, decode_canonical_json_value, sha256_digest
from docspec.domain.references import BlobRef, LayerRef
from docspec.domain.storage import RecordSchema, PartitionPolicy
from docspec.domain.selected_values import validate_fields_value
from docspec.domain.streams import owned_iterator
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.core_ledger import MetadataBatch, MetadataLink
from docspec.ports.record_storage import AdmittedRecordLayer, BATCH_BYTES, BATCH_ROWS, bounded_batches


_MEDIA = "application/vnd.docspec.selected-members+json"
_COLUMNS = (("occurrence_id", "string"), ("sort_key", "string"), ("content", "binary"))
_ROWS = pa.schema([("record_identity", pa.string()), ("partition_value", pa.string()), ("record_json", pa.binary()),
                   ("occurrence_id", pa.string()), ("sort_key", pa.string()), ("content", pa.binary())])
_SCHEMA = RecordSchema("core-selected-members:2", tuple(_ROWS.names), "record_identity", "partition_value", _COLUMNS)
_SOURCE = pa.schema([("member_key", pa.string()), ("occurrence_id", pa.string()), ("payload", pa.string())])
_POLICY = PartitionPolicy("core-keys:1", 1)
_COMPUTED = "computed_selection"
_ROW_COLUMNS = ", ".join(_ROWS.names)


@dataclass
class _Members:
    """Retained base and optional session-owned native changes."""
    selected: core.SelectedValue
    layer: AdmittedRecordLayer
    evidence: core.ComparisonEvidence
    owner: ExitStack | None = None
    cursor: duckdb.DuckDBPyConnection | None = None
    changes: duckdb.DuckDBPyRelation | None = None
    resolved_evidence: core.ComparisonEvidence | None = None

    def close(self):
        if self.owner is not None:
            self.owner.close()


def _selector_key(definition):
    return sha256_digest(canonical_value_bytes(selector_value(definition)))


def _member_record(key, entity, comparison, sort_key, content):
    return key, key, comparison, entity, sort_key, None if content is None else canonical_value_bytes(content)


def _member_size(row):
    return 0 if row is None else sum(len(value.encode() if isinstance(value, str) else value) for value in row if value is not None)



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

    def _member_values(self, session, parent_id, definition, *, sort_fields=(), addresses=None, cursor=None):
        selector = definition.member_selector
        fields = None if isinstance(selector, core.Whole) else [(field.label, field.pointer) for field in selector.selectors]
        pending, size = [], 0

        def external_values():
            with self.records._cursor() as cursor:
                source = cursor.from_arrow(pa.Table.from_pylist(pending, schema=_SOURCE))
                with closing(extracted_rows(source, fields=fields, sort_fields=sort_fields)) as rows:
                    for key, entity, value, sort_key in rows:
                        yield key, entity, value, sort_key, None

        with self.states.relation(session, parent_id, scope=definition.scope if addresses is None else None, addresses=addresses, cursor=cursor) as relation:
            # One parent scan feeds native inline extraction and external routing.
            # Only the small ContentRef crosses Python for external values.
            source = relation.project(
                "member_key, occurrence_id, "
                "CASE WHEN json_extract_string(decode(occurrence_record), '/value/kind') = 'content' "
                "THEN 'null' ELSE json_extract(decode(occurrence_record), '/value/value')::VARCHAR END AS payload, "
                "CASE WHEN json_extract_string(decode(occurrence_record), '/value/kind') = 'content' "
                "THEN json_extract(decode(occurrence_record), '/value')::VARCHAR END AS external_content")
            with closing(extracted_rows(source, fields=fields, sort_fields=sort_fields, passthrough=("external_content",))) as rows:
                for key, entity, value, sort_key, external in rows:
                    if external is None:
                        yield key, entity, value, sort_key, None
                        continue
                    content = msgspec.convert(msgspec.json.decode(external), type=core.ContentRef, strict=True)
                    if content.codec == "bytes-v1":
                        if fields is not None or sort_fields:
                            raise IntegrityError("JSON fields require JSON values, not opaque bytes")
                        session.blobs.stat(BlobRef(content.locator, content.digest, content.byte_size, content.media_type))
                        yield key, entity, ["opaque", content.codec, content.digest, content.byte_size], "[]", record_value(content, core.ContentRef)
                    else:
                        payload = _content_bytes(session, content)
                        row_size = _member_size((key, entity, payload))
                        if row_size > BATCH_BYTES:
                            raise LimitExceededError("external selected input exceeds the batch byte limit")
                        if pending and (len(pending) == BATCH_ROWS or size + row_size > BATCH_BYTES):
                            yield from external_values()
                            pending, size = [], 0
                        pending.append({"member_key": key, "occurrence_id": entity, "payload": payload.decode("utf-8")})
                        size += row_size
                if pending:
                    yield from external_values()

    def _computed_rows(self, session, parent_id, definition, *, addresses=None, cursor=None):
        sort_fields, descending = self._sort(session, definition)
        material_entities = definition.member_selector.comparison == "identity"
        with closing(self._member_values(session, parent_id, definition, sort_fields=sort_fields, addresses=addresses, cursor=cursor)) as values:
            for key, entity, value, sort_key, content in values:
                comparison = member_bytes(value, member_key=key if definition.material_keys else None,
                                          entity_id=entity if material_entities else None)
                yield key, entity, comparison, sort_key, content

    def _remember(self, session, parent_id, definition, plan):
        key = parent_id, _selector_key(definition)
        previous = session.member_selections.pop(key, None)
        if previous is not None and previous is not plan:
            previous.close()
        # Bound native resource owners, never the number/bytes of changed rows.
        while len(session.member_selections) >= 32:
            session.member_selections.pop(next(iter(session.member_selections))).close()
        session.member_selections[key] = plan

    def _certified(self, session, parent_id, definition):
        if getattr(session.ledger, "read_only", False):
            return None
        label = _selector_key(definition)
        cached = session.member_selections.get((parent_id, label))
        if cached is not None:
            row = next(session.read_records([("selected_value", cached.selected.selected_value_id)]))[0]
            if row is not None and row.retained and row.available:
                return cached
            session.member_selections.pop((parent_id, label)).close()
        # Export admission cannot treat links supplied in an external snapshot
        # as evidence that this process's evaluator computed a parent value.
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
                    plan = _Members(selected, self.records.available(self._reference(manifest)), self._manifest_evidence(manifest))
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

    def _members(self, session, parent_id, definition):
        """Diff actual addresses against a certified retained base, then evaluate
        only changed values. Temporary tables may spill; batch limits never
        turn a large delta into a second full evaluation. Revision metadata
        locates candidates and cannot certify advertised edits.
        """
        _record(session, ("state", parent_id))
        current, seen = parent_id, set()
        while current is not None:
            if current in seen:
                raise IntegrityError("computed selection revision history contains a cycle")
            seen.add(current)
            plan = self._certified(session, current, definition)
            if plan is not None:
                break
            current = self._base_state(session, current)
        else:
            return None
        if current == parent_id:
            return plan
        owner = ExitStack()
        try:
            cursor = owner.enter_context(self.records._cursor())
            membership = self.states.layers(session, parent_id)["membership"]
            certified_keys = self.states.changed_keys(session, parent_id, plan.selected.origin.parent_entity_id, cursor)
            tables = {} if definition.scope is None else {"wanted": pa.table({"wanted_key": pa.array(definition.scope, type=pa.string())})}
            identities = None if definition.scope is None else {"membership": list(definition.scope), "selected": list(definition.scope)}
            with self.records.relations({"membership": membership, "selected": plan.layer}, tables=tables,
                                        identities=identities, cursor=cursor) as relations:
                current_rows = relations["membership"].project("record_identity AS current_key, json_extract_string(decode(record_json), '/occurrence_id') AS current_id")
                if definition.scope is not None:
                    current_rows = current_rows.join(relations["wanted"], "current_key = wanted_key", how="semi")
                prior = relations["selected"].project("record_identity AS prior_key, occurrence_id AS prior_id")
                if certified_keys is not None:
                    current_rows = current_rows.join(certified_keys, "current_key = changed_key", how="semi")
                    prior = prior.join(certified_keys, "prior_key = changed_key", how="semi")
                delta = prior.join(current_rows, "prior_key = current_key", how="outer").filter("prior_id IS DISTINCT FROM current_id")
                delta.project("coalesce(prior_key, current_key) AS wanted_key").create_view("selection_delta_input")
                cursor.execute("CREATE TEMP TABLE selection_delta AS SELECT * FROM selection_delta_input")
            addresses = cursor.table("selection_delta")
            def rows():
                with closing(self._computed_rows(session, parent_id, definition, addresses=addresses, cursor=cursor)) as values:
                    for row in values:
                        key, entity = row[:2]
                        yield (key, key, None, None, None, None) if entity is None and definition.scope is None else _member_record(*row)
            output_cursor = owner.enter_context(self.records._cursor())
            batches = encoded_batches(rows(), _ROWS, byte_column=tuple(range(len(_ROWS))))
            with closing(batches), closing(pa.RecordBatchReader.from_batches(_ROWS, batches)) as source:
                output_cursor.register("selection_updates", source.__arrow_c_stream__())
                output_cursor.execute("CREATE TEMP TABLE selection_changes AS SELECT * FROM selection_updates")
                output_cursor.unregister("selection_updates")
            cursor.execute("DROP VIEW selection_delta_input")
            cursor.execute("DROP TABLE selection_delta")
            cursor.close()
            result = _Members(plan.selected, plan.layer, plan.evidence, owner, output_cursor, output_cursor.table("selection_changes"))
            # Always diff the retained base directly: intermediate edits that
            # revert disappear without a growing Python overlay/history.
            self._remember(session, parent_id, definition, result)
            return result
        except BaseException:
            owner.close()
            raise

    def _member_relation(self, original, plan):
        original = original.project(_ROW_COLUMNS)
        if plan.changes is None:
            return original
        kept = original.join(plan.changes.project("record_identity AS change_key"), "record_identity = change_key", how="anti")
        return kept.union(plan.changes.filter("record_json IS NOT NULL").project(_ROW_COLUMNS))

    def _plan_evidence(self, session, definition, plan):
        if plan.resolved_evidence is not None:
            return plan.resolved_evidence
        if plan.changes is None:
            return plan.evidence
        with self.records.relations({"selected": plan.layer}, cursor=plan.cursor) as relations:
            old = relations["selected"].join(plan.changes.project("record_identity AS change_key"), "record_identity = change_key", how="semi")
            new = plan.changes.filter("record_json IS NOT NULL")
            if definition.sort_rule is None:
                delta = old.project("record_json AS encoded, -1::BIGINT AS weight").union(new.project("record_json AS encoded, 1::BIGINT AS weight"))
                differs = delta.aggregate("encoded, sum(weight) AS balance").filter("balance != 0").limit(1).fetchone()
            else:
                # Sufficient proof only: unchanged bytes and ordering tokens.
                before = old.project("record_identity AS old_key, record_json AS old_bytes, sort_key AS old_sort")
                after = new.project("record_identity AS new_key, record_json AS new_bytes, sort_key AS new_sort")
                differs = before.join(after, "old_key = new_key", how="outer").filter(
                    "old_key IS DISTINCT FROM new_key OR old_bytes IS DISTINCT FROM new_bytes OR old_sort IS DISTINCT FROM new_sort").limit(1).fetchone()
            if not differs:
                plan.resolved_evidence = plan.evidence
                return plan.evidence
            _, descending = self._sort(session, definition)
            with closing(self._comparison_rows(self._member_relation(relations["selected"], plan),
                    ordered=definition.sort_rule is not None, descending=descending)) as rows:
                plan.resolved_evidence = member_stream_evidence((encoded for _, _, encoded in rows), ordered=definition.sort_rule is not None)
                return plan.resolved_evidence

    def _write_members(self, session, parent_id, definition):
        _, descending = self._sort(session, definition)
        plan = self._members(session, parent_id, definition)
        if plan is not None:
            evidence = self._plan_evidence(session, definition, plan)
            admitted = plan.layer
            if plan.changes is not None:
                with closing(plan.changes.to_arrow_reader(256)) as rows:
                    admitted = self.records.apply_changes(admitted, rows)
        else:
            def rows():
                with closing(self._computed_rows(session, parent_id, definition)) as values:
                    for row in values:
                        yield _member_record(*row)
            admitted = self.records.retain_batches(encoded_batches(rows(), _ROWS, byte_column=tuple(range(len(_ROWS)))),
                                                   layer_kind="core-selected-members", schema=_SCHEMA, partition_policy=_POLICY, ordered=False)
        manifest = {"format": "docspec-selected-members", "version": 3, "layer": admitted.reference.to_dict(),
                    "ordered": definition.sort_rule is not None, "descending": descending}
        if plan is None:
            with closing(self._ordered_rows(manifest, reference=admitted.reference)) as rows:
                evidence = member_stream_evidence((encoded for _, _, encoded in rows), ordered=manifest["ordered"])
        manifest["evidence"] = record_value(evidence, core.ComparisonEvidence)
        return manifest, admitted

    def _recover_rows(self, session, parent_id, definition):
        """Sort an Arrow stream in the native engine without retaining new files."""
        _, descending = self._sort(session, definition)
        schema = pa.schema([("member_key", pa.string()), ("occurrence_id", pa.string()), ("encoded", pa.binary()), ("sort_key", pa.string())])
        with closing(self._computed_rows(session, parent_id, definition)) as values:
            batches = encoded_batches((row[:4] for row in values), schema, byte_column=(0, 1, 2, 3))
            with closing(batches), closing(pa.RecordBatchReader.from_batches(schema, batches)) as source, self.records._cursor() as cursor:
                cursor.register("computed", source.__arrow_c_stream__())
                order = self._ordering(definition.sort_rule is not None, descending)
                with closing(cursor.sql(f"SELECT member_key, occurrence_id, encoded FROM computed ORDER BY {order}").to_arrow_reader(256)) as reader:
                    for batch in bounded_batches(reader, byte_column=("member_key", "occurrence_id", "encoded")):
                        yield from zip(*(column.to_pylist() for column in batch.columns), strict=True)

    @staticmethod
    def _ordering(ordered, descending):
        return ("sort_key DESC, member_key" if descending else "sort_key, member_key") if ordered else "encoded, member_key"

    def _comparison_rows(self, relation, *, ordered, descending):
        rows = relation.project(
            "record_identity AS member_key, occurrence_id, record_json AS encoded, sort_key")
        with closing(rows.order(self._ordering(ordered, descending)).project("member_key, occurrence_id, encoded").to_arrow_reader(256)) as reader:
            for batch in bounded_batches(reader, byte_column=("member_key", "occurrence_id", "encoded")):
                yield from zip(*(column.to_pylist() for column in batch.columns), strict=True)

    def _ordered_rows(self, manifest, *, reference=None):
        with self.records.relations({"selected": reference or self._reference(manifest)}) as relations:
            yield from self._comparison_rows(relations["selected"], ordered=manifest["ordered"], descending=manifest["descending"])

    @staticmethod
    def _manifest_evidence(manifest):
        try:
            evidence = msgspec.convert(manifest["evidence"], type=core.ComparisonEvidence, strict=True)
            record_value(evidence, core.ComparisonEvidence)
            if evidence.codec != "members-v1" or evidence.entity_id is not None:
                raise ValueError("invalid member evidence")
            return evidence
        except (KeyError, ValueError, TypeError) as error:
            raise IntegrityError("invalid selected-member evidence") from error

    def _reference(self, manifest):
        if (not isinstance(manifest, dict) or set(manifest) != {"format", "version", "layer", "ordered", "descending", "evidence"}
                or manifest["format"] != "docspec-selected-members" or type(manifest["version"]) is not int or manifest["version"] != 3
                or type(manifest["ordered"]) is not bool or type(manifest["descending"]) is not bool):
            raise IntegrityError("invalid selected-member manifest")
        self._manifest_evidence(manifest)
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
                with closing(self._ordered_rows(manifest)) as rows:
                    evidence = member_stream_evidence((encoded for _, _, encoded in rows), ordered=manifest["ordered"])
                if evidence != self._manifest_evidence(manifest):
                    raise IntegrityError("selected-member evidence differs from its retained rows")
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
                for row in batch.to_pylist():
                    key, entity = row["record_identity"], row["occurrence_id"]
                    if not isinstance(key, str) or row["partition_value"] != key or (entity is not None and (not isinstance(entity, str) or not entity)):
                        raise IntegrityError("invalid selected-member origin")
                    if not isinstance(row["record_json"], bytes) or not isinstance(row["sort_key"], str):
                        raise IntegrityError("selected-member encodings must be canonical bytes and sort keys")
                    if wanted is not None:
                        if key not in wanted:
                            raise IntegrityError("selected-member rows differ from their named scope")
                        wanted.remove(key)
                    encoded = row["record_json"]
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
                        content = record_value(decode_canonical_json_value(row["content"], label="selected content reference"), core.ContentRef)
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
            session.computed_records[key] = payload, link
        try:
            session.publish(MetadataBatch(unit_id or selected_value_id + ":retain", records=(selected,), retained=(key,)))
        finally:
            session.computed_records.pop(key, None)
        if computed:
            self._remember(session, origin.parent_entity_id, definition, _Members(selected, admitted, self._manifest_evidence(manifest)))
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
            with closing(self.content_references(reference)) as contents:
                for content in contents:
                    session.check_content(content, retained=retained)
        elif isinstance(selected.value, core.FromParent) and not retained:
            self._single(session, selected.definition, selected.origin.parent_entity_id)

    def content_references(self, reference):
        """Stream distinct retained blob obligations for publication and cleanup."""
        with self.records.relations({"rows": reference}) as relations:
            contents = relations["rows"].filter("content IS NOT NULL").project("content").distinct()
            with closing(contents.to_arrow_reader(256)) as reader:
                for batch in bounded_batches(reader, byte_column="content"):
                    for payload in batch.column(0).to_pylist():
                        yield msgspec.convert(msgspec.json.decode(payload), type=core.ContentRef, strict=True)

    def evidence(self, session, selected):
        session._active()
        if isinstance(selected.definition, core.StateMembers):
            if not isinstance(selected.value, core.FromParent):
                return self._manifest_evidence(self._manifest(session, selected))
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
            evidence = self._plan_evidence(session, selector, plan)
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
                raise LimitExceededError("selected value exceeds the requested byte limit")
            yield payload
        else:
            reference = BlobRef(value.locator, value.digest, value.byte_size, value.media_type)
            with owned_iterator(session.blobs.read(reference, max_bytes=max_bytes)) as chunks:
                for chunk in chunks:
                    session._active()
                    yield chunk
