"""Real C25 writer/readers and cleanup/publication races over the Core owners.

Build the frozen fixture with core_runtime_experiment first, then run this module
against that directory. Defaults are 100 batches of 1,024 changed members and four
reader processes. Small smoke runs change only the two writer dimensions.
"""

from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import argparse
import hashlib
import importlib.metadata
import json
from math import ceil
import multiprocessing
import os
import platform
import queue
import resource
import sys
import time
import traceback

import msgspec

from docspec.adapters.storage.engine import ENGINE_MEMORY_BYTES
from docspec.domain import core
from docspec.domain.identity import decode_canonical_json_value
from docspec.domain.references import BlobRef
from docspec.errors import IntegrityError, StaleBaseError, StateTransitionError
from docspec.ports.core_ledger import MetadataBatch, RemovalContent
from docspec.runtime import CoreWorkspace
from tests.support.core_reference import selected_value, value_key
from tests.support.core_runtime_experiment import count_fixture, observations, revised
from tests.support.core_workload import CORE_MEMBER_COUNT, core_value


FIELDS = core.JsonFields(selectors=(core.Field(label="title", pointer="/title"), core.Field(label="url", pointer="/url")))
FIELD_RECORDS = [{"label": item.label, "pointer": item.pointer} for item in FIELDS.selectors]


def _rss():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)


def _quantile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, ceil(len(ordered) * fraction) - 1))] if ordered else None


def _entry(target, role, reports, arguments):
    started = time.perf_counter()
    try:
        result = target(*arguments)
    except BaseException as error:
        result = {"error": {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}}
    result.update(role=role, pid=os.getpid(), elapsed_seconds=time.perf_counter() - started, peak_rss_bytes=_rss())
    reports.put(result)


def _collect(processes, reports, *, timeout):
    results = []
    deadline = time.monotonic() + timeout
    try:
        for process in processes:
            process.start()
        while len(results) < len(processes):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("contention probe exceeded its declared process deadline")
            try:
                result = reports.get(timeout=min(.25, remaining))
            except queue.Empty:
                if all(process.exitcode is not None for process in processes):
                    raise RuntimeError("worker exited without a complete receipt") from None
                continue
            results.append(result)
            if "error" in result:
                raise RuntimeError(json.dumps(result))
        for process in processes:
            process.join(timeout=max(0, deadline - time.monotonic()))
            assert process.exitcode == 0, (process.name, process.exitcode)
        return results
    finally:
        for process in processes:
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)


def _selected_rows(workspace, state, width):
    selected = core.SelectedValue(format_version=1, selected_value_id="contention:read",
        definition=core.StateMembers(member_selector=FIELDS,
            scope=tuple(f"{i:07d}" for i in range(width)) + ("missing",), material_keys=True),
        origin=core.Origin(parent_entity_id=state), value=core.FromParent())
    with workspace.publisher.session() as session, closing(workspace.selections.rows(session, selected)) as rows:
        yield from rows


def _check_named(rows, state, expected, expected_ids=None):
    generation = -1 if state == "root" else int(state.rsplit(":", 1)[1])
    count = 0
    for key, identity, encoded in rows:
        if key == "missing":
            value = ["absent", ["key", key]]
            assert identity is None
        else:
            fields = expected[int(key)].copy()
            if generation >= 0:
                fields["title"] = f"contention-{generation}-{key}"
            value = ["present", selected_value(fields, FIELD_RECORDS), ["key", key]]
            assert identity is not None
            if expected_ids is not None:
                assert identity == expected_ids[key]
        assert value_key(decode_canonical_json_value(encoded)) == value_key(value)
        count += 1
    assert count == len(expected) + 1
    return count


def _expected(width):
    return [{field: value[field] for field in ("title", "url")} for value in (core_value(i) for i in range(width))]


def _writer(path, batches, width, barrier, readers_started, stopped, acknowledged, acknowledgements):
    metrics, receipts = {}, []
    ids = {f"{i:07d}": f"urn:docspec:fixture:occurrence:{i}" for i in range(width)}
    base = "root"
    try:
        with CoreWorkspace(path) as workspace, observations(workspace, metrics), acknowledgements.open("x") as output:
            barrier.wait(timeout=60)
            assert all(event.wait(60) for event in readers_started), "reader never started"
            for index in range(batches):
                start = time.perf_counter()
                state = f"contention:{index}"
                with workspace.publisher.session() as session:
                    ids.update(revised(workspace, session, base=base, state=state,
                        changes=((key, entity, "/title", f"contention-{index}-{key}") for key, entity in ids.items())))
                    workspace.maintenance.select_current(state + ":current", "contention", ("state", state), ("state", base))
                    try:
                        workspace.maintenance.select_current(state + ":stale", "contention", ("state", "root"), ("state", base))
                    except StaleBaseError:
                        pass
                    else:
                        raise AssertionError("stale current update was accepted")
                receipt = {"batch": index, "state": state, "changed_members": width,
                    "seconds": time.perf_counter() - start, "stale_update_refused": True}
                output.write(json.dumps(receipt) + "\n")
                output.flush()
                os.fsync(output.fileno())
                receipts.append(receipt)
                with acknowledged.get_lock():
                    acknowledged.value = index
                base = state
        return {"batches": receipts, "changed_members": batches * width, "metrics": metrics}
    finally:
        stopped.set()


def _reader(path, width, barrier, started, stopped, acknowledged):
    expected = _expected(width)
    metadata_times, named_times, behind, versions = [], [], [], set()
    metrics = {}
    with CoreWorkspace(path) as workspace, observations(workspace, metrics):
        barrier.wait(timeout=60)
        started.set()
        while not stopped.is_set() or not metadata_times:
            start = time.perf_counter()
            current = workspace.ledger.current("contention")
            assert current is not None and current[0] == "state"
            state = current[1]
            record = workspace.inspect("state", state, progress_limit=0)
            assert record["available"] and record["retained"]
            metadata_times.append(time.perf_counter() - start)
            start = time.perf_counter()
            with closing(_selected_rows(workspace, state, width)) as rows:
                # Include actual decoding and consumption in named-read latency.
                _check_named(rows, state, expected)
            named_times.append(time.perf_counter() - start)
            generation = -1 if state == "root" else int(state.rsplit(":", 1)[1])
            with acknowledged.get_lock():
                behind.append(max(0, acknowledged.value - generation))
            versions.add(state)
    return {"metadata_seconds": metadata_times, "named_seconds": named_times,
        "metadata_p95_seconds": _quantile(metadata_times, .95), "named_p95_seconds": _quantile(named_times, .95),
        "maximum_backlog_batches": max(behind), "observed_states": sorted(versions), "metrics": metrics}


def contention(directory, *, batches=100, changed_members=1024, timeout=600):
    directory = Path(directory)
    fixture_members = count_fixture(directory)
    if min(batches, changed_members) <= 0 or changed_members > fixture_members:
        raise ValueError("positive batch dimensions must fit the frozen fixture")
    path, acknowledgements = directory / "workspace", directory / "contention-acknowledgements.jsonl"
    assert not acknowledgements.exists(), "use a fresh trial for another contention run"
    with CoreWorkspace(path) as workspace:
        workspace.maintenance.select_current("contention:initial", "contention", ("state", "root"), None)
    context = multiprocessing.get_context("spawn")
    reports, barrier, stopped = context.Queue(), context.Barrier(5), context.Event()
    readers_started = [context.Event() for _ in range(4)]
    acknowledged = context.Value("i", -1)
    work = [("writer", _writer, (path, batches, changed_members, barrier, readers_started, stopped, acknowledged, acknowledgements))]
    work.extend((f"reader:{index}", _reader, (path, changed_members, barrier, readers_started[index], stopped, acknowledged)) for index in range(4))
    processes = [context.Process(target=_entry, args=(target, role, reports, args), name=role) for role, target, args in work]
    started = time.perf_counter()
    result = _collect(processes, reports, timeout=timeout)
    concurrent_seconds = time.perf_counter() - started
    expected = _expected(changed_members)
    reconciled = 0
    prior_state = "root"
    prior_ids = {f"{i:07d}": f"urn:docspec:fixture:occurrence:{i}" for i in range(changed_members)}
    with CoreWorkspace(path) as workspace, acknowledgements.open() as receipts:
        for line in receipts:
            receipt = json.loads(line)
            assert receipt["batch"] == reconciled and receipt["changed_members"] == changed_members
            with closing(workspace.ledger.read_records([("revision", receipt["state"] + ":revision")])) as records:
                revision = next(records)[0].value
            assert isinstance(revision, core.Revision) and revision.base_state_id == prior_state
            assert len(revision.edits) == len(revision.value_edits) == changed_members
            evidence = {edit.result_occurrence_id: edit for edit in revision.value_edits}
            current_ids = {}
            for put in revision.edits:
                edit = evidence[put.occurrence_id]
                assert edit.source_occurrence_id == prior_ids[put.member_key]
                assert edit.result_occurrence_id != edit.source_occurrence_id
                assert edit.patch == ({"op": "replace", "path": "/title", "value": f"contention-{reconciled}-{put.member_key}"},)
                current_ids[put.member_key] = put.occurrence_id
            with closing(_selected_rows(workspace, receipt["state"], changed_members)) as rows:
                _check_named(rows, receipt["state"], expected, current_ids)
            prior_ids, prior_state = current_ids, receipt["state"]
            reconciled += 1
        assert reconciled == batches
        assert workspace.ledger.current("contention") == ("state", f"contention:{batches - 1}")
    elapsed = time.perf_counter() - started
    readers = [row for row in result if row["role"].startswith("reader:")]
    parent_peak = _rss()
    summed_peak = sum(row["peak_rss_bytes"] for row in result) + parent_peak
    return {"fixture_members": fixture_members, "full_C01_dimensions": fixture_members == CORE_MEMBER_COUNT and batches == 100 and changed_members == 1024,
        "workers": result, "concurrent_seconds": concurrent_seconds, "elapsed_including_reconciliation_seconds": elapsed,
        "acknowledged_batches_reconciled": reconciled, "reconciled_value_edits": reconciled * changed_members,
        "changed_members_per_batch": changed_members,
        "process_count": len({row["pid"] for row in result}), "parent_peak_rss_bytes": parent_peak,
        "sum_individual_peak_rss_bytes": summed_peak,
        "targets": {"elapsed_600s": elapsed <= 600, "each_process_2gib": parent_peak <= 2 * 1024**3 and all(row["peak_rss_bytes"] <= 2 * 1024**3 for row in result),
            "sum_peak_upper_bound_8gib": summed_peak <= 8 * 1024**3,
            "reader_metadata_p95_5s": all(row["metadata_p95_seconds"] <= 5 for row in readers)}}


def _removal(content):
    return RemovalContent("blobs", BlobRef(content.locator, content.digest, content.byte_size, content.media_type))


def _race_publisher(path, shared, staged, attempted, published, deleting, reverse_attempted):
    with CoreWorkspace(path) as workspace:
        with workspace.publisher.session() as session:
            fresh = session.retain_bytes([b"new publication bytes"])
            staged.put(msgspec.to_builtins(fresh))
            assert attempted.wait(30), "cleanup did not attempt removal"
            entities = (core.Entity(format_version=1, entity_id="shared-copy", entity_type="artifact", value=shared),
                core.Entity(format_version=1, entity_id="new", entity_type="artifact", value=fresh))
            session.publish(MetadataBatch("race:publish", records=entities, retained=(("entity", "shared-copy"), ("entity", "new"))))
        published.set()
        assert deleting.wait(30), "cleanup did not enter deletion"
        try:
            with workspace.publisher.session():
                raise AssertionError("publication entered during exclusive cleanup")
        except StateTransitionError:
            reverse_attempted.set()
        return {"publication_during_cleanup_refused": True, "shared_and_new_published": True}


def _race_cleanup(path, shared, orphan, staged, attempted, published, deleting, reverse_attempted):
    fresh = msgspec.convert(staged.get(timeout=30), type=core.ContentRef)
    with CoreWorkspace(path) as workspace:
        try:
            workspace.maintenance.remove_under_policy("race:inflight", "race:policy", orphan_content=[_removal(fresh)])
        except StateTransitionError:
            assert workspace.ledger.removal("race:inflight") is None
        else:
            raise AssertionError("cleanup entered an in-flight publication")
        attempted.set()
        assert published.wait(30), "publication did not finish"
        for identity, content in (("shared", shared), ("new", fresh)):
            try:
                workspace.maintenance.remove_under_policy("race:retained:" + identity, "race:policy", orphan_content=[_removal(content)])
            except IntegrityError:
                assert workspace.ledger.removal("race:retained:" + identity) is None
            else:
                raise AssertionError("cleanup removed retained publication content")
        original = workspace.blobs.delete
        def interrupted_delete(reference):
            deleting.set()
            assert reverse_attempted.wait(30), "publication did not try the exclusive cleanup window"
            original(reference)
            raise OSError("injected interruption after physical deletion")
        try:
            with patch.object(workspace.blobs, "delete", interrupted_delete):
                workspace.maintenance.remove_under_policy("race:orphan", "race:policy", orphan_content=[_removal(orphan)])
        except OSError:
            assert workspace.ledger.removal("race:orphan")[2] is False
        else:
            raise AssertionError("removal interruption was not observed")
    with CoreWorkspace(path) as workspace:
        recovered = workspace.maintenance.resume("race:orphan")
        assert recovered == {"absent": 1} and workspace.ledger.removal("race:orphan")[2]
        for content in (shared, fresh):
            workspace.blobs.verify(_removal(content).reference)
        try:
            workspace.blobs.stat(_removal(orphan).reference)
        except IntegrityError:
            pass
        else:
            raise AssertionError("removed orphan remains physically available")
    return {"inflight_cleanup_refused": True, "retained_shared_and_new_survive": True,
        "interrupted_removal_recovered": recovered}


def cleanup_race(directory, *, timeout=120):
    path = Path(directory) / "cleanup-race-workspace"
    assert not path.exists(), "use a fresh cleanup race workspace"
    with CoreWorkspace(path) as workspace, workspace.publisher.session() as session:
        shared = session.retain_bytes([b"shared retained bytes"])
        orphan = session.retain_bytes([b"unreferenced removable bytes"])
        entity = core.Entity(format_version=1, entity_id="shared", entity_type="artifact", value=shared)
        policy = core.RetentionPolicy(format_version=1, policy_id="race:policy", description={"remove": [], "collect_unreferenced": True})
        session.publish(MetadataBatch("race:setup", records=(entity, policy), retained=(("entity", "shared"), ("retention_policy", "race:policy"))))
    context = multiprocessing.get_context("spawn")
    reports, staged = context.Queue(), context.Queue()
    attempted, published, deleting, reverse_attempted = (context.Event() for _ in range(4))
    work = [("publication", _race_publisher, (path, shared, staged, attempted, published, deleting, reverse_attempted)),
        ("cleanup", _race_cleanup, (path, shared, orphan, staged, attempted, published, deleting, reverse_attempted))]
    processes = [context.Process(target=_entry, args=(target, role, reports, args), name=role) for role, target, args in work]
    return {"workers": _collect(processes, reports, timeout=timeout), "physical_guards_exercised": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("contention", "cleanup", "run"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--batches", type=int, default=100)
    parser.add_argument("--changed-members", type=int, default=1024)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    source_files = [Path(__file__), *(Path(sys.modules[name].__file__) for name in (
        "tests.support.core_runtime_experiment", "tests.support.core_bulk_experiment",
        "tests.support.core_workload", "tests.support.core_reference"))]
    source_hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}
    started = time.perf_counter()
    try:
        result = {}
        if args.stage in {"contention", "run"}:
            result["contention"] = contention(args.directory, batches=args.batches, changed_members=args.changed_members, timeout=args.timeout)
        if args.stage in {"cleanup", "run"}:
            result["cleanup"] = cleanup_race(args.directory, timeout=args.timeout)
    except Exception as error:
        result = {"error": {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}}
    result.update(stage=args.stage, requested_batches=args.batches, requested_changed_members=args.changed_members,
        process_deadline_seconds=args.timeout, source_sha256=source_hashes,
        sources_unchanged_during_run=all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in source_hashes.items()),
        elapsed_seconds=time.perf_counter() - started, parent_peak_rss_bytes=_rss(),
        platform=platform.platform(), python=platform.python_version(),
        versions={name: importlib.metadata.version(name) for name in ("docspec", "duckdb", "pyarrow", "msgspec", "jsonschema-rs")},
        engine_threads=1, engine_memory_bytes=ENGINE_MEMORY_BYTES,
        limitations=["Sum of individual process RSS peaks is a conservative upper bound, not a simultaneous sample.",
            "Named-read latency includes consuming and independently checking returned fields.",
            "A fresh process does not establish a cold operating-system cache."])
    args.directory.mkdir(parents=True, exist_ok=True)
    output = args.directory / ("contention-" + args.stage + "-report.json")
    with output.open("x") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
    if "error" in result or ("contention" in result and not all(result["contention"]["targets"].values())):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
