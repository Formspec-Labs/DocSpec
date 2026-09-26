"""C27 on Regulations.gov: admit the live dockets and documents families over HTTPS and check them.

Each subcommand runs in its own process, under the PM01 watch wrapper, from
~/Work/corpora/c27-gate-20260925/regulations-gov:

  reference            project the retained 2026-09-14 catalogue through DocSpec's JSON decoder and
                       spicy-docs' public-table profiles, one Parquet file per table
  pointer NAME         read the live publication.json and record both families' pins
  admit NAME TABLE     admit one family over HTTPS into its own dataset; record time, memory and bytes
  compare              in a fresh process, compare the admitted states with the reference and the Python oracle
  retry                re-admit the recorded pins; the states, record files and ledger must not change

Receipts are JSON files under ``receipts/``; nothing here writes outside the gate directory.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import resource
import sqlite3
import subprocess
import sys
import time
from urllib.request import Request, urlopen

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

BASE = "https://pub-72e95c0c20a84508b42b03a6ff6d55f8.r2.dev"
GATE = Path("/Users/mikewolfd/Work/corpora/c27-gate-20260925/regulations-gov")
WORKSPACE, RECEIPTS, REFERENCE, COMPARE = GATE / "workspace", GATE / "receipts", GATE / "reference", GATE / "compare"
CATALOG = Path("/Users/mikewolfd/Work/corpora/docspec-iceberg-reimport-2173b92-20260914/regulations-gov/workspace")
CATALOG_PIN = "sha256:2200b5e68601decb8ac664d824d025c4e2031663c10bae900dcfcd20cbd74643"
TABLES = {"documents": "document_id", "dockets": "docket_id"}
# The retained catalogue was reimported on 2026-09-14 from the 2026-09-02 supply (its receipt's source).
SNAPSHOT = "2026-09-02"
# Columns spicy-regs fills from its own PDF extraction, never from the API record the catalogue holds.
ENRICHED = ("text_content", "text_extraction_status")
JSON_COLUMNS = ("attachments_json", "additional_rins")


def _get(url, timeout=600):
    """Open a published object; the bucket refuses urllib's default User-Agent."""
    return urlopen(Request(url, headers={"User-Agent": "DocSpec C27 regulations gate"}), timeout=timeout)


def _write(name, value):
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    (RECEIPTS / f"{name}.json").write_text(json.dumps(value, indent=1, sort_keys=True, default=str) + "\n")
    print(json.dumps(value, sort_keys=True, default=str)[:3000])


def _read(name):
    return json.loads((RECEIPTS / f"{name}.json").read_text())


def _peak_rss_bytes():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # bytes on macOS


def _environment():
    commit = subprocess.run(["git", "-C", str(Path(__file__).resolve().parents[3]), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    import importlib.metadata as metadata
    return {"commit": commit, "python": platform.python_version(), "duckdb": duckdb.__version__, "pyarrow": pa.__version__,
            "spicy_docs": metadata.version("spicy-docs"), "load": os.getloadavg(), "at": datetime.now().isoformat(timespec="seconds")}


def _profiles():
    from spicy_docs.public_tables.profiles import REGULATIONS_GOV_DOCKET_PUBLIC_TABLE, REGULATIONS_GOV_DOCUMENT_PUBLIC_TABLE
    return {"documents": REGULATIONS_GOV_DOCUMENT_PUBLIC_TABLE, "dockets": REGULATIONS_GOV_DOCKET_PUBLIC_TABLE}


# ---------------------------------------------------------------- reference

def reference():
    """Each catalogue item projected by its own raw fact: a document item by its document, a docket item by its docket."""
    from docspec.domain.core_admission import stored_record
    from docspec.runtime import CoreWorkspace

    started, profiles = time.perf_counter(), _profiles()
    by_schema = {profile.source_schema_name: name for name, profile in profiles.items()}
    REFERENCE.mkdir(parents=True, exist_ok=True)
    schemas = {name: pa.schema([("member_key", pa.string()), *((column, pa.string()) for column in profile.columns)])
               for name, profile in profiles.items()}
    counts, refused = {name: 0 for name in profiles}, []
    with CoreWorkspace(CATALOG, create=False) as workspace:
        with workspace.open_state("catalogue", expected_pin=CATALOG_PIN) as reader:
            members = reader.record_count
        with workspace.publisher.session() as session:
            layers = workspace.states.layers(session, "catalogue")
            with workspace.records.relations({"members": layers["membership"]}) as relations:
                keys = dict(relations["members"].project(
                    "json_extract_string(decode(record_json), '/occurrence_id'), record_identity").fetchall())
            writers = {name: pq.ParquetWriter(REFERENCE / f"{name}.parquet", schema) for name, schema in schemas.items()}
            try:
                with workspace.records.relations({"entities": layers["entities"]}) as relations:
                    for batch in relations["entities"].project("record_identity, record_json").to_arrow_reader(1024):
                        rows = {name: [] for name in profiles}
                        for identity, payload in zip(batch.column(0).to_pylist(), batch.column(1).to_pylist(), strict=True):
                            item = stored_record(payload).value.value
                            key = keys[identity]
                            # An item's own record is the fact whose projected key is the item's.
                            for fact in item["sourceNativeFacts"]:
                                name = by_schema.get(fact["schemaName"])
                                if name is None:
                                    continue
                                source = {"schemaName": fact["schemaName"], "record": fact["fields"], "sourceRecordId": key}
                                try:
                                    projected = profiles[name].project(source)
                                except ValueError:
                                    continue  # a joined parent's fact names another key
                                rows[name].append({"member_key": key, **projected})
                                counts[name] += 1
                                break
                            else:
                                refused.append(key)
                        for name, values in rows.items():
                            if values:
                                writers[name].write_table(pa.Table.from_pylist(values, schema=schemas[name]))
            finally:
                for writer in writers.values():
                    writer.close()
    _write("reference", {"state": "catalogue", "pin": CATALOG_PIN, "members": members, "projected": counts,
                         "unprojected": len(refused), "unprojected_sample": refused[:20],
                         "seconds": time.perf_counter() - started, "peak_rss_bytes": _peak_rss_bytes(),
                         "environment": _environment()})


# ---------------------------------------------------------------- pointer and admission

def pointer(name):
    with _get(BASE + "/publication.json") as response:
        payload = response.read()
    value = json.loads(payload)
    families = {family: {key: value["families"][family][key] for key in ("logicalId", "artifactDigest", "prefix")}
                | {"tables": value["families"][family]["tables"]} for family in TABLES}
    _write("pointer-" + name, {"read_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               "publication_json_bytes": len(payload), "families": families})


def _files(root):
    return {str(path.relative_to(root)): path.stat().st_size for path in root.rglob("*") if path.is_file()}


def _ledger(path):
    if not path.exists():
        return {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("records", "retention", "units", "links", "heads")}


def _state_files(workspace, state_id):
    """Each layer's table directory and data files, relative to the record store."""
    from docspec.domain.references import LayerRef
    with workspace.publisher.session() as session:
        manifest = workspace.states.manifest(session, state_id)
        result = {}
        for name in ("table", "membership", "occurrences"):
            layer = workspace.records.admitted(LayerRef.from_dict(manifest[name]))
            result[name] = (Path(layer.table.metadata_location).parent.parent.relative_to(WORKSPACE / "records").as_posix(),
                            workspace.records.data_files(layer.reference))
    return result


def admit(name, table):
    from docspec.runtime import CoreWorkspace

    pointed = _read("pointer-" + name)["families"][table]
    before = {"records": _files(WORKSPACE / "records") if WORKSPACE.exists() else {}, "ledger": _ledger(WORKSPACE / "ledger.sqlite"),
              "ledger_bytes": (WORKSPACE / "ledger.sqlite").stat().st_size if WORKSPACE.exists() else 0}
    started = time.perf_counter()
    with CoreWorkspace(WORKSPACE, create=not WORKSPACE.exists()) as workspace:
        admitted = workspace.admit_generation(BASE, family=table, table=table, dataset="regulations-gov-" + table)
        seconds = time.perf_counter() - started
        layers = _state_files(workspace, admitted.state_id)
    after = _files(WORKSPACE / "records")
    new = {path: size for path, size in after.items() if path not in before["records"]}
    written = {layer: {"all_bytes": sum(size for path, size in new.items() if path.startswith(directory + "/")),
                       "data_bytes": sum(size for path, size in new.items() if path in files), "data_files_total": len(files)}
               for layer, (directory, files) in layers.items()}
    member = admitted.report["member"]
    # Staging checks every downloaded object's size against its descriptor, so these are the bytes fetched.
    root = _get(f"{BASE}/{pointed['prefix']}/artifact.json").read()
    manifest = _get(f"{BASE}/{pointed['prefix']}/members.json").read()
    downloaded = _read("pointer-" + name)["publication_json_bytes"] + len(root) + len(manifest) + member["byteSize"]
    _write(f"admit-{name}-{table}", {
        "name": name, "table": table, "state_id": admitted.state_id, "report": admitted.report, "pointer": pointed,
        "seconds": seconds, "peak_rss_bytes": _peak_rss_bytes(), "downloaded_bytes": downloaded, "written": written,
        "layer_roots_bytes": sum(size for path, size in new.items() if path.startswith("record-layers/")),
        "records_bytes_new": sum(new.values()), "ledger_bytes": (WORKSPACE / "ledger.sqlite").stat().st_size,
        "ledger_bytes_new": (WORKSPACE / "ledger.sqlite").stat().st_size - before["ledger_bytes"],
        "ledger": _ledger(WORKSPACE / "ledger.sqlite"), "ledger_before": before["ledger"], "environment": _environment()})


def retry(name):
    """Re-admit each recorded pin: same states and reports, and not one record file or ledger row more."""
    from docspec.runtime import CoreWorkspace

    before = {"records": _files(WORKSPACE / "records"), "ledger": _ledger(WORKSPACE / "ledger.sqlite"),
              "ledger_bytes": (WORKSPACE / "ledger.sqlite").stat().st_size}
    result = {}
    with CoreWorkspace(WORKSPACE, create=False) as workspace:
        for table in TABLES:
            first = _read(f"admit-{name}-{table}")
            started = time.perf_counter()
            again = workspace.admit_generation(BASE, family=table, table=table, dataset="regulations-gov-" + table)
            result[table] = {"seconds": time.perf_counter() - started, "same_state": again.state_id == first["state_id"],
                             "same_report": again.report == first["report"],
                             "current": list(workspace.ledger.current("regulations-gov-" + table))}
    _write("retry-" + name, {"tables": result, "records_unchanged": _files(WORKSPACE / "records") == before["records"],
                             "ledger_unchanged": _ledger(WORKSPACE / "ledger.sqlite") == before["ledger"],
                             "ledger_bytes_unchanged": (WORKSPACE / "ledger.sqlite").stat().st_size == before["ledger_bytes"],
                             "peak_rss_bytes": _peak_rss_bytes(), "environment": _environment()})


# ---------------------------------------------------------------- comparison, in a fresh process

def _stringified(value):
    if isinstance(value, dict):
        return {key: _stringified(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stringified(item) for item in value]
    return None if value is None else str(value)


def _canonical(text):
    return None if text is None else json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _rules(table, schema):
    """The identity rules built from the member's pyarrow schema and the table contract, not read from the admission."""
    from docspec.domain.table_rows import KeySpelling, TableIdentity
    kinds = {pa.string(): "VARCHAR"}
    return TableIdentity(table, table, KeySpelling("value", "1", (TABLES[table],)),
                         tuple((field.name, kinds[field.type]) for field in schema))


def _oracle(workspace, state, source, identity):
    """Python reference key and occurrence for every member row against the admitted membership."""
    from docspec.adapters.storage.table_occurrences import reference_identity
    with workspace.publisher.session() as session:
        membership = workspace.states.layers(session, state)["membership"]
        with workspace.records.relations({"members": membership}) as relations:
            native = dict(relations["members"].project(
                "record_identity, json_extract_string(decode(record_json), '/occurrence_id')").fetchall())
    mismatches, rows = [], 0
    for batch in pq.ParquetFile(source).iter_batches(batch_size=8192):
        for row in batch.to_pylist():
            rows += 1
            key, _, urn = reference_identity(identity, row)
            if native.get(key) != urn:
                mismatches.append(key)
    return {"rows": rows, "native_rows": len(native), "mismatches": len(mismatches), "sample": mismatches[:20]}


def _against_catalogue(connection, table, exported, shared):
    """Keys only one side holds, and a two-way EXCEPT over the shared columns on the keys both hold."""
    key = TABLES[table]
    connection.execute(f"CREATE OR REPLACE VIEW g AS SELECT * FROM read_parquet('{exported}')")
    connection.execute(f"CREATE OR REPLACE VIEW r AS SELECT * FROM read_parquet('{REFERENCE / (table + '.parquet')}')")
    generation_only = [row[0] for row in connection.execute(
        f"SELECT g.{key} FROM g ANTI JOIN r ON g.{key} = r.member_key ORDER BY 1").fetchall()]
    catalogue_only = [row[0] for row in connection.execute(
        f"SELECT r.member_key FROM r ANTI JOIN g ON g.{key} = r.member_key ORDER BY 1").fetchall()]
    both = connection.execute(f"SELECT count(*) FROM g JOIN r ON g.{key} = r.member_key").fetchone()[0]
    columns = ", ".join(shared)
    scoped = f"SELECT {columns} FROM g WHERE {key} IN (SELECT member_key FROM r)"
    reference = f"SELECT {columns} FROM r WHERE member_key IN (SELECT {key} FROM g)"
    connection.execute(f"CREATE OR REPLACE TEMP TABLE exceptions AS SELECT {key} FROM ({scoped} EXCEPT {reference})")
    backwards = connection.execute(f"SELECT count(*) FROM ({reference} EXCEPT {scoped})").fetchone()[0]
    pairs = ", ".join(f"g.{name}, r.{name}" for name in shared)
    by_column, kinds, listed = {}, {"type-only": 0, "json-order": 0, "value": 0}, {}
    for row_key, *values in connection.execute(f"SELECT e.{key}, {pairs} FROM exceptions e JOIN g USING ({key}) "
                                               f"JOIN r ON r.member_key = e.{key} ORDER BY 1").fetchall():
        differing = {name: values[2 * index:2 * index + 2] for index, name in enumerate(shared)
                     if values[2 * index] != values[2 * index + 1]}
        for name, (left, right) in differing.items():
            by_column[name] = by_column.get(name, 0) + 1
            if name in JSON_COLUMNS and left is not None and right is not None and _canonical(left) == _canonical(right):
                kind = "json-order"
            elif name in JSON_COLUMNS and left is not None and right is not None \
                    and _stringified(json.loads(left)) == _stringified(json.loads(right)):
                kind = "type-only"
            else:
                kind = "value"
            kinds[kind] += 1
            if kind == "value" and len(listed) < 200:
                listed.setdefault(row_key, {})[name] = [left, right]
    # Adjudication by key: a row the publisher modified after the snapshot carries a later modify_date.
    later, earlier_or_equal = connection.execute(
        f"SELECT count(*) FILTER (WHERE g.modify_date > r.modify_date), count(*) FILTER (WHERE NOT g.modify_date > r.modify_date "
        f"OR g.modify_date IS NULL OR r.modify_date IS NULL) FROM exceptions e JOIN g USING ({key}) "
        f"JOIN r ON r.member_key = e.{key}").fetchone()
    unexplained = [row[0] for row in connection.execute(
        f"SELECT e.{key} FROM exceptions e JOIN g USING ({key}) JOIN r ON r.member_key = e.{key} "
        f"WHERE NOT g.modify_date > r.modify_date OR g.modify_date IS NULL OR r.modify_date IS NULL ORDER BY 1").fetchall()]
    dated = "posted_date" if "posted_date" in shared else "modify_date"
    fresh = connection.execute(
        f"SELECT min(g.{dated}), max(g.{dated}), count(*) FILTER (WHERE g.{dated} >= '{SNAPSHOT}'), count(*) "
        f"FROM g ANTI JOIN r ON g.{key} = r.member_key").fetchone()
    return {"generation_rows": connection.execute("SELECT count(*) FROM g").fetchone()[0],
            "exceptions_with_later_modify_date": later, "exceptions_otherwise": earlier_or_equal,
            "exceptions_otherwise_listed": {row_key: listed.get(row_key) for row_key in unexplained[:200]},
            "generation_only_dated_by": dated, "generation_only_date_range": list(fresh[:2]),
            "generation_only_on_or_after_snapshot": fresh[2],
            "reference_rows": connection.execute("SELECT count(*) FROM r").fetchone()[0], "compared_keys": both,
            "generation_only_keys": len(generation_only), "generation_only_sample": generation_only[:50],
            "catalogue_only_keys": len(catalogue_only), "catalogue_only_listed": catalogue_only[:500],
            "shared_columns": shared, "generation_except_reference": connection.execute("SELECT count(*) FROM exceptions").fetchone()[0],
            "reference_except_generation": backwards, "differences_by_column": by_column, "difference_kinds": kinds,
            "value_differences_listed": listed}


def compare(name):
    from docspec.runtime import CoreWorkspace

    started, profiles = time.perf_counter(), _profiles()
    result = {"environment": _environment(), "tables": {}}
    COMPARE.mkdir(parents=True, exist_ok=True)
    with CoreWorkspace(WORKSPACE, create=False) as workspace:
        for table in TABLES:
            admitted = _read(f"admit-{name}-{table}")
            state = admitted["state_id"]
            exported = COMPARE / f"{table}-{name}.parquet"
            with workspace.open_state(state) as reader:
                count = reader.record_count
                with reader.table() as typed:
                    typed.write_parquet(str(exported))
            # The member as the producer wrote it, fetched again for the oracle.
            source = COMPARE / f"{table}-{name}-member.parquet"
            if not source.exists():
                with _get(f"{BASE}/{admitted['pointer']['prefix']}/{table}.parquet") as response, \
                        source.open("wb") as output:
                    while chunk := response.read(1 << 20):
                        output.write(chunk)
            identity = _rules(table, pq.read_schema(source))
            from docspec.domain.table_rows import TableIdentity
            with workspace.publisher.session() as session:
                rules = TableIdentity.from_dict(workspace.states.manifest(session, state)["rules"])
            columns = [column for column in profiles[table].columns]
            with duckdb.connect() as connection:
                connection.execute("SET memory_limit = '4GB'")
                enriched = {column: connection.execute(f"SELECT count({column}) FROM read_parquet('{exported}')").fetchone()[0]
                            for column in ENRICHED if column in columns}
                versus = _against_catalogue(connection, table, exported, [c for c in columns if c not in ENRICHED])
            result["tables"][table] = {
                "state_id": state, "admitted": count, "recordCount": admitted["report"]["member"]["recordCount"],
                "rules_match": rules == identity, "enriched_non_null": enriched, "catalogue": versus,
                "oracle": _oracle(workspace, state, source, identity)}
    result["seconds"] = time.perf_counter() - started
    result["peak_rss_bytes"] = _peak_rss_bytes()
    _write("compare-" + name, result)


def changes(older, newer, table):
    """The later generation's changes against pyarrow's direct comparison of the two members."""
    import pyarrow.compute as pc
    from docspec.runtime import CoreWorkspace

    first, second = _read(f"admit-{older}-{table}"), _read(f"admit-{newer}-{table}")
    members = []
    for run in (older, newer):
        path = COMPARE / f"{table}-{run}-member.parquet"
        if not path.exists():
            prefix = _read(f"admit-{run}-{table}")["pointer"]["prefix"]
            with _get(f"{BASE}/{prefix}/{table}.parquet") as response, path.open("wb") as output:
                while chunk := response.read(1 << 20):
                    output.write(chunk)
        members.append(pq.read_table(path))
    key = TABLES[table]
    old, new = members
    added = new.filter(pc.invert(pc.is_in(new.column(key), value_set=old.column(key))))
    removed = old.filter(pc.invert(pc.is_in(old.column(key), value_set=new.column(key))))
    old = old.filter(pc.is_in(old.column(key), value_set=new.column(key))).sort_by(key)
    new = new.filter(pc.is_in(new.column(key), value_set=old.column(key))).sort_by(key)
    differs = pa.array([False] * new.num_rows)
    for column in new.column_names:
        left, right = new.column(column), old.column(column)
        equal = pc.or_(pc.fill_null(pc.equal(left, right), False), pc.and_(pc.is_null(left), pc.is_null(right)))
        differs = pc.or_(differs, pc.invert(equal))
    direct = {"added": sorted(added.column(key).to_pylist()), "removed": sorted(removed.column(key).to_pylist()),
              "changed": sorted(new.filter(differs).column(key).to_pylist())}
    docspec = {"added": [], "removed": [], "changed": []}
    with CoreWorkspace(WORKSPACE, create=False) as workspace:
        with workspace.publisher.session() as session, workspace.records.relations(
                {"members": workspace.states.layers(session, first["state_id"])["membership"]}) as relations:
            old_keys = {row[0] for row in relations["members"].project("record_identity").fetchall()}
        with workspace.open_state(first["state_id"]) as reader_old, workspace.open_state(second["state_id"]) as reader_new:
            for member_key, occurrence, _ in reader_new.changes(reader_old):
                docspec["removed" if occurrence is None else "changed" if member_key in old_keys else "added"].append(member_key)
        counts = workspace.compare(first["state_id"], second["state_id"], sample_limit=0)["counts"]
    _write(f"changes-{newer}-{table}", {"direct": {k: len(v) for k, v in direct.items()}, "docspec": {k: len(v) for k, v in docspec.items()},
                                        "compare": counts, "equal": all(sorted(docspec[k]) == direct[k] for k in direct),
                                        "report": second["report"]["counts"], "environment": _environment()})


if __name__ == "__main__":
    command, *arguments = sys.argv[1:]
    {"reference": reference, "pointer": lambda: pointer(*arguments), "admit": lambda: admit(*arguments),
     "retry": lambda: retry(*arguments), "compare": lambda: compare(*arguments),
     "changes": lambda: changes(*arguments)}[command]()
