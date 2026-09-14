"""C25 production Core probe; run each stage in a fresh process.

Example: python -m tests.support.core_runtime_experiment generate /tmp/core-c25 --count 64
Then run build, open, fields, named, whole, edits, history, checkpoint and audit.
The full fixture defaults to 1,048,576 members, 1,024 edits and 1,000 revisions.
Measurements describe the actual production owners, including failed targets.
"""

from contextlib import ExitStack, closing, contextmanager
from functools import wraps
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch
import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import resource
import re
import stat
import sys
import time
import tracemalloc

import duckdb
import msgspec
import pyarrow.parquet as pq

from docspec.adapters.storage.engine import ENGINE_MEMORY_BYTES
from docspec.application.core_edits import VALUE_EDIT_BATCH_ROWS, prepare_revision, prepare_value_edits
from docspec.domain import core
from docspec.domain.identity import canonical_value_bytes, decode_canonical_json_value
from docspec.ports.record_storage import bounded_rows
from docspec.ports.core_ledger import MetadataBatch
from docspec.runtime import CoreWorkspace
from tests.support.core_bulk_experiment import generate
from tests.support.core_reference import selected_value, value_key
from tests.support.core_workload import CORE_MEMBER_COUNT, core_value


FIELDS = core.JsonFields(selectors=(core.Field(label="url", pointer="/url"), core.Field(label="metadata", pointer="/metadata/field")))
URL = core.JsonFields(selectors=(core.Field(label="url", pointer="/url"),))


class QueryProfiles:
    """Observe every actual relation materialization, including empty passes."""
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.count = 0
        self.profiles = []
        self.collected = set()

    def begin(self, cursor):
        self.count += 1
        path = self.directory / f"query-{self.count:06d}.json"
        while path.exists():
            self.count += 1
            path = self.directory / f"query-{self.count:06d}.json"
        cursor.execute("PRAGMA enable_profiling='json'")
        cursor.execute("SET profiling_output = ?", [str(path)])
        return path

    def collect(self, path):
        if path is not None and path.exists() and path not in self.collected:
            self.collected.add(path)
            profile = json.loads(path.read_text())
            scans = []
            def visit(node):
                if "PARQUET" in node.get("operator_name", "") or "PARQUET" in node.get("extra_info", {}).get("Function", ""):
                    scans.append({key: node.get(key) for key in ("operator_name", "operator_cardinality", "operator_rows_scanned", "extra_info")})
                for child in node.get("children", []):
                    visit(child)
            visit(profile)
            self.profiles.append({"path": path.name, "total_bytes_read": profile.get("total_bytes_read"),
                "rows_returned": profile.get("rows_returned"),
                "query_sha256": hashlib.sha256((profile.get("query_name") or "").encode()).hexdigest(),
                "query_files": sorted(set(re.findall(r"[0-9a-f]{64}\.parquet", profile.get("query_name") or ""))),
                "scans": scans})

    def wrap(self, value, cursor):
        return _Relation(value, cursor, self) if isinstance(value, duckdb.DuckDBPyRelation) else value


class _Reader:
    def __init__(self, reader, profiles, path):
        self.reader, self.profiles, self.path = reader, profiles, path
        self.closed = False
    def __iter__(self):
        return self
    def __next__(self):
        return next(self.reader)
    def __getattr__(self, name):
        return getattr(self.reader, name)
    def close(self):
        if not self.closed:
            self.closed = True
            self.reader.close()
            self.profiles.collect(self.path)
    def __enter__(self):
        return self
    def __exit__(self, *_):
        self.close()


class _Relation:
    def __init__(self, relation, cursor, profiles):
        self.relation, self.cursor, self.profiles = relation, cursor, profiles
    def __getattr__(self, name):
        attribute = getattr(self.relation, name)
        if not callable(attribute):
            return attribute
        def invoke(*args, **kwargs):
            args = tuple(value.relation if isinstance(value, _Relation) else value for value in args)
            materialized = name in {"to_arrow_reader", "fetchall", "fetchone", "to_arrow_table"}
            path = self.profiles.begin(self.cursor) if materialized else None
            result = attribute(*args, **kwargs)
            if name == "to_arrow_reader":
                return _Reader(result, self.profiles, path)
            if materialized:
                self.profiles.collect(path)
            return self.profiles.wrap(result, self.cursor)
        return invoke


class _Cursor:
    def __init__(self, cursor, profiles):
        self.cursor, self.profiles = cursor, profiles
        self.pending = None
    def finish(self):
        self.cursor.close()
        self.profiles.collect(self.pending)
    def __getattr__(self, name):
        attribute = getattr(self.cursor, name)
        if not callable(attribute):
            return attribute
        def invoke(*args, **kwargs):
            previous = self.pending
            path = self.profiles.begin(self.cursor) if name in {"sql", "execute"} else None
            if path is not None:
                self.profiles.collect(previous)
                self.pending = path
            result = attribute(*args, **kwargs)
            if path is not None or name in {"fetchone", "fetchall", "fetchmany", "close"}:
                self.profiles.collect(self.pending)
            if result is self.cursor:
                return self
            return self.profiles.wrap(result, self.cursor)
        return invoke


def _unobserved(function):
    return getattr(function, "_core_probe_original", function)


@contextmanager
def observations(workspace, metrics, profiles=None):
    """Instrumentation changes no returned rows, selection definitions or queries."""
    from docspec.adapters.storage import core_selections
    from docspec.domain import identity
    original_cursor, original_open = workspace.records._cursor, workspace.ledger._open
    # Nested clean-workspace observations own their global gateway calls once.
    extracted, encode_member = _unobserved(core_selections.extracted_rows), _unobserved(core_selections.member_bytes)
    encode = _unobserved(identity._artifact_canonical_json_bytes)
    decode = _unobserved(identity._artifact_parse_canonical_json)
    blob_read, blob_range, blob_verify = workspace.blobs.read, workspace.blobs.read_range, workspace.blobs.verify
    metrics.update(sqlite_statements=0, sqlite_transactions=0, python_fsync_calls=0, python_fsync_seconds=0.0,
                   extraction={}, selection_member_encoder_calls=0, selection_member_encoded_bytes=0,
                   canonical_encoder_calls=0, canonical_encoded_bytes=0,
                   canonical_decoder_calls=0, canonical_decoded_bytes=0, blob_reads_by_media_type={})
    original_fsync = _unobserved(os.fsync)
    @wraps(original_fsync)
    def fsync(descriptor):
        start = time.perf_counter()
        try:
            return original_fsync(descriptor)
        finally:
            metrics["python_fsync_calls"] += 1
            metrics["python_fsync_seconds"] += time.perf_counter() - start
    def trace(statement):
        metrics["sqlite_statements"] += 1
        metrics["sqlite_transactions"] += statement.startswith("BEGIN")
    def opened(*args, **kwargs):
        connection = original_open(*args, **kwargs)
        connection.set_trace_callback(trace)
        return connection
    @wraps(extracted)
    def extraction(*args, **kwargs):
        kwargs["metrics"] = metrics["extraction"]
        yield from extracted(*args, **kwargs)
    @wraps(encode_member)
    def member_bytes(*args, **kwargs):
        encoded = encode_member(*args, **kwargs)
        metrics["selection_member_encoder_calls"] += 1
        metrics["selection_member_encoded_bytes"] += len(encoded)
        return encoded
    @wraps(encode)
    def canonical_encode(*args, **kwargs):
        encoded = encode(*args, **kwargs)
        metrics["canonical_encoder_calls"] += 1
        metrics["canonical_encoded_bytes"] += len(encoded)
        return encoded
    @wraps(decode)
    def canonical_decode(data, *args, **kwargs):
        decoded = decode(data, *args, **kwargs)
        metrics["canonical_decoder_calls"] += 1
        metrics["canonical_decoded_bytes"] += len(data)
        return decoded
    def blob_metrics(reference):
        return metrics["blob_reads_by_media_type"].setdefault(reference.media_type,
            dict(stream_calls=0, returned_chunks=0, returned_bytes=0, range_calls=0,
                 range_returned_bytes=0, verified_calls=0, verified_bytes=0))
    def read(reference, *args, **kwargs):
        counters = blob_metrics(reference)
        counters["stream_calls"] += 1
        with closing(blob_read(reference, *args, **kwargs)) as chunks:
            for chunk in chunks:
                counters["returned_chunks"] += 1
                counters["returned_bytes"] += len(chunk)
                yield chunk
    def read_range(reference, *args, **kwargs):
        counters = blob_metrics(reference)
        counters["range_calls"] += 1
        result = blob_range(reference, *args, **kwargs)
        counters["range_returned_bytes"] += len(result)
        return result
    def verify(reference):
        result = blob_verify(reference)
        counters = blob_metrics(reference)
        counters["verified_calls"] += 1
        counters["verified_bytes"] += reference.byte_size
        return result
    @contextmanager
    def cursor():
        with original_cursor() as connection:
            proxy = _Cursor(connection, profiles) if profiles is not None else None
            try:
                yield proxy if proxy is not None else connection
            finally:
                if proxy is not None:
                    proxy.finish()
    with ExitStack() as stack:
        for owner, name, observer in ((os, "fsync", fsync), (workspace.ledger, "_open", opened),
                (workspace.records, "_cursor", cursor), (core_selections, "extracted_rows", extraction),
                (core_selections, "member_bytes", member_bytes),
                (identity, "_artifact_canonical_json_bytes", canonical_encode),
                (identity, "_artifact_parse_canonical_json", canonical_decode),
                (workspace.blobs, "read", read), (workspace.blobs, "read_range", read_range),
                (workspace.blobs, "verify", verify)):
            observer._core_probe_original = _unobserved(getattr(owner, name))
            stack.enter_context(patch.object(owner, name, observer))
        yield


def count_fixture(directory):
    return pq.ParquetFile(directory / "base.parquet").metadata.num_rows


def build(workspace, directory, metrics):
    metrics["fixture_rows_decoded"] = 0
    def rows():
        with pq.ParquetFile(directory / "base.parquet") as source:
            for batch in source.iter_batches(batch_size=256):
                for key, entity, payload in zip(*(column.to_pylist() for column in batch.columns), strict=True):
                    metrics["fixture_rows_decoded"] += 1
                    yield key, core.Entity(format_version=1, entity_id=entity, entity_type="occurrence", value=core.InlineValue(value=decode_canonical_json_value(payload.encode("utf-8"))))
    with workspace.publisher.session() as session:
        workspace.states.create_keyed(session, state_id="root", representation_id="root:physical", unit_id="root:import", rows=rows())
    workspace.maintenance.select_current("root:current", "fixture", ("state", "root"), None)
    return {"members": metrics["fixture_rows_decoded"], "state_id": "root"}


def retained_selection(workspace, session, identity, state, selector, *, recover=False):
    return workspace.selections.retain(session, selected_value_id=identity, definition=selector,
        origin=core.Origin(parent_entity_id=state), from_parent=recover)


def evaluate(workspace, mode, count, metrics):
    scope = tuple(f"{i:07d}" for i in range(min(count, 1024))) + ("missing",) if mode == "named" else None
    ordered = mode == "ordered-fields"
    rule = core.OperationDefinition(format_version=1, definition_id="capacity:position-order",
        implementation_id="docspec.sort.canonical-json", implementation_version="1", operation_kind="transformation",
        configuration={"pointers": ["/position"], "descending": False})
    definition = core.StateMembers(member_selector=core.Whole() if mode == "whole" else FIELDS, scope=scope,
        material_keys=True, sort_rule=rule.definition_id if ordered else None)
    with workspace.publisher.session() as session:
        if ordered:
            session.publish(MetadataBatch("capacity:position-order", records=(rule,),
                retained=(("operation_definition", rule.definition_id),)))
        # Separate fresh metadata/availability checks from warm named queries.
        # This is not a full physical integrity audit; audit has its own stage.
        start = time.perf_counter()
        workspace.states.manifest(session, "root")
        admission = time.perf_counter() - start
        start = time.perf_counter()
        selected = retained_selection(workspace, session, "selected:" + mode, "root", definition)
        evidence = workspace.selections.evidence(session, selected)
        seconds = time.perf_counter() - start
        rows = 0
        checked = 0
        seen, missing, previous_order = bytearray((count + 7) // 8), False, None
        # Oracle work is outside the measured selection. It consumes each result
        # once and generates expected values independently, without a dataset list.
        start = time.perf_counter()
        with closing(workspace.selections.rows(session, selected)) as actual:
            for key, entity, encoded in actual:
                if key == "missing":
                    assert scope is not None and not missing
                    missing = True
                    expected = ["absent", ["key", key]]
                    assert entity is None
                else:
                    ordinal = int(key)
                    assert 0 <= ordinal < (min(count, 1024) if scope is not None else count)
                    assert key == f"{ordinal:07d}"
                    byte, mask = ordinal // 8, 1 << (ordinal % 8)
                    assert not seen[byte] & mask
                    seen[byte] |= mask
                    value = core_value(ordinal)
                    if ordered:
                        order = (canonical_value_bytes([["present", value["position"]]]), key)
                        assert previous_order is None or previous_order < order
                        previous_order = order
                    fields = None if mode == "whole" else [{"label": field.label, "pointer": field.pointer} for field in FIELDS.selectors]
                    expected = ["present", selected_value(value, fields), ["key", key]]
                    assert entity == f"urn:docspec:fixture:occurrence:{ordinal}"
                assert value_key(decode_canonical_json_value(encoded)) == value_key(expected)
                rows += 1
                checked += len(encoded)
        assert rows == (len(scope) if scope is not None else count)
        return {"rows": rows, "selected_evidence": msgspec.to_builtins(evidence), "warm_evaluation_seconds": seconds,
            "retained_position_order_checked": ordered,
            "fresh_state_manifest_seconds": admission, "manifest_check_is_full_integrity_audit": False, "oracle_seconds": time.perf_counter() - start, "oracle_checked_selected_bytes": checked}


def resolve_url(workspace, session, state, identity, executions):
    definition = core.OperationDefinition(format_version=1, definition_id="url-operation", implementation_id="capacity:url-values",
        implementation_version="1", operation_kind="transformation", configuration={})
    request = core.Request(format_version=1, request_id=identity + ":request", definition_id=definition.definition_id,
        inputs=(core.StateInput(label="source", state_id=state),),
        dependencies=(core.Dependency(label="urls", binding_label="source", selection=core.StateMembers(member_selector=URL, material_keys=True)),))
    def produce(context):
        executions.append(identity)
        context.use(state)
        selected = retained_selection(workspace, session, identity + ":consumed-urls", state,
            core.StateMembers(member_selector=URL, material_keys=True))
        digest, rows = hashlib.sha256(), 0
        with closing(workspace.selections.rows(session, selected)) as values:
            for _, _, encoded in values:
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
                rows += 1
        context.generate(core.InlineValue(value={"url_rows": rows, "digest": digest.hexdigest()}), label="answer")
    return workspace.operations.resolve(definition, request, produce, selection_id=identity + ":selection",
        target=core.Origin(parent_entity_id=state), reuse_policy=lambda result: result.outcome.status == "success", session=session)


def revised(workspace, session, *, base, state, changes):
    evidence, puts = [], []
    with closing(bounded_rows(changes, size=lambda row: len(canonical_value_bytes(row)), max_rows=VALUE_EDIT_BATCH_ROWS)) as groups:
        for group in groups:
            operation, edits = prepare_value_edits(workspace.operations,
                ((entity, [{"op": "replace", "path": path, "value": value}]) for _, entity, path, value in group), session=session)
            for (key, _, _, _), edit in zip(group, edits, strict=True):
                evidence.append(edit)
                puts.append(core.Put(sequence=len(puts), member_key=key, occurrence_id=edit.result_occurrence_id))
            workspace.operations.publish((operation,), session=session)
    revision = core.Revision(format_version=1, revision_id=state + ":revision", base_state_id=base, result_state_id=state,
        edits=tuple(puts), value_edits=tuple(evidence))
    workspace.operations.publish((prepare_revision(workspace.operations, revision, session=session),), session=session)
    return {put.member_key: put.occurrence_id for put in puts}


def edits(workspace, count, edit_count, *, profiles=None):
    count = min(count, edit_count)
    executions = []
    ranges = {}
    with workspace.publisher.session() as session:
        baseline_start = len(profiles.profiles) if profiles is not None else 0
        original = resolve_url(workspace, session, "root", "root-urls", executions)
        if profiles is not None:
            ranges["baseline"] = [baseline_start, len(profiles.profiles)]
        baseline_calls = list(executions)
        ids = [(f"{i:07d}", f"urn:docspec:fixture:occurrence:{i}") for i in range(count)]
        timings, choices = {}, []
        for name, path in (("title", "/title"), ("url", "/url")):
            profile_start = len(profiles.profiles) if profiles is not None else 0
            start = time.perf_counter()
            revised(workspace, session, base="root", state=name, changes=((key, entity, path, "changed-" + name + key) for key, entity in ids))
            selected = resolve_url(workspace, session, name, name + "-urls", executions)
            timings[name] = time.perf_counter() - start
            if profiles is not None:
                ranges[name] = [profile_start, len(profiles.profiles)]
            choices.append(selected.result.result_id)
        assert choices[0] == original.result.result_id and choices[1] != original.result.result_id
        assert executions == baseline_calls + ["url-urls"]
        workspace.maintenance.select_current("url:current", "fixture", ("state", "url"), ("state", "root"))
        return {"changed_members_per_revision": count, "edit_through_reuse_seconds": timings, "actual_executions": executions,
            "exact_title_reuse": True, "url_result_changed": True, "query_profile_ranges": ranges}


def membership_edits(workspace, count, edit_count):
    """Remove and restore actual keys; compare complete addresses and reuse."""
    width = min(count, edit_count)
    assert width > 0
    executions, choices, timings = [], [], {}
    with workspace.publisher.session() as session:
        original = resolve_url(workspace, session, "root", "membership:original", executions)
        baseline_calls = list(executions)
        base = "root"
        for state, restoring in (("members:removed", False), ("members:restored", True)):
            start = time.perf_counter()
            changes = tuple(core.Put(sequence=i, member_key=f"{i:07d}", occurrence_id=f"urn:docspec:fixture:occurrence:{i}")
                            if restoring else core.Remove(sequence=i, member_key=f"{i:07d}") for i in range(width))
            revision = core.Revision(format_version=1, revision_id=state + ":revision", base_state_id=base,
                result_state_id=state, edits=changes)
            workspace.operations.publish((prepare_revision(workspace.operations, revision, session=session),), session=session)
            choice = resolve_url(workspace, session, state, state + ":urls", executions)
            timings[state] = time.perf_counter() - start
            choices.append(choice.result.result_id)
            # Verify every address without loading unchanged body values.
            expected = 0 if restoring else width
            with workspace.states.relation(session, state) as relation:
                with closing(relation.project("member_key, occurrence_id").order("member_key").to_arrow_reader(256)) as batches:
                    for batch in batches:
                        for key, identity in zip(*(column.to_pylist() for column in batch.columns), strict=True):
                            assert key == f"{expected:07d}"
                            assert identity == f"urn:docspec:fixture:occurrence:{expected}"
                            expected += 1
            assert expected == count
            base = state
        assert choices[0] != original.result.result_id and choices[1] == original.result.result_id
        assert executions == baseline_calls + ["members:removed:urls"]
    return {"removed_members": width, "restored_members": width, "state_id": base,
            "membership_edit_through_reuse_seconds": timings, "complete_addresses_checked": True,
            "restoration_reuses_original_result": True, "actual_executions": executions}


def history(workspace, count, length):
    width = min(1024, count)
    ids = {f"{i:07d}": f"urn:docspec:fixture:occurrence:{i}" for i in range(width)}
    base = "root"
    start = time.perf_counter()
    with workspace.publisher.session() as session:
        for index in range(length):
            key = f"{index % width:07d}"
            state = f"history:{index}"
            ids.update(revised(workspace, session, base=base, state=state, changes=((key, ids[key], "/title", f"history-{index}"),)))
            base = state
    return {"revision_count": length, "state_id": base, "creation_seconds": time.perf_counter() - start}


def expected_value(ordinal, state, count, edit_count):
    value = core_value(ordinal)
    if state in {"title", "url"} and ordinal < min(count, edit_count):
        value[state] = "changed-" + state + f"{ordinal:07d}"
    elif state.startswith("history:"):
        length, width = int(state.split(":")[1]) + 1, min(1024, count)
        if ordinal < min(width, length):
            latest = ordinal + ((length - 1 - ordinal) // width) * width
            value["title"] = f"history-{latest}"
    return value


def audit(workspace, state, fixture_count, edit_count):
    count = total = 0
    with workspace.publisher.session() as session:
        manifest = workspace.states.manifest(session, state)
        for reference in workspace.states._references(manifest).values():
            workspace.records.verify(reference)
    with closing(workspace.rows(state)) as rows:
        for key, entity in rows:
            # The public reader orders keys; require the exact frozen domain,
            # so duplicates cannot substitute for omitted or unexpected keys.
            assert key == f"{count:07d}"
            value = entity.value.value
            assert value_key(value) == value_key(expected_value(int(key), state, fixture_count, edit_count))
            count += 1
            total += len(canonical_value_bytes(value))
    assert count == fixture_count
    return {"members": count, "verified_value_bytes": total, "physical_integrity_and_schema_checked": True}


def clean_directory(directory):
    directory = Path(directory)
    return directory.parent / (directory.name + "-clean")


@contextmanager
def control_workspace(directory, engine_memory_bytes=ENGINE_MEMORY_BYTES):
    directory = Path(directory)
    scratch = directory / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    with CoreWorkspace(directory / "workspace", engine_memory_bytes=engine_memory_bytes) as workspace:
        workspace.records.merge_scratch_root = scratch
        yield workspace


def clean(workspace, state, count, edit_count):
    """Build independently expected payloads in a separate clean allocation."""
    destination = clean_directory(workspace.path.parent)
    metrics = {}
    with workspace.publisher.session() as source, control_workspace(destination, workspace.records.engine_memory_bytes) as control:
        def rows():
            seen = 0
            with workspace.states.relation(source, state) as relation:
                with closing(relation.project("member_key, occurrence_id").order("member_key").to_arrow_reader(256)) as batches:
                    for batch in batches:
                        for key, identity in zip(*(column.to_pylist() for column in batch.columns), strict=True):
                            assert key == f"{seen:07d}", "source addresses differ from the complete fixture"
                            yield key, core.Entity(format_version=1, entity_id=identity, entity_type="occurrence",
                                value=core.InlineValue(value=expected_value(seen, state, count, edit_count)))
                            seen += 1
            assert seen == count
        with observations(control, metrics), control.publisher.session() as target:
            control.states.create_keyed(target, state_id=state, representation_id=state + ":clean-physical",
                unit_id=state + ":clean-import", rows=rows())
        control.maintenance.select_current(state + ":clean-current", "fixture", ("state", state), control.ledger.current("fixture"))
    return {"state_id": state, "members": count, "independent_payloads": True,
            "clean_directory": str(destination), "separate_storage_allowance_bytes": 80 * 1024**3, "clean_metrics": metrics}


def compare_clean(directory, state, count, edit_count, *, engine_memory_bytes=ENGINE_MEMORY_BYTES):
    """Reopen both authorities, audit their complete domains and compare rows."""
    from itertools import zip_longest

    destination = clean_directory(directory)
    with control_workspace(directory, engine_memory_bytes) as original, control_workspace(destination, engine_memory_bytes) as control:
        original_audit = audit(original, state, count, edit_count)
        clean_audit = audit(control, state, count, edit_count)
        compared = 0
        with closing(original.rows(state)) as original_rows, closing(control.rows(state)) as clean_rows:
            for left, right in zip_longest(original_rows, clean_rows):
                assert left is not None and right is not None, "clean and original populations differ"
                left_key, left_entity = left
                right_key, right_entity = right
                assert left_key == right_key and left_entity.entity_id == right_entity.entity_id
                assert canonical_value_bytes(left_entity.value.value) == canonical_value_bytes(right_entity.value.value)
                compared += 1
        assert compared == count
        return {"counts": {"added": 0, "removed": 0, "changed": 0}, "compared_members": compared,
                "occurrence_identities_equal": True, "original_audit": original_audit, "clean_audit": clean_audit,
                "clean_directory": str(destination)}


def append_history_suffix(workspace, count, *, start_index, length):
    """Continue the declared history ordinal, retaining its actual input IDs."""
    if start_index < 1 or length < 1:
        raise ValueError("a history suffix requires a positive start index and length")
    width = min(1024, count)
    base = f"history:{start_index - 1}"
    with workspace.publisher.session() as session:
        with workspace.states.relation(session, base, scope=tuple(f"{index:07d}" for index in range(width))) as relation:
            with closing(relation.project("member_key, occurrence_id").to_arrow_reader(256)) as batches:
                ids = {key: identity for batch in batches for key, identity in zip(
                    *(column.to_pylist() for column in batch.columns), strict=True)}
        assert len(ids) == width
        for index in range(start_index, start_index + length):
            key, state = f"{index % width:07d}", f"history:{index}"
            ids.update(revised(workspace, session, base=base, state=state,
                               changes=((key, ids[key], "/title", f"history-{index}"),)))
            base = state
    return {"state_id": base, "start_index": start_index, "revision_count": length}


def checkpoint_history_suffix(directory, *, history_length, suffix_length, count=None, edit_count=1024,
                              engine_memory_bytes=ENGINE_MEMORY_BYTES):
    """Verify recovery before/after checkpoint, then append and reopen a suffix."""
    count = count_fixture(Path(directory)) if count is None else count
    base = f"history:{history_length - 1}"
    phases = {}
    with control_workspace(directory, engine_memory_bytes) as workspace:
        start = time.perf_counter()
        before = audit(workspace, base, count, edit_count)
        phases["before_checkpoint_recovery_seconds"] = time.perf_counter() - start
        with workspace.publisher.session() as session:
            start = time.perf_counter()
            checkpoint = workspace.states.checkpoint(session, base, representation_id=base + ":suffix-checkpoint",
                                                       unit_id=base + ":suffix-checkpoint-import")
            assert checkpoint.state_id == base
            phases["checkpoint_seconds"] = time.perf_counter() - start
    with control_workspace(directory, engine_memory_bytes) as workspace:
        start = time.perf_counter()
        assert audit(workspace, base, count, edit_count) == before
        phases["after_checkpoint_recovery_seconds"] = time.perf_counter() - start
        start = time.perf_counter()
        result = append_history_suffix(workspace, count, start_index=history_length, length=suffix_length)
        phases["suffix_creation_seconds"] = time.perf_counter() - start
    with control_workspace(directory, engine_memory_bytes) as workspace:
        start = time.perf_counter()
        recovered = audit(workspace, result["state_id"], count, edit_count)
        phases["suffix_recovery_seconds"] = time.perf_counter() - start
        assert audit(workspace, base, count, edit_count) == before
        with workspace.publisher.session() as session:
            assert workspace.states.representation(session, base).representation_id == checkpoint.representation_id
    return result | {"checkpoint_state_id": base, "original_history_preserved": True,
                     "recovered_audit": recovered, "phases": phases}


def parent_payload_columns(workspace, state_id):
    """Read each parent file's column sizes once for scan accounting."""
    with workspace.publisher.session() as session:
        manifest = workspace.states.manifest(session, state_id)
        reference = workspace.states._references(manifest)["entities"]
        files = {}
        for content in workspace.records.physical_references(reference):
            if content.media_type == "application/vnd.apache.parquet":
                path = workspace.records.root / content.locator
                metadata = pq.read_metadata(path)
                size = 0
                for index in range(metadata.num_row_groups):
                    group = metadata.row_group(index)
                    size += next(group.column(column).total_uncompressed_size for column in range(group.num_columns)
                                 if group.column(column).path_in_schema == "record_json")
                files[path.name] = size
    return files


def payload_scan_bound(workspace, state_id, profiles, *, parent_files=None):
    """Bound parent payload reads by profiled files' Parquet column chunks.

    Counting every row group in each opened file is conservative: predicates may
    read fewer groups. Truncated filenames use the complete query's file union
    only when its count agrees with the scan; otherwise charge the whole parent.
    Cached transport bytes and returned rows are unrelated.
    """
    files = parent_payload_columns(workspace, state_id) if parent_files is None else parent_files
    scans = []
    for profile in profiles:
        for scan in profile["scans"]:
            info = scan["extra_info"]
            if "record_json" not in info.get("Projections", []):
                continue
            names = info.get("Filename(s)", "")
            matched = [name for name in files if name in names]
            if matched:
                truncated = "..." in names
                query_files = set(profile["query_files"]) & files.keys()
                query_resolves_names = (truncated and set(matched) <= query_files
                                        and len(query_files) == int(info.get("Total Files Read", -1)))
                selected = sorted(query_files) if query_resolves_names else list(files) if truncated else matched
                scans.append({"profile": profile["path"], "files": len(selected),
                              "truncated_names_resolved_from_query": query_resolves_names,
                              "truncated_names_charge_whole_layer": truncated and not query_resolves_names,
                              "uncompressed_encoded_payload_column_bytes_upper_bound": sum(files[name] for name in selected)})
    total = sum(scan["uncompressed_encoded_payload_column_bytes_upper_bound"] for scan in scans)
    return {"method": "All record_json chunks in each profiled parent file per scan. Truncated names use the full SQL file union when its count matches Total Files Read; otherwise charge the complete layer.",
            "parent_files": len(files), "scans": scans,
            "uncompressed_encoded_payload_column_bytes_upper_bound": total,
            "within_64_mib": bool(scans) and total <= 64 * 1024**2,
            "is_physical_io_measurement": False}


def run_stage(directory, stage, *, count=CORE_MEMBER_COUNT, edit_count=1024, history_length=1000, suffix_length=16,
              state="root", engine_memory_bytes=ENGINE_MEMORY_BYTES):
    if stage == "generate":
        return generate(directory, count)
    count = count_fixture(directory)
    metrics = {}
    profiles = QueryProfiles(directory / "profiles" / stage) if stage in {
        "open", "fields", "named", "whole", "ordered-fields", "edits", "membership", "checkpoint"} else None
    with CoreWorkspace(directory / "workspace", engine_memory_bytes=engine_memory_bytes) as workspace, observations(workspace, metrics, profiles):
        scratch = directory / "scratch"
        scratch.mkdir(exist_ok=True)
        workspace.records.merge_scratch_root = scratch
        if stage == "build":
            result = build(workspace, directory, metrics)
        elif stage == "open":
            assert workspace.inspect("state", state)["available"]
            result = {"state_id": state, "current": workspace.ledger.current("fixture")}
        elif stage in {"fields", "named", "whole", "ordered-fields"}:
            result = evaluate(workspace, stage, count, metrics)
        elif stage == "edits":
            result = edits(workspace, count, edit_count, profiles=profiles)
        elif stage == "membership":
            result = membership_edits(workspace, count, edit_count)
        elif stage == "history":
            result = history(workspace, count, history_length)
        elif stage == "history-suffix":
            result = checkpoint_history_suffix(directory, history_length=history_length, suffix_length=suffix_length,
                count=count, edit_count=edit_count, engine_memory_bytes=engine_memory_bytes)
        elif stage == "clean":
            result = clean(workspace, state, count, edit_count)
        elif stage == "compare":
            result = compare_clean(directory, state, count, edit_count, engine_memory_bytes=engine_memory_bytes)
        elif stage == "recover":
            with workspace.publisher.session() as session:
                with closing(workspace.ledger.read_records([("selected_value", "selected:fields")])) as rows:
                    selected = next(rows)[0].value
                direct = workspace.selections.evidence(session, selected)
                recovered = retained_selection(workspace, session, "recovered:fields", "root", selected.definition, recover=True)
                assert workspace.selections.evidence(session, recovered) == direct
                result = {"direct_and_parent_evidence_equal": True, "evidence": msgspec.to_builtins(direct)}
        elif stage == "checkpoint":
            with workspace.publisher.session() as session:
                before = workspace.states.representation(session, state)
                start = time.perf_counter()
                checkpoint = workspace.states.checkpoint(session, state, representation_id=state + ":checkpoint", unit_id=state + ":checkpoint-unit")
                result = {"state_id": state, "checkpoint_seconds": time.perf_counter() - start, "logical_identity_preserved": checkpoint.state_id == before.state_id}
            result["audit"] = audit(workspace, state, count, edit_count)
        elif stage == "audit":
            result = audit(workspace, state, count, edit_count)
        else:
            raise ValueError("unknown experiment stage")
        if stage == "named":
            result["parent_payload_scan_bound"] = payload_scan_bound(workspace, "root", profiles.profiles)
        elif stage == "edits":
            parent_files = parent_payload_columns(workspace, "root")
            result["parent_payload_scan_bound"] = {name: payload_scan_bound(workspace, "root", profiles.profiles[start:end], parent_files=parent_files)
                for name, (start, end) in result["query_profile_ranges"].items() if name != "baseline"}
    metrics["extraction"].pop("shared_encoder_calls", None)
    return result | {"metrics": metrics, "query_profiles": None if profiles is None else profiles.profiles}



def storage_files(directory):
    """Yield relative file parts and sizes with one stat per file.

    Like the original recursive sampler, follow file links but never descend
    into linked directories. A file disappearing during a sample is harmless.
    """
    for parent, _, names in os.walk(directory):
        relative = Path(parent).relative_to(directory).parts
        for name in names:
            try:
                metadata = os.stat(os.path.join(parent, name))
            except FileNotFoundError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                yield (*relative, name), metadata.st_size


class StorageSamples:
    """Sample both trial trees; count durable paths present at the end vs start.

    File counts exclude staging, scratch, lock files and SQLite WAL/SHM. They
    count newly present paths, not overwritten files or all create syscalls.
    """
    def __init__(self, directory):
        self.roots = {"": directory, "clean_": clean_directory(directory)}
        self.peak = {prefix + kind: 0 for prefix in self.roots
                     for kind in ("storage_bytes", "scratch_bytes", "retained_bytes")}
        self.current_files = {}
        self.sample()
        self.initial_files = self.current_files.copy()

    def sample(self):
        for prefix, root in self.roots.items():
            total = scratch = retained = 0
            durable = set()
            for parts, size in storage_files(root):
                total += size
                if "scratch" in parts or ".staging" in parts:
                    scratch += size
                elif parts[0] == "workspace":
                    retained += size
                    if not parts[-1].endswith(("-wal", "-shm", ".lock")):
                        durable.add("/".join(parts))
            for key, value in (("storage_bytes", total), ("scratch_bytes", scratch), ("retained_bytes", retained)):
                self.peak[prefix + key] = max(self.peak[prefix + key], value)
            self.current_files[prefix] = durable

    def new_durable_files(self):
        return {prefix + "new_durable_files": len(paths - self.initial_files[prefix])
                for prefix, paths in self.current_files.items()}


@contextmanager
def allocation_observations(enabled, metrics):
    """Optional Python tracing: peak bytes and net live blocks, never gross/native allocations."""
    if not enabled:
        yield
        return
    tracemalloc.start(1)
    before = tracemalloc.take_snapshot()
    try:
        yield
    finally:
        current, peak = tracemalloc.get_traced_memory()
        after = tracemalloc.take_snapshot()
        metrics.update(current_bytes=current, peak_bytes=peak,
                       net_live_allocation_count=sum(item.count_diff for item in after.compare_to(before, "traceback")))
        tracemalloc.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("generate", "build", "open", "fields", "named", "whole", "ordered-fields", "edits", "membership", "history", "history-suffix", "clean", "compare", "recover", "checkpoint", "audit"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--count", type=int, default=CORE_MEMBER_COUNT)
    parser.add_argument("--edit-count", type=int, default=1024)
    parser.add_argument("--history-length", type=int, default=1000)
    parser.add_argument("--suffix-length", type=int, default=16)
    parser.add_argument("--state", default="root")
    parser.add_argument("--engine-memory-bytes", type=int, default=ENGINE_MEMORY_BYTES)
    parser.add_argument("--cache-description", default="unspecified",
                        help="Observed cache preparation/state for this trial; does not change caches.")
    parser.add_argument("--trace-allocations", action="store_true",
                        help="Diagnostic Python tracing; adds overhead and excludes native allocations.")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    path = args.directory / (args.stage + "-runtime.json")
    if path.exists():
        raise FileExistsError(path)
    storage = StorageSamples(args.directory)
    stopped = Event()
    def monitor():
        while not stopped.is_set():
            storage.sample()
            stopped.wait(0.25)
    worker = Thread(target=monitor, daemon=True)
    worker.start()
    source_files = (Path(__file__), Path("tests/support/core_workload.py"), Path("uv.lock"), *sorted(Path("src/docspec").rglob("*.py")))
    source_hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files}
    allocations = {}
    start = time.perf_counter()
    try:
        with allocation_observations(args.trace_allocations, allocations):
            result = run_stage(args.directory, args.stage, count=args.count, edit_count=args.edit_count,
                history_length=args.history_length, suffix_length=args.suffix_length, state=args.state,
                engine_memory_bytes=args.engine_memory_bytes)
    except Exception as error:
        result = {"error": {"type": type(error).__name__, "message": str(error)}}
    finally:
        stopped.set()
        worker.join()
        storage.sample()
    result.update(stage=args.stage, elapsed_seconds=time.perf_counter() - start,
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
        peak_rss_scope="whole fresh process including setup, independent oracle and enabled observers",
        sampled_peak=storage.peak, storage_file_changes=storage.new_durable_files(),
        sample_interval_seconds=0.25, platform=platform.platform(), python=platform.python_version(),
        cache_description=args.cache_description, cache_state_declared=bool(args.cache_description.strip() and args.cache_description != "unspecified"),
        python_allocations=allocations if args.trace_allocations else None,
        versions={name: importlib.metadata.version(name) for name in ("docspec", "duckdb", "pyarrow", "msgspec", "jsonschema-rs")},
        engine_memory_bytes=args.engine_memory_bytes, engine_threads=1,
        source_sha256=source_hashes, sources_unchanged_during_run=all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in source_hashes.items()),
        limitations=["Fresh process RSS includes independent verification; per-phase RSS not isolated.",
            "Storage is sampled; native record scratch is directed into the measured output tree.",
            "DuckDB profile bytes are reported I/O, not proven uncompressed payload column bytes.",
            "Query profiles cover the observed stage, including its independent verification; raw profile files retain full SQL.",
            "Codec counters count successful DocSpec gateway calls/bytes, not recursive or Rulespec-internal work; conversion metrics cover the shared selection extractor.",
            "Blob counters count returned stream/range bytes plus successful whole-blob verification bytes by media type; media type does not establish the Core codec.",
            "Optional allocation tracing reports Python peak bytes and net live blocks, not gross allocations or native memory; tracing adds overhead.",
            "New durable file counts compare final vs initial paths, exclude scratch/staging/locks/WAL/SHM, and do not count transient creations or overwrites.",
            "Cache description is operator supplied; the probe does not flush or warm caches.",
            "Fsync metrics cover Python file and directory sync calls; SQLite/native sync calls are not intercepted."])
    path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
    if "error" in result:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
