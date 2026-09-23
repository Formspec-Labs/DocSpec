"""D2 spike: register a producer generation's Parquet as a pinned Iceberg table without rewriting it.

Run from the repository root, through the project's own runner and REST fixture:

    uv run --frozen python tools/with_iceberg.py \
        uv run --frozen python docs/history/probes/2026-09-23-admit-by-reference-spike.py \
        PINS.json RECEIPT.json

PINS.json names two admitted generation directories (``current`` and ``prior``),
each with ``dir``, ``logicalId`` and ``artifactDigest`` taken from the producer's
publication index. Each arm runs in a fresh process so its peak RSS is its own.
Nothing here changes DocSpec source; ``IcebergRecordStorage._pin`` is called
directly because no public API admits a foreign table yet (that is C27).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

TABLE = "federal_register.parquet"
NAMESPACE_KIND = "table-occurrence"


def _rss():
    # macOS reports bytes; Linux reports KiB.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def _emit(result):
    result["peak_rss_bytes"] = _rss()
    result["load_average"] = os.getloadavg()
    print("RESULT " + json.dumps(result), flush=True)


def _scan(path):
    """The exact read form of IcebergRecordStorage._relation, without its encoded-record projection."""
    from docspec.adapters.storage.iceberg import literal

    path = Path(path)
    return (f"iceberg_scan({literal(path.parent.parent)}, "
            f"version={literal(path.name.removesuffix('.metadata.json'))}, "
            "version_name_format='%s%s.metadata.json', allow_moved_paths=true)")


def _connect():
    from docspec.adapters.storage.engine import connect

    scratch = tempfile.mkdtemp(prefix="spike-scratch-")
    con = connect(scratch)  # DocSpec's own limits: one thread, 6 GiB managed memory.
    con.execute("INSTALL iceberg")
    con.execute("LOAD iceberg")
    return con


def _storage(root, *, writes=False):
    from docspec.adapters.storage.iceberg import IcebergCatalog
    from docspec.adapters.storage.records import IcebergRecordStorage

    return IcebergRecordStorage(Path(root), create=writes,
                                catalog=IcebergCatalog.environment() if writes else None)


def _schema_and_policy(columns):
    from docspec.domain.storage import PartitionPolicy, RecordSchema

    # A stand-in logical schema: _pin records it; file-level admission never reads rows.
    return (RecordSchema("urn:spike:producer-table:federal-register", tuple(columns), "document_number",
                         "publication_date"), PartitionPolicy("urn:spike:unpartitioned", 1))


# ---------------------------------------------------------------- arms


def arm_noop():
    import docspec.adapters.storage.records  # noqa: F401 - import cost only
    import duckdb  # noqa: F401
    import pyiceberg  # noqa: F401
    _emit({"arm": "noop"})


def _admit(generation, logical_id, artifact_digest):
    from rulespec_artifacts import ArtifactPin, LocalMemberSource, admit_artifact, iter_member_descriptors

    started = time.perf_counter()
    source = LocalMemberSource(Path(generation))
    artifact = admit_artifact(source, expected_pin=ArtifactPin(logical_id, artifact_digest))
    members = [m for m in iter_member_descriptors(artifact, source)]
    return {"seconds": time.perf_counter() - started, "kind": artifact.root["kind"],
            "family": artifact.root["spec"]["family"], "total_record_count": artifact.total_record_count,
            "members": [{"object_key": m.object_key, "sha256": m.sha256, "byte_size": m.byte_size,
                         "record_count": m.record_count} for m in members]}


def arm_admit(generation, logical_id, artifact_digest):
    _emit({"arm": "admit", **_admit(generation, logical_id, artifact_digest)})


def _register(root, generation, member_sha256, record_count, label):
    """Place the member once (hash while copying, as a download would), register it, pin it."""
    import pyarrow.parquet as pq

    from docspec.adapters.storage.iceberg import recovery_references
    from docspec.domain.references import BlobRef

    root = Path(root)
    storage = _storage(root, writes=True)
    table_dir = root / "iceberg" / uuid4().hex
    (table_dir / "metadata").mkdir(parents=True)
    (table_dir / "data").mkdir()
    source, placed = Path(generation) / TABLE, table_dir / "data" / TABLE

    started = time.perf_counter()
    digest = hashlib.sha256()
    with source.open("rb") as reader, placed.open("xb") as writer:
        while chunk := reader.read(8 * 1024**2):
            digest.update(chunk)
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    place_seconds = time.perf_counter() - started
    if "sha256:" + digest.hexdigest() != member_sha256:
        raise SystemExit("placed member differs from its admitted descriptor")

    started = time.perf_counter()
    with storage._cursor():  # the catalog attaches to DocSpec's native connection, as _write_table does
        client = storage._client()
    name = (storage.catalog.namespace, "spike_" + uuid4().hex)
    arrow_schema = pq.read_schema(placed)
    client.create_table(name, schema=arrow_schema, location=str(table_dir))
    client.load_table(name).add_files([str(placed)])
    table = client.load_table(name)
    register_seconds = time.perf_counter() - started
    client.drop_table(name)  # no purge: a disposable write handle, as DocSpec uses the catalog

    schema, policy = _schema_and_policy(arrow_schema.names)
    started = time.perf_counter()
    layer = storage._pin(table, schema=schema, partition_policy=policy, layer_kind="producer-table",
                         record_count=int(record_count))
    pin_seconds = time.perf_counter() - started

    # The seal's checksum for the data file must be the producer's own member digest.
    sealed = {ref.locator: ref for ref in recovery_references(root, BlobRef.from_dict(layer._root["integrity"]))}
    data_ref = sealed[placed.relative_to(root).as_posix()]
    added = sum(p.stat().st_size for p in table_dir.rglob("*") if p.is_file() and p != placed)
    root_bytes = (root / layer.reference.state_ref).stat().st_size
    (root / f"{label}.layer.json").write_text(json.dumps({
        "layer": layer.reference.to_dict(),
        "metadata_location": table.metadata_location, "member": str(placed)}))
    storage.close()
    return {"label": label, "place_seconds": place_seconds, "register_seconds": register_seconds,
            "pin_seconds": pin_seconds, "name_mapping_set": "schema.name-mapping.default" in table.properties,
            "table_properties": sorted(table.properties),
            "data_files": [e.data_file.file_path.rsplit("/", 1)[1] for m in table.current_snapshot().manifests(table.io)
                           for e in m.fetch_manifest_entry(table.io, discard_deleted=True)],
            "sealed_data_digest_equals_member": data_ref.digest == member_sha256,
            "docspec_added_bytes": added + root_bytes, "layer_root_bytes": root_bytes}


def arm_register(root, generation, member_sha256, record_count, label):
    _emit({"arm": "register", **_register(root, generation, member_sha256, record_count, label)})


def _identity_pass(con, root, label):
    """The native (member key, row digest, occurrence) table for one registered generation."""
    _, saved = _load_layer(root, label)
    scan = _scan(saved["metadata_location"])
    columns = [row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {scan}").fetchall()]
    started = time.perf_counter()
    con.execute(f"CREATE TEMP TABLE occ_{label} AS SELECT member_key, row_digest, {_occurrence()} AS occurrence_id "
                f"FROM (SELECT {KEY} AS member_key, sha256({_row_json(columns)}) AS row_digest FROM {scan})")
    seconds = time.perf_counter() - started
    duplicates = con.execute(f"SELECT count(*) FROM (SELECT member_key FROM occ_{label} GROUP BY 1 HAVING count(*) > 1)").fetchone()[0]
    rows = con.execute(f"SELECT count(*) FROM occ_{label}").fetchone()[0]
    return {f"{label}_identity_seconds": seconds, f"{label}_duplicate_keys": duplicates, f"{label}_rows": rows}


def _write_membership(con, root, label):
    """The only per-row bytes DocSpec writes: key -> occurrence, as today's canonical Membership records."""
    from docspec.adapters.storage.iceberg import literal

    path = Path(root) / f"membership-{label}.parquet"
    started = time.perf_counter()
    # Membership layers use one bucket ("core-keys:1"), so every row's bucket is 0.
    con.execute(f"COPY (SELECT member_key AS record_identity, member_key AS partition_value, "
                f"encode({_membership()}) AS record_json, 0 AS bucket "
                f"FROM occ_{label} ORDER BY member_key) TO {literal(path)} (FORMAT parquet, COMPRESSION zstd)")
    return {"membership_write_seconds": time.perf_counter() - started, "membership_bytes": path.stat().st_size}


def arm_admission(root, generation, logical_id, artifact_digest):
    """The whole by-reference path for one generation, in one process: the number set against the baseline."""
    started = time.perf_counter()
    admitted = _admit(generation, logical_id, artifact_digest)
    member = admitted["members"][0]
    registered = _register(root, generation, member["sha256"], member["record_count"], "admission")
    con = _connect()
    identity = _identity_pass(con, root, "admission")
    membership = _write_membership(con, root, "admission")
    _emit({"arm": "admission", "seconds": time.perf_counter() - started, "admit_seconds": admitted["seconds"],
           **{k: v for k, v in registered.items() if k.endswith("_seconds") or k.endswith("_bytes")},
           **identity, **membership})


def _load_layer(root, label):
    from docspec.domain.references import LayerRef

    saved = json.loads((Path(root) / f"{label}.layer.json").read_text())
    return LayerRef.from_dict(saved["layer"]), saved


def arm_verify(root, label):
    from docspec.errors import IntegrityError

    ref, _ = _load_layer(root, label)
    storage = _storage(root)
    started = time.perf_counter()
    storage.verify_members(ref)
    verify_seconds = time.perf_counter() - started
    started = time.perf_counter()
    layer = storage.available(ref)
    available_seconds = time.perf_counter() - started
    # The current row path projects the encoded-record columns; record what it says.
    try:
        with storage.relations({"t": layer}) as relations:
            relations["t"].limit(1).fetchall()
        relation_error = None
    except IntegrityError as error:
        relation_error = str(error)[:240]
    _emit({"arm": "verify", "label": label, "verify_members_seconds": verify_seconds,
           "available_seconds": available_seconds, "encoded_relation_error": relation_error})


def arm_read(root, label, reference_parquet):
    """Fresh process: DocSpec's scan form against read_parquet of the admitted member, both directions."""
    from docspec.adapters.storage.iceberg import literal

    _, saved = _load_layer(root, label)
    con = _connect()
    scan, member = _scan(saved["metadata_location"]), f"read_parquet({literal(reference_parquet)})"
    columns = [row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {member}").fetchall()]
    nonnull = ", ".join(f"count({c})" for c in columns)
    started = time.perf_counter()
    scanned = con.execute(f"SELECT count(*), {nonnull} FROM {scan}").fetchone()
    count_seconds = time.perf_counter() - started
    expected = con.execute(f"SELECT count(*), {nonnull} FROM {member}").fetchone()
    started = time.perf_counter()
    only_scan = con.execute(f"SELECT count(*) FROM (SELECT * FROM {scan} EXCEPT ALL SELECT * FROM {member})").fetchone()[0]
    only_member = con.execute(f"SELECT count(*) FROM (SELECT * FROM {member} EXCEPT ALL SELECT * FROM {scan})").fetchone()[0]
    except_seconds = time.perf_counter() - started

    # A second reader family: PyIceberg's own name-mapped scan of the pinned StaticTable.
    storage = _storage(root)
    ref, _ = _load_layer(root, label)
    table = storage.available(ref).table
    started = time.perf_counter()
    arrow = table.scan(selected_fields=("document_number", "publication_date", "rin")).to_arrow()
    pyiceberg_seconds = time.perf_counter() - started
    _emit({"arm": "read", "label": label, "rows": scanned[0], "expected_rows": expected[0],
           "nonnull_equal": list(scanned) == list(expected), "nonnull_counts": dict(zip(["*", *columns], scanned)),
           "count_seconds": count_seconds, "except_all_scan_only": only_scan, "except_all_member_only": only_member,
           "except_seconds": except_seconds, "pyiceberg_rows": arrow.num_rows,
           "pyiceberg_nonnull": {c: arrow.num_rows - arrow.column(c).null_count for c in arrow.column_names},
           "pyiceberg_seconds": pyiceberg_seconds})


def arm_relocate(root, label, new_root, reference_parquet):
    """Clone the whole storage root elsewhere; the pin must still verify and read; a flipped byte must not."""
    from docspec.adapters.storage.iceberg import literal
    from docspec.errors import IntegrityError

    subprocess.run(["cp", "-cR", str(root), str(new_root)], check=True)
    ref, saved = _load_layer(new_root, label)
    storage = _storage(new_root)
    storage.verify_members(ref)
    moved = Path(new_root) / Path(saved["metadata_location"]).relative_to(root)
    con = _connect()
    scan, member = _scan(moved), f"read_parquet({literal(reference_parquet)})"
    rows = con.execute(f"SELECT count(*) FROM {scan}").fetchone()[0]
    only_scan = con.execute(f"SELECT count(*) FROM (SELECT * FROM {scan} EXCEPT ALL SELECT * FROM {member})").fetchone()[0]
    only_member = con.execute(f"SELECT count(*) FROM (SELECT * FROM {member} EXCEPT ALL SELECT * FROM {scan})").fetchone()[0]
    data = Path(new_root) / Path(saved["member"]).relative_to(root)
    with data.open("r+b") as handle:  # an APFS clone: the original's blocks are untouched
        handle.seek(4096)
        byte = handle.read(1)
        handle.seek(4096)
        handle.write(bytes([byte[0] ^ 0xFF]))
    try:
        storage.verify_members(ref)
        tamper = "accepted"
    except IntegrityError as error:
        tamper = "refused: " + str(error)
    _emit({"arm": "relocate", "label": label, "rows": rows, "except_all_scan_only": only_scan,
           "except_all_member_only": only_member, "tamper": tamper})


def arm_ctas(root, generation, record_count):
    """Fallback: one vectorized copy through the REST fixture, then the same pin."""
    import pyarrow.parquet as pq

    from docspec.adapters.storage.iceberg import identifier, literal

    root = Path(root)
    storage = _storage(root, writes=True)
    member = Path(generation) / TABLE
    arrow_schema = pq.read_schema(member)
    table_dir = root / "iceberg" / uuid4().hex
    (table_dir / "metadata").mkdir(parents=True)
    (table_dir / "data").mkdir()
    with storage._cursor() as cursor:
        client = storage._client()
        name = "ctas_" + uuid4().hex
        qualified = f"iceberg.{identifier(storage.catalog.namespace)}.{identifier(name)}"
        columns = ", ".join(f"{identifier(c)} VARCHAR" for c in arrow_schema.names)
        started = time.perf_counter()
        cursor.execute(f"CREATE TABLE {qualified} ({columns}) WITH ('location'={literal(table_dir)}, "
                       "'format-version'='2', 'write.target-file-size-bytes'='134217728', "
                       "'write.parquet.row-group-size-bytes'='1048576')")
        cursor.execute(f"INSERT INTO {qualified} SELECT * FROM read_parquet({literal(member)})")
        copy_seconds = time.perf_counter() - started
    table = client.load_table((storage.catalog.namespace, name))
    client.drop_table((storage.catalog.namespace, name))
    schema, policy = _schema_and_policy(arrow_schema.names)
    started = time.perf_counter()
    layer = storage._pin(table, schema=schema, partition_policy=policy, layer_kind="producer-table-copy",
                         record_count=int(record_count))
    pin_seconds = time.perf_counter() - started
    data = [p for p in (table_dir / "data").rglob("*.parquet")]
    (root / "ctas.layer.json").write_text(json.dumps({"layer": layer.reference.to_dict(),
                                                      "metadata_location": table.metadata_location,
                                                      "member": None}))
    _emit({"arm": "ctas", "copy_seconds": copy_seconds, "pin_seconds": pin_seconds,
           "data_files": len(data), "data_bytes": sum(p.stat().st_size for p in data)})


_SHORT_ESCAPES = {8: "\\b", 9: "\\t", 10: "\\n", 12: "\\f", 13: "\\r"}


def _jcs_chain(x):
    """Canonical (RFC 8785) string escaping on the raw text: backslash, quote, then each control character."""
    expression = f"replace(replace({x}, '\\', '\\\\'), '\"', '\\\"')"
    for code in range(32):
        expression = f"replace({expression}, chr({code}), '{_SHORT_ESCAPES.get(code, '\\u%04x' % code)}')"
    return "'\"' || " + expression + " || '\"'"


def _jcs_string(x):
    # DuckDB's to_json spells control-character escapes in upper-case hex (\u001F); canonical JSON
    # uses lower case. Only strings holding a control character take the (slower) raw-text chain.
    return f"CASE WHEN regexp_matches({x}, '[\\x00-\\x1f]') THEN {_jcs_chain(x)} ELSE to_json({x}) END"


def _row_json(columns):
    """Canonical JSON of an all-VARCHAR row: keys sorted (ASCII names, so byte order), null for NULL."""
    return ("'{' || " + " || ',' || ".join(f"'\"{c}\":' || coalesce({_jcs_string(f'\"{c}\"')}, 'null')"
                                           for c in sorted(columns)) + " || '}'")


def _row_json_to_json(columns):
    """DuckDB's own spelling; canonical only for strings without control characters (see arm_spellings)."""
    return "to_json(struct_pack(" + ", ".join(f'"{c}" := "{c}"' for c in sorted(columns)) + "))"


# Decision 0003's composite Federal Register identity, spelled as DocSpec's catalog states spell member keys.
KEY = "document_number || '@' || publication_date"


def _occurrence(p=""):
    return (f"'urn:docspec:{NAMESPACE_KIND}:v1:' || sha256(to_json(['{TABLE}', {p}member_key, "
            f"'sha256:' || {p}row_digest]))")


def _membership(p=""):
    return (f"'{{\"kind\":\"Membership\",\"member_key\":' || to_json({p}member_key) || "
            f"',\"occurrence_id\":' || to_json({p}occurrence_id) || '}}'")


def _occurrence_record(row_json, p=""):
    # inline_occurrence_payload's canonical entity record, around the row's canonical bytes.
    return (f"'{{\"entity_id\":' || to_json({p}occurrence_id) || ',\"entity_type\":\"occurrence\",\"format_version\":1,"
            f"\"kind\":\"entity\",\"value\":{{\"codec\":\"json-v1\",\"kind\":\"inline\",\"value\":' || {row_json} || '}}}}'")


def _bucket_hash(p=""):
    # partition_bucket's first eight SHA-256 bytes, big-endian, before the modulus.
    return f"CAST('0x' || sha256({p}member_key)[1:16] AS UBIGINT)"


def arm_identity(root, current_member, prior_member):
    """Occurrence identity as f(table, key, row digest), computed natively; the oracle arm re-derives it."""
    from docspec.adapters.storage.iceberg import literal

    con = _connect()
    timings = {**_identity_pass(con, root, "current"), **_identity_pass(con, root, "prior"),
               **_write_membership(con, root, "current")}
    _write_membership(con, root, "prior")
    # A compact alternative: the typed occurrence index (key, 32-byte row digest); the occurrence
    # id is a pure function of the table, key and digest, so it need not be stored.
    compact = Path(root) / "occurrence-index-current.parquet"
    started = time.perf_counter()
    con.execute(f"COPY (SELECT member_key, unhex(row_digest) AS row_digest FROM occ_current ORDER BY member_key) "
                f"TO {literal(compact)} (FORMAT parquet, COMPRESSION zstd)")
    timings["compact_index_seconds"] = time.perf_counter() - started
    timings["compact_index_bytes"] = compact.stat().st_size

    # changes: CoreStateStorage.changes' own form, one native outer join over the two written
    # membership layers, keeping keys whose canonical membership bytes differ.
    old, new = (literal(Path(root) / f"membership-{label}.parquet") for label in ("prior", "current"))
    started = time.perf_counter()
    added, removed, changed, unchanged = con.execute(
        "SELECT count(*) FILTER (WHERE o.record_identity IS NULL), count(*) FILTER (WHERE n.record_identity IS NULL), "
        "count(*) FILTER (WHERE o.record_identity IS NOT NULL AND n.record_identity IS NOT NULL "
        "AND o.record_json IS DISTINCT FROM n.record_json), count(*) FILTER (WHERE o.record_json = n.record_json) "
        f"FROM read_parquet({old}) o FULL OUTER JOIN read_parquet({new}) n ON o.record_identity = n.record_identity"
    ).fetchone()
    timings["changes_seconds"] = time.perf_counter() - started

    # Cross-check "changed" without digests: compare every column of the two members directly.
    columns = [row[0] for row in con.execute(f"DESCRIBE SELECT * FROM read_parquet({literal(current_member)})").fetchall()]
    distinct = " OR ".join(f'o."{c}" IS DISTINCT FROM n."{c}"' for c in columns)
    started = time.perf_counter()
    by_columns = con.execute(
        f"SELECT count(*) FROM read_parquet({literal(prior_member)}) o JOIN read_parquet({literal(current_member)}) n "
        f"ON o.document_number = n.document_number AND o.publication_date = n.publication_date WHERE {distinct}").fetchone()[0]
    timings["column_compare_seconds"] = time.perf_counter() - started
    added_dates = con.execute("SELECT min(split_part(member_key, '@', 2)), max(split_part(member_key, '@', 2)) FROM occ_current "
                              "WHERE member_key NOT IN (SELECT member_key FROM occ_prior)").fetchone()
    changed_fields = con.execute(
        "SELECT " + ", ".join(f'count(*) FILTER (WHERE o."{c}" IS DISTINCT FROM n."{c}") AS "{c}"' for c in columns) +
        f" FROM read_parquet({literal(prior_member)}) o JOIN read_parquet({literal(current_member)}) n "
        "ON o.document_number = n.document_number AND o.publication_date = n.publication_date").fetchone()

    con.execute(f"COPY (SELECT member_key, row_digest, occurrence_id FROM occ_current) TO "
                f"{literal(Path(root) / 'occurrences-current.parquet')} (FORMAT parquet)")
    _emit({"arm": "identity", **timings,
           "changes": {"added": added, "removed": removed, "changed": changed, "unchanged": unchanged},
           "changed_by_column_compare": by_columns, "added_publication_dates": list(added_dates),
           "changed_fields": dict(zip(columns, changed_fields)),
           "remint_would_report": added + removed + changed + unchanged})


def arm_spellings(current_member):
    """Native spellings against the Python reference on edge cases and random corpora, then their cost on FR."""
    import math
    import random
    import struct

    from rulespec_artifacts import canonical_json_bytes

    from docspec.adapters.storage.iceberg import literal

    random.seed(20260923)
    con = _connect()
    edge = ["", '"', "\\", "/", "\x00", "\x01", "\x08", "\x09", "\x0a", "\x0b", "\x0c", "\x0d", "\x1f", "\x7f",
            "\u0080", "\u00e9", "\u2028", "\u2029", "\ufeff", "\U0001F600", "\\u001F", "\\\x1f", "\\\\u000B\x0b"]
    planes = (lambda: random.randint(0, 0x7F), lambda: random.randint(0x80, 0xD7FF),
              lambda: random.randint(0xE000, 0xFFFF), lambda: random.randint(0x10000, 0x10FFFF))
    strings = edge + ["".join(chr(random.choice(planes)()) for _ in range(random.randint(0, 12))) for _ in range(200_000)]
    con.execute("CREATE TABLE strings AS SELECT unnest($1) AS v", [strings])
    native = con.execute(f"SELECT to_json(v), {_jcs_string('v')} FROM strings").fetchall()
    expected = [canonical_json_bytes(v).decode() for v in strings]
    control = [any(ord(ch) < 32 for ch in v) for v in strings]
    doubles = [0.0, -0.0, 1.0, 0.1, 1 / 3, 1e21, 1e-7, 5e-324, 1.7976931348623157e308, float(2**53), 1e16,
               float("inf"), float("-inf")]
    doubles += [v for v in (struct.unpack("<d", struct.pack("<Q", random.getrandbits(64)))[0] for _ in range(300_000))
                if not math.isnan(v)]
    con.execute("CREATE TABLE doubles AS SELECT unnest($1::DOUBLE[]) AS v", [doubles])
    double_text = [row[0] for row in con.execute("SELECT CAST(v AS VARCHAR) FROM doubles").fetchall()]
    integers = [0, -1, 2**53 - 1, -(2**53 - 1), 2**53, 2**63 - 1, -(2**63)]
    integers += [random.randint(-(2**63), 2**63 - 1) for _ in range(100_000)]
    con.execute("CREATE TABLE integers AS SELECT unnest($1::BIGINT[]) AS v", [integers])
    integer_text = [row[0] for row in con.execute("SELECT to_json(v) FROM integers").fetchall()]

    member = f"read_parquet({literal(current_member)})"
    columns = [row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {member}").fetchall()]
    costs = {}
    for name, expression in (("to_json", _row_json_to_json(columns)), ("guarded", _row_json(columns))):
        started = time.perf_counter()
        con.execute(f"CREATE TEMP TABLE cost_{name} AS SELECT {KEY} AS member_key, sha256({expression}) AS d FROM {member}")
        costs[name + "_seconds"] = time.perf_counter() - started
    costs["digest_disagreements"] = con.execute(
        "SELECT count(*) FROM cost_to_json a JOIN cost_guarded b USING (member_key) WHERE a.d <> b.d").fetchone()[0]
    costs["rows_with_control_characters"] = con.execute(
        f"SELECT count(*) FROM {member} WHERE " + " OR ".join(f"regexp_matches(\"{c}\", '[\\x00-\\x1f]')" for c in columns)
    ).fetchone()[0]
    _emit({"arm": "spellings",
           "strings": len(strings),
           "to_json_mismatches": sum(a != e for (a, _), e in zip(native, expected)),
           "to_json_mismatches_without_control_characters": sum(a != e and not c for (a, _), e, c in zip(native, expected, control)),
           "to_json_mismatched_edge_cases": [repr(v) for v, (a, _), e in zip(edge, native, expected) if a != e],
           "guarded_mismatches": sum(g != e for (_, g), e in zip(native, expected)),
           "doubles": len(doubles), "double_cast_vs_python_repr_mismatches": sum(t != repr(v) for v, t in zip(doubles, double_text)),
           "integers": len(integers), "bigint_to_json_vs_str_mismatches": sum(t != str(v) for v, t in zip(integers, integer_text)),
           "federal_register": costs})


def arm_oracle(root, current_member):
    """Re-derive every current row in Python, through pyarrow and the Rulespec encoder, against what was written."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from rulespec_artifacts import canonical_json_bytes

    from docspec.adapters.storage.iceberg import literal
    from docspec.domain import core
    from docspec.domain.core_admission import inline_occurrence_payload, record_value
    from docspec.domain.identity import canonical_value_bytes, stable_urn

    started = time.perf_counter()
    keys, digests, occurrences, memberships, buckets, records = [], [], [], [], [], []
    for batch in pq.ParquetFile(current_member).iter_batches(batch_size=65536):
        for row in batch.to_pylist():
            member_key = row["document_number"] + "@" + row["publication_date"]
            row_bytes = canonical_json_bytes(row)
            row_digest = "sha256:" + hashlib.sha256(row_bytes).hexdigest()
            occurrence_id = stable_urn(NAMESPACE_KIND, [TABLE, member_key, row_digest])
            records.append(hashlib.sha256(inline_occurrence_payload(occurrence_id, row_bytes)).hexdigest())
            keys.append(member_key)
            digests.append(row_digest.removeprefix("sha256:"))
            occurrences.append(occurrence_id)
            memberships.append(canonical_value_bytes(record_value(core.Membership(member_key=member_key,
                                                                                  occurrence_id=occurrence_id),
                                                                  core.Membership)))
            buckets.append(int.from_bytes(hashlib.sha256(member_key.encode()).digest()[:8], "big"))
    oracle_seconds = time.perf_counter() - started
    con = _connect()
    con.register("oracle", pa.table({"member_key": keys, "row_digest": digests, "occurrence_id": occurrences,
                                     "membership": memberships, "bucket": pa.array(buckets, type=pa.uint64()),
                                     "record_digest": records}))
    # The occurrence record existing readers consume, generated natively over the pinned table.
    _, saved = _load_layer(root, "current")
    scan = _scan(saved["metadata_location"])
    columns = [row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {scan}").fetchall()]
    row_json = _row_json(columns)
    started = time.perf_counter()
    con.execute(f"CREATE TEMP TABLE native_records AS SELECT member_key, sha256({_occurrence_record('row_json')}) AS record_digest "
                f"FROM (SELECT member_key, row_json, {_occurrence()} AS occurrence_id FROM (SELECT {KEY} AS member_key, "
                f"{row_json} AS row_json, sha256({row_json}) AS row_digest FROM {scan}))")
    native_record_seconds = time.perf_counter() - started
    record_mismatches = con.execute("SELECT count(*) FILTER (WHERE r.record_digest IS DISTINCT FROM p.record_digest), count(*) "
                                    "FROM native_records r FULL OUTER JOIN oracle p ON r.member_key = p.member_key").fetchone()
    occurrences_path = literal(Path(root) / "occurrences-current.parquet")
    membership_path = literal(Path(root) / "membership-current.parquet")
    mismatches = con.execute(
        "SELECT count(*) FILTER (WHERE n.member_key IS NULL OR p.member_key IS NULL OR m.record_identity IS NULL), "
        "count(*) FILTER (WHERE n.row_digest IS DISTINCT FROM p.row_digest), "
        "count(*) FILTER (WHERE n.occurrence_id IS DISTINCT FROM p.occurrence_id), "
        "count(*) FILTER (WHERE m.record_json IS DISTINCT FROM p.membership), "
        f"count(*) FILTER (WHERE {_bucket_hash('n.')} IS DISTINCT FROM p.bucket), count(*) "
        f"FROM read_parquet({occurrences_path}) n FULL OUTER JOIN oracle p ON n.member_key = p.member_key "
        f"FULL OUTER JOIN read_parquet({membership_path}) m ON m.record_identity = coalesce(n.member_key, p.member_key)"
    ).fetchone()
    _emit({"arm": "oracle", "oracle_seconds": oracle_seconds, "native_occurrence_record_seconds": native_record_seconds,
           "mismatches": {**dict(zip(["key_sets", "row_digest", "occurrence_id", "membership_bytes_written",
                                      "bucket_hash", "compared_rows"], mismatches)),
                          "occurrence_record": record_mismatches[0], "occurrence_record_rows": record_mismatches[1]}})


# ---------------------------------------------------------------- orchestration


def _run(*args):
    started = time.perf_counter()
    completed = subprocess.run([sys.executable, __file__, "arm", *map(str, args)], capture_output=True, text=True)
    if completed.returncode:
        sys.stderr.write(completed.stdout + completed.stderr)
        raise SystemExit(f"arm {args[0]} failed")
    line = next(line for line in completed.stdout.splitlines() if line.startswith("RESULT "))
    result = json.loads(line.removeprefix("RESULT "))
    result["process_seconds"] = time.perf_counter() - started
    print(json.dumps(result)[:400], flush=True)
    return result


def main(pins_path, receipt_path):
    import duckdb
    import pyarrow
    import pyiceberg
    import rulespec_artifacts  # noqa: F401

    pins = json.loads(Path(pins_path).read_text())
    root = Path(tempfile.mkdtemp(prefix="admit-by-reference-"))  # under the fixture's mounted TMPDIR
    relocated = root.with_name(root.name + "-relocated")
    try:
        arms = {"noop": _run("noop")}
        pin = pins["current"]
        arms["admission_current"] = _run("admission", root, pin["dir"], pin["logicalId"], pin["artifactDigest"])
        for label in ("current", "prior"):
            pin = pins[label]
            admitted = arms[f"admit_{label}"] = _run("admit", pin["dir"], pin["logicalId"], pin["artifactDigest"])
            member = admitted["members"][0]
            arms[f"register_{label}"] = _run("register", root, pin["dir"], member["sha256"], member["record_count"], label)
        arms["verify_current"] = _run("verify", root, "current")
        current_member = Path(pins["current"]["dir"]) / TABLE
        prior_member = Path(pins["prior"]["dir"]) / TABLE
        arms["read_current"] = _run("read", root, "current", current_member)
        arms["identity"] = _run("identity", root, current_member, prior_member)
        arms["oracle"] = _run("oracle", root, current_member)
        arms["spellings"] = _run("spellings", current_member)
        arms["ctas_current"] = _run("ctas", root, pins["current"]["dir"], arms["admit_current"]["members"][0]["record_count"])
        arms["read_ctas"] = _run("read", root, "ctas", current_member)
        arms["relocate_current"] = _run("relocate", root, "current", relocated, current_member)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(relocated, ignore_errors=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    receipt = {
        "question": "Can DocSpec register a producer generation's id-less Parquet as a pinned Iceberg table "
                    "without rewriting its bytes, and read it through its own iceberg_scan form?",
        "pins": pins, "software": {"python": platform.python_version(), "duckdb": duckdb.__version__,
                                   "pyiceberg": pyiceberg.__version__, "pyarrow": pyarrow.__version__,
                                   "docspec_commit": commit, "machine": platform.platform(),
                                   "cpus": os.cpu_count()},
        "settings": {"engine": "docspec.adapters.storage.engine.connect defaults: 1 thread, 6 GiB managed memory",
                     "catalog": "tools/with_iceberg.py REST fixture", "repetitions": 1},
        "arms": arms,
    }
    Path(receipt_path).write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    if sys.argv[1] == "arm":
        globals()["arm_" + sys.argv[2]](*sys.argv[3:])
    else:
        main(*sys.argv[1:3])
