"""Derive the Regulations.gov attribute tables from DocSpec's catalogue capture and export them as spicy-regs members.

The thin spicy-regs ``documents`` and ``dockets`` tables carry a few publisher
attributes; this tool carries the others a record states, typed as the
spicy-docs contracts type them (proposal:
``docs/research/regulations-attributes-contract-2026-09-26.md``). Each command
runs in its own process:

  extract  each member's own Regulations.gov record, natively, from the pinned catalogue state
  census   per kind and path: counts, JSON types and spelling hazards, the column proposal's evidence
  derive   one typed derived layer (C29 ``derive_table``) per table from the extracted records
  export   each layer as one spicy-regs member (key-sorted, zstd, row groups of at most 64 MiB, no field IDs)
  check    in a fresh process: member rows equal the layer's, and every row equals its catalogue record

Typing is native: each record's attributes are read once into a typed struct
and once for their JSON types, which the struct's casts would otherwise hide.
A value the contract type does not describe exactly is refused, never cast.
``check`` alone decodes rows in Python, as the reference.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from docspec.adapters.storage.files import sha256_file

# A standalone connection's bounds; DuckDB's default takes 80% of the machine's memory.
_BOUNDED = {"memory_limit": "3GB", "threads": "2", "preserve_insertion_order": "false", "TimeZone": "UTC"}
VARCHAR, BOOLEAN, INTEGER, TIMESTAMPTZ, LIST = "VARCHAR", "BOOLEAN", "INTEGER", "TIMESTAMPTZ", "VARCHAR[]"
JSON = "JSON"  # a VARCHAR holding spicy-docs' json_column spelling
MAX_MEMBER_BYTES = 2**30
MAX_ROW_GROUP_BYTES = 64 * 2**20
ROW_GROUP_TARGET = 48 * 2**20  # of the estimate below, which ignores levels and page headers
_VECTOR_ROWS = 2048  # DuckDB rounds a row group up to whole vectors
# An instant as spicy-docs' reference projection admits one: whole seconds, UTC, spelled exactly so.
_INSTANT = r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z"
# displayProperties, as the source's canonical record holds it: an array of {label, name, tooltip} in that key
# order, each null or a printable-ASCII string whose only escape is \". Such text is already json_column's spelling.
_DISPLAY_OBJECT = '\\{"label":S,"name":S,"tooltip":S\\}'.replace("S", r'(null|"([ !#-\[\]-~]|\\")*")')
_DISPLAY = rf"\[({_DISPLAY_OBJECT}(,{_DISPLAY_OBJECT})*)?\]"
_DUCKDB_TYPES = {VARCHAR: "VARCHAR", JSON: "VARCHAR", BOOLEAN: "BOOLEAN", INTEGER: "INTEGER",
                 TIMESTAMPTZ: "TIMESTAMP WITH TIME ZONE", LIST: "VARCHAR[]"}


@dataclass(frozen=True)
class Table:
    """One attribute table: the record kind it holds, its key, and each (API attribute, type) it carries."""

    name: str
    kind: str
    key: str
    attributes: tuple[tuple[str, str], ...]

    @property
    def columns(self) -> tuple[tuple[str, str, str], ...]:
        """(column, attribute, type): the column is the attribute in snake_case, ``_json`` after JSON text."""
        return tuple((re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower() + ("_json" if kind == JSON else ""),
                      name, kind) for name, kind in self.attributes)


# Every attribute a record states except those the thin table carries and those never stated, constant or spelling
# the key (spicy-regs decisions 66 and 67 name the submitter contact fields and the types).
TABLES = {table.name: table for table in (
    Table("document_attributes", "documents", "document_id", (
        ("address1", VARCHAR), ("address2", VARCHAR), ("allowLateComments", BOOLEAN), ("authorDate", TIMESTAMPTZ),
        ("authors", LIST), ("category", VARCHAR), ("cfrPart", VARCHAR), ("city", VARCHAR), ("comment", VARCHAR),
        ("country", VARCHAR), ("displayProperties", JSON), ("docAbstract", VARCHAR), ("effectiveDate", TIMESTAMPTZ),
        ("exhibitLocation", VARCHAR), ("exhibitType", VARCHAR), ("fax", VARCHAR), ("field1", VARCHAR),
        ("field2", VARCHAR), ("firstName", VARCHAR), ("frVolNum", VARCHAR), ("govAgency", VARCHAR),
        ("govAgencyType", VARCHAR), ("implementationDate", TIMESTAMPTZ), ("lastName", VARCHAR), ("legacyId", VARCHAR),
        ("media", VARCHAR), ("objectId", VARCHAR), ("ombApproval", VARCHAR), ("openForComment", BOOLEAN),
        ("organization", VARCHAR), ("originalDocumentId", VARCHAR), ("pageCount", INTEGER),
        ("postmarkDate", TIMESTAMPTZ), ("receiveDate", TIMESTAMPTZ), ("regWriterInstruction", VARCHAR),
        ("restrictReason", VARCHAR), ("restrictReasonType", VARCHAR), ("sourceCitation", VARCHAR),
        ("startEndPage", VARCHAR), ("stateProvinceRegion", VARCHAR), ("subject", VARCHAR), ("submitterRep", VARCHAR),
        ("subtype", VARCHAR), ("topics", LIST), ("trackingNbr", VARCHAR), ("withinCommentPeriod", BOOLEAN),
        ("zip", VARCHAR))),
    Table("docket_attributes", "dockets", "docket_id", (
        ("category", VARCHAR), ("displayProperties", JSON), ("effectiveDate", TIMESTAMPTZ), ("field1", VARCHAR),
        ("field2", VARCHAR), ("generic", VARCHAR), ("keywords", LIST), ("legacyId", VARCHAR), ("objectId", VARCHAR),
        ("organization", VARCHAR), ("petitionNbr", VARCHAR), ("program", VARCHAR), ("shortTitle", VARCHAR),
        ("subType", VARCHAR), ("subType2", VARCHAR))),
)}

# The record's own Regulations.gov fact is the one whose data.id is the member's key: a document also carries its
# joined docket and Federal Register facts, so a fixed array position would read the wrong record. One parse of the
# value yields every fact's id, type and scope, and the own fact's attributes, links and relationships as JSON text.
_FACTS = json.dumps({"sourceNativeFacts": [{"scopeId": VARCHAR, "fields": {"data": {
    "id": VARCHAR, "type": VARCHAR, "attributes": "JSON", "links": "JSON", "relationships": "JSON"}}}]})
_KINDS = {("regulations-gov-documents", "documents"), ("regulations-gov-dockets", "dockets")}

# What the typed struct reads each type as, and the JSON types (json_structure's names) each may be stated as.
_TARGETS = {VARCHAR: VARCHAR, BOOLEAN: BOOLEAN, INTEGER: "BIGINT", TIMESTAMPTZ: VARCHAR, LIST: [VARCHAR], JSON: "JSON"}
_SHAPES = {VARCHAR: ("VARCHAR",), BOOLEAN: ("BOOLEAN",), INTEGER: ("UBIGINT", "BIGINT"), TIMESTAMPTZ: ("VARCHAR",),
           LIST: ('["VARCHAR"]', '["NULL"]'),
           JSON: ('[{"label":"VARCHAR","name":"VARCHAR","tooltip":"VARCHAR"}]', '["NULL"]')}


# ------------------------------------------------------------------ typing

def _literal(text):
    return "'" + text.replace("'", "''") + "'"


def _value_sql(attribute, kind, context):
    """The column's value from ``a`` (the typed struct) once ``s`` (its JSON types) has admitted the stated type."""
    value = f'a."{attribute}"'
    if kind == INTEGER:
        return (f"CASE WHEN {value} BETWEEN -2147483648 AND 2147483647 THEN {value}::INTEGER "
                f"ELSE error({_literal(context + ' is not a 32-bit integer')}) END")
    if kind == TIMESTAMPTZ:
        return (f"CASE WHEN regexp_full_match({value}, {_literal(_INSTANT)}) "
                f"AND NOT starts_with({value}, '0000') AND TRY_CAST({value} AS TIMESTAMPTZ) IS NOT NULL "
                f"THEN CAST({value} AS TIMESTAMPTZ) "
                f"ELSE error({_literal(context + ' is not an instant spelled YYYY-MM-DDTHH:MM:SSZ: ')} "
                f"|| {value}) END")
    if kind == LIST:
        return (f"CASE WHEN list_bool_or(list_transform({value}, lambda item: item IS NULL)) "
                f"THEN error({_literal(context + ' holds a null')}) ELSE {value} END")
    if kind == JSON:
        return (f"CASE WHEN regexp_full_match({value}, {_literal(_DISPLAY)}) THEN {value} ELSE error("
                f"{_literal(context + ' is not an array of {label, name, tooltip} in printable ASCII: ')} || {value}) END")
    return value


def typed_sql(table, own):
    """The table's layer rows from ``own`` (member_key, occurrence_id, kind, attributes), each record read once for
    its values and once for their JSON types."""
    targets = json.dumps({attribute: _TARGETS[kind] for _, attribute, kind in table.columns})
    shapes = json.dumps({attribute: VARCHAR for _, attribute, _ in table.columns})
    columns = []
    for name, attribute, kind in table.columns:
        stated, context = f's."{attribute}"', f"{table.kind} {attribute}"
        allowed = ", ".join(map(_literal, _SHAPES[kind]))
        columns.append(f"CASE WHEN coalesce({stated}, 'NULL') = 'NULL' THEN NULL WHEN {stated} IN ({allowed}) "
                       f"THEN {_value_sql(attribute, kind, context)} ELSE error({_literal(context + ' is stated as ')} "
                       f"|| {stated} || {_literal(', not ' + kind)}) END AS {name}")
    return (f"SELECT member_key, occurrence_id AS source_occurrence_id, {', '.join(columns)} FROM ("
            f"SELECT member_key, occurrence_id, json_transform(attributes, {_literal(targets)}) AS a, "
            f"json_transform(json_structure(attributes), {_literal(shapes)}) AS s FROM {own} "
            f"WHERE kind = {_literal(table.kind)})")


def layer_schema(table):
    from docspec.domain.storage import TableSchema
    return TableSchema(f"regulations-gov-{table.name.replace('_', '-')}:2",
                       (("member_key", VARCHAR), ("source_occurrence_id", VARCHAR),
                        *((name, VARCHAR if kind == JSON else kind) for name, _, kind in table.columns)))


def definition(table):
    """The derive's definition: its ID scopes every occurrence, so any change to the mapping derives anew."""
    from docspec.domain import core
    from docspec.domain.identity import stable_urn
    configuration = {"table": table.name, "kind": table.kind, "key": table.key,
                     "record": "the sourceNativeFacts element whose fields.data.id is the member key",
                     "columns": [list(column) for column in table.columns],
                     "types": "spicy-docs contract types; JSON is json_column text in a VARCHAR"}
    return core.OperationDefinition(format_version=1, definition_id=stable_urn("regulations-gov-attributes", configuration),
                                    implementation_id="docspec.tools.export-regulations-attributes",
                                    implementation_version="2", operation_kind="transformation",
                                    configuration=configuration)


# ------------------------------------------------------------------ commands

def own_facts(values):
    """Project a relation of (member_key, occurrence_id, value) to each member's own fact, parsing each value once."""
    own = values.project(f"member_key, occurrence_id, list_filter(json_transform(value, {_literal(_FACTS)})"
                         ".sourceNativeFacts, lambda fact: fact.fields.data.id = member_key) AS own")
    return own.project("member_key, occurrence_id, len(own) AS own_facts, own[1].scopeId AS scope, "
                       "own[1].fields.data.type AS kind, own[1].fields.data.attributes AS attributes, "
                       "own[1].fields.data.links AS links, own[1].fields.data.relationships AS relationships")


def extract(workspace, state_id, pin, output, *, engine_memory_bytes):
    """Write each member's own fact natively, one pass over the state and one parse of each value.

    Columns: member_key, occurrence_id, own_facts (how many facts carry the
    key), scope, kind, and the own record's attributes, links and relationships
    as JSON text.
    """
    from docspec.runtime import CoreWorkspace
    with CoreWorkspace(workspace, create=False, engine_memory_bytes=engine_memory_bytes) as opened, \
            opened.open_state(state_id, expected_pin=pin) as reader, reader.value_relation() as values:
        own_facts(values).write_parquet(str(output), compression="zstd")
        rows = reader.record_count
    with duckdb.connect(config=_BOUNDED) as connection:
        shape = connection.execute("SELECT own_facts, scope, kind, count(*) FROM read_parquet(?) GROUP BY ALL "
                                   "ORDER BY ALL", [str(output)]).fetchall()
    if any(facts != 1 or (scope, kind) not in _KINDS for facts, scope, kind, _ in shape):
        raise SystemExit(f"a member has no single own document or docket fact: {shape}")
    return {"state": state_id, "pin": pin, "rows": rows, "own": str(output), "own_sha256": sha256_file(Path(output))[0],
            "kinds": {kind: count for _, _, kind, count in shape}}


def census(own):
    """Per kind and path: present, non-null and non-empty counts, JSON types, and spelling hazards."""
    names = ("present", "non_null", "non_empty", "types", "approx_distinct", "max_bytes", "non_ascii", "control_escapes")
    branches = ("attributes", "links", "relationships")
    report = {}
    with duckdb.connect(config=_BOUNDED) as connection:
        for (kind,) in connection.execute("SELECT DISTINCT kind FROM read_parquet(?) ORDER BY 1", [str(own)]).fetchall():
            paths = [(branch, f"$.{json.dumps(key)}") for branch in branches
                     for (key,) in connection.execute(f"SELECT DISTINCT unnest(json_keys({branch})) FROM read_parquet(?) "
                                                      "WHERE kind = ? ORDER BY 1", [str(own), kind]).fetchall()]
            lists = ", ".join(f"json_extract({branch}, {[path for part, path in paths if part == branch]!r}) AS {branch}"
                              for branch in branches if any(part == branch for part, _ in paths))
            measures = []
            for branch, path in paths:
                index = [path for part, path in paths if part == branch].index(path) + 1
                value, text = f"{branch}[{index}]", f"{branch}[{index}]::VARCHAR"
                measures += [f"count({value})", f"count(*) FILTER (json_type({value}) <> 'NULL')",
                             f"count(*) FILTER (json_type({value}) <> 'NULL' AND {text} NOT IN ('\"\"', '[]', '{{}}'))",
                             f"histogram(json_type({value}))", f"approx_count_distinct(hash({text}))",
                             f"max(strlen({text}))", f"count(*) FILTER (regexp_matches({text}, '[^\\x00-\\x7f]'))",
                             f"count(*) FILTER (regexp_matches({text}, '\\\\u00[01]'))"]
            row = connection.execute(f"SELECT count(*), {', '.join(measures)} FROM (SELECT {lists} FROM read_parquet(?) "
                                     "WHERE kind = ?)", [str(own), kind]).fetchone()
            report[kind] = {"records": row[0], "fields": {
                f"$.{branch}.{path[2:]}": dict(zip(names, row[1 + 8 * index:9 + 8 * index], strict=True))
                for index, (branch, path) in enumerate(paths)}}
    return report


def derive(workspace, table, own, pin, *, state_id, engine_memory_bytes):
    """Derive the table's typed layer over the pinned catalogue state as one metadata unit; return its receipt."""
    from docspec.domain import core
    from docspec.runtime import CoreWorkspace
    derivation = definition(table)
    with CoreWorkspace(workspace, create=False, engine_memory_bytes=engine_memory_bytes) as opened:
        with opened.open_state(state_id, expected_pin=pin):
            pass
        with duckdb.connect(config=_BOUNDED) as connection:
            rows = connection.sql(typed_sql(table, f"read_parquet({_literal(str(own))})")).to_arrow_reader(8192)
            started = time.perf_counter()
            derived = opened.derive_table(rows, schema=layer_schema(table),
                                          batch_id=f"{derivation.definition_id}@{pin}", definition=derivation,
                                          inputs=(core.StateInput(label="catalogue", state_id=state_id),),
                                          dataset=f"regulations-gov-{table.name.replace('_', '-')}")
            seconds = time.perf_counter() - started
    return {"table": table.name, "state_id": derived.state_id, "report": derived.report, "catalogue_pin": pin,
            "definition": derivation.definition_id, "derive_seconds": round(seconds, 2)}


def _width_sql(name, kind):
    """A column's PLAIN-encoded bytes in one row, the bound its dictionary encoding stays under."""
    if kind in {VARCHAR, JSON}:
        return f"coalesce(strlen({name}), 0) + 4"
    if kind == LIST:
        return f"coalesce(list_sum(list_transform({name}, lambda item: strlen(item) + 4)), 0) + 4"
    return {BOOLEAN: "1", INTEGER: "4", TIMESTAMPTZ: "8"}[kind]


def group_rows(widths, target):
    """The most rows, in whole vectors, a row group may hold so that no group's estimated bytes exceed ``target``.

    ``widths`` is a relation of (i, w): each row's position in key order and
    its estimated bytes. Groups fill in key order, so each candidate size is
    checked against every group it would write.
    """
    rows = widths.aggregate("count(*)").fetchone()[0]

    def largest(size):
        return widths.aggregate(f"i // {size} AS g, sum(w) AS s").aggregate("max(s)").fetchone()[0] or 0

    low, high = 1, max(1, -(-rows // _VECTOR_ROWS))
    if largest(_VECTOR_ROWS) > target:
        raise SystemExit(f"a group of {_VECTOR_ROWS} rows already holds about {largest(_VECTOR_ROWS)} bytes")
    while low < high:
        middle = (low + high + 1) // 2
        low, high = (middle, high) if largest(middle * _VECTOR_ROWS) <= target else (low, middle - 1)
    return low * _VECTOR_ROWS


def export(workspace, table, state_id, output, *, engine_memory_bytes):
    """Write the layer as one key-sorted zstd member without field IDs, its row groups sized before writing."""
    from docspec.runtime import CoreWorkspace
    names = [name for name, _, _ in table.columns]
    width = " + ".join(_width_sql(name, kind) for name, _, kind in [("member_key", "", VARCHAR), *table.columns])
    with CoreWorkspace(workspace, create=False, engine_memory_bytes=engine_memory_bytes) as opened, \
            opened.open_state(state_id) as reader, reader.table() as relation:
        widths = relation.project(f"row_number() OVER (ORDER BY member_key) - 1 AS i, {width} AS w").to_arrow_table()
        with duckdb.connect(config=_BOUNDED) as connection:
            rows_per_group = group_rows(connection.from_arrow(widths), ROW_GROUP_TARGET)
        relation.project(", ".join([f"member_key AS {table.key}", *names])).order(table.key).write_parquet(
            str(output), compression="zstd", row_group_size=rows_per_group, overwrite=True)
        digest = relation.aggregate(_multiset_sql(["member_key", *names])).fetchone()
        layer_rows = reader.record_count
    return _member(output, table, layer_rows, digest)


def _multiset_sql(columns):
    """An order-free digest of a relation's rows: their count and the sum of each row's hash."""
    return f"count(*), sum(hash({', '.join(columns)})::HUGEINT)"


def _member(path, table, layer_rows, layer_digest):
    """Verify the written member and describe it; refuse a bound it breaks.

    The member must hold the layer's rows (count and multiset digest) with the
    contract's column types, in key order without repeats, zstd throughout,
    with no field IDs, no row group over 64 MiB uncompressed and at most 1 GiB
    in all.
    """
    parquet = pq.ParquetFile(path)
    metadata = parquet.metadata
    groups = [metadata.row_group(i) for i in range(metadata.num_row_groups)]
    codecs = {group.column(j).compression for group in groups for j in range(group.num_columns)}
    largest = max((group.total_byte_size for group in groups), default=0)
    field_ids = [field.name for field in parquet.schema_arrow if field.metadata and b"PARQUET:field_id" in field.metadata]
    names = [name for name, _, _ in table.columns]
    expected = [[table.key, "VARCHAR"], *([name, _DUCKDB_TYPES[kind]] for name, _, kind in table.columns)]
    with duckdb.connect(config=_BOUNDED) as connection:
        source = f"read_parquet({_literal(str(path))}, file_row_number=true)"
        columns = [list(row) for row in connection.execute(
            f"SELECT column_name, column_type FROM (DESCRIBE SELECT * EXCLUDE (file_row_number) FROM {source})").fetchall()]
        unsorted = connection.execute(f"SELECT count(*) FILTER (WHERE {table.key} <= previous) FROM (SELECT {table.key}, "
                                      f"lag({table.key}) OVER (ORDER BY file_row_number) AS previous FROM {source})"
                                      ).fetchone()[0]
        digest = connection.execute(f"SELECT {_multiset_sql([table.key, *names])} FROM {source}").fetchone()
    size = path.stat().st_size
    problems = [message for failed, message in (
        (metadata.num_rows != layer_rows or digest != layer_digest,
         f"{metadata.num_rows} rows (digest {digest}) where the layer holds {layer_rows} (digest {layer_digest})"),
        (columns != expected, f"columns {columns} where the contract has {expected}"),
        (unsorted, f"{unsorted} rows out of key order or repeating a key"),
        (codecs != {"ZSTD"}, f"codecs {sorted(codecs)}"), (field_ids, f"field IDs on {field_ids}"),
        (largest > MAX_ROW_GROUP_BYTES, f"a {largest} B row group"),
        (size > MAX_MEMBER_BYTES, f"{size} B, over 1 GiB: propose a split by agency_code rather than splitting")) if failed]
    if problems:
        raise SystemExit(f"{path.name}: " + "; ".join(problems))
    return {"path": path.name, "sha256": sha256_file(path)[0], "byteSize": size, "rows": metadata.num_rows,
            "columns": columns, "sortedBy": table.key, "rowGroups": len(groups),
            "rowGroupRows": max((group.num_rows for group in groups), default=0),
            "largestRowGroupUncompressedBytes": largest, "uncompressedBytes": sum(g.total_byte_size for g in groups)}


def publish(workspace, receipts, members, source, *, engine_memory_bytes):
    """Export every table's layer into ``members`` and write the receipt naming the capture each derives from."""
    derived = {name: json.loads((receipts / f"derive-{name}.json").read_text()) for name in TABLES}
    pins = {receipt["catalogue_pin"] for receipt in derived.values()}
    if len(pins) != 1:
        raise SystemExit(f"the layers derive from different catalogue pins: {sorted(pins)}")
    members.mkdir(parents=True, exist_ok=True)
    exported = {name: export(workspace, table, derived[name]["state_id"], members / f"{name}.parquet",
                             engine_memory_bytes=engine_memory_bytes) for name, table in TABLES.items()}
    receipt = {"format": "regulations-attributes-bootstrap", "version": 2, "source": {**source, "pin": pins.pop()},
               "tables": {name: {"member": exported[name], "key": table.key, "keySpelling": "value/1",
                                 "references": {"table": table.kind, "column": table.key},
                                 "derivedState": derived[name]["state_id"],
                                 "definition": derived[name]["definition"]}
                          for name, table in TABLES.items()}}
    (members / "receipt.json").write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")
    return receipt


# ------------------------------------------------------------------ the reference

def _reference_value(kind, stated, context):
    """One stated attribute as the contract types it, in plain Python; anything else raises ValueError."""
    from spicy_docs.schemas.tables import json_column
    if stated is None:
        return None
    if kind == VARCHAR and isinstance(stated, str):
        return stated
    if kind == BOOLEAN and isinstance(stated, bool):
        return stated
    if kind == INTEGER and type(stated) is int and -2**31 <= stated < 2**31:
        return stated
    if kind == TIMESTAMPTZ and isinstance(stated, str) and (match := re.fullmatch(_INSTANT, stated)):
        try:
            return datetime(*map(int, match.groups()), tzinfo=timezone.utc)
        except ValueError as error:
            raise ValueError(f"{context} states {stated!r}: {error}") from error
    if kind == LIST and isinstance(stated, list) and all(isinstance(item, str) for item in stated):
        return stated
    if kind == JSON and isinstance(stated, list):
        return json_column(stated)
    raise ValueError(f"{context} states {stated!r}, not {kind}")


def expected_row(table, key, value):
    """The reference: one member's row from its catalogue value by Python's ``json``; None for another kind."""
    own = [fact["fields"]["data"] for fact in value["sourceNativeFacts"]
           if fact["fields"].get("data", {}).get("id") == key]
    if len(own) != 1:
        raise ValueError(f"{key} has {len(own)} own facts")
    if own[0]["type"] != table.kind:
        return None
    return {table.key: key, **{name: _reference_value(kind, own[0]["attributes"].get(attribute), f"{key} {attribute}")
                               for name, attribute, kind in table.columns}}


def compare_all(workspace, pin, state_id, members, *, engine_memory_bytes):
    """Compare every member row with its catalogue record, both streamed in key order, in one pass over the state."""
    from docspec.runtime import CoreWorkspace
    streams, results = {}, {}
    for name, table in TABLES.items():
        columns = [table.key, *(column for column, _, _ in table.columns)]
        batches = pq.ParquetFile(members / f"{name}.parquet").iter_batches(batch_size=4096, columns=columns)
        streams[name] = (row for batch in batches for row in batch.to_pylist())
        results[name] = {"rows": 0, "values": 0, "differing": 0, "examples": []}
    started = time.perf_counter()
    with CoreWorkspace(workspace, create=False, engine_memory_bytes=engine_memory_bytes) as opened, \
            opened.open_state(state_id, expected_pin=pin) as reader:
        for key, _, value in reader.values():
            for name, table in TABLES.items():
                expected = expected_row(table, key, value)
                if expected is None:
                    continue
                stored, result = next(streams[name], None), results[name]
                if stored is None or stored[table.key] != key:
                    raise SystemExit(f"{name}: the member holds {stored and stored[table.key]!r} where the "
                                     f"catalogue's next {table.kind} record is {key!r}")
                result["rows"] += 1
                result["values"] += len(expected)
                for column, wanted in expected.items():
                    if stored[column] != wanted:
                        result["differing"] += 1
                        if len(result["examples"]) < 10:
                            result["examples"].append([key, column])
    for name, stream in streams.items():
        if (extra := next(stream, None)) is not None:
            raise SystemExit(f"{name}: the member holds {extra[TABLES[name].key]!r}, which no catalogue record states")
    return {"seconds": round(time.perf_counter() - started, 1), "tables": results}


def check(workspace, pin, state_id, table, member, derived, *, sample, engine_memory_bytes, parents=None):
    """Compare a member with its layer and, for sampled keys, with the catalogue record decoded in Python.

    The sample is ``sample`` keys by md5 order, as many whose list columns hold
    text outside printable ASCII, and the member's widest row: the hazards a
    random draw would rarely reach. ``compare_all`` checks every row.
    """
    from docspec.runtime import CoreWorkspace
    names = [name for name, _, _ in table.columns]
    lists = [name for name, _, kind in table.columns if kind == LIST]
    hazard = " OR ".join(f"regexp_matches(array_to_string({name}, ''), '[^ -~]')" for name in lists) or "false"
    width = " + ".join(_width_sql(name, kind) for name, _, kind in table.columns)
    with duckdb.connect(config=_BOUNDED) as connection:
        rows = f"read_parquet({_literal(str(member))})"
        keys = [key for (key,) in connection.execute(
            f"(SELECT {table.key} FROM {rows} ORDER BY md5({table.key}) LIMIT {sample}) UNION "
            f"(SELECT {table.key} FROM {rows} WHERE {hazard} ORDER BY md5({table.key}) LIMIT {sample}) UNION "
            f"(SELECT {table.key} FROM {rows} ORDER BY {width} DESC, {table.key} LIMIT 1)").fetchall()]
        stored = {row[table.key]: row for row in connection.execute(
            f"SELECT {table.key}, {', '.join(names)} FROM {rows} WHERE {table.key} IN (SELECT unnest(?))",
            [keys]).to_arrow_table().to_pylist()}
        member_rows = connection.execute(f"SELECT count(*) FROM {rows}").fetchone()[0]
        orphans, uncovered = (None, None) if parents is None else connection.execute(
            f"SELECT (SELECT count(*) FROM {rows} a ANTI JOIN read_parquet('{parents}') p USING ({table.key})), "
            f"(SELECT count(*) FROM read_parquet('{parents}') p ANTI JOIN {rows} a USING ({table.key}))").fetchone()
    with CoreWorkspace(workspace, create=False, engine_memory_bytes=engine_memory_bytes) as opened:
        with opened.open_state(derived["state_id"]) as layer:
            layer_rows = layer.record_count
        with opened.open_state(state_id, expected_pin=pin) as reader:
            expected = {key: expected_row(table, key, value) for key, _, value in reader.values(member_keys=sorted(keys))}
    differing = sorted([key, name] for key in keys for name in [table.key, *names]
                       if stored[key][name] != expected[key][name])
    return {"table": table.name, "member_rows": member_rows, "layer_rows": layer_rows,
            "rows_equal": member_rows == layer_rows, "sampled_keys": sorted(keys),
            "compared_keys_values": [len(keys), len(keys) * (1 + len(names))], "differing": differing,
            "parents": None if parents is None else str(parents), "rows_without_parent": orphans,
            "parent_rows_without_attributes": uncovered}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("extract", "census", "derive", "export", "check"):
        command = commands.add_parser(name)
        command.add_argument("--out", type=Path, required=True, help="the file, or for export the directory, to write")
        if name != "census":
            command.add_argument("--workspace", type=Path, required=True)
            command.add_argument("--engine-memory-gib", type=int, default=3)
        if name in {"extract", "derive", "check"}:
            command.add_argument("--state", default="catalogue")
            command.add_argument("--pin", required=True, help="the catalogue state's read pin")
        if name in {"extract", "census", "derive"}:
            command.add_argument("--own", type=Path, required=True, help="the extracted own-fact Parquet")
        if name == "derive":
            command.add_argument("--table", choices=sorted(TABLES), required=True)
        if name in {"export", "check"}:
            command.add_argument("--receipts", type=Path, required=True, help="the directory holding derive receipts")
        if name == "export":
            command.add_argument("--source", type=Path, required=True, help="JSON describing the capture")
        if name == "check":
            command.add_argument("--members", type=Path, required=True)
            command.add_argument("--sample", type=int, default=10)
            command.add_argument("--all", action="store_true", help="also compare every row with its record")
            command.add_argument("--parent", action="append", default=[], metavar="TABLE=PARQUET",
                                 help="the thin table a table references, to count keys either lacks")
    args = parser.parse_args(argv)
    memory = getattr(args, "engine_memory_gib", 0) * 2**30
    if args.command == "extract":
        result = extract(args.workspace, args.state, args.pin, args.own, engine_memory_bytes=memory)
    elif args.command == "census":
        result = census(args.own)
    elif args.command == "derive":
        result = derive(args.workspace, TABLES[args.table], args.own, args.pin, state_id=args.state,
                        engine_memory_bytes=memory)
    elif args.command == "export":
        result = publish(args.workspace, args.receipts, args.out, json.loads(args.source.read_text()),
                         engine_memory_bytes=memory)
    else:
        parents = dict(value.split("=", 1) for value in args.parent)
        result = {name: check(args.workspace, args.pin, args.state, table, args.members / f"{name}.parquet",
                              json.loads((args.receipts / f"derive-{name}.json").read_text()), sample=args.sample,
                              engine_memory_bytes=memory, parents=parents.get(name))
                  for name, table in TABLES.items()}
        if args.all:
            result["every_row"] = compare_all(args.workspace, args.pin, args.state, args.members,
                                              engine_memory_bytes=memory)
    if args.command != "export":
        args.out.write_text(json.dumps(result, indent=1, sort_keys=True, default=str) + "\n")
    json.dump(result if args.command != "census" else {"census": str(args.out)}, sys.stdout, sort_keys=True,
              default=str)
    print()


if __name__ == "__main__":
    main()
