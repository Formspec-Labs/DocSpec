"""Measure one retained state's ordered read, or its entity layer's native rewrite, at a thread count.

Run from the repository root under the shared resource watcher, one run at a time:

    watch.sh LOG CAP_GIB -- uv run --frozen python \
        docs/history/probes/2026-09-25-read-keys-not-payloads.py --memory-gib 6 --threads 1 RESULT.json
    watch.sh LOG CAP_GIB -- uv run --frozen python tools/with_iceberg.py uv run --frozen python \
        docs/history/probes/2026-09-25-read-keys-not-payloads.py --memory-gib 6 --threads 1 --rewrite RESULT.json

The workspace opens with ``create=False`` and the state opens at its expected
pin; nothing in it is derived, admitted or written. A read folds every
(member_key, occurrence_record) pair of ``CoreStateReader.batches()`` into one
SHA-256, length-prefixed, in delivered order, so equal fingerprints mean the
same rows, order and bytes; batch row counts fold into a second digest, so
equal digests also mean equal batch boundaries. A rewrite streams the entity
layer's exact bytes into a fresh store under the Iceberg fixture's TMPDIR, the
write path's wide sort. A thread samples the engine's temporary directory.
Set PYTHONPATH to another checkout's ``src`` to measure that code instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
import threading
import time
from pathlib import Path

WORKSPACE = "/Users/mikewolfd/Work/corpora/docspec-iceberg-reimport-2173b92-20260914/federal-register/workspace"
PIN = "sha256:b456349d6ec916a8f145ec9ab5ee39e95f5e13a0de70ca7220dfe1c6a5508a61"


def _tree_bytes(root):
    total = 0
    for directory, _, names in os.walk(root):
        for name in names:
            try:
                total += os.stat(os.path.join(directory, name)).st_size
            except FileNotFoundError:
                pass
    return total


class _SpillSampler(threading.Thread):
    """Peak bytes under the engine's temporary directory, sampled every 0.2 s."""

    def __init__(self, root):
        super().__init__(daemon=True)
        self.root, self.peak, self._done = root, 0, threading.Event()

    def run(self):
        while not self._done.wait(0.2):
            self.peak = max(self.peak, _tree_bytes(self.root))

    def stop(self):
        self._done.set()
        self.join()
        self.peak = max(self.peak, _tree_bytes(self.root))


def _engine(storage, threads):
    """Open a record store's lazy connection, apply any thread override, and return (threads, scratch dir)."""
    with storage._cursor() as cursor:
        if threads is not None:
            cursor.execute(f"SET threads = {int(threads)}")
        return cursor.execute("SELECT current_setting('threads')").fetchone()[0], storage._scratch.name


def _read(reader, result):
    rows = payload_bytes = batches = max_rows = max_batch_bytes = 0
    fingerprint, boundaries = hashlib.sha256(), hashlib.sha256()
    first = None
    reading = time.monotonic()
    for batch in reader.batches():
        if first is None:
            first = time.monotonic() - reading
        keys = batch.column("member_key").to_pylist()
        payloads = batch.column("occurrence_record").to_pylist()
        size = 0
        for key, payload in zip(keys, payloads, strict=True):
            encoded = key.encode("utf-8")
            fingerprint.update(len(encoded).to_bytes(8, "big"))
            fingerprint.update(encoded)
            fingerprint.update(len(payload).to_bytes(8, "big"))
            fingerprint.update(payload)
            size += len(payload)
        boundaries.update(batch.num_rows.to_bytes(8, "big"))
        rows += batch.num_rows
        payload_bytes += size
        batches += 1
        max_rows, max_batch_bytes = max(max_rows, batch.num_rows), max(max_batch_bytes, size)
    result.update({
        "read_seconds": time.monotonic() - reading, "first_batch_seconds": first,
        "rows": rows, "record_count": reader.record_count, "payload_bytes": payload_bytes,
        "batches": batches, "max_batch_rows": max_rows, "max_batch_payload_bytes": max_batch_bytes,
        "fingerprint": "sha256:" + fingerprint.hexdigest(), "batch_boundaries": "sha256:" + boundaries.hexdigest(),
    })


def _rewrite(workspace, reader, target, result):
    """Stream the state's entity rows, unordered with exact bytes, through a scratch store's native write path.

    This is the path a derive's encoded rows take: an incoming temporary table,
    a duplicate check, then INSERT ... ORDER BY record_identity over every payload.
    """
    from contextlib import closing

    from docspec.adapters.storage.core_entities import ENTITY_POLICY, ENTITY_SCHEMA
    from docspec.ports.record_storage import BATCH_ROWS, bounded_batches

    layer = reader._layers["entities"]
    started = time.monotonic()
    with workspace.records.relations({"entities": layer}) as relations, closing(relations["entities"].project(
            "record_identity, partition_value, record_json").to_arrow_reader(BATCH_ROWS)) as batches:
        written = target.retain_batches(bounded_batches(batches, byte_column="record_json"), layer_kind="core-entities",
                                        schema=ENTITY_SCHEMA, partition_policy=ENTITY_POLICY, ordered=False)
    result.update({"rewrite_seconds": time.monotonic() - started, "rows": written.reference.record_count,
                   "record_count": layer.reference.record_count})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("result")
    parser.add_argument("--workspace", default=WORKSPACE)
    parser.add_argument("--state", default="catalogue")
    parser.add_argument("--pin", default=PIN)
    parser.add_argument("--memory-gib", type=float, default=6)
    parser.add_argument("--threads", type=int, default=None, help="override the engine's default thread count")
    parser.add_argument("--rewrite", action="store_true",
                        help="write the entity layer into a scratch store under TMPDIR (run inside tools/with_iceberg.py)")
    args = parser.parse_args()

    from contextlib import ExitStack, closing

    import duckdb
    import docspec
    from docspec.adapters.storage.records import IcebergRecordStorage
    from docspec.runtime import CoreWorkspace

    # Identify the code actually imported (PYTHONPATH may name another checkout's src).
    package = Path(docspec.__file__).resolve().parent
    source = hashlib.sha256()
    for module in sorted(package.rglob("*.py")):
        source.update(module.relative_to(package).as_posix().encode() + b"\0" + module.read_bytes() + b"\0")
    repository = package.parents[1]
    commit = subprocess.run(["git", "-C", str(repository), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    result = {"mode": "rewrite" if args.rewrite else "read", "docspec_package": str(package),
              "docspec_source_sha256": source.hexdigest(), "docspec_head": commit, "duckdb": duckdb.__version__,
              "python": platform.python_version(), "machine": platform.machine(), "cpus": os.cpu_count(),
              "workspace": args.workspace, "state": args.state, "pin": args.pin,
              "engine_memory_bytes": int(args.memory_gib * 1024**3), "load_average_start": os.getloadavg()}
    started = time.monotonic()
    with CoreWorkspace(args.workspace, create=False, engine_memory_bytes=result["engine_memory_bytes"]) as workspace, \
            ExitStack() as stack:
        result["engine_threads"], scratch = _engine(workspace.records, args.threads)
        target = None
        if args.rewrite:
            target = stack.enter_context(closing(IcebergRecordStorage(
                Path(os.environ["TMPDIR"]) / "rewrite-records", engine_memory_bytes=result["engine_memory_bytes"])))
            _, scratch = _engine(target, args.threads)
        sampler = _SpillSampler(scratch)
        sampler.start()
        try:
            opened = time.monotonic()
            with workspace.open_state(args.state, expected_pin=args.pin or None) as reader:
                result["open_seconds"] = time.monotonic() - opened
                result["pin"] = reader.pin
                if target is None:
                    _read(reader, result)
                else:
                    _rewrite(workspace, reader, target, result)
        finally:
            sampler.stop()
            result["peak_spill_bytes"] = sampler.peak
    result["process_seconds"] = time.monotonic() - started
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result["peak_rss_bytes"] = peak if sys.platform == "darwin" else peak * 1024
    result["load_average_end"] = os.getloadavg()
    Path(args.result).write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    print("RESULT " + json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
