"""C27 gate: admit fork-host Federal Register generations by reference and check them against independent references.

Each subcommand runs in its own process, under the PM01 watch wrapper, from the
gate directory (the Iceberg REST fixture must see the workspace):

  reference   project the retained catalogue through DocSpec's JSON decoder and spicy-docs' FR projection
  synthesize  write the synthetic third FR generation (A->B->A, a control-character key) and a typed A->B->A dataset
  admit NAME SOURCE DATASET   admit one generation and record its time, memory and bytes written
  compare     in a fresh process, compare the admitted states with the references

Receipts are JSON files under ``receipts/``; nothing here writes outside the gate directory.
"""

from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import platform
import resource
import sqlite3
import struct
import subprocess
import sys
import time

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from rulespec_artifacts import (LocalMemberSource, Producer, build_artifact_root, canonical_json_bytes, describe_member,
    write_member_manifest)

GATE = Path("/Users/mikewolfd/Work/corpora/c27-gate-20260925")
WORKSPACE, RECEIPTS, SYNTHETIC = GATE / "workspace", GATE / "receipts", GATE / "synthetic"
REFERENCE = GATE / "reference" / "catalogue-projection.parquet"
FORK = Path("/Users/mikewolfd/Work/corpora/fork-fr-generation-2026-09-23")
CATALOG = Path("/Users/mikewolfd/Work/corpora/docspec-iceberg-reimport-2173b92-20260914/federal-register/workspace")
CATALOG_PIN = "sha256:b456349d6ec916a8f145ec9ab5ee39e95f5e13a0de70ca7220dfe1c6a5508a61"
FAMILY, TABLE = "federal-register", "federal_register"
MEMBER = "federal_register.parquet"
# A synthetic generation is sealed by a test producer; the real ones by spicy-regs.
_IMPLEMENTATION = "git+https://example.test/spicy-regs@" + "1" * 40
PRODUCER = Producer("spicy-regs", _IMPLEMENTATION, "urn:test:c27-gate", "1", _IMPLEMENTATION)
NAN_SIGNED = struct.unpack(">d", bytes.fromhex("fff8000000000001"))[0]


def _write(name, value):
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    (RECEIPTS / f"{name}.json").write_text(json.dumps(value, indent=1, sort_keys=True, default=str) + "\n")
    print(json.dumps(value, sort_keys=True, default=str)[:4000])


def _read(name):
    return json.loads((RECEIPTS / f"{name}.json").read_text())


def _peak_rss_bytes():
    # macOS reports ru_maxrss in bytes.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _environment():
    commit = subprocess.run(["git", "-C", str(Path(__file__).resolve().parents[3]), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    return {"commit": commit, "python": platform.python_version(), "duckdb": duckdb.__version__,
            "pyarrow": pa.__version__, "load": os.getloadavg(), "at": datetime.now().isoformat(timespec="seconds")}


# ---------------------------------------------------------------- reference

def reference():
    """The retained catalogue projected with spicy-docs' FEDERAL_REGISTER_COLUMNS, keyed by its own membership."""
    from spicy_docs.schemas.federal_register import FEDERAL_REGISTER_COLUMNS, project_federal_register_document
    from docspec.domain.core_admission import stored_record
    from docspec.runtime import CoreWorkspace

    started = time.perf_counter()
    REFERENCE.parent.mkdir(parents=True, exist_ok=True)
    values = REFERENCE.with_name("catalogue-values.parquet")
    keys = REFERENCE.with_name("catalogue-membership.parquet")
    schema = pa.schema([("occurrence_id", pa.string()), *((name, pa.string()) for name in FEDERAL_REGISTER_COLUMNS)])
    decoded = 0
    with CoreWorkspace(CATALOG, create=False) as workspace:
        with workspace.open_state("catalogue", expected_pin=CATALOG_PIN) as reader:
            count = reader.record_count
        with workspace.publisher.session() as session:
            layers = workspace.states.layers(session, "catalogue")
            with workspace.records.relations({"members": layers["membership"]}) as relations:
                relations["members"].project(
                    "record_identity AS member_key, json_extract_string(decode(record_json), '/occurrence_id') AS occurrence_id"
                ).write_parquet(str(keys))
            with workspace.records.relations({"entities": layers["entities"]}) as relations, \
                    pq.ParquetWriter(values, schema) as writer:
                for batch in relations["entities"].project("record_identity, record_json").to_arrow_reader(1024):
                    rows = {name: [] for name in schema.names}
                    for identity, payload in zip(batch.column(0).to_pylist(), batch.column(1).to_pylist(), strict=True):
                        # DocSpec's JSON path: the occurrence record, then its catalog item's one source fact.
                        entity = stored_record(payload)
                        [fact] = entity.value.value["sourceNativeFacts"]
                        projected = project_federal_register_document(fact["fields"])
                        rows["occurrence_id"].append(entity.entity_id)
                        for name in FEDERAL_REGISTER_COLUMNS:
                            rows[name].append(projected[name])
                        decoded += 1
                    writer.write_table(pa.table(rows, schema=schema))
    with duckdb.connect() as connection:
        connection.execute(f"COPY (SELECT k.member_key, v.* EXCLUDE (occurrence_id) FROM read_parquet('{keys}') k "
                           f"JOIN read_parquet('{values}') v USING (occurrence_id)) TO '{REFERENCE}' (FORMAT parquet)")
        rows, distinct = connection.execute(f"SELECT count(*), count(DISTINCT member_key) FROM read_parquet('{REFERENCE}')").fetchone()
    _write("reference", {"state": "catalogue", "pin": CATALOG_PIN, "record_count": count, "decoded": decoded,
                         "rows": rows, "distinct_keys": distinct, "seconds": time.perf_counter() - started,
                         "peak_rss_bytes": _peak_rss_bytes(), "environment": _environment()})


# ---------------------------------------------------------------- synthetic generations

def _seal(path, data, *, row_group_size=51_200):
    """Write ``data`` as a complete-family FR generation sealed like the producer's; return its pin."""
    path.mkdir(parents=True)
    pq.write_table(data, path / MEMBER, row_group_size=row_group_size)
    member = describe_member(LocalMemberSource(path), object_key=MEMBER, role="table",
                             media_type="application/vnd.apache.parquet", record_count=data.num_rows)
    with (path / "members.json").open("wb") as output:
        manifest = write_member_manifest(output, scope_kind="global", scope_id=FAMILY, object_key="members.json", members=[member])
    with duckdb.connect() as connection:
        columns = [[name, kind] for name, kind, *_ in connection.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(path / MEMBER)]).fetchall()]
    root = build_artifact_root(kind="spicy-regs-rollup-generation", producer=PRODUCER, manifests=[manifest], spec={
        "family": FAMILY, "publicationStatus": "complete-family", "tables": {MEMBER: {"columns": columns, "rows": data.num_rows}}})
    (path / "artifact.json").write_bytes(canonical_json_bytes(root))
    return {"logicalId": root["logicalId"], "artifactDigest": root["artifactDigest"], "rows": data.num_rows}


def _keyed(table):
    return table.append_column("member_key", pc.binary_join_element_wise(
        table.column("document_number"), table.column("publication_date"), "@"))


def _typed_rows(count):
    """Deterministic typed rows: signed NaN, signed zero, infinities, BIGINT beyond 2^53, both date bounds, controls."""
    rows = []
    for index in range(count):
        rows.append({
            "document_number": f"2026-\x1f{index:05d}" if index % 97 == 0 else f"2026-{index:05d}",
            "publication_date": date(1, 1, 1) if index == 1 else date(9999, 12, 31) if index == 2 else date(2026, 1, 1) + timedelta(days=index % 365),
            "title": None if index % 11 == 0 else f"Title {index} \"quoted\" \\u001F \x00\x1f é😀" if index % 13 == 0 else f"Title {index}",
            "ratio": NAN_SIGNED if index % 50 == 0 else -0.0 if index % 51 == 0 else float("inf") if index % 53 == 0 else index / 7,
            "flag": None if index % 5 == 0 else index % 2 == 0,
            "pages": -(2**31) if index == 3 else 2**31 - 1 if index == 4 else index - 1000,
            "big": 2**53 + index if index % 17 == 0 else -(2**63) if index == 5 else index,
            "seen": datetime(2026, 9, 25, 1, 2, 3, index),
            "topics": None if index % 7 == 0 else [f"topic {index}", None, "\x1f"] if index % 19 == 0 else [],
        })
    return rows


TYPED_SCHEMA = pa.schema([("document_number", pa.string()), ("publication_date", pa.date32()), ("title", pa.string()),
                          ("ratio", pa.float64()), ("flag", pa.bool_()), ("pages", pa.int32()), ("big", pa.int64()),
                          ("seen", pa.timestamp("us")), ("topics", pa.list_(pa.string()))])


def synthesize():
    """FR G3 restores G1's values on the rows G2 changed and adds a key with a control character; typed A, B, A'."""
    changed = _direct_changes(FORK / "prior" / MEMBER, FORK / "current" / MEMBER)["changed"]
    prior, current = (_keyed(pq.read_table(FORK / name / MEMBER)) for name in ("prior", "current"))
    names = [name for name in current.column_names if name != "member_key"]
    restored = prior.filter(pc.is_in(prior.column("member_key"), pa.array(changed))).select(names)
    kept = current.filter(pc.invert(pc.is_in(current.column("member_key"), pa.array(changed)))).select(names)
    template = kept.slice(0, 1).to_pylist()[0]
    control = {**template, "document_number": "2026-\x1fC27", "publication_date": "2026-09-25",
               "title": "A synthetic row whose key holds a unit separator"}
    g3 = pa.concat_tables([kept, restored, pa.Table.from_pylist([control], schema=kept.schema)])
    result = {"fr-g3": {**_seal(SYNTHETIC / "fr-g3", g3), "restored_keys": changed,
                        "control_key": control["document_number"] + "@" + control["publication_date"]}}
    rows = _typed_rows(2000)
    changed_row = {**rows[7], "title": "Changed in B", "ratio": 0.0}
    generations = {"typed-a": rows, "typed-b": [*rows[:7], changed_row, *rows[8:]],
                   "typed-a2": [*rows, {**rows[0], "document_number": "2026-\x1fA2", "title": "Added in A'"}]}
    for name, values in generations.items():
        result[name] = _seal(SYNTHETIC / name, pa.Table.from_pylist(values, schema=TYPED_SCHEMA), row_group_size=512)
    result["typed_restored_key"] = "2026-00007@2026-01-08"
    _write("synthesize", result)


# ---------------------------------------------------------------- admission

def _files(root):
    return {str(path.relative_to(root)): path.stat().st_size for path in root.rglob("*") if path.is_file()}


def _ledger(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("records", "retention", "units", "links", "heads")}


def admit(name, source, dataset):
    from docspec.domain.references import LayerRef
    from docspec.runtime import CoreWorkspace

    create = not WORKSPACE.exists()
    before = {"records": _files(WORKSPACE / "records") if not create else {},
              "blobs": sum(_files(WORKSPACE / "blobs").values()) if not create else 0,
              "ledger_bytes": (WORKSPACE / "ledger.sqlite").stat().st_size if not create else 0,
              "ledger": _ledger(WORKSPACE / "ledger.sqlite") if not create else {}}
    started = time.perf_counter()
    with CoreWorkspace(WORKSPACE, create=create) as workspace:
        admitted = workspace.admit_generation(source, family=FAMILY, table=TABLE, dataset=dataset)
        seconds = time.perf_counter() - started
        with workspace.publisher.session() as session:
            manifest = workspace.states.manifest(session, admitted.state_id)
            layers = {layer: workspace.records.admitted(LayerRef.from_dict(manifest[layer]))
                      for layer in ("table", "membership", "occurrences")}
            directories = {layer: Path(admitted_layer.table.metadata_location).parent.parent.relative_to(WORKSPACE / "records").as_posix()
                           for layer, admitted_layer in layers.items()}
            data = {layer: workspace.records.data_files(admitted_layer.reference) for layer, admitted_layer in layers.items()}
    after = _files(WORKSPACE / "records")
    new = {path: size for path, size in after.items() if path not in before["records"]}
    written = {layer: {"data_bytes": sum(size for path, size in new.items() if path in data[layer]),
                       "all_bytes": sum(size for path, size in new.items() if path.startswith(directory + "/")),
                       "data_files_total": len(data[layer])} for layer, directory in directories.items()}
    written["layer_roots_bytes"] = sum(size for path, size in new.items() if path.startswith("record-layers/"))
    _write("admit-" + name, {
        "name": name, "source": str(source), "dataset": dataset, "state_id": admitted.state_id, "report": admitted.report,
        "seconds": seconds, "peak_rss_bytes": _peak_rss_bytes(), "written": written,
        "records_bytes_new": sum(new.values()), "blobs_bytes_new": sum(_files(WORKSPACE / "blobs").values()) - before["blobs"],
        "ledger_bytes": (WORKSPACE / "ledger.sqlite").stat().st_size, "ledger_bytes_new": (WORKSPACE / "ledger.sqlite").stat().st_size - before["ledger_bytes"],
        "ledger": _ledger(WORKSPACE / "ledger.sqlite"), "ledger_before": before["ledger"], "environment": _environment()})


# ---------------------------------------------------------------- comparison, in a fresh process

def _direct_changes(older, newer):
    """A direct all-column comparison of two members by pyarrow: added, removed and changed keys.

    Keys are unique in each member, so the common rows of both, sorted by
    key, align row for row and compare column by column without a join.
    """
    old, new = (_keyed(pq.read_table(path)) for path in (older, newer))
    names = [name for name in new.column_names if name != "member_key"]
    added = new.filter(pc.invert(pc.is_in(new.column("member_key"), value_set=old.column("member_key"))))
    removed = old.filter(pc.invert(pc.is_in(old.column("member_key"), value_set=new.column("member_key"))))
    old = old.filter(pc.is_in(old.column("member_key"), value_set=new.column("member_key"))).sort_by("member_key")
    new = new.filter(pc.is_in(new.column("member_key"), value_set=old.column("member_key"))).sort_by("member_key")
    if not old.column("member_key").equals(new.column("member_key")):
        raise AssertionError("common keys do not align")
    differs = pa.array([False] * new.num_rows)
    for name in names:
        left, right = new.column(name), old.column(name)
        equal = pc.or_(pc.fill_null(pc.equal(left, right), False), pc.and_(pc.is_null(left), pc.is_null(right)))
        differs = pc.or_(differs, pc.invert(equal))
    return {"added": sorted(added.column("member_key").to_pylist()), "removed": sorted(removed.column("member_key").to_pylist()),
            "changed": sorted(new.filter(differs).column("member_key").to_pylist()),
            "unchanged": new.num_rows - pc.sum(differs.cast(pa.int64())).as_py()}


def _changes(workspace, older, newer, older_members):
    """DocSpec's own reader: what the newer state changed against the older, classified by the older membership."""
    result = {"added": [], "removed": [], "changed": []}
    with workspace.open_state(older) as old, workspace.open_state(newer) as new:
        for key, occurrence, _ in new.changes(old):
            result["removed" if occurrence is None else "changed" if key in older_members else "added"].append(key)
    return result


def _oracle(workspace, state, source, identity, *, members=None):
    """Recompute every row's key, digest and occurrence in Python and compare with the admitted state.

    With ``members`` (key -> occurrence) only occurrences are compared;
    otherwise every occurrence record the reader spells is compared too.
    """
    from docspec.adapters.storage.table_occurrences import reference_identity
    from docspec.domain.core_admission import inline_occurrence_payload
    from docspec.domain.table_rows import table_row_bytes

    records = members is None
    if records:
        with workspace.open_state(state) as reader, reader.relation() as relation:
            native = {key: (occurrence, bytes(record)) for key, occurrence, record in relation.fetchall()}
    else:
        native = {key: (occurrence,) for key, occurrence in members.items()}
    mismatches, rows = [], 0
    for batch in pq.ParquetFile(source / MEMBER).iter_batches(batch_size=8192):
        for row in batch.to_pylist():
            rows += 1
            key, _, urn = reference_identity(identity, row)
            expected = (urn, inline_occurrence_payload(urn, table_row_bytes(row, identity.columns))) if records else (urn,)
            if native.get(key) != expected:
                mismatches.append(key)
    return {"rows": rows, "native_rows": len(native), "mismatches": len(mismatches), "mismatch_sample": mismatches[:20]}


def _occurrence_map(workspace, state):
    """Member key -> occurrence, read from the state's membership layer."""
    with workspace.publisher.session() as session:
        membership = workspace.states.layers(session, state)["membership"]
        with workspace.records.relations({"members": membership}) as relations:
            return dict(relations["members"].project(
                "record_identity, json_extract_string(decode(record_json), '/occurrence_id')").fetchall())


_KINDS = {pa.string(): "VARCHAR", pa.date32(): "DATE", pa.float64(): "DOUBLE", pa.bool_(): "BOOLEAN", pa.int32(): "INTEGER",
          pa.int64(): "BIGINT", pa.timestamp("us"): "TIMESTAMP", pa.list_(pa.string()): "VARCHAR[]"}


def compare():
    from spicy_docs.schemas.federal_register import FEDERAL_REGISTER_COLUMNS
    from docspec.adapters.storage.table_occurrences import lookup_occurrences
    from docspec.domain.references import LayerRef
    from docspec.domain.table_rows import KeySpelling, TableIdentity
    from docspec.runtime import CoreWorkspace

    started = time.perf_counter()
    admitted = {name: _read("admit-" + name) for name in ("prior", "current", "fr-g3", "typed-a", "typed-b", "typed-a2")}
    synthetic = _read("synthesize")
    states = {name: receipt["state_id"] for name, receipt in admitted.items()}
    result = {"environment": _environment(), "states": states}
    exported = GATE / "compare" / "current-table.parquet"
    with CoreWorkspace(WORKSPACE, create=False) as workspace:
        # 1. Pins and counts against the fork host's pins and the producers' recordCount.
        pins = json.loads((FORK / "pins.json").read_text())
        result["counts"] = {}
        for name in ("prior", "current"):
            root = json.loads((FORK / name / "artifact.json").read_text())
            with workspace.open_state(states[name]) as reader:
                result["counts"][name] = {
                    "admitted": reader.record_count, "recordCount": root["counts"]["totalRecordCount"],
                    "pin_matches": admitted[name]["report"]["pin"] == {key: pins[name][key] for key in ("logicalId", "artifactDigest")}}
        # 2. The contract columns against the retained catalogue, read through table().
        with workspace.open_state(states["current"]) as reader, reader.table() as typed:
            typed.write_parquet(str(exported))
        with workspace.publisher.session() as session:
            manifests = {name: workspace.states.manifest(session, state) for name, state in states.items()}
        # 3. changes() against pyarrow's direct comparison of the two members.
        direct = _direct_changes(FORK / "prior" / MEMBER, FORK / "current" / MEMBER)
        prior_map, current_map = _occurrence_map(workspace, states["prior"]), _occurrence_map(workspace, states["current"])
        docspec = _changes(workspace, states["prior"], states["current"], prior_map)
        comparison = workspace.compare(states["prior"], states["current"], sample_limit=0)
        carried = sum(1 for key, occurrence in current_map.items() if prior_map.get(key) == occurrence)
        result["changes"] = {
            "direct": {name: len(value) if isinstance(value, list) else value for name, value in direct.items()},
            "docspec": {name: len(value) for name, value in docspec.items()},
            "compare": comparison["counts"], "carried_same_occurrence": carried,
            "equal": all(sorted(docspec[name]) == direct[name] for name in ("added", "removed", "changed")),
            "changed_keys": direct["changed"], "report": admitted["current"]["report"]["counts"]}
        # 4. The synthetic FR generation: A->B->A resolves to the first occurrence (R1).
        g3 = _occurrence_map(workspace, states["fr-g3"])
        identity = TableIdentity.from_dict(manifests["current"]["rules"])
        restored = synthetic["fr-g3"]["restored_keys"]
        index = LayerRef.from_dict(manifests["fr-g3"]["occurrences"])
        found = lookup_occurrences(workspace.records, index, identity, [g3[key] for key in restored])
        result["fr_restored"] = {
            "keys": restored, "same_as_first": all(g3[key] == prior_map[key] for key in restored),
            "differs_from_second": all(g3[key] != current_map[key] for key in restored),
            "first_state": {key: found[g3[key]].first_state_id for key in restored},
            "first_state_is_prior": all(found[g3[key]].first_state_id == states["prior"] for key in restored),
            "control_key_present": synthetic["fr-g3"]["control_key"] in g3, "report": admitted["fr-g3"]["report"]["counts"]}
        # 5. The typed dataset: A->B->A and every value spelled as the Python reference spells it.
        # The rules are built here from the pyarrow schema, not read from the admission.
        typed_identity = TableIdentity(FAMILY, TABLE, KeySpelling("federal-register-source-record-id", "1",
            ("document_number", "publication_date")), tuple((field.name, _KINDS[field.type]) for field in TYPED_SCHEMA))
        typed_maps = {name: _occurrence_map(workspace, states[name]) for name in ("typed-a", "typed-b", "typed-a2")}
        key = synthetic["typed_restored_key"]
        typed_index = LayerRef.from_dict(manifests["typed-a2"]["occurrences"])
        first = lookup_occurrences(workspace.records, typed_index, typed_identity, [typed_maps["typed-a2"][key]])
        result["typed"] = {
            "rules_match": all(TableIdentity.from_dict(manifests[name]["rules"]) == typed_identity
                               for name in ("typed-a", "typed-b", "typed-a2")),
            "restored_same_as_first": typed_maps["typed-a2"][key] == typed_maps["typed-a"][key] != typed_maps["typed-b"][key],
            "first_state_is_a": first[typed_maps["typed-a2"][key]].first_state_id == states["typed-a"],
            "reports": {name: admitted[name]["report"]["counts"] for name in ("typed-a", "typed-b", "typed-a2")},
            "oracle": {name: _oracle(workspace, states[name], SYNTHETIC / name, typed_identity)
                       for name in ("typed-a", "typed-b", "typed-a2")}}
        # 6. The Python oracle over every current row.
        result["fr_oracle"] = _oracle(workspace, states["current"], FORK / "current", identity, members=current_map)
    result["catalogue"] = _catalogue(exported, [name for name in FEDERAL_REGISTER_COLUMNS if name != "topics_json"])
    result["seconds"] = time.perf_counter() - started
    result["peak_rss_bytes"] = _peak_rss_bytes()
    _write("compare", result)


# Object keys: DocSpec's canonical JSON sorts them, while the producer keeps the
# API's order. The JSON columns therefore compare as JSON values.
_JSON_COLUMNS = ("agencies_json", "docket_ids_json", "regulation_id_numbers_json", "cfr_references_json")


def _canonical_json(path, target, columns):
    """Rewrite ``columns`` of a Parquet file as key-sorted, whitespace-free JSON (a different encoder from DocSpec's)."""
    reader = pq.ParquetFile(path)
    with pq.ParquetWriter(target, reader.schema_arrow) as writer:
        for batch in reader.iter_batches(batch_size=65536):
            table = pa.Table.from_batches([batch])
            for name in columns:
                values = [None if text is None else json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"),
                                                                ensure_ascii=False) for text in table.column(name).to_pylist()]
                table = table.set_column(table.column_names.index(name), name, pa.array(values, pa.string()))
            writer.write_table(table)


def _catalogue(exported, shared):
    """Two-way EXCEPT of the admitted current generation against the catalogue projection, JSON columns as JSON."""
    generation, reference = GATE / "compare" / "current-canonical.parquet", GATE / "compare" / "catalogue-canonical.parquet"
    _canonical_json(exported, generation, _JSON_COLUMNS)
    _canonical_json(REFERENCE, reference, _JSON_COLUMNS)
    with duckdb.connect() as connection:
        connection.execute("SET memory_limit = '4GB'")
        connection.execute(f"CREATE VIEW generation AS SELECT * FROM read_parquet('{generation}')")
        connection.execute(f"CREATE VIEW reference AS SELECT * FROM read_parquet('{reference}')")
        text = {name: connection.execute(
            f"SELECT count(*) FROM read_parquet('{exported}') g JOIN read_parquet('{REFERENCE}') r USING (member_key) "
            f"WHERE g.{name} IS DISTINCT FROM r.{name}").fetchone()[0] for name in _JSON_COLUMNS}
        columns = ", ".join(["member_key", *shared])
        scoped = f"SELECT {columns} FROM generation WHERE member_key IN (SELECT member_key FROM reference)"
        both = connection.execute(f"SELECT count(*) FROM ({scoped})").fetchone()[0]
        generation_only = connection.execute(
            "SELECT member_key, publication_date FROM generation WHERE member_key NOT IN (SELECT member_key FROM reference) "
            "ORDER BY publication_date, member_key").fetchall()
        reference_only = connection.execute(
            "SELECT count(*) FROM reference WHERE member_key NOT IN (SELECT member_key FROM generation)").fetchone()[0]
        connection.execute(f"CREATE TEMP TABLE exceptions AS SELECT member_key FROM ({scoped} EXCEPT SELECT {columns} FROM reference)")
        gen_minus_ref = connection.execute("SELECT member_key FROM exceptions ORDER BY 1").fetchall()
        ref_minus_gen = connection.execute(f"SELECT member_key FROM (SELECT {columns} FROM reference EXCEPT {scoped}) ORDER BY 1").fetchall()
        pairs = ", ".join(f"g.{name}, r.{name}" for name in shared)
        differing = {}
        for key, *values in connection.execute(f"SELECT member_key, {pairs} FROM exceptions JOIN generation g USING (member_key) "
                                               "JOIN reference r USING (member_key) ORDER BY member_key").fetchall():
            differing[key] = {name: values[2 * index:2 * index + 2] for index, name in enumerate(shared)
                              if values[2 * index] != values[2 * index + 1]}
        modify = connection.execute("SELECT count(modify_date) FROM generation").fetchone()[0], \
            connection.execute("SELECT count(modify_date) FROM reference").fetchone()[0]
        reference_rows = connection.execute("SELECT count(*) FROM reference").fetchone()[0]
    return {
        "shared_columns": shared, "absent_from_generation": ["topics_json"], "extra_in_generation": ["rin"],
        "json_columns_compared_as_json": list(_JSON_COLUMNS), "json_text_differences": text,
        "reference_rows": reference_rows, "compared_keys": both, "reference_only_keys": reference_only,
        "generation_only_keys": len(generation_only),
        "generation_only_first_date": generation_only[0][1] if generation_only else None,
        "generation_only_after_catalogue_supply_2026_09_02": all(published > "2026-09-02" for _, published in generation_only),
        "generation_only_on_or_before_2026_09_14": sum(published <= "2026-09-14" for _, published in generation_only),
        "generation_only_listed": [key for key, _ in generation_only],
        "generation_except_reference": len(gen_minus_ref), "reference_except_generation": len(ref_minus_gen),
        "exception_columns": sorted({name for difference in differing.values() for name in difference}),
        "exceptions_by_key": differing, "modify_date_non_null": {"generation": modify[0], "reference": modify[1]}}


if __name__ == "__main__":
    command, *arguments = sys.argv[1:]
    {"reference": reference, "synthesize": synthesize, "compare": compare,
     "admit": lambda: admit(arguments[0], Path(arguments[1]), arguments[2] if len(arguments) > 2 else None)}[command]()
