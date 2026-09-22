"""Reproducible C03 extraction/encoding experiment, not a second runtime.

Run each stage in a fresh process to give it an independent RSS measurement:
  uv run --no-sync python -m tests.support.core_bulk_experiment generate /tmp/core-c03
  uv run --no-sync python -m tests.support.core_bulk_experiment fields /tmp/core-c03
  uv run --no-sync python -m tests.support.core_bulk_experiment whole /tmp/core-c03
  uv run --no-sync python -m tests.support.core_bulk_experiment named /tmp/core-c03

Generation writes the fixed workload as bounded Parquet row groups. Evaluation
extracts JSON values in DuckDB, converts only the selected values, encodes with
the shared codec, and sorts retained member bytes with DuckDB before streaming
the framed digest. Engine JSON bytes never become canonical identity bytes.
The fixture mapping and codec tests qualify semantics independently. This recipe
does not yet qualify revisions, publication, retention, or the final Core API.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import resource
import sys
from threading import Event, Thread
import time
import tracemalloc

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from docspec.adapters.storage.batches import encoded_batches
from docspec.adapters.storage.engine import ENGINE_MEMORY_BYTES, ENGINE_THREADS, connect
from docspec.adapters.storage.selection import encoded_members, field_paths
from docspec.domain.core_encoding import member_stream_evidence
from docspec.domain.identity import canonical_value_bytes
from docspec.errors import IntegrityError
from tests.support.core_workload import CORE_MEMBER_COUNT, core_members


SOURCE_SCHEMA = pa.schema([
    ("member_key", pa.string()), ("occurrence_id", pa.string()), ("payload", pa.string()),
])
SELECTED_SCHEMA = pa.schema([("encoded", pa.binary())])
FIELDS = (("url", "/url"), ("metadata", "/metadata/field"))


@contextmanager
def connection(scratch: Path):
    """Yield the experiment's bounded DuckDB connection over ``scratch``."""

    scratch.mkdir(parents=True, exist_ok=True)
    con = connect(scratch, memory_bytes=ENGINE_MEMORY_BYTES, threads=ENGINE_THREADS)
    try:
        yield con
    finally:
        con.close()


def write_rows(path: Path, rows: Iterable[tuple], schema: pa.Schema, *, byte_column: int) -> dict:
    """At most one pending batch, limited by encoded value bytes and row count."""
    if path.exists():
        raise FileExistsError(path)
    count = total = peak = 0
    with pq.ParquetWriter(path, schema, compression="zstd", use_dictionary=False) as writer:
        for batch in encoded_batches(rows, schema, byte_column=byte_column):
            writer.write_batch(batch)
            size = pc.sum(pc.binary_length(batch.column(byte_column))).as_py()
            count += batch.num_rows
            total += size
            peak = max(peak, size)
    return {"rows": count, "encoded_bytes": total, "largest_write_batch_bytes": peak}


def generate(directory: Path, count: int) -> dict:
    """Write the frozen workload as bounded Parquet row groups; an existing file is refused."""

    directory.mkdir(parents=True, exist_ok=True)
    rows = (
        (key, entity, canonical_value_bytes(value).decode("utf-8"))
        for key, entity, value in core_members(count)
    )
    return {
        **write_rows(directory / "base.parquet", rows, SOURCE_SCHEMA, byte_column=2),
        "shared_encoder_calls": count,
    }


def selected_rows(
    con: duckdb.DuckDBPyConnection, source: Path, *,
    fields: Sequence[tuple[str, str]] | None = FIELDS,
    scope: Sequence[str] | None = None, material_keys: bool = False,
    material_entities: bool = False, read_rows: int = 256, metrics: dict | None = None,
) -> Iterator[tuple[bytes]]:
    """Run the production extraction owner over the experiment's admitted file."""
    field_paths(fields)
    if scope is not None and len(set(scope)) != len(scope):
        raise IntegrityError("duplicate requested member key")
    relation = con.sql("SELECT * FROM read_parquet(?)", params=[str(source)]) if scope is None else con.sql(
        "SELECT member_key, occurrence_id, payload FROM (SELECT unnest(?::VARCHAR[]) member_key) wanted "
        "LEFT JOIN read_parquet(?) USING (member_key)", params=[list(scope), str(source)])
    for _, _, encoded in encoded_members(relation, fields=fields, material_keys=material_keys,
                                        material_entities=material_entities, read_rows=read_rows, metrics=metrics):
        yield (encoded,)


def framed_digest(con: duckdb.DuckDBPyConnection, source: Path, *, read_rows: int = 256) -> dict:
    """Multiset comparison retains duplicate rows; no whole-state aggregate.

    The production Core encoder owns framing; the experiment measures its caller.
    The canonical JSON preimage is ["docspec-selected-members",1,[...rows...]].
    Each row has already passed the one canonical encoder.
    """
    count = total = peak = 0
    reader = con.execute("SELECT encoded FROM read_parquet(?) ORDER BY encoded", [str(source)]).to_arrow_reader(read_rows)

    def rows():
        nonlocal count, total, peak
        for batch in reader:
            peak = max(peak, batch.nbytes)
            for scalar in batch.column(0):
                encoded = scalar.as_py()
                count += 1
                total += len(encoded)
                yield encoded

    try:
        evidence = member_stream_evidence(rows())
    finally:
        reader.close()
    return {"digest": evidence.digest, "rows": count,
            "encoded_bytes": total, "largest_sorted_arrow_batch_bytes": peak}


def evaluate(directory: Path, mode: str, *, read_rows: int = 256) -> dict:
    """Extract, convert and digest one mode, asserting extraction and comparison agree."""

    metrics = {}
    selected = directory / f"{mode}.parquet"
    with connection(directory / "scratch") as con:
        started = time.perf_counter()
        writing = write_rows(
            selected, selected_rows(
                con, directory / "base.parquet", fields=None if mode == "whole" else FIELDS,
                scope=[f"{i:07d}" for i in range(1024)] + ["missing"] if mode == "named" else None,
                material_keys=mode == "whole", read_rows=read_rows, metrics=metrics,
            ), SELECTED_SCHEMA, byte_column=0,
        )
        extraction_seconds = time.perf_counter() - started
        started = time.perf_counter()
        comparison = framed_digest(con, selected, read_rows=read_rows)
        comparison_seconds = time.perf_counter() - started
    assert writing["rows"] == comparison["rows"] and writing["encoded_bytes"] == comparison["encoded_bytes"]
    return {"extraction": writing | metrics | {"seconds": extraction_seconds},
            "comparison": comparison | {"seconds": comparison_seconds}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("generate", "fields", "whole", "named"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--count", type=int, default=CORE_MEMBER_COUNT)
    parser.add_argument("--read-rows", type=int, default=256)
    parser.add_argument("--trace-allocations", action="store_true")
    parser.add_argument("--compare-only", action="store_true")
    args = parser.parse_args()
    suffix = f"-comparison-{ENGINE_MEMORY_BYTES // 1024**3}g-{ENGINE_THREADS}t" if args.compare_only else ""
    receipt_path = args.directory / f"{args.stage}{suffix}.json"
    if receipt_path.exists():
        raise FileExistsError(receipt_path)
    sampled_storage = {"scratch_bytes": 0, "retained_bytes": 0}
    stopped = Event()

    def sample_storage():
        while not stopped.is_set():
            scratch = retained = 0
            for path in args.directory.rglob("*"):
                try:
                    if path.is_file():
                        if "scratch" in path.relative_to(args.directory).parts:
                            scratch += path.stat().st_size
                        else:
                            retained += path.stat().st_size
                except FileNotFoundError:
                    pass  # Native spill files can disappear between observations.
            sampled_storage["scratch_bytes"] = max(sampled_storage["scratch_bytes"], scratch)
            sampled_storage["retained_bytes"] = max(sampled_storage["retained_bytes"], retained)
            stopped.wait(0.1)

    monitor = Thread(target=sample_storage, daemon=True)
    monitor.start()
    if args.trace_allocations:
        tracemalloc.start(1)
    started = time.perf_counter()
    try:
        if args.compare_only:
            with connection(args.directory / "scratch") as con:
                result = {"comparison": framed_digest(con, args.directory / f"{args.stage}.parquet",
                                                     read_rows=args.read_rows)}
        else:
            result = generate(args.directory, args.count) if args.stage == "generate" else evaluate(
                args.directory, args.stage, read_rows=args.read_rows,
            )
    except Exception as error:
        result = {"error": {"type": type(error).__name__, "message": str(error)}}
    finally:
        stopped.set()
        monitor.join()
    result.update(
        stage=args.stage, compare_only=args.compare_only, elapsed_seconds=time.perf_counter() - started,
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
        platform=platform.platform(), python=platform.python_version(),
        versions={name: importlib.metadata.version(name) for name in ("duckdb", "pyarrow", "msgspec", "jsonschema-rs")},
        engine_memory_bytes=ENGINE_MEMORY_BYTES, engine_threads=ENGINE_THREADS,
        sampled_peak_storage_bytes=sampled_storage, storage_sample_interval_seconds=0.1,
        traced_python_peak_bytes=tracemalloc.get_traced_memory()[1] if args.trace_allocations else None,
        source_sha256={str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (
            Path(__file__).relative_to(Path.cwd()), Path("tests/support/core_workload.py"),
            Path("src/docspec/domain/core_encoding.py"), Path("src/docspec/domain/identity.py"),
            Path("src/docspec/adapters/storage/engine.py"),
            Path("uv.lock"),
        )},
        retained_file_bytes=sum(path.stat().st_size for path in args.directory.glob("*.parquet")),
        limitations="C03 extraction/encoding only; no runtime publication, retention, or full qualification",
    )
    receipt_path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
    if "error" in result:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
