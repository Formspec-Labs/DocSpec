"""C29 gate: derive Search's prepared fields as a typed layer and check them against the JSON derive's values.

Each subcommand runs in its own process with the worktree's environment
(``uv run --frozen --project $WT python $G COMMAND``), the large ones under the
PM01 watch wrapper. Receipts are JSON files under ``GATE/receipts``; nothing
here writes outside the gate directory, and retained workspaces are read only.

  engine-id          pin Engine's stable_id: Engine's own functions, read from its source, against the native spelling
  type NAME          type a JSON sample's prepared values into Engine's columns natively (DuckDB's JSON reader)
  derive             derive PM01's 10,000 FR values as a typed layer into a copy of the PM01 workspace
  compare NAME       in a fresh process, compare every (member key, field) with the values Python's json decodes
  revise             derive PM01's second source revision incrementally over the first layer
  coverage           type and derive Regulations.gov's 10,000 values and one member with every filter and date
  json-derive        the baseline: C26's JSON derive of the same 10,000 values, whole and cut to the typed fields
  scale-type         type all prepared values of tonight's FR cutover state, read-only, into one Parquet file
  scale-derive       derive them into a fresh workspace; then scale-revise, an incremental derive over that base
  scale-check        in a fresh process, compare the derived table with the typed input natively, both directions
  scale-json         the baseline at scale: C26's JSON derive of the same values, cut to the typed fields
  scale-layers       bytes, files and rows of each full-scale state's layers
  scale-lookups      point reads on the full-scale typed state
  scale-affected     a typed layer over an admitted FR generation, the next admitted, and the rows its changes affect
  scale-affected-wide  the same over a wide layer of every producer column
  scale-affected-certified  affected() over the 1M JSON state revised by a certified 50-member derive

The typing step stands in for Search's future typed preparer. It is native
(DuckDB's JSON functions), and the reference is plain Python over ``json``, so
the two share only the mapping rules of Engine's ``row()``, never code.
"""

import ast
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import sqlite3
import subprocess
import sys
import time

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

GATE = Path("/Users/mikewolfd/Work/corpora/c29-gate-20260925")
RECEIPTS, INPUTS, WORKSPACES = GATE / "receipts", GATE / "inputs", GATE / "workspaces"
PM01 = Path("/Users/mikewolfd/Work/corpora/pm01-gate-2026-09-23")
PREPARED = {1: PM01 / "compare" / "fr-10k.prepared-1.jsonl", 2: PM01 / "compare" / "fr-10k.prepared-2.jsonl"}
REVISION_KEYS = PM01 / "samples" / "fr-10k.source-2.keys.json"
SOURCE_STATES = {1: "source", 2: "urn:docspec:execution:743fbd0a-d981-4919-8b54-8e46a9a4634d:state"}
TYPED_BASE = "c29-gate-10k-first"
CUTOVER = Path("/Users/mikewolfd/Work/corpora/docspec-iceberg-reimport-2173b92-20260914/federal-register/workspace")
CUTOVER_PREPARED = ("urn:docspec:execution:13e28a19-e7b0-48a8-9946-8d4a207f7337:state",
                    "sha256:445789ab6f5cee7dc7afec33f4325f6a2cee58f8ec92b8c397fe5096b77f25c3")
CUTOVER_SOURCE = ("catalogue", "sha256:b456349d6ec916a8f145ec9ab5ee39e95f5e13a0de70ca7220dfe1c6a5508a61")
ENGINE = Path("/Users/mikewolfd/Work/spicy-stack/spicyengine")
SOURCE_ID = "federal-register"

# Engine's prepared columns (spicyengine prepared_fields.py and indexing/prepared_table.py), minus what the
# spike found Engine need not store: retained_ref, content_sha256 and publication_date. Engine's occurrence_id
# is the source occurrence here; the typed reader supplies the derived one.
FILTERS = ("agency", "parent_agency", "record_kind", "document_type", "docket_type", "subtype", "docket_id",
           "document_id", "regulation_identifier_number", "cfr", "cfr_title", "topic", "organization", "author",
           "program", "file_format", "withdrawn", "body_indexed")
DATES = {"publication_date": "DATE", "published_on": "DATE", "posted_on": "DATE", "updated_on": "DATE",
         "author_on": "DATE", "received_on": "DATE", "implementation_on": "DATE", "published_at": "TIMESTAMPTZ",
         "signing_at": "TIMESTAMPTZ", "effective_on": "DATE", "comment_opens_on": "DATE",
         "comment_closes_on": "DATE", "signing_on": "DATE", "posted_at": "TIMESTAMPTZ", "updated_at": "TIMESTAMPTZ",
         "effective_at": "TIMESTAMPTZ", "comment_opens_at": "TIMESTAMPTZ", "comment_closes_at": "TIMESTAMPTZ",
         "author_at": "TIMESTAMPTZ", "received_at": "TIMESTAMPTZ", "implementation_at": "TIMESTAMPTZ"}
TEXTS = ("title", "source_url", "primary_text", "related_text")
DISPLAY = ("format", "display", "filters", "dates", "facet_labels", "facet_aliases")
COLUMNS = (("id", "VARCHAR"), ("source_id", "VARCHAR"), ("member_key", "VARCHAR"), ("source_occurrence_id", "VARCHAR"),
           *((name, "VARCHAR") for name in TEXTS), ("metadata", "VARCHAR"), ("identifiers", "VARCHAR[]"),
           *((f"filter_{name}", "VARCHAR[]") for name in FILTERS), ("filter_agency_scope", "VARCHAR[]"),
           *((f"date_{name}", kind) for name, kind in DATES.items()))


def _schema():
    from docspec.domain.storage import TableSchema
    return TableSchema("c29-gate-prepared-fields:1", COLUMNS)


def _definition(source_id=SOURCE_ID):
    from docspec.domain import core
    from docspec.domain.identity import stable_urn
    configuration = {"source_id": source_id, "columns": "spicyengine indexing/prepared_table.py arrow_schema, "
                     "without retained_ref, content_sha256 and publication_date", "preparer": "PM01 Search b150fdd"}
    return core.OperationDefinition(format_version=1, definition_id=stable_urn("c29-gate-typed-prepared", configuration),
                                    implementation_id="c29-gate.typed-prepared-fields", implementation_version="1",
                                    operation_kind="transformation", configuration=configuration)


def _write(name, value):
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    value = {"recorded_at": datetime.now().isoformat(timespec="seconds"), "environment": _environment(), **value}
    (RECEIPTS / f"{name}.json").write_text(json.dumps(value, indent=1, sort_keys=True, default=str) + "\n")
    print(json.dumps(value, sort_keys=True, default=str)[:3000])


def _read(name):
    return json.loads((RECEIPTS / f"{name}.json").read_text())


def _environment():
    commit = subprocess.run(["git", "-C", str(Path(__file__).resolve().parents[3]), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    return {"commit": commit, "python": platform.python_version(), "duckdb": duckdb.__version__,
            "pyarrow": pa.__version__, "load": os.getloadavg()}


class Clock:
    def __enter__(self):
        self.load_start = os.getloadavg()
        self.wall, self.cpu = time.perf_counter(), time.process_time()
        return self

    def __exit__(self, *exc):
        self.wall, self.cpu = time.perf_counter() - self.wall, time.process_time() - self.cpu
        self.load_end = os.getloadavg()

    def receipt(self, records=None):
        value = {"wall_seconds": round(self.wall, 3), "cpu_seconds": round(self.cpu, 3),
                 "load_start": [round(x, 2) for x in self.load_start], "load_end": [round(x, 2) for x in self.load_end],
                 "peak_rss_bytes_process": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
        if records:
            value["ms_per_record"] = round(1000 * self.wall / records, 4)
        return value


def footprint(root):
    """Apparent bytes by kind: the ledger, Parquet data, other record metadata and blobs."""
    out = dict(sqlite=0, parquet=0, record_metadata=0, blobs=0, other=0, files=0)
    for path in Path(root).rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        kind = ("blobs" if parts[0] == "blobs" else "sqlite" if path.name.startswith("ledger.sqlite")
                else "parquet" if path.suffix == ".parquet" else "record_metadata" if parts[0] == "records" else "other")
        out[kind] += path.stat().st_size
        out["files"] += 1
    out["total"] = sum(out[kind] for kind in ("sqlite", "parquet", "record_metadata", "blobs", "other"))
    return out


def ledger_counts(root):
    with sqlite3.connect(f"file:{root / 'ledger.sqlite'}?mode=ro", uri=True) as connection:
        return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("records", "retention", "links", "units")}


def _delta(before, after):
    return {name: after[name] - before[name] for name in after}


# ---------------------------------------------------------------- Engine's ID

def _engine_functions():
    """Engine's own encoded/digest/stable_id, read from its source file and run without importing spicyengine."""
    path = ENGINE / "src" / "spicyengine" / "indexing" / "prepared_table.py"
    tree = ast.parse(path.read_text())
    wanted = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in {"encoded", "digest", "stable_id"}]
    namespace = {"json": json, "hashlib": hashlib}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(path), "exec"), namespace)
    commit = subprocess.run(["git", "-C", str(ENGINE), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return namespace["stable_id"], commit, ast.get_source_segment(path.read_text(), wanted[-1])


def _id_sql(key, source_id=SOURCE_ID):
    """Engine's stable_id natively: sha256 of the compact JSON array [source_id, member_key]."""
    from docspec.adapters.storage.iceberg import literal
    from docspec.adapters.storage.table_sql import json_array_sql
    return f"sha256({json_array_sql(literal(source_id), key)})"


def engine_id():
    stable_id, commit, source = _engine_functions()
    strings = ["2026-12345@2026-09-25", "quote\"key", "back\\slash", "ctl\x00\x01\x1f\x7f", "\n\r\t\b\f", "é😀",
               "  ", "/solidus", "\\u001F", " "]
    strings += [chr(code) + "k" for code in range(32)]
    keys = [key for key, _ in _jsonl(PREPARED[1])] + strings
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE keys (member_key VARCHAR)")
        connection.executemany("INSERT INTO keys VALUES (?)", [(key,) for key in keys])
        native = connection.execute(f"SELECT member_key, {_id_sql('member_key')} FROM keys").fetchall()
    differ = [key for key, value in native if value != stable_id(SOURCE_ID, key)]
    _write("engine-id", {"engine_commit": commit, "engine_function": source, "keys": len(keys),
                         "adversarial": len(strings), "differing": differ})
    if differ:
        raise SystemExit("the native id differs from Engine's stable_id")


# ---------------------------------------------------------------- typing

def _jsonl(path):
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            key, value = json.loads(line)
            yield key, value


_STRUCTURE = json.dumps({**{name: "VARCHAR" for name in TEXTS}, "identifiers": "VARCHAR[]",
                         **{name: "JSON" for name in DISPLAY}})
_FILTERS = json.dumps({name: "VARCHAR[]" for name in FILTERS})
_DATES = json.dumps({name: "VARCHAR" for name in DATES})


def _keywords(expression):
    """Engine's exact_values: sorted distinct keywords, empty when absent."""
    return f"list_sort(list_distinct(coalesce({expression}, []::VARCHAR[])))"


def typed_sql(values, source_id=SOURCE_ID):
    """Engine's row() columns from ``values`` (member_key, value JSON, source_occurrence_id), each parsed once."""
    # Engine's display JSON: the six display keys present, in sorted order; each subtree as DuckDB writes it.
    display = ", ".join(f"'\"{name}\":' || p.{name}" for name in sorted(DISPLAY))
    columns = [f"{_id_sql('member_key', source_id)} AS id", f"'{source_id}' AS source_id", "member_key", "source_occurrence_id",
               *(f"coalesce(p.{name}, '') AS {name}" for name in TEXTS), f"'{{' || concat_ws(',', {display}) || '}}' AS metadata",
               f"{_keywords('p.identifiers')} AS identifiers",
               *(f"{_keywords(f'f.{name}')} AS filter_{name}" for name in FILTERS),
               f"{_keywords('list_concat(f.agency, f.parent_agency)')} AS filter_agency_scope",
               *(f"CAST(d.{name} AS {kind}) AS date_{name}" for name, kind in DATES.items())]
    return (f"SELECT {', '.join(columns)} FROM (SELECT member_key, source_occurrence_id, p, "
            f"from_json(p.filters, '{_FILTERS}') AS f, from_json(p.dates, '{_DATES}') AS d "
            f"FROM (SELECT member_key, source_occurrence_id, from_json(v, '{_STRUCTURE}') AS p FROM {values}))")


def _source_occurrences(workspace, state_id, path, extra=()):
    """Write a state's (member_key, occurrence_id) pairs, and any ``extra`` pairs, to ``path`` through DocSpec's reader."""
    from docspec.runtime import CoreWorkspace
    with CoreWorkspace(workspace, create=False) as opened, opened.open_state(state_id) as reader, \
            reader.relation() as relation:
        pairs = relation.project("member_key, occurrence_id").to_arrow_table()
    extra = pa.table({"member_key": [key for key, _ in extra], "occurrence_id": [value for _, value in extra]},
                     schema=pairs.schema)
    pq.write_table(pa.concat_tables([pairs, extra]), path)


def type_sample(name, prepared, workspace, state_id, source_id=SOURCE_ID, extra_sources=()):
    """Type a PM01 JSONL sample natively, joining each member's source occurrence from DocSpec's reader."""
    INPUTS.mkdir(parents=True, exist_ok=True)
    sources, typed = INPUTS / f"{name}.source-occurrences.parquet", INPUTS / f"{name}.typed.parquet"
    _source_occurrences(workspace, state_id, sources, extra_sources)
    with Clock() as clock, duckdb.connect() as connection:
        connection.sql(f"SELECT json->>'$[0]' AS member_key, json->'$[1]' AS v FROM read_json_objects("
                       f"'{prepared}', format='newline_delimited', maximum_object_size=268435456)").create_view("lines")
        connection.sql(f"SELECT l.member_key, l.v, s.occurrence_id AS source_occurrence_id FROM lines l "
                       f"JOIN read_parquet('{sources}') s USING (member_key)").create_view("sample")
        connection.sql(typed_sql("sample", source_id)).write_parquet(str(typed), compression="zstd")
        rows, unmatched = connection.execute(
            f"SELECT (SELECT count(*) FROM read_parquet('{typed}')), (SELECT count(*) FROM lines l ANTI JOIN "
            f"read_parquet('{sources}') s USING (member_key))").fetchone()
    _write(f"type-{name}", {"prepared": str(prepared), "source_state": state_id, "typed": str(typed), "rows": rows,
                            "unmatched_keys": unmatched, "type": clock.receipt(rows)})


def _batches(path, keys=None):
    table = pq.read_table(path)
    if keys is not None:
        table = table.filter(pa.compute.is_in(table.column("member_key"), value_set=pa.array(sorted(keys))))
    return table.num_rows, table.to_batches(max_chunksize=2048)


# ---------------------------------------------------------------- the 10k sample

def workspace_10k():
    return WORKSPACES / "fr-10k"


def derive():
    """Derive the typed layer of PM01's first prepared values over its source state, as one unit."""
    from docspec.domain import core
    from docspec.runtime import CoreWorkspace
    target = workspace_10k()
    if target.exists():
        raise SystemExit("use a fresh gate workspace")
    shutil.copytree(PM01 / "workspaces" / "fr-10k", target)
    count, batches = _batches(INPUTS / "fr-10k-1.typed.parquet")
    before, ledger = footprint(target), ledger_counts(target)
    with CoreWorkspace(target, create=False) as workspace, Clock() as clock:
        derived = workspace.derive_table(batches, schema=_schema(), batch_id=TYPED_BASE, definition=_definition(),
                                         inputs=(core.StateInput(label="source", state_id=SOURCE_STATES[1]),),
                                         dataset="fr-prepared-typed")
    _write("derive-10k", {"state_id": derived.state_id, "report": derived.report, "rows": count,
                          "derive": clock.receipt(count), "bytes": _delta(before, footprint(target)),
                          "ledger": _delta(ledger, ledger_counts(target)), "pm01_json_derive_ms_per_record": 0.81})


def _expected(value, occurrence, source_id):
    """Engine's row() fields for one prepared value, in plain Python over json: the independent reference."""
    filters, dates = value.get("filters", {}), value.get("dates", {})
    row = {"source_id": source_id, "source_occurrence_id": occurrence,
           **{name: value.get(name, "") for name in TEXTS},
           "metadata": {key: value[key] for key in DISPLAY if key in value},
           "identifiers": sorted(set(value.get("identifiers", [])))}
    for name in FILTERS:
        row[f"filter_{name}"] = sorted(set(filters.get(name, [])))
    row["filter_agency_scope"] = sorted(set(row["filter_agency"]) | set(row["filter_parent_agency"]))
    for name, kind in DATES.items():
        text = dates.get(name)
        if text is None:
            row[f"date_{name}"] = None
        elif kind == "DATE":
            row[f"date_{name}"] = date.fromisoformat(text)
        else:
            row[f"date_{name}"] = datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    return row


def _stored(row):
    """A read row in the reference's terms: metadata decoded, timestamps in UTC."""
    row = dict(row)
    row["metadata"] = json.loads(row["metadata"])
    for name, kind in DATES.items():
        if kind == "TIMESTAMPTZ" and row[f"date_{name}"] is not None:
            row[f"date_{name}"] = row[f"date_{name}"].astimezone(timezone.utc)
    return row


def compare(workspace_path, state_id, prepared, sources, name, source_id=SOURCE_ID):
    """Compare every (member key, field) of a derived state with Python's reading of the JSON values.

    ``sources`` holds each member's source occurrence as DocSpec's reader of
    the source state gave it. The occurrence of every row is also recomputed
    by DocSpec's Python reference from the row read back.
    """
    from docspec.adapters.storage.table_occurrences import reference_identity
    from docspec.domain.table_rows import TableIdentity
    from docspec.runtime import CoreWorkspace
    stable_id, engine_commit, _ = _engine_functions()
    identity = TableIdentity.derived(_definition(source_id).definition_id, _schema())
    occurrences = dict(zip(*(column.to_pylist() for column in pq.read_table(sources).columns), strict=True))
    with Clock() as clock, CoreWorkspace(workspace_path, create=False) as workspace, \
            workspace.open_state(state_id) as reader, reader.table() as relation:
        stored = {row["member_key"]: row for row in relation.to_arrow_table().to_pylist()}
        record_count = reader.record_count
    reference = dict(_jsonl(prepared))
    differing, occurrence_differs, metadata_bytes_differ, fields = [], [], [], 0
    for key in sorted(reference.keys() | stored.keys()):
        if key not in stored or key not in reference:
            differing.append({"member_key": key, "field": "(row)", "stored": key in stored})
            continue
        expected = {"id": stable_id(source_id, key), **_expected(reference[key], occurrences.get(key), source_id)}
        row = _stored(stored[key])
        for field, value in expected.items():
            fields += 1
            if row[field] != value:
                differing.append({"member_key": key, "field": field, "stored": row[field], "expected": value})
        typed = {name: stored[key][name] for name, _ in COLUMNS}
        if reference_identity(identity, typed)[2] != stored[key]["occurrence_id"]:
            occurrence_differs.append(key)
        if stored[key]["metadata"] != json.dumps(expected["metadata"], ensure_ascii=False, sort_keys=True,
                                                 separators=(",", ":")):
            metadata_bytes_differ.append(key)
    populated = {field: sum(1 for row in stored.values() if row[field] not in (None, [], ""))
                 for field in (*(f"filter_{name}" for name in FILTERS), *(f"date_{name}" for name in DATES))}
    _write(f"compare-{name}", {
        "state_id": state_id, "prepared": str(prepared), "members": len(reference), "stored": len(stored),
        "record_count": record_count, "fields_compared": fields, "differing": differing[:50],
        "differing_count": len(differing), "occurrence_differs": occurrence_differs,
        "metadata_not_engine_bytes": len(metadata_bytes_differ), "engine_commit": engine_commit,
        "populated_filters_and_dates": populated, "compare": clock.receipt()})
    if differing or occurrence_differs:
        raise SystemExit("typed values differ from the JSON-derived reference")


def revise():
    """Derive PM01's second source revision onto the first layer: 50 rewrites, 1 removal and 5 unchanged controls."""
    from docspec.domain import core
    from docspec.runtime import CoreWorkspace
    keys = json.loads(REVISION_KEYS.read_text())
    first = _read("derive-10k")["state_id"]
    target = workspace_10k()
    with CoreWorkspace(target, create=False) as workspace:
        with workspace.open_state(SOURCE_STATES[2]) as newer, workspace.open_state(SOURCE_STATES[1]) as older:
            source_changes = [(key, occurrence is None) for key, occurrence, _ in newer.changes(older)]
    count, batches = _batches(INPUTS / "fr-10k-2.typed.parquet", keys["rewritten"] + keys["controls"])
    before, ledger = footprint(target), ledger_counts(target)
    with CoreWorkspace(target, create=False) as workspace, Clock() as clock:
        revised = workspace.derive_table(batches, schema=_schema(), batch_id="c29-gate-10k-second",
                                         definition=_definition(), base_state_id=first, removals=tuple(keys["removed"]),
                                         inputs=(core.StateInput(label="source", state_id=SOURCE_STATES[2]),),
                                         dataset="fr-prepared-typed")
    grown, ledger_growth = _delta(before, footprint(target)), _delta(ledger, ledger_counts(target))
    with CoreWorkspace(target, create=False) as workspace:
        with workspace.open_state(revised.state_id) as newer, workspace.open_state(first) as older:
            derived_changes = [(key, occurrence is None) for key, occurrence, _ in newer.changes(older)]
        with workspace.publisher.session() as session:
            tables = {state: workspace.states.layers(session, state)["table"].reference for state in (first, revised.state_id)}
        base_files = set(workspace.records.data_files(tables[first]))
        new_files = [str(workspace.records.root / path) for path in workspace.records.data_files(tables[revised.state_id])
                     if path not in base_files]
        with workspace.records._cursor() as cursor:
            written = sorted(key for (key,) in cursor.read_parquet(new_files).project("member_key").fetchall())
    # The members whose prepared value differs, by Python's json: the threshold's "exactly".
    first_values, second_values = dict(_jsonl(PREPARED[1])), dict(_jsonl(PREPARED[2]))
    value_changed = sorted(key for key in first_values.keys() | second_values.keys()
                           if first_values.get(key) != second_values.get(key))
    _write("revise-10k", {
        "state_id": revised.state_id, "base": first, "report": revised.report, "supplied_rows": count,
        "source_changes": len(source_changes), "source_removed": sum(removed for _, removed in source_changes),
        "derived_changes": len(derived_changes), "derived_removed": sum(removed for _, removed in derived_changes),
        "derived_equals_value_changes": [key for key, _ in derived_changes] == value_changed,
        "value_changed": len(value_changed), "written_rows_in_new_table_files": len(written),
        "written_are_the_rewrites": written == sorted(keys["rewritten"]), "new_table_files": len(new_files),
        "derive": clock.receipt(count), "bytes": grown, "ledger": ledger_growth})


def json_derive():
    """The like-for-like baseline: C26's JSON derive of the same 10,000 values, whole and cut to the typed fields."""
    from docspec.domain import core
    from docspec.runtime import CoreWorkspace
    source = SOURCE_STATES[1]
    whole = list(_jsonl(PREPARED[1]))
    projected = [(key, {name: value[name] for name in (*TEXTS, "identifiers", *DISPLAY) if name in value}) for key, value in whole]
    receipts = {}
    for name, rows in (("whole", whole), ("typed-fields", projected)):
        target = WORKSPACES / f"fr-10k-json-{name}"
        if target.exists():
            raise SystemExit("use a fresh gate workspace")
        shutil.copytree(PM01 / "workspaces" / "fr-10k", target)
        before, ledger = footprint(target), ledger_counts(target)
        with CoreWorkspace(target, create=False) as workspace, Clock() as clock:
            state = workspace.derive(rows, batch_id=f"c29-gate-json-{name}", definition=_definition(), inputs=(
                core.StateInput(label="source", state_id=source),))
        receipts[name] = {"state_id": state.state_id, "rows": len(rows), "derive": clock.receipt(len(rows)),
                          "bytes": _delta(before, footprint(target)), "ledger": _delta(ledger, ledger_counts(target))}
    _write("json-derive-10k", receipts)


SYNTHETIC_KEY = "c29-gate synthetic\x1f#1"


def _synthetic():
    """One prepared value with every Engine filter and date populated, control characters and a zoned time."""
    return {"format": "spicysearch-prepared-metadata-v1", "title": "Synthetic \x1f \"quoted\" \\ é😀",
            "source_url": "https://example.test/synthetic", "primary_text": "line\nbreak", "related_text": "tab\there",
            "identifiers": ["b", "a", "b", "\u2028"],
            "filters": {name: [f"{name} z", f"{name} a", f"{name} a"] for name in FILTERS},
            "dates": {name: "2026-09-25" if kind == "DATE" else "2026-09-25T01:02:03.123456-04:00"
                      for name, kind in DATES.items()},
            "display": {"sections": [{"items": [], "label": "Synthetic \x1f é"}]},
            "facet_labels": {"agency": {"synthetic": "Synthetic agency"}}, "facet_aliases": {}}


def coverage():
    """Regulations.gov's 10,000 prepared values plus one member with every filter and date populated, typed and derived."""
    from docspec.domain import core
    from docspec.runtime import CoreWorkspace
    source = INPUTS / "rg-10k-source"
    if not source.exists():
        shutil.copytree(PM01 / "workspaces" / "rg-10k", source)
    sample = INPUTS / "coverage.jsonl"
    with open(sample, "w", encoding="utf-8") as output:
        output.write((PM01 / "compare" / "rg-10k.prepared-1.jsonl").read_text(encoding="utf-8"))
        output.write(json.dumps([SYNTHETIC_KEY, _synthetic()], ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    type_sample("coverage", sample, source, "source", source_id="regulations-gov",
                extra_sources=((SYNTHETIC_KEY, "urn:c29-gate:synthetic-source"),))
    target = WORKSPACES / "coverage"
    if target.exists():
        raise SystemExit("use a fresh gate workspace")
    count, batches = _batches(INPUTS / "coverage.typed.parquet")
    with CoreWorkspace(target) as workspace:
        workspace.create("sources", [("regulations-gov", {"workspace": str(PM01 / "workspaces" / "rg-10k"), "state": "source"})])
        pin = dict(workspace.rows("sources"))["regulations-gov"].entity_id
        with Clock() as clock:
            derived = workspace.derive_table(batches, schema=_schema(), batch_id="c29-gate-coverage",
                                             definition=_definition("regulations-gov"),
                                             inputs=(core.WholeInput(label="sources", entity_id=pin),))
    _write("derive-coverage", {"state_id": derived.state_id, "report": derived.report, "rows": count,
                               "derive": clock.receipt(count)})


# ---------------------------------------------------------------- full scale

def _cutover():
    """The cutover workspace's state storage, opened read-only: no lock, journal or scratch file lands in it."""
    from docspec.adapters.storage.blobs import LocalContentAddressedBlobStore
    from docspec.adapters.storage.core_states import CoreStateStorage
    from docspec.adapters.storage.ledger import LocalSqliteCoreLedger
    from docspec.adapters.storage.records import IcebergRecordStorage
    from docspec.application.core_publication import CorePublisher
    records = IcebergRecordStorage(CUTOVER / "records", create=False, merge_scratch_root=GATE / "scratch",
                                   engine_memory_bytes=6 * 1024**3)
    ledger = LocalSqliteCoreLedger(CUTOVER / "ledger.sqlite", create=False, read_only=True, record_storage=records)
    states = CoreStateStorage(records)
    publisher = CorePublisher(ledger, LocalContentAddressedBlobStore(CUTOVER / "blobs", create=False), states=states)
    return records, ledger, states, publisher


def scale_type():
    """Type every prepared value of the cutover FR state, read through DocSpec's key-ordered reader, with its source occurrence.

    The values stream in bounded windows, in member-key order, into a second
    DuckDB connection that types them. The catalogue's occurrences are written
    in the same order, so one positional join, checked key by key, adds each
    row's source occurrence without a hash table over the wide rows.
    """
    from docspec.runtime.state_reader import CoreStateReader
    scratch = GATE / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    typed, values = INPUTS / "fr-1m.typed.parquet", INPUTS / "fr-1m.values-typed.parquet"
    sources = INPUTS / "fr-1m.source-occurrences.parquet"
    config = {"memory_limit": "3GB", "temp_directory": str(scratch), "threads": "4"}
    records, ledger, states, publisher = _cutover()
    try:
        with Clock() as clock, publisher.session() as session:
            pins = {state_id: CoreStateReader(session, states, state_id, expected_pin=pin).pin
                    for state_id, pin in (CUTOVER_PREPARED, CUTOVER_SOURCE)}
            prepared, source = (states.layers(session, state_id) for state_id, _ in (CUTOVER_PREPARED, CUTOVER_SOURCE))
            with records.relations({"sources": source["membership"]}) as relations:
                relations["sources"].project("record_identity AS member_key, json_extract_string(decode(record_json), "
                                             "'$.occurrence_id') AS occurrence_id").order("member_key").write_parquet(str(sources))
            batches = states.ordered_batches(session, CUTOVER_PREPARED[0], layers=prepared)
            first = next(batches)
            stream = pa.RecordBatchReader.from_batches(first.schema, (batch for part in ([first], batches) for batch in part))
            with duckdb.connect(config=config) as connection:
                connection.register("prepared_rows", stream)
                connection.sql("SELECT member_key, json_extract(decode(occurrence_record), '$.value.value') AS v, "
                               "NULL::VARCHAR AS source_occurrence_id FROM prepared_rows").create_view("sample")
                connection.sql(typed_sql("sample")).write_parquet(str(values), compression="zstd")
            prepared_count = prepared["membership"].reference.record_count
    finally:
        ledger.close()
        records.close()
    with Clock() as join_clock, duckdb.connect(config=config) as connection:
        pairs = f"read_parquet('{values}') t POSITIONAL JOIN read_parquet('{sources}') s"
        misaligned = connection.execute(f"SELECT count(*) FROM {pairs} WHERE t.member_key IS DISTINCT FROM s.member_key").fetchone()[0]
        if misaligned:
            raise SystemExit(f"{misaligned} prepared rows do not align with the catalogue's keys")
        connection.execute(f"COPY (SELECT t.* REPLACE (s.occurrence_id AS source_occurrence_id) FROM {pairs}) "
                           f"TO '{typed}' (FORMAT parquet, COMPRESSION zstd)")
        rows = connection.execute(f"SELECT count(*) FROM read_parquet('{typed}')").fetchone()[0]
    values.unlink()
    _write("scale-type", {"prepared": CUTOVER_PREPARED, "source": CUTOVER_SOURCE, "pins_checked": pins, "rows": rows,
                          "prepared_members": prepared_count, "misaligned_keys": misaligned, "typed": str(typed),
                          "typed_bytes": typed.stat().st_size, "type": clock.receipt(rows), "join": join_clock.receipt(rows)})


def scale_derive():
    """Derive the full FR typed layer into a fresh workspace, recording time, memory, bytes and ledger growth."""
    from docspec.domain import core
    from docspec.runtime import CoreWorkspace
    target = WORKSPACES / "fr-1m"
    if target.exists():
        raise SystemExit("use a fresh gate workspace")
    with CoreWorkspace(target) as workspace:
        workspace.create("sources", [(SOURCE_ID, {"workspace": str(CUTOVER), "prepared": list(CUTOVER_PREPARED),
                                                  "source": list(CUTOVER_SOURCE)})])
        pin = dict(workspace.rows("sources"))[SOURCE_ID].entity_id
    parquet = pq.ParquetFile(INPUTS / "fr-1m.typed.parquet")
    count = parquet.metadata.num_rows
    before, ledger = footprint(target), ledger_counts(target)
    with CoreWorkspace(target, create=False) as workspace, Clock() as clock:
        derived = workspace.derive_table(parquet.iter_batches(batch_size=2048), schema=_schema(), batch_id="c29-gate-1m",
                                         definition=_definition(), inputs=(core.WholeInput(label="sources", entity_id=pin),),
                                         dataset="fr-prepared-typed")
    _write("scale-derive", {"state_id": derived.state_id, "report": derived.report, "rows": count,
                            "derive": clock.receipt(count), "bytes": _delta(before, footprint(target)),
                            "ledger": _delta(ledger, ledger_counts(target)),
                            "cutover_json_derive": {"minutes": 19.4, "peak_gb": 12.5, "ms_per_record": round(19.4 * 60000 / 1007639, 3)}})


def scale_revise():
    """An incremental derive over the full layer: 50 rows rewritten (a marker in the title) and 1 removed."""
    from docspec.domain import core
    from docspec.runtime import CoreWorkspace
    target = WORKSPACES / "fr-1m"
    base = _read("scale-derive")["state_id"]
    with duckdb.connect() as connection:
        ordered = connection.sql(f"SELECT * FROM read_parquet('{INPUTS / 'fr-1m.typed.parquet'}') "
                                 "ORDER BY md5(member_key) LIMIT 51").to_arrow_table()
    removed, rewritten = ordered.column("member_key")[0].as_py(), ordered.slice(1)
    titles = pa.array([title + " C29GATE" for title in rewritten.column("title").to_pylist()])
    rewritten = rewritten.set_column(rewritten.schema.get_field_index("title"), "title", titles)
    before, ledger = footprint(target), ledger_counts(target)
    with CoreWorkspace(target, create=False) as workspace, Clock() as clock:
        pin = dict(workspace.rows("sources"))[SOURCE_ID].entity_id
        revised = workspace.derive_table(rewritten.to_batches(), schema=_schema(), batch_id="c29-gate-1m-second",
                                         definition=_definition(), base_state_id=base, removals=(removed,),
                                         inputs=(core.WholeInput(label="sources", entity_id=pin),), dataset="fr-prepared-typed")
    with CoreWorkspace(target, create=False) as workspace, Clock() as changes_clock, \
            workspace.open_state(revised.state_id) as newer, workspace.open_state(base) as older:
        changes = [(key, occurrence is None) for key, occurrence, _ in newer.changes(older)]
    _write("scale-revise", {"state_id": revised.state_id, "base": base, "report": revised.report,
                            "derive": clock.receipt(), "bytes": _delta(before, footprint(target)),
                            "ledger": _delta(ledger, ledger_counts(target)), "changes": len(changes),
                            "removed": sum(flag for _, flag in changes), "changes_read": changes_clock.receipt()})


def scale_json():
    """The like-for-like baseline at scale: C26's JSON derive of the same 1M values, cut to the typed fields.

    Each value is the typed row's display JSON with its text and identifier
    fields, built from the typed input as a caller holding Python values would
    pass them. Building them is timed alone and reported beside the derive.
    """
    from docspec.domain import core
    from docspec.runtime import CoreWorkspace
    names = ("member_key", "metadata", *TEXTS, "identifiers")

    def rows():
        for batch in pq.ParquetFile(INPUTS / "fr-1m.typed.parquet").iter_batches(batch_size=2048, columns=list(names)):
            columns = [batch.column(name).to_pylist() for name in names]
            for key, metadata, *fields in zip(*columns, strict=True):
                yield key, {**json.loads(metadata), **dict(zip(names[2:], fields, strict=True))}
    with Clock() as decode_clock:
        count = sum(1 for _ in rows())
    target = WORKSPACES / "fr-1m-json"
    if target.exists():
        raise SystemExit("use a fresh gate workspace")
    with CoreWorkspace(target) as workspace:
        workspace.create("sources", [(SOURCE_ID, {"workspace": str(CUTOVER), "prepared": list(CUTOVER_PREPARED),
                                                  "source": list(CUTOVER_SOURCE)})])
        pin = dict(workspace.rows("sources"))[SOURCE_ID].entity_id
    before, ledger = footprint(target), ledger_counts(target)
    with CoreWorkspace(target, create=False) as workspace, Clock() as clock:
        state = workspace.derive(rows(), batch_id="c29-gate-1m-json", definition=_definition(),
                                 inputs=(core.WholeInput(label="sources", entity_id=pin),))
    _write("scale-json", {"state_id": state.state_id, "rows": count, "values_only": decode_clock.receipt(count),
                          "derive_including_values": clock.receipt(count),
                          "derive_ms_per_record_without_values": round(1000 * (clock.wall - decode_clock.wall) / count, 4),
                          "bytes": _delta(before, footprint(target)), "ledger": _delta(ledger, ledger_counts(target))})


def scale_check():
    """In a fresh process: the derived table equals the typed input natively, both directions, and counts agree."""
    from docspec.runtime import CoreWorkspace
    state_id = _read("scale-derive")["state_id"]
    names = ", ".join(f'"{name}"' for name, _ in COLUMNS)
    typed = INPUTS / "fr-1m.typed.parquet"
    with Clock() as clock, CoreWorkspace(WORKSPACES / "fr-1m", create=False) as workspace, \
            workspace.open_state(state_id) as reader, reader.table() as relation:
        missing, extra, rows, distinct = relation.query("derived", (
            f"SELECT (SELECT count(*) FROM (SELECT {names} FROM read_parquet('{typed}') EXCEPT ALL SELECT {names} FROM derived)), "
            f"(SELECT count(*) FROM (SELECT {names} FROM derived EXCEPT ALL SELECT {names} FROM read_parquet('{typed}'))), "
            "(SELECT count(*) FROM derived), (SELECT count(DISTINCT occurrence_id) FROM derived)")).fetchone()
    _write("scale-check", {"state_id": state_id, "rows": rows, "distinct_occurrences": distinct,
                           "typed_rows_missing_from_layer": missing, "layer_rows_not_typed": extra,
                           "check": clock.receipt()})
    if missing or extra:
        raise SystemExit("the derived table differs from its typed input")


def scale_layers():
    """Bytes, files and rows of each layer of the full-scale states: typed, revised and the JSON baseline."""
    from docspec.runtime import CoreWorkspace
    out = {}
    for name, receipt, workspace_name in (("typed", "scale-derive", "fr-1m"), ("typed-revised", "scale-revise", "fr-1m"),
                                          ("json", "scale-json", "fr-1m-json")):
        with CoreWorkspace(WORKSPACES / workspace_name, create=False) as workspace, workspace.publisher.session() as session:
            layers = workspace.states.layers(session, _read(receipt)["state_id"])
            out[name] = {layer_name: {"files": len(files), "rows": layer.reference.record_count,
                                      "bytes": sum((workspace.records.root / path).stat().st_size for path in files)}
                         for layer_name, layer in layers.items()
                         for files in [workspace.records.data_files(layer.reference)]}
    _write("scale-layers", out)


def scale_lookups():
    """Point reads on the full-scale typed state: 50 single keys, then one bounded group of 256 keys."""
    import random
    from docspec.runtime import CoreWorkspace
    keys = pq.read_table(INPUTS / "fr-1m.typed.parquet", columns=["member_key"]).column(0).to_pylist()
    random.seed(29)
    single, group = random.sample(keys, 50), random.sample(keys, 256)
    with CoreWorkspace(WORKSPACES / "fr-1m", create=False) as workspace, \
            workspace.open_state(_read("scale-derive")["state_id"]) as reader:
        reader.read_value(single[0])
        with Clock() as reads:
            for key in single:
                reader.read_value(key)
        with Clock() as grouped:
            count = sum(1 for _ in reader.values(member_keys=group))
    _write("scale-lookups", {"read_value_ms": round(1000 * reads.wall / len(single), 1), "keys": len(single),
                             "values_256_ms": round(1000 * grouped.wall, 1), "values_256_rows": count})


FORK = Path("/Users/mikewolfd/Work/corpora/fork-fr-generation-2026-09-23")
TITLES = (("member_key", "VARCHAR"), ("source_occurrence_id", "VARCHAR"), ("title", "VARCHAR"),
          ("document_type", "VARCHAR"), ("publication_date", "VARCHAR"))


def _titles_definition():
    from docspec.domain import core
    from docspec.domain.identity import stable_urn
    configuration = {"columns": [name for name, _ in TITLES], "source": "the admitted federal_register table"}
    return core.OperationDefinition(format_version=1, definition_id=stable_urn("c29-gate-fr-titles", configuration),
                                    implementation_id="c29-gate.fr-titles", implementation_version="1",
                                    operation_kind="transformation", configuration=configuration)


def _affected_before(workspace, derived, older, newer):
    """affected() as 5988213 ran it, for the before timing: a full anti-join of both memberships, then the
    layer's whole typed relation, then the semi-join."""
    from docspec.adapters.storage.core_tables import typed_relation
    records = workspace.records
    with workspace.publisher.session() as session:
        layers = workspace.states.layers(session, derived)
        old, new = (workspace.states.layers(session, state)["membership"] for state in (older, newer))
        with records._cursor() as cursor, records.relations({"old": old, "new": new, "table": layers["table"]},
                                                            cursor=cursor) as inputs, \
                typed_relation(records, layers, layers.identity, cursor=cursor) as rows:
            changed = inputs["old"].project("record_json AS old_member").join(
                inputs["new"].project("record_json AS new_member"), "old_member = new_member", how="anti").project(
                "json_extract_string(decode(old_member), '/occurrence_id') AS changed_occurrence")
            found = inputs["table"].project("member_key AS hit_key, source_occurrence_id AS reference").join(
                changed, "reference = changed_occurrence", how="semi").project("hit_key").distinct()
            return rows.join(found, "member_key = hit_key", how="semi").project("member_key, source_occurrence_id").fetchall()


def _point_reads(workspace, state_id, count=50):
    """Mean read_value milliseconds over ``count`` random keys of a state, after one warm read."""
    import random
    with workspace.open_state(state_id) as reader:
        with reader.relation() as relation:
            keys = [key for (key,) in relation.project("member_key").fetchall()]
        random.seed(29)
        sample = random.sample(keys, count)
        reader.read_value(sample[0])
        with Clock() as clock:
            for key in sample:
                reader.read_value(key)
    return {"keys": count, "read_value_ms": round(1000 * clock.wall / count, 1)}


def scale_affected(name="affected"):
    """A typed layer over an admitted FR generation, the next generation admitted, then the rows its changes affect.

    The inputs are table-shaped states, as a producer's generations will be.
    The fork-host generations differ in 102 added and 2 changed rows. The
    affected rows must be exactly the derived rows of the members the
    source's ``changes`` report changed or removed; they are re-derived with
    the added members in one incremental derive.
    """
    from docspec.domain import core
    from docspec.domain.storage import TableSchema
    from docspec.runtime import CoreWorkspace
    target = WORKSPACES / f"fr-1m-{name}"
    if target.exists():
        raise SystemExit("use a fresh gate workspace")
    schema, receipt = TableSchema("c29-gate-fr-titles:1", TITLES), {}
    columns = "member_key, occurrence_id AS source_occurrence_id, title, document_type, publication_date"
    with CoreWorkspace(target) as workspace:
        with Clock() as clock:
            prior = workspace.admit_generation(FORK / "prior", family="federal-register", table="federal_register", dataset="fr")
        receipt["admit_prior"] = clock.receipt()
        with workspace.open_state(prior.state_id) as reader, reader.table() as relation:
            rows = relation.project(columns).to_arrow_table()
        with Clock() as clock:
            derived = workspace.derive_table(rows.to_batches(max_chunksize=2048), schema=schema, batch_id="titles-prior",
                                             definition=_titles_definition(), dataset="fr-titles",
                                             inputs=(core.StateInput(label="source", state_id=prior.state_id),))
        receipt["derive"] = {"rows": rows.num_rows, **clock.receipt(rows.num_rows)}
        del rows
        with Clock() as clock:
            current = workspace.admit_generation(FORK / "current", family="federal-register", table="federal_register", dataset="fr")
        receipt["admit_current"] = {"counts": current.report["counts"], **clock.receipt()}
        with workspace.open_state(prior.state_id) as older, workspace.open_state(current.state_id) as newer:
            with Clock() as clock:
                source_changes = [(key, occurrence) for key, occurrence, _ in newer.changes(older)]
            receipt["source_changes"] = {"count": len(source_changes), **clock.receipt()}
            with workspace.open_state(prior.state_id) as older_again:
                with Clock() as clock:
                    earlier = {key: occurrence for key, occurrence, _ in older_again.changes(newer)}
            with workspace.open_state(derived.state_id) as layer, Clock() as clock, layer.affected(older, newer) as affected:
                found = affected.project("member_key, source_occurrence_id").fetchall()
            receipt["affected"] = {"rows": len(found), **clock.receipt()}
        with Clock() as clock:
            before = _affected_before(workspace, derived.state_id, prior.state_id, current.state_id)
        receipt["affected_before"] = {"rows": len(before), "same_rows": sorted(before) == sorted(found), **clock.receipt()}
        expected = sorted((key, occurrence) for key, occurrence in earlier.items() if occurrence is not None)
        receipt["affected"]["equals_the_source_changes"] = sorted(found) == expected
        receipt["affected"]["expected"] = len(expected)
        receipt["admitted_point_reads"] = _point_reads(workspace, prior.state_id)
        # Re-derive the affected rows and the added members over the new generation.
        wanted = sorted(key for key, _ in source_changes)
        with workspace.open_state(current.state_id) as reader, reader.table() as relation:
            refreshed = relation.project(columns).filter(
                "member_key IN (" + ", ".join("'" + key.replace("'", "''") + "'" for key in wanted) + ")").to_arrow_table()
        removed = tuple(key for key, occurrence in source_changes if occurrence is None)
        with Clock() as clock:
            revised = workspace.derive_table(refreshed.to_batches(), schema=schema, batch_id="titles-current",
                                             definition=_titles_definition(), base_state_id=derived.state_id,
                                             removals=removed, dataset="fr-titles",
                                             inputs=(core.StateInput(label="source", state_id=current.state_id),))
        receipt["revise"] = {"counts": revised.report["counts"], **clock.receipt()}
    _write(f"scale-{name}", receipt)


def scale_affected_wide():
    """affected() on a wide layer over the same admitted generations: every producer column, as a prepared layer is wide."""
    from docspec.domain import core
    from docspec.domain.storage import TableSchema
    from docspec.runtime import CoreWorkspace
    receipt, base = {}, _read("scale-affected")
    target = WORKSPACES / "fr-1m-affected"
    with CoreWorkspace(target, create=False) as workspace:
        states = [row.value.state_id for batch in workspace.ledger.retained_records(kind="state") for row in batch]
        admitted = sorted(state for state in states if state.startswith("urn:docspec:generation-admission"))
        with workspace.open_state(admitted[0]) as first, workspace.open_state(admitted[1]) as second:
            prior, current = (admitted[0], admitted[1]) if first.record_count < second.record_count else (admitted[1], admitted[0])
        with workspace.open_state(prior) as reader, reader.table() as relation:
            producer = [name for name in relation.columns if name not in ("member_key", "occurrence_id")]
            rows = relation.project("member_key, occurrence_id AS source_occurrence_id, "
                                    + ", ".join(f'"{name}"' for name in producer)).to_arrow_table()
        schema = TableSchema("c29-gate-fr-wide:1", (("member_key", "VARCHAR"), ("source_occurrence_id", "VARCHAR"),
                                                     *((name, "VARCHAR") for name in producer)))
        with Clock() as clock:
            wide = workspace.derive_table(rows.to_batches(max_chunksize=2048), schema=schema, batch_id="wide-prior",
                                          definition=_titles_definition(), inputs=(core.StateInput(label="source", state_id=prior),))
        receipt["derive"] = {"rows": rows.num_rows, "columns": len(schema.columns), **clock.receipt(rows.num_rows)}
        del rows
        with workspace.open_state(prior) as older, workspace.open_state(current) as newer, \
                workspace.open_state(wide.state_id) as layer, Clock() as clock, layer.affected(older, newer) as affected:
            found = affected.project("member_key, source_occurrence_id").fetchall()
        receipt["affected"] = {"rows": len(found), **clock.receipt()}
        with Clock() as clock:
            before = _affected_before(workspace, wide.state_id, prior, current)
        receipt["affected_before"] = {"rows": len(before), "same_rows": sorted(before) == sorted(found), **clock.receipt()}
    receipt["narrow_affected_rows"] = base["affected"]["rows"]
    _write("scale-affected-wide", receipt)


def scale_affected_certified():
    """affected() over an input whose history is certified: the 1M JSON state revised by a C26 derive of 50 members.

    A typed layer takes its lineage from the JSON state's membership. The
    revision records its edited keys, so the diff reads only those; the
    earlier formulation compares both 1M memberships in full.
    """
    from docspec.adapters.storage.core_entities import MEMBERSHIP_ADDRESSES
    from docspec.domain import core
    from docspec.runtime import CoreWorkspace
    receipt, target = {}, WORKSPACES / "fr-1m-json"
    base = _read("scale-json")["state_id"]
    with CoreWorkspace(target, create=False) as workspace:
        with workspace.publisher.session() as session, \
                workspace.records.relations({"members": workspace.states.layers(session, base)["membership"]}) as relations:
            lineage = relations["members"].project(MEMBERSHIP_ADDRESSES).order("member_key").to_arrow_table()
        # The typed rows are in member-key order (DocSpec's key-ordered reader wrote them), as is the lineage:
        # one positional join, checked key by key, streams without a hash table over the wide rows.
        relinked = INPUTS / "fr-1m.json-lineage.parquet"
        pairs = f"read_parquet('{INPUTS / 'fr-1m.typed.parquet'}') t POSITIONAL JOIN lineage l"
        with duckdb.connect(config={"memory_limit": "3GB", "temp_directory": str(GATE / "scratch")}) as connection:
            connection.register("lineage", lineage)
            if connection.execute(f"SELECT count(*) FROM {pairs} WHERE t.member_key IS DISTINCT FROM l.member_key").fetchone()[0]:
                raise SystemExit("the JSON state's keys differ from the typed rows'")
            connection.execute(f"COPY (SELECT t.* REPLACE (l.occurrence_id AS source_occurrence_id) FROM {pairs}) "
                               f"TO '{relinked}' (FORMAT parquet)")
            rows = connection.execute(f"SELECT count(*) FROM read_parquet('{relinked}')").fetchone()[0]
            rewritten = [key for (key,) in connection.execute(
                f"SELECT member_key FROM read_parquet('{relinked}') ORDER BY md5(member_key) LIMIT 50").fetchall()]
        with Clock() as clock:
            layer = workspace.derive_table(pq.ParquetFile(relinked).iter_batches(batch_size=2048), schema=_schema(),
                                           batch_id="over-json", definition=_definition(),
                                           inputs=(core.StateInput(label="source", state_id=base),))
        receipt["derive"] = {"rows": rows, **clock.receipt(rows)}
        rewritten, lineage = sorted(rewritten), None
        relinked.unlink()
        with workspace.open_state(base) as reader:
            values = {key: {**value, "title": value["title"] + " C29GATE"} for key, _, value in reader.values(member_keys=rewritten)}
        with Clock() as clock:
            revised = workspace.derive(list(values.items()), batch_id="c29-gate-1m-json-revision",
                                       definition=_definition(), inputs=(), base_state_id=base)
        receipt["revise_source"] = {"rows": len(values), **clock.receipt()}
        with workspace.open_state(base) as older, workspace.open_state(revised.state_id) as newer, \
                workspace.open_state(layer.state_id) as derived, Clock() as clock, derived.affected(older, newer) as affected:
            found = sorted(key for (key,) in affected.project("member_key").fetchall())
        receipt["affected"] = {"rows": len(found), "equals_the_rewrites": found == rewritten, **clock.receipt()}
        with Clock() as clock:
            before = _affected_before(workspace, layer.state_id, base, revised.state_id)
        receipt["affected_before"] = {"rows": len(before), "same_rows": sorted(key for key, _ in before) == found,
                                      **clock.receipt()}
    _write("scale-affected-certified", receipt)


def main():
    command, *arguments = sys.argv[1:]
    if command == "engine-id":
        engine_id()
    elif command == "type":
        revision = int(arguments[0])
        type_sample(f"fr-10k-{revision}", PREPARED[revision], INPUTS / "fr-10k-source", SOURCE_STATES[revision])
    elif command == "compare" and arguments[0] == "coverage":
        compare(WORKSPACES / "coverage", _read("derive-coverage")["state_id"], INPUTS / "coverage.jsonl",
                INPUTS / "coverage.source-occurrences.parquet", "coverage", "regulations-gov")
    elif command == "compare":
        revision = int(arguments[0])
        compare(workspace_10k(), _read("derive-10k" if revision == 1 else "revise-10k")["state_id"], PREPARED[revision],
                INPUTS / f"fr-10k-{revision}.source-occurrences.parquet", f"10k-{revision}")
    elif command == "scale-affected":
        scale_affected(*arguments)
    elif command == "scale-affected-wide":
        scale_affected_wide()
    elif command == "scale-affected-certified":
        scale_affected_certified()
    elif command in {"derive", "revise", "coverage", "json-derive", "scale-type", "scale-derive", "scale-revise",
                     "scale-check", "scale-json", "scale-layers", "scale-lookups"}:
        globals()[command.replace("-", "_")]()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
