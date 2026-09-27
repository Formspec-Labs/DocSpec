"""Publish the live documents table split by agency_code with spicy-regs' own builder, as a local v2 base.

documents has a one-column identity (document_id, value/1), so DocSpec admits it, and agency_code is already a
declared column that no document changes, so the split adds no column: its single-file form is the live
generation itself. Run with spicy-regs' environment and its code at multifile-builder 8d24b96, never DocSpec's:

    git -C spicy-regs archive 8d24b96 src | tar -x -C SRC
    PYTHONPATH=SRC/src PYTHONDONTWRITEBYTECODE=1 SPICY_REGS_PYTHON split_documents.py LIVE_GENERATION ROOT

It writes ROOT/documents-split/work (DuckDB's one pass, partitioned with the column kept in each file, each
file renamed part-000000.parquet), ROOT/documents-split/generation (build_generation(partitioned=...), which
checks every member's partition and columns and verifies the sealed artifact) and ROOT/bases/documents-split,
which serves it under publication.v2.json (table_entries) and the publication.json derive_v1 makes of it.
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import duckdb
from rulespec_artifacts import canonical_json_bytes

from spicy_regs.generations import build_generation
from spicy_regs.sources.publication import derive_v1, parse_index, table_entries

live, root = Path(sys.argv[1]), Path(sys.argv[2])
work, generation = root / "documents-split" / "work" / "documents", root / "documents-split" / "generation"
started = time.monotonic()
work.parent.mkdir(parents=True)
with duckdb.connect() as connection:
    connection.execute("SET threads = 1")  # one file per agency: a partitioned write emits one file per thread
    connection.execute(f"COPY (SELECT * FROM read_parquet('{live / 'documents.parquet'}', hive_partitioning = false)) "
                       f"TO '{work}' (FORMAT parquet, PARTITION_BY (agency_code), WRITE_PARTITION_COLUMNS true, "
                       "ROW_GROUP_SIZE 100000)")
for directory in sorted(work.iterdir()):
    [written] = list(directory.iterdir())  # one file per agency, renamed to the builder's member name
    written.rename(directory / "part-000000.parquet")
written_seconds = round(time.monotonic() - started, 2)
artifact = build_generation(generation, family="documents", files=[work], expected_keys=["documents.parquet"],
                            partitioned={"documents.parquet": ["agency_code"]})
built_seconds = round(time.monotonic() - started - written_seconds, 2)
digest = artifact.pin.artifact_digest
prefix = f"generations/documents/{digest.removeprefix('sha256:')}"
base = root / "bases" / "documents-split"
(base / prefix).parent.mkdir(parents=True)
subprocess.run(["cp", "-c", "-R", str(generation), str(base / prefix)], check=True)
members = [SimpleNamespace(object_key=member["objectKey"], sha256=member["sha256"], byte_size=member["byteSize"],
                           record_count=member["recordCount"])
           for member in json.loads((generation / "members.json").read_bytes())["members"]]
entry = {"prefix": prefix, "logicalId": artifact.pin.logical_id, "artifactDigest": digest,
         "tables": table_entries(artifact.root["spec"]["tables"], members)}
v2 = {"format": "spicy-regs-publication", "version": 2, "families": {"documents": entry}}
for key, value in (("publication.v2.json", v2), ("publication.json", derive_v1(v2))):
    payload = canonical_json_bytes(value)
    parse_index(payload)
    (base / key).write_bytes(payload)
table = entry["tables"]["documents.parquet"]
print(json.dumps({"artifactDigest": digest, "members": len(table["members"]), "rows": table["rows"],
                  "byteSize": table["byteSize"], "writeSeconds": written_seconds, "buildSeconds": built_seconds,
                  "v1Families": list(derive_v1(v2)["families"])}))
shutil.rmtree(work.parent)
