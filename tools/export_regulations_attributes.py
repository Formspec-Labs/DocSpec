"""Derive the Regulations.gov attribute tables from DocSpec's catalogue capture and export them as spicy-regs members.

The thin spicy-regs ``documents`` and ``dockets`` tables carry a few publisher
attributes; this tool carries the others a record states (proposal:
``docs/research/regulations-attributes-contract-2026-09-26.md``). Each command
runs in its own process:

  extract  each member's own Regulations.gov record, natively, from the pinned catalogue state
  census   per kind and path: counts, JSON types and spelling hazards, the column proposal's evidence
  derive   one typed derived layer (C29 ``derive_table``) per table from the extracted records
  export   the layer as one spicy-regs member: key-sorted, zstd, row groups of at most 64 MiB, no field IDs
  check    in a fresh process: member rows equal the layer's, and sampled rows equal the catalogue record

Columns are VARCHAR, spelled as spicy-docs' ``schemas.tables`` helpers spell a
published row: ``text`` for a scalar, ``json_column`` for a list or object.
Both spellings run natively; no row is decoded in Python except by ``check``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

# A standalone connection's bounds; DuckDB's default takes 80% of the machine's memory.
_BOUNDED = {"memory_limit": "3GB", "threads": "2", "preserve_insertion_order": "false"}
TEXT, JSON = "text", "json"
MAX_MEMBER_BYTES = 2**30
MAX_ROW_GROUP_BYTES = 64 * 2**20
_ROW_GROUP_TARGET = 32 * 2**20


@dataclass(frozen=True)
class Table:
    """One attribute table: the record kind it holds, its key, and each (API attribute, spelling) it carries."""

    name: str
    kind: str
    key: str
    attributes: tuple[tuple[str, str], ...]

    @property
    def columns(self) -> tuple[tuple[str, str, str], ...]:
        """(column, attribute, spelling): the column is the attribute in snake_case, ``_json`` after a list."""
        return tuple((re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower() + ("_json" if spelling == JSON else ""),
                      name, spelling) for name, spelling in self.attributes)


# Every attribute a record states except those the thin table carries, those never stated, constant or derivable
# from the key, and submitters' contact details; the proposal lists each with its count.
TABLES = {table.name: table for table in (
    Table("document_attributes", "documents", "document_id", (
        ("allowLateComments", TEXT), ("authorDate", TEXT), ("authors", JSON), ("category", TEXT), ("cfrPart", TEXT),
        ("comment", TEXT), ("displayProperties", JSON), ("docAbstract", TEXT), ("effectiveDate", TEXT),
        ("exhibitLocation", TEXT), ("exhibitType", TEXT), ("field1", TEXT), ("field2", TEXT), ("frVolNum", TEXT),
        ("govAgency", TEXT), ("govAgencyType", TEXT), ("implementationDate", TEXT), ("legacyId", TEXT),
        ("media", TEXT), ("objectId", TEXT), ("ombApproval", TEXT), ("openForComment", TEXT),
        ("organization", TEXT), ("originalDocumentId", TEXT), ("pageCount", TEXT), ("postmarkDate", TEXT),
        ("receiveDate", TEXT), ("regWriterInstruction", TEXT), ("restrictReason", TEXT),
        ("restrictReasonType", TEXT), ("sourceCitation", TEXT), ("startEndPage", TEXT), ("subject", TEXT),
        ("subtype", TEXT), ("topics", JSON), ("trackingNbr", TEXT), ("withinCommentPeriod", TEXT))),
    Table("docket_attributes", "dockets", "docket_id", (
        ("category", TEXT), ("displayProperties", JSON), ("effectiveDate", TEXT), ("field1", TEXT), ("field2", TEXT),
        ("generic", TEXT), ("keywords", JSON), ("legacyId", TEXT), ("objectId", TEXT), ("organization", TEXT),
        ("petitionNbr", TEXT), ("program", TEXT), ("shortTitle", TEXT), ("subType", TEXT), ("subType2", TEXT))),
)}

# The record's own Regulations.gov fact: the one whose data.id is the member's key. A document also carries its
# joined docket and Federal Register facts, so a fixed array position would read the wrong record.
_OWN_FACT = ("list_filter(json_extract(value, '$.sourceNativeFacts[*]'), "
             "lambda fact: (fact->>'$.fields.data.id') = member_key)")
_KINDS = {("regulations-gov-documents", "documents"), ("regulations-gov-dockets", "dockets")}


# ------------------------------------------------------------------ spelling

def _ascii_sql(text):
    """Escape DEL and each non-ASCII character of JSON text as ``\\uxxxx``, above U+FFFF as a surrogate pair, as
    Python's ``ensure_ascii`` does. Only string contents hold them, and the escapes add none."""
    point = "unicode(c)"
    escaped = (f"CASE WHEN {point} < 127 THEN c WHEN {point} < 65536 THEN printf('\\u%04x', {point}) "
               f"ELSE printf('\\u%04x\\u%04x', 55296 + (({point} - 65536) >> 10), 56320 + (({point} - 65536) & 1023)) END")
    return (f"CASE WHEN regexp_matches({text}, '[^\\x00-\\x7e]') THEN array_to_string(list_transform("
            f"regexp_extract_all({text}, '(?s).'), lambda c: {escaped}), '') ELSE {text} END")


def _string_sql(value):
    """A VARCHAR as ``json.dumps`` spells it: DocSpec's canonical string escapes, then non-ASCII escaped."""
    from docspec.adapters.storage.table_sql import json_string_sql
    return _ascii_sql(json_string_sql(value))


def _scalar_sql(value, context):
    """A JSON scalar as ``json.dumps`` spells it. A double, which DuckDB and Python spell differently, refuses."""
    text = f"json_extract_string({value}, '$')"
    return (f"CASE json_type({value}) WHEN 'VARCHAR' THEN {_string_sql(text)} WHEN 'NULL' THEN 'null' "
            f"WHEN 'BOOLEAN' THEN {text} WHEN 'BIGINT' THEN {text} WHEN 'UBIGINT' THEN {text} "
            f"ELSE error('{context} holds a value other than a string, boolean, integer or null') END")


def _json_sql(value, context):
    """``json_column``'s spelling of an array of scalars or of flat objects: compact, keys sorted, ASCII."""
    field = "json_extract(element, '$.\"' || name || '\"')"
    member = f"{_string_sql('name')} || ':' || {_scalar_sql(field, context)}"
    keys_ok = "list_bool_and(list_transform(json_keys(element), lambda name: regexp_full_match(name, '[A-Za-z0-9_]+')))"
    obj = (f"CASE WHEN {keys_ok} IS NOT FALSE THEN '{{' || coalesce(array_to_string(list_transform("
           f"list_sort(json_keys(element)), lambda name: {member}), ','), '') || '}}' "
           f"ELSE error('{context} holds an object key outside [A-Za-z0-9_]') END")
    element = f"CASE WHEN json_type(element) = 'OBJECT' THEN {obj} ELSE {_scalar_sql('element', context)} END"
    return (f"CASE WHEN {value} IS NULL OR json_type({value}) = 'NULL' THEN NULL WHEN json_type({value}) = 'ARRAY' "
            f"THEN '[' || coalesce(array_to_string(list_transform(json_extract({value}, '$[*]'), "
            f"lambda element: {element}), ','), '') || ']' ELSE error('{context} is not an array') END")


def _text_sql(value, context):
    """``text``'s spelling: a string as stated, a boolean ``true``/``false``, an integer in decimal; NULL if absent."""
    return (f"CASE WHEN {value} IS NULL OR json_type({value}) = 'NULL' THEN NULL "
            f"WHEN json_type({value}) IN ('VARCHAR', 'BOOLEAN', 'BIGINT', 'UBIGINT') "
            f"THEN json_extract_string({value}, '$') ELSE error('{context} is not a string, boolean or integer') END")


def typed_sql(table, own):
    """The table's layer rows from ``own`` (member_key, occurrence_id, kind, data), each record parsed once."""
    paths = [f"$.attributes.{attribute}" for _, attribute, _ in table.columns]
    columns = [(_json_sql if spelling == JSON else _text_sql)(f"v[{index}]", f"{table.kind} {attribute}") + f" AS {name}"
               for index, (name, attribute, spelling) in enumerate(table.columns, start=1)]
    return (f"SELECT member_key, occurrence_id AS source_occurrence_id, {', '.join(columns)} FROM ("
            f"SELECT member_key, occurrence_id, json_extract(data, {paths!r}) AS v FROM {own} "
            f"WHERE kind = '{table.kind}')")


def layer_schema(table):
    from docspec.domain.storage import TableSchema
    return TableSchema(f"regulations-gov-{table.name.replace('_', '-')}:1",
                       (("member_key", "VARCHAR"), ("source_occurrence_id", "VARCHAR"),
                        *((name, "VARCHAR") for name, _, _ in table.columns)))


def definition(table):
    """The derive's definition: its ID scopes every occurrence, so any change to the mapping derives anew."""
    from docspec.domain import core
    from docspec.domain.identity import stable_urn
    configuration = {"table": table.name, "kind": table.kind, "key": table.key,
                     "record": "the sourceNativeFacts element whose fields.data.id is the member key",
                     "columns": [list(column) for column in table.columns],
                     "spelling": "spicy-docs schemas.tables text() and json_column()"}
    return core.OperationDefinition(format_version=1, definition_id=stable_urn("regulations-gov-attributes", configuration),
                                    implementation_id="docspec.tools.export-regulations-attributes",
                                    implementation_version="1", operation_kind="transformation",
                                    configuration=configuration)


# ------------------------------------------------------------------ commands

def extract(workspace, state_id, pin, output, *, engine_memory_bytes):
    """Write each member's own fact (member_key, occurrence_id, kind, data JSON) natively, one pass over the state."""
    from docspec.runtime import CoreWorkspace
    with CoreWorkspace(workspace, create=False, engine_memory_bytes=engine_memory_bytes) as opened, \
            opened.open_state(state_id, expected_pin=pin) as reader, reader.value_relation() as values:
        own = values.project(f"member_key, occurrence_id, {_OWN_FACT} AS own")
        own.project("member_key, occurrence_id, len(own) AS own_facts, own[1]->>'$.scopeId' AS scope, "
                    "own[1]->>'$.fields.data.type' AS kind, own[1]->'$.fields.data' AS data"
                    ).write_parquet(str(output), compression="zstd")
        rows = reader.record_count
    with duckdb.connect(config=_BOUNDED) as connection:
        shape = connection.execute("SELECT own_facts, scope, kind, count(*) FROM read_parquet(?) GROUP BY ALL "
                                   "ORDER BY ALL", [str(output)]).fetchall()
    if any(facts != 1 or (scope, kind) not in _KINDS for facts, scope, kind, _ in shape):
        raise SystemExit(f"a member has no single own document or docket fact: {shape}")
    return {"state": state_id, "pin": pin, "rows": rows, "own": str(output), "own_sha256": "sha256:" + _sha256(output),
            "kinds": {kind: count for _, _, kind, count in shape}}


def census(own):
    """Per kind and path: present, non-null and non-empty counts, JSON types, and spelling hazards.

    Each record is parsed once, into a list of its values at every path the
    kind states; a lateral ``json_each`` over the same rows exceeded 3 GB.
    """
    names = ("present", "non_null", "non_empty", "types", "approx_distinct", "max_bytes", "non_ascii", "control_escapes")
    report = {}
    with duckdb.connect(config=_BOUNDED) as connection:
        for (kind,) in connection.execute("SELECT DISTINCT kind FROM read_parquet(?) ORDER BY 1", [str(own)]).fetchall():
            paths = [f"$.{branch}.{json.dumps(key)}" for branch in ("attributes", "links", "relationships")
                     for (key,) in connection.execute(
                         f"SELECT DISTINCT unnest(json_keys(data, '$.{branch}')) FROM read_parquet(?) WHERE kind = ? "
                         "ORDER BY 1", [str(own), kind]).fetchall()]
            measures = []
            for index in range(1, len(paths) + 1):
                value, text = f"v[{index}]", f"v[{index}]::VARCHAR"
                measures += [f"count({value})", f"count(*) FILTER (json_type({value}) <> 'NULL')",
                             f"count(*) FILTER (json_type({value}) <> 'NULL' AND {text} NOT IN ('\"\"', '[]', '{{}}'))",
                             f"histogram(json_type({value}))", f"approx_count_distinct(hash({text}))",
                             f"max(strlen({text}))", f"count(*) FILTER (regexp_matches({text}, '[^\\x00-\\x7f]'))",
                             f"count(*) FILTER (regexp_matches({text}, '\\\\u00[01]'))"]
            row = connection.execute(
                f"SELECT count(*), {', '.join(measures)} FROM (SELECT json_extract(data, {paths!r}) AS v "
                "FROM read_parquet(?) WHERE kind = ?)", [str(own), kind]).fetchone()
            report[kind] = {"records": row[0], "fields": {
                path: dict(zip(names, row[1 + 8 * index:9 + 8 * index], strict=True)) for index, path in enumerate(paths)}}
    return report


def derive(workspace, table, own, pin, *, state_id, engine_memory_bytes):
    """Derive the table's typed layer over the pinned catalogue state as one metadata unit; return its receipt."""
    from docspec.domain import core
    from docspec.runtime import CoreWorkspace
    before = _footprint(workspace)
    with CoreWorkspace(workspace, create=False, engine_memory_bytes=engine_memory_bytes) as opened:
        with opened.open_state(state_id, expected_pin=pin):
            pass
        with duckdb.connect(config=_BOUNDED) as connection:
            rows = connection.sql(typed_sql(table, f"read_parquet('{own}')")).to_arrow_reader(8192)
            started = time.perf_counter()
            derived = opened.derive_table(rows, schema=layer_schema(table), batch_id=f"{table.name}@{pin}",
                                          definition=definition(table),
                                          inputs=(core.StateInput(label="catalogue", state_id=state_id),),
                                          dataset=f"regulations-gov-{table.name.replace('_', '-')}")
            seconds = time.perf_counter() - started
    return {"table": table.name, "state_id": derived.state_id, "report": derived.report, "catalogue_pin": pin,
            "derive_seconds": round(seconds, 2), "workspace_bytes_added": _footprint(workspace) - before}


def export(workspace, table, state_id, output, *, engine_memory_bytes):
    """Write the layer as one key-sorted zstd member without field IDs, its row groups at most 64 MiB uncompressed.

    DuckDB ignores a byte bound on row groups for sorted output, so the row
    count is set from the layer's mean row width and halved until the largest
    written group fits.
    """
    from docspec.runtime import CoreWorkspace
    names = [name for name, _, _ in table.columns]
    with CoreWorkspace(workspace, create=False, engine_memory_bytes=engine_memory_bytes) as opened, \
            opened.open_state(state_id) as reader, reader.table() as relation:
        layer_rows = reader.record_count
        width = relation.aggregate("avg(" + " + ".join(f"coalesce(strlen({name}), 0) + 4"
                                                       for name in ["member_key", *names]) + ")").fetchone()[0]
        member = relation.project(", ".join([f"member_key AS {table.key}", *names])).order(table.key)
        group_rows = max(1024, int(_ROW_GROUP_TARGET / width))
        while True:
            member.write_parquet(str(output), compression="zstd", row_group_size=group_rows, overwrite=True)
            if max(group.total_byte_size for group in _row_groups(output)) <= MAX_ROW_GROUP_BYTES or group_rows == 1024:
                break
            group_rows = max(1024, group_rows // 2)
        digest = relation.aggregate(_multiset_sql(["member_key", *names])).fetchone()
    return _member(output, table, layer_rows, digest, group_rows)


def _multiset_sql(columns):
    """An order-free digest of a relation's rows: their count and the sum of each row's hash."""
    return f"count(*), sum(hash({', '.join(columns)})::HUGEINT)"


def _member(path, table, layer_rows, layer_digest, group_rows):
    """Verify the written member and describe it; refuse a bound it breaks.

    The member must hold the layer's rows (count and multiset digest), in key
    order without repeats, zstd throughout, with no field IDs, no row group
    over 64 MiB uncompressed and at most 1 GiB in all.
    """
    parquet = pq.ParquetFile(path)
    metadata, groups = parquet.metadata, _row_groups(path)
    codecs = {group.column(j).compression for group in groups for j in range(group.num_columns)}
    largest = max((group.total_byte_size for group in groups), default=0)
    field_ids = [field.name for field in parquet.schema_arrow if field.metadata and b"PARQUET:field_id" in field.metadata]
    names = [name for name, _, _ in table.columns]
    with duckdb.connect(config=_BOUNDED) as connection:
        source = f"read_parquet('{path}', file_row_number=true)"
        columns = connection.execute(f"SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM "
                                     f"read_parquet('{path}'))").fetchall()
        unsorted = connection.execute(f"SELECT count(*) FILTER (WHERE {table.key} <= previous) FROM (SELECT {table.key}, "
                                      f"lag({table.key}) OVER (ORDER BY file_row_number) AS previous FROM {source})"
                                      ).fetchone()[0]
        digest = connection.execute(f"SELECT {_multiset_sql([table.key, *names])} FROM read_parquet('{path}')").fetchone()
    size = path.stat().st_size
    problems = [message for failed, message in (
        (metadata.num_rows != layer_rows or digest != layer_digest,
         f"{metadata.num_rows} rows (digest {digest}) where the layer holds {layer_rows} (digest {layer_digest})"),
        (unsorted, f"{unsorted} rows out of key order or repeating a key"),
        (codecs != {"ZSTD"}, f"codecs {sorted(codecs)}"), (field_ids, f"field IDs on {field_ids}"),
        (largest > MAX_ROW_GROUP_BYTES, f"a {largest} B row group"),
        (size > MAX_MEMBER_BYTES, f"{size} B, over 1 GiB: propose a split by agency_code rather than splitting")) if failed]
    if problems:
        raise SystemExit(f"{path.name}: " + "; ".join(problems))
    return {"path": path.name, "sha256": "sha256:" + _sha256(path), "byteSize": size, "rows": metadata.num_rows,
            "columns": [list(column) for column in columns], "sortedBy": table.key, "rowGroups": len(groups),
            "rowGroupRows": group_rows, "largestRowGroupUncompressedBytes": largest,
            "uncompressedBytes": sum(group.total_byte_size for group in groups)}


def expected_row(table, key, value):
    """The reference: one member's row from its catalogue value by Python's ``json`` and spicy-docs' helpers;
    None when the member is a record of another kind."""
    from spicy_docs.schemas.tables import json_column, text
    own = [fact["fields"]["data"] for fact in value["sourceNativeFacts"]
           if fact["fields"].get("data", {}).get("id") == key]
    if len(own) != 1:
        raise ValueError(f"{key} has {len(own)} own facts")
    if own[0]["type"] != table.kind:
        return None
    row = {table.key: key}
    for name, attribute, spelling in table.columns:
        stated = own[0]["attributes"].get(attribute)
        row[name] = None if stated is None else json_column(stated) if spelling == JSON else text(stated)
    return row


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

    The sample is ``sample`` keys by md5 order, the same number of keys whose
    JSON columns hold an escaped character (non-ASCII or control), and the
    member's longest row: the spelling hazards a random draw would rarely reach.
    """
    from docspec.runtime import CoreWorkspace
    json_columns = [name for name, _, spelling in table.columns if spelling == JSON]
    names = [name for name, _, _ in table.columns]
    hazard = " OR ".join(f"contains({name}, '\\u')" for name in json_columns) or "false"
    width = " + ".join(f"coalesce(strlen({name}), 0)" for name in names)
    with duckdb.connect(config=_BOUNDED) as connection:
        rows = f"read_parquet('{member}')"
        keys = [key for (key,) in connection.execute(
            f"(SELECT {table.key} FROM {rows} ORDER BY md5({table.key}) LIMIT {sample}) UNION "
            f"(SELECT {table.key} FROM {rows} WHERE {hazard} ORDER BY md5({table.key}) LIMIT {sample}) UNION "
            f"(SELECT {table.key} FROM {rows} ORDER BY {width} DESC, {table.key} LIMIT 1)").fetchall()]
        stored = {row[0]: dict(zip([table.key, *names], row, strict=True)) for row in connection.execute(
            f"SELECT {table.key}, {', '.join(names)} FROM {rows} WHERE {table.key} IN (SELECT unnest(?))",
            [keys]).fetchall()}
        member_rows = connection.execute(f"SELECT count(*) FROM {rows}").fetchone()[0]
        orphans, uncovered = (None, None) if parents is None else connection.execute(
            f"SELECT (SELECT count(*) FROM {rows} a ANTI JOIN read_parquet('{parents}') p USING ({table.key})), "
            f"(SELECT count(*) FROM read_parquet('{parents}') p ANTI JOIN {rows} a USING ({table.key}))").fetchone()
    with CoreWorkspace(workspace, create=False, engine_memory_bytes=engine_memory_bytes) as opened:
        with opened.open_state(derived["state_id"]) as layer:
            layer_rows = layer.record_count
        with opened.open_state(state_id, expected_pin=pin) as reader:
            expected = {key: expected_row(table, key, value)
                        for key, _, value in reader.values(member_keys=sorted(keys))}
    differing = sorted([key, name] for key in keys for name in [table.key, *names]
                       if stored[key][name] != expected[key][name])
    compared = [len(keys), len(keys) * (1 + len(names))]
    return {"table": table.name, "member_rows": member_rows, "layer_rows": layer_rows,
            "rows_equal": member_rows == layer_rows, "sampled_keys": sorted(keys), "compared_keys_values": compared,
            "differing": differing, "parents": None if parents is None else str(parents),
            "rows_without_parent": orphans, "parent_rows_without_attributes": uncovered}


# ------------------------------------------------------------------ helpers

def _row_groups(path):
    metadata = pq.ParquetFile(path).metadata
    return [metadata.row_group(i) for i in range(metadata.num_row_groups)]


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _footprint(root):
    return sum(path.stat().st_size for path in Path(root).rglob("*") if path.is_file())


def publish(workspace, receipts, members, source, *, engine_memory_bytes):
    """Export every table's layer into ``members`` and write the receipt naming the capture each derives from."""
    derived = {name: json.loads((receipts / f"derive-{name}.json").read_text()) for name in TABLES}
    pins = {receipt["catalogue_pin"] for receipt in derived.values()}
    if len(pins) != 1:
        raise SystemExit(f"the layers derive from different catalogue pins: {sorted(pins)}")
    members.mkdir(parents=True, exist_ok=True)
    exported = {name: export(workspace, table, derived[name]["state_id"], members / f"{name}.parquet",
                             engine_memory_bytes=engine_memory_bytes) for name, table in TABLES.items()}
    receipt = {"format": "regulations-attributes-bootstrap", "version": 1, "source": {**source, "pin": pins.pop()},
               "tables": {name: {"member": exported[name], "key": table.key, "keySpelling": "value/1",
                                 "references": {"table": table.kind, "column": table.key},
                                 "derivedState": derived[name]["state_id"],
                                 "definition": definition(table).definition_id}
                          for name, table in TABLES.items()}}
    (members / "receipt.json").write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")
    return receipt


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
                                 help="the thin table a table references, to count keys it lacks")
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
        args.out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    json.dump(result if args.command != "census" else {"census": str(args.out)}, sys.stdout, sort_keys=True)
    print()


if __name__ == "__main__":
    main()
