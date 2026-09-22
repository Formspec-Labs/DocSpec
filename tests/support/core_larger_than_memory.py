"""Two-copy Core qualification: derive, build, select, verify in fresh processes."""

from contextlib import closing
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import resource
import sys
from threading import Event, Thread
import time

import msgspec
import pyarrow.parquet as pq

from docspec.adapters.storage.engine import ENGINE_MEMORY_BYTES
from docspec.domain import core
from docspec.domain.identity import canonical_value_bytes, decode_canonical_json_value
from docspec.runtime import CoreWorkspace
from tests.support.core_bulk_experiment import connection
from tests.support.core_reference import selected_value
from tests.support.core_runtime_experiment import build, observations, retained_selection, storage_files
from tests.support.core_workload import CORE_BODY_BYTES, CORE_MEMBER_COUNT, core_value


SOURCE_SHA256 = "019402bb59cace267f58a3e6bdd8f89ca3860882b17be252f8962f7196180b23"
ENGINE_BYTES = ENGINE_MEMORY_BYTES
PROCESS_BYTES = 12 * 1024**3
BUILD_PROCESS_BYTES = 20 * 1024**3
STORAGE_BYTES = 80 * 1024**3
RECIPE = "core-bulk-v1-two-copy-v1"


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_pins():
    """Hash the recipe, lock, project config, production sources and support modules this trial pins."""

    paths = [Path(__file__), Path("uv.lock"), Path("pyproject.toml"),
             *sorted(Path("src/docspec").rglob("*.py")),
             *(Path("tests/support") / (name + ".py") for name in
               ("core_bulk_experiment", "core_runtime_experiment", "core_reference", "core_workload"))]
    return {str(path): file_digest(path) for path in paths}


def derive(directory, source, digest, count):
    """Duplicate the pinned source fixture into two copies.

    Refuses a count outside the frozen fixture, source bytes or a row count that
    differ from the supplied pin, and an already derived destination.
    """

    if not 0 < count <= CORE_MEMBER_COUNT:
        raise ValueError("source count must be within the frozen fixture")
    if file_digest(source) != digest or pq.ParquetFile(source).metadata.num_rows != count:
        raise ValueError("source bytes or row count differ from the supplied pin")
    destination = directory / "base.parquet"
    if destination.exists():
        raise FileExistsError(destination)
    with connection(directory / "scratch") as con:
        # Payload strings cross no Python decoder or encoder during duplication.
        con.execute("COPY (SELECT copy::VARCHAR || ':' || member_key AS member_key, "
                    "'urn:docspec:two-copy:' || copy::VARCHAR || ':' || occurrence_id AS occurrence_id, payload "
                    "FROM read_parquet($source) CROSS JOIN range(2) copies(copy)) TO $destination "
                    "(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 2048)", {"source": str(source), "destination": str(destination)})
    manifest = {"recipe": RECIPE, "source": str(source.resolve()), "source_sha256": digest,
                "source_rows": count, "copies": 2, "rows": count * 2,
                "body_bytes": count * 2 * CORE_BODY_BYTES,
                "fixture_sha256": file_digest(destination), "fixture_bytes": destination.stat().st_size,
                "source_pins": source_pins(), "engine_memory_bytes": ENGINE_BYTES,
                "process_target_bytes": PROCESS_BYTES, "build_process_target_bytes": BUILD_PROCESS_BYTES,
                "storage_target_bytes": STORAGE_BYTES}
    (directory / "fixture.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def selected(workspace):
    """Retain the whole-state two-copy selection and return its evidence."""

    with workspace.publisher.session() as session:
        value = retained_selection(workspace, session, "selected:two-copy", "root",
            core.StateMembers(member_selector=core.Whole(), material_keys=True))
        return msgspec.to_builtins(workspace.selections.evidence(session, value))


def verify(workspace, count):
    """Check every retained value against the independent fixture, after timing."""
    with workspace.publisher.session() as session:
        record = next(session.read_records([("selected_value", "selected:two-copy")]))[0].value
        checked = 0
        seen = bytearray((count * 2 + 7) // 8)
        with closing(workspace.selections.rows(session, record)) as rows:
            for key, entity, encoded in rows:
                copy, ordinal = key.split(":")
                assert copy in {"0", "1"} and key == f"{copy}:{int(ordinal):07d}"
                assert 0 <= int(ordinal) < count
                byte, bit = divmod(int(copy) * count + int(ordinal), 8)
                mask = 1 << bit
                assert not seen[byte] & mask, "duplicate copied member key"
                seen[byte] |= mask
                assert entity == f"urn:docspec:two-copy:{copy}:urn:docspec:fixture:occurrence:{int(ordinal)}"
                expected = ["present", selected_value(core_value(int(ordinal))), ["key", key]]
                assert canonical_value_bytes(decode_canonical_json_value(encoded)) == canonical_value_bytes(expected)
                checked += 1
        assert checked == count * 2, "copied member population is incomplete"
        return {"checked_rows": checked, "evidence": msgspec.to_builtins(workspace.selections.evidence(session, record))}


def run_stage(directory, stage, *, source=None, digest=SOURCE_SHA256, count=CORE_MEMBER_COUNT):
    """Run one qualification stage, refusing a directory whose source pins or derived fixture changed."""

    if stage == "derive":
        return derive(directory, source, digest, count)
    manifest = json.loads((directory / "fixture.json").read_text())
    if manifest["source_pins"] != source_pins():
        raise ValueError("recipe or implementation changed; use a new qualification directory")
    if stage == "build" and file_digest(directory / "base.parquet") != manifest["fixture_sha256"]:
        raise ValueError("derived fixture bytes changed")
    metrics = {}
    with CoreWorkspace(directory / "workspace", engine_memory_bytes=ENGINE_BYTES) as workspace:
        scratch = directory / "scratch"
        scratch.mkdir(exist_ok=True)
        workspace.records.merge_scratch_root = scratch
        with observations(workspace, metrics):
            if stage == "build":
                result = build(workspace, directory, metrics)
            elif stage == "select":
                result = {"evidence": selected(workspace)}
            else:
                result = verify(workspace, manifest["source_rows"])
    target = BUILD_PROCESS_BYTES if stage == "build" else PROCESS_BYTES
    return result | {"metrics": metrics, "fixture_sha256": manifest["fixture_sha256"],
                     "process_target_bytes": target, "body_bytes": manifest["body_bytes"],
                     "larger_than_process_target": manifest["body_bytes"] > target}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("derive", "build", "select", "verify"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--source-sha256", default=SOURCE_SHA256)
    parser.add_argument("--count", type=int, default=CORE_MEMBER_COUNT)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    receipt = args.directory / (args.stage + ".json")
    if receipt.exists():
        raise FileExistsError(receipt)
    pins = source_pins()
    peak = {"scratch_bytes": 0, "total_trial_bytes": 0}
    stopped = Event()
    def sample():
        scratch = total = 0
        staging_root = ".staging" in args.directory.parts
        for parts, size in storage_files(args.directory):
            total += size
            scratch += size if "scratch" in parts or ".staging" in parts or staging_root else 0
        peak["scratch_bytes"] = max(peak["scratch_bytes"], scratch)
        peak["total_trial_bytes"] = max(peak["total_trial_bytes"], total)
    def monitor():
        while not stopped.is_set():
            sample()
            stopped.wait(0.25)
    worker = Thread(target=monitor, daemon=True)
    worker.start()
    start = time.perf_counter()
    try:
        result = run_stage(args.directory, args.stage, source=args.source, digest=args.source_sha256, count=args.count)
    except Exception as error:
        result = {"error": {"type": type(error).__name__, "message": str(error)}}
    finally:
        stopped.set()
        worker.join()
        sample()
    result.update(stage=args.stage, elapsed_seconds=time.perf_counter() - start,
                  peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
                  sampled_peak=peak, sample_interval_seconds=0.25, source_pins=pins,
                  sources_unchanged_during_run=source_pins() == pins, platform=platform.platform(),
                  python=platform.python_version(), versions={name: importlib.metadata.version(name) for name in
                      ("docspec", "duckdb", "pyarrow", "msgspec", "jsonschema-rs")},
                  engine_memory_bytes=ENGINE_BYTES, engine_threads=1,
                  limitations="Storage peaks are sampled, not hard bounds. Fresh process does not imply cold OS cache. Derive/build include input hashing; verify is a separate oracle process.")
    receipt.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
    if "error" in result or not result["sources_unchanged_during_run"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
