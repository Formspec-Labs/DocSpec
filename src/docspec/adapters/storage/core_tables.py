"""Typed Core states: native row identity, membership, and bounded occurrence reads."""

from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa

from docspec.adapters.storage.core_entities import ENTITY_POLICY, ENTITY_SCHEMA
from docspec.adapters.storage.iceberg import identifier, literal
from docspec.adapters.storage.table_sql import membership_json_sql, occurrence_id_sql, occurrence_json_sql, row_digest_sql, row_json_sql
from docspec.domain import core
from docspec.domain.identity import require_text
from docspec.domain.references import LayerRef
from docspec.domain.storage import PartitionPolicy, RecordSchema, TableSchema
from docspec.domain.table_rows import ROW_RULE, table_columns
from docspec.errors import IntegrityError
from docspec.ports.record_storage import BATCH_ROWS, bounded_batches


MEMBERS = RecordSchema("core-membership:1", ("kind", *core.Membership.__struct_fields__), "member_key", "member_key")
POLICY = PartitionPolicy("core-keys:1", 1)
OCCURRENCES = TableSchema("core-table-occurrences:1", (
    ("occurrence_hash", "BLOB"), ("member_key", "VARCHAR"), ("row_digest", "BLOB"), ("first_state_id", "VARCHAR")))
CHANGES = TableSchema("core-table-changes:1", (
    ("member_key", "VARCHAR"), ("previous_occurrence_id", "VARCHAR"), ("occurrence_id", "VARCHAR")))
_PREFIX = "urn:docspec:table-occurrence:v1:"


def key_sql(rules, *, qualifier=None):
    prefix = "" if qualifier is None else identifier(qualifier) + "."
    fields = [prefix + identifier(name) for name in rules["key"]["fields"]]
    if (rules["key"]["id"], rules["key"]["version"]) == ("value", "1") and len(fields) == 1:
        return f"CAST({fields[0]} AS VARCHAR)"
    if (rules["key"]["id"], rules["key"]["version"]) == ("federal-register-source-record-id", "1") and len(fields) == 2:
        return f"CAST({fields[0]} AS VARCHAR) || '@' || CAST({fields[1]} AS VARCHAR)"
    raise IntegrityError("table member-key spelling is not supported")


def checked_rules(rules):
    if not isinstance(rules, dict) or set(rules) != {"row", "family", "table", "columns", "key"} or rules["row"] != ROW_RULE:
        raise IntegrityError("invalid table state rules")
    try:
        require_text(rules["family"], "table family")
        require_text(rules["table"], "logical table")
        columns = table_columns(rules["columns"])
        key = rules["key"]
        if not isinstance(key, dict) or set(key) != {"id", "version", "fields"}:
            raise ValueError("invalid table key declaration")
        fields = key["fields"]
        if not isinstance(fields, (list, tuple)) or not fields or len(set(fields)) != len(fields):
            raise ValueError("invalid table key fields")
        if not set(fields) <= {name for name, _ in columns}:
            raise ValueError("table key fields must belong to its declared projection")
        key_sql(rules)
    except (TypeError, ValueError, KeyError) as error:
        raise IntegrityError("invalid table state rules") from error
    return columns


def references(manifest):
    required = {"format", "version", "table", "membership", "occurrences", "rules"}
    if (not isinstance(manifest, dict) or set(manifest) not in (required, required | {"change"})
            or manifest["format"] != "docspec-core-state" or manifest["version"] != 3):
        raise IntegrityError("invalid typed Core state manifest")
    checked_rules(manifest["rules"])
    try:
        refs = {name: LayerRef.from_dict(manifest[name]) for name in ("table", "membership", "occurrences")}
        if "change" in manifest:
            change = manifest["change"]
            if set(change) != {"from", "layer"}:
                raise ValueError("invalid generation change reference")
            require_text(change["from"], "base state identity")
            refs["changes"] = LayerRef.from_dict(change["layer"])
    except (KeyError, TypeError, ValueError) as error:
        raise IntegrityError("invalid typed Core state layer reference") from error
    if refs["membership"].schema_id != MEMBERS.schema_id or refs["occurrences"].schema_id != OCCURRENCES.schema_id:
        raise IntegrityError("typed state indexes differ from their required schemas")
    if "changes" in refs and refs["changes"].schema_id != CHANGES.schema_id:
        raise IntegrityError("typed state changes differ from their required schema")
    return refs


@dataclass(frozen=True)
class TableMembers:
    """A small resolver description, never materialized occurrence JSON for a layer."""
    reference: LayerRef
    committed_ms: int
    manifest: dict


class CoreTableStorage:
    def __init__(self, states):
        self.states, self.records = states, states.records

    def view(self, manifest):
        reference = references(manifest)["occurrences"]
        return TableMembers(reference, self.records.layer_files(reference).committed_ms, manifest)

    def layers(self, manifest, *, retained=True):
        admit = self.records.admitted if retained else self.records.admit
        layers = {name: admit(ref) for name, ref in references(manifest).items()}
        if layers["membership"].schema != MEMBERS or layers["occurrences"].schema != OCCURRENCES:
            raise IntegrityError("typed state indexes differ from their declared schemas")
        declared = checked_rules(manifest["rules"])
        if not set(declared) <= set(layers["table"].schema.columns):
            raise IntegrityError("typed state projection differs from the admitted table schema")
        return layers

    @staticmethod
    def _keyed(relation, rules):
        return relation.project(key_sql(rules) + " AS __docs_key, " + ", ".join(identifier(name) for name, _ in checked_rules(rules)))

    @staticmethod
    def _check_keys(relation, rules):
        invalid = " OR ".join(f"{identifier(name)} IS NULL OR CAST({identifier(name)} AS VARCHAR) = ''" for name in rules["key"]["fields"])
        if relation.filter(invalid).limit(1).fetchone():
            raise IntegrityError("table member keys require nonempty components")
        if relation.aggregate("__docs_key, count(*) AS n", "__docs_key").filter("n > 1").limit(1).fetchone():
            raise IntegrityError("table contains a duplicate member key")

    def prepare(self, session, table, *, state_id, rules, base_state_id=None):
        """Write compact identities once, carrying unchanged and reappearing occurrences forward."""
        columns = checked_rules(rules)
        older = None if base_state_id is None else self.states.manifest(session, base_state_id)
        if older is not None:
            if older.get("version") != 3 or any(older["rules"][key] != rules[key] for key in ("family", "table", "key", "row")):
                raise IntegrityError("a table generation must keep its dataset and member-key spelling")
        refs = {"new_table": table}
        if older is not None:
            refs.update({"old_" + key: ref for key, ref in self.layers(older).items() if key in {"table", "membership", "occurrences"}})
        with TemporaryDirectory(prefix="docspec-table-identities-", dir=self.records.merge_scratch_root) as scratch, \
                self.records._cursor() as cursor, self.records.relations(refs, cursor=cursor) as tables:
            current = self._keyed(tables["new_table"], rules)
            self._check_keys(current, rules)
            current.create_view("new_table_rows")
            if older is None:
                changed = current
                removed = cursor.sql("SELECT NULL::VARCHAR AS member_key, NULL::VARCHAR AS previous_occurrence_id WHERE false")
            else:
                previous = self._keyed(tables["old_table"], older["rules"])
                previous.create_view("old_table_rows")
                if tuple(table_columns(older["rules"]["columns"])) == tuple(columns):
                    tests = []
                    for name, kind in columns:
                        left, right = f"o.{identifier(name)}", f"n.{identifier(name)}"
                        # SQL equates signed zeros; their declared canonical spellings differ.
                        if kind == "DOUBLE":
                            left, right = f"CAST({left} AS VARCHAR)", f"CAST({right} AS VARCHAR)"
                        tests.append(f"({left} IS DISTINCT FROM {right})")
                    predicate = "o.__docs_key IS NULL OR " + " OR ".join(tests)
                else:
                    predicate = "true"
                changed = cursor.sql("SELECT n.* FROM new_table_rows n LEFT JOIN old_table_rows o USING (__docs_key) WHERE " + predicate)
                tables["old_membership"].project("record_identity AS member_key, json_extract_string(decode(record_json), '/occurrence_id') AS previous_occurrence_id").create_view("old_members")
                removed = cursor.sql("SELECT o.* FROM old_members o ANTI JOIN new_table_rows n ON o.member_key = n.__docs_key")
            # Spill the one identity pass to a temporary Parquet file. Both the
            # membership and append-only index consume it without rehashing rows.
            digested = changed.project("__docs_key AS member_key, " + row_digest_sql(columns) + " AS row_digest")
            minted = digested.project("*, " + occurrence_id_sql(rules["family"], rules["table"]) + " AS occurrence_id")
            minted.order("member_key").write_parquet(str(Path(scratch) / "identities.parquet"))
            minted = cursor.read_parquet(str(Path(scratch) / "identities.parquet"))
            additions = minted.project("from_hex(substr(occurrence_id, " + str(len(_PREFIX) + 1) + ")) AS occurrence_hash, member_key, from_hex(substr(row_digest, 8)) AS row_digest, " + literal(state_id) + " AS first_state_id")
            if older is None:
                occurrences = self.records.retain_table(additions, schema=OCCURRENCES, layer_kind="core-table-occurrences", sort_by=("occurrence_hash",))
                generated = occurrences.reference.record_count
            else:
                prior_index = tables["old_occurrences"].project("occurrence_hash AS existing_hash")
                fresh = additions.join(prior_index, "occurrence_hash = existing_hash", how="anti")
                generated = fresh.count("*").fetchone()[0]
                occurrences = self.records.append_table(refs["old_occurrences"], fresh, key_columns=("occurrence_hash",), sort_by=("occurrence_hash",)) if generated else refs["old_occurrences"]
            canonical = minted.project("member_key AS record_identity, member_key AS partition_value, encode(" + membership_json_sql() + ") AS record_json")
            if older is None:
                membership = self.records.retain_relation(canonical, schema=MEMBERS, partition_policy=POLICY, layer_kind="core-membership")
                change = None
            else:
                changes = canonical.union(removed.project("member_key AS record_identity, member_key AS partition_value, NULL::BLOB AS record_json"))
                membership = self.records.apply_relation_changes(refs["old_membership"], changes)
                minted.create_view("minted_rows")
                compared = cursor.sql("SELECT n.member_key, o.previous_occurrence_id, n.occurrence_id FROM minted_rows n LEFT JOIN old_members o USING(member_key)")
                changed_ids = compared.union(removed.project("member_key, previous_occurrence_id, NULL::VARCHAR AS occurrence_id"))
                delta = self.records.retain_table(changed_ids, schema=CHANGES, layer_kind="core-table-changes", sort_by=("member_key",))
                change = {"from": base_state_id, "layer": delta.reference.to_dict()}
            manifest = {"format": "docspec-core-state", "version": 3, "table": table.reference.to_dict(),
                        "membership": membership.reference.to_dict(), "occurrences": occurrences.reference.to_dict(), "rules": rules}
            if change is not None:
                manifest["change"] = change
            content = session.retain_value(manifest)
            self.states._remember(session, content.digest, manifest)
            return content, {"generated_occurrences": generated, "adopted_occurrences": table.reference.record_count - generated}

    @contextmanager
    def table(self, manifest, *, scope=None, addresses=None, cursor=None):
        """Yield typed projected rows joined to already-minted membership IDs."""
        with self.records.relations(self.layers(manifest), cursor=cursor) as relations:
            members = relations["membership"].project("record_identity AS member_key, json_extract_string(decode(record_json), '/occurrence_id') AS occurrence_id")
            if scope is not None:
                if not scope:
                    members = members.filter("false")
                else:
                    # The Arrow join handles keys containing NUL without SQL literals.
                    with self.records.relations({}, tables={"wanted": pa.table({"wanted_key": pa.array(scope, type=pa.string())})}, cursor=cursor) as wanted:
                        members = wanted["wanted"].join(members, "wanted_key = member_key", how="left").project("wanted_key AS member_key, occurrence_id")
                        yield self._join_rows(members, relations["table"], manifest["rules"])
                    return
            if addresses is not None:
                members = addresses.join(members, "wanted_key = member_key", how="left").project("wanted_key AS member_key, occurrence_id")
            yield self._join_rows(members, relations["table"], manifest["rules"])

    def _join_rows(self, members, table, rules):
        columns = checked_rules(rules)
        values = self._keyed(table, rules)
        # A derived table can already contain its own member_key. The public
        # address column is identical, and appears once in the typed view.
        if "member_key" in {name for name, _ in columns}:
            values = values.project("* EXCLUDE(member_key)")
        return members.join(values, "member_key = __docs_key", how="left").project("* EXCLUDE(__docs_key)")

    @contextmanager
    def relation(self, manifest, *, scope=None, addresses=None, cursor=None):
        with self.table(manifest, scope=scope, addresses=addresses, cursor=cursor) as rows:
            payload = occurrence_json_sql(row_json_sql(manifest["rules"]["columns"]))
            yield rows.project("member_key, occurrence_id, CASE WHEN occurrence_id IS NULL THEN NULL::BLOB ELSE encode(" + payload + ") END AS occurrence_record")

    def find(self, views, identities):
        """Resolve only requested identities; no table-wide JSON view is built."""
        found = {}
        for view in views:
            wanted = [identity for identity in identities if identity.startswith(_PREFIX) and identity not in found]
            if not wanted:
                continue
            hashes = []
            for identity in wanted:
                try:
                    raw = bytes.fromhex(identity[len(_PREFIX):])
                    if len(raw) == 32:
                        hashes.append(raw)
                except ValueError:
                    pass
            if not hashes:
                continue
            with self.records._cursor() as cursor, self.records.relations({"index": view.reference}, cursor=cursor) as relations:
                index = relations["index"].filter("occurrence_hash IN (" + ",".join("from_hex(" + literal(value.hex()) + ")" for value in hashes) + ")")
                rows = index.project(literal(_PREFIX) + " || lower(hex(occurrence_hash)) AS occurrence_id, member_key, 'sha256:' || lower(hex(row_digest)) AS row_digest").fetchall()
                for identity, key, digest in rows:
                    with self.table(view.manifest, scope=(key,), cursor=cursor) as values:
                        if values.filter("occurrence_id = " + literal(identity)).limit(1).fetchone() is None:
                            continue
                        if values.project(row_digest_sql(view.manifest["rules"]["columns"])).fetchone()[0] != digest:
                            raise IntegrityError("table occurrence value differs from its minted digest")
                    with self.relation(view.manifest, scope=(key,), cursor=cursor) as values:
                        payload = values.project("occurrence_record").fetchone()[0]
                    from hashlib import sha256
                    found[identity] = (view, len(payload), key, sha256(payload).hexdigest())
        return found

    def payloads(self, located):
        for identity, (view, _, key, digest) in located.items():
            with self.relation(view.manifest, scope=(key,)) as rows:
                with closing(bounded_batches(rows.project("occurrence_id, occurrence_record").to_arrow_reader(BATCH_ROWS), byte_column="occurrence_record")) as batches:
                    for batch in batches:
                        for found, payload in zip(*(column.to_pylist() for column in batch.columns), strict=True):
                            if found != identity:
                                raise IntegrityError("table membership differs from the requested occurrence")
                            yield found, payload

    def pin_members(self, members):
        """Retain explicit individual references without pinning an entire producer file.

        Bulk state bindings create no copies. Only individually retained
        occurrences use the ordinary entity store, so superseded source files
        can be collected while those exact values remain available.
        """
        selected = {key: value for key, value in members.items() if isinstance(value[1], TableMembers)}
        if not selected:
            return members
        from docspec.adapters.storage.core_entities import retain_entities
        layer = retain_entities(self.records, (record for record, _ in selected.values()))
        view = self.records.layer_files(layer.reference)
        return {key: (record, view if key in selected else source) for key, (record, source) in members.items()}
