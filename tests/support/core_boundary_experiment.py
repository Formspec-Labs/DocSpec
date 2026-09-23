"""Measured C03 boundary cases through Core's production owners.

Run each stage in a fresh process: python -m tests.support.core_boundary_experiment
{nested,schema,boundary} /tmp/core-boundaries [--count 1024].
"""

from contextlib import closing
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import resource
import sys
import time
from unittest.mock import patch

import docspec
from docspec.adapters.schema_validation import compile_payload_schema, validate_payload
from docspec.adapters.storage import ledger as ledger_module
from docspec.application.core_edits import VALUE_EDIT_BATCH_ROWS, prepare_value_edits
from docspec.domain import core
from docspec.domain.core_admission import admit_record, encode_record
from docspec.domain.identity import canonical_value_bytes
from docspec.errors import LimitExceededError, SchemaValidationError
from docspec.ports.core_ledger import MetadataBatch
from docspec.ports.record_storage import BATCH_BYTES, bounded_rows
from docspec.runtime import CoreWorkspace
from tests.support import core_runtime_experiment, core_workload


def _entity(identity, value):
    return core.Entity(format_version=1, entity_id=identity, entity_type="occurrence", value=core.InlineValue(value=value))


def _nested_patch(value):
    """Return the JSON Patch instruction list exercised against one fixture value."""

    instructions = []
    if "field" in value["metadata"]:
        instructions.append({"op": "test", "path": "/metadata/field", "value": value["metadata"]["field"]})
    return [*instructions,
        {"op": "add", "path": "/metadata/review", "value": {"nested": [None, True, 1, "1", {"a/b": "before"}]}},
        {"op": "replace", "path": "/metadata/review/nested/4/a~1b", "value": "e\u0301😀"},
        {"op": "copy", "from": "/metadata/review/nested/0", "path": "/metadata/copied"},
        {"op": "move", "from": "/metadata/review/nested/3", "path": "/metadata/review/nested/0"},
    ]


def _expected_nested(ordinal):
    """Return the fixture value independently expected after ``_nested_patch``."""

    expected = core_workload.core_value(ordinal)
    expected["metadata"]["review"] = {"nested": ["1", None, True, 1, {"a/b": "e\u0301😀"}]}
    expected["metadata"]["copied"] = None
    return expected


def nested(directory, count):
    """Publish actual independent nested edits, reopen and check every value."""
    path, receipts = directory / "nested-workspace", directory / "nested-outputs.jsonl"
    metrics, transformed, activities = {}, 0, 0
    with CoreWorkspace(path) as workspace:
        workspace.create("root", ((f"{i:07d}", core_workload.core_value(i)) for i in range(count)))
        started = time.perf_counter()
        with core_runtime_experiment.observations(workspace, metrics), receipts.open("x") as output:
            with closing(bounded_rows(workspace.rows("root"), size=lambda row: len(encode_record(row[1])),
                                      max_rows=VALUE_EDIT_BATCH_ROWS)) as groups:
                for group in groups:
                    prepared, edits = prepare_value_edits(workspace.operations,
                        ((entity.entity_id, _nested_patch(entity.value.value)) for _, entity in group))
                    workspace.operations.publish((prepared,))
                    assert len(prepared.result.usages) == len(prepared.result.generations) == len(prepared.result.derivations) == len(group)
                    for (key, _), edit in zip(group, edits, strict=True):
                        output.write(json.dumps({"ordinal": int(key), "entity_id": edit.result_occurrence_id}) + "\n")
                    transformed += len(group)
                    activities += 1
        seconds = time.perf_counter() - started
    checked, digest = 0, hashlib.sha256()
    with CoreWorkspace(path) as workspace, receipts.open() as source:
        with closing(bounded_rows((json.loads(line) for line in source), size=lambda row: len(canonical_value_bytes(row)),
                                  max_rows=VALUE_EDIT_BATCH_ROWS)) as groups:
            for group in groups:
                stored = [row for batch in workspace.ledger.read_records(("entity", row["entity_id"]) for row in group) for row in batch]
                for receipt, row in zip(group, stored, strict=True):
                    expected = canonical_value_bytes(_expected_nested(receipt["ordinal"]))
                    assert row.retained and row.available and canonical_value_bytes(row.value.value.value) == expected
                    digest.update(expected)
                    checked += 1
        originals = 0
        for key, entity in workspace.rows("root"):
            assert canonical_value_bytes(entity.value.value) == canonical_value_bytes(core_workload.core_value(int(key)))
            originals += 1
    assert transformed == checked == originals == count
    return {"members": count, "transformed": transformed, "activities": activities, "reopened_verified": checked,
            "unchanged_originals": originals, "independent_output_sha256": digest.hexdigest(),
            "operation_seconds": seconds, "metrics": metrics}


_PAYLOAD_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "additionalProperties": False,
    "required": ["body", "url", "title", "position", "metadata"],
    "properties": {"body": {"type": "string", "minLength": 8192, "maxLength": 8192},
        "url": {"type": "string", "format": "uri"}, "title": {"type": "string"},
        "position": {"type": "integer", "minimum": 0}, "metadata": {"type": "object"}},
}


def schema(directory, count):
    """Compile one supplied schema; admit, validate and publish streamed rows."""
    started = time.perf_counter()
    validator = compile_payload_schema(_PAYLOAD_SCHEMA, validate_formats=True)
    compilation_seconds = time.perf_counter() - started
    stats = {"fixed_record_admissions": 0, "payload_schema_calls": 0, "fixed_record_admission_seconds": 0.0,
             "payload_schema_seconds": 0.0}
    def admitted(identity, value):
        started = time.perf_counter()
        record = admit_record(encode_record(_entity(identity, value)))
        stats["fixed_record_admissions"] += 1
        stats["fixed_record_admission_seconds"] += time.perf_counter() - started
        started = time.perf_counter()
        try:
            stats["payload_schema_calls"] += 1
            validate_payload(validator, record.value.value, identity)
        finally:
            stats["payload_schema_seconds"] += time.perf_counter() - started
        return record
    path, metrics = directory / "schema-workspace", {}
    with CoreWorkspace(path) as workspace, core_runtime_experiment.observations(workspace, metrics):
        with workspace.publisher.session() as session:
            workspace.states.create_keyed(session, state_id="valid", representation_id="valid:physical", unit_id="valid:import",
                rows=((f"{i:07d}", admitted(f"schema:{i}", core_workload.core_value(i))) for i in range(count)))
            invalid = core_workload.core_value(0)
            invalid["position"] = True
            def invalid_rows():
                yield "invalid", admitted("schema:invalid", invalid)
            try:
                workspace.states.create_keyed(session, state_id="invalid", representation_id="invalid:physical", unit_id="invalid:import", rows=invalid_rows())
            except SchemaValidationError as error:
                refusal = {"type": type(error).__name__, "message": str(error), "instance_path": error.instance_path, "schema_path": error.schema_path}
            else:
                raise AssertionError("supplied integer schema admitted a boolean")
    with CoreWorkspace(path) as workspace:
        checked = 0
        for key, entity in workspace.rows("valid"):
            assert canonical_value_bytes(entity.value.value) == canonical_value_bytes(core_workload.core_value(int(key)))
            checked += 1
        assert next(workspace.ledger.read_records((("state", "invalid"), ("entity", "schema:invalid")))) == (None, None)
    assert checked == count and stats["payload_schema_calls"] == count + 1
    return {"members": count, "reopened_verified": checked, "compiled_validators": 1,
            "validator": type(validator).__module__ + "." + type(validator).__name__,
            "compilation_seconds": compilation_seconds, "invalid_refused_before_publication": refusal,
            "admission": stats, "metrics": metrics}


def _sized_entity(identity, record_bytes):
    overhead = len(encode_record(_entity(identity, "")))
    entity = _entity(identity, "x" * (record_bytes - overhead))
    assert len(encode_record(entity)) == record_bytes
    return entity, overhead


def _bulk_attempt(path, record_bytes):
    """Import one state member of exactly ``record_bytes`` and report whether it published and reopened.

    A state registers its members through its layer, so only the native
    record ceiling bounds a member; no per-member commit receipt shares it.
    """

    entity, overhead = _sized_entity("boundary:member", record_bytes)
    observed, metrics = {}, {}
    with CoreWorkspace(path) as workspace, core_runtime_experiment.observations(workspace, metrics):
        try:
            with workspace.publisher.session() as session:
                workspace.states.create_keyed(session, state_id="root", representation_id="root:physical", unit_id="root:import", rows=(("one", entity),))
        except LimitExceededError as error:
            observed["refusal"] = str(error)
    with CoreWorkspace(path) as workspace:
        published = next(workspace.ledger.read_records((("state", "root"),)))[0] is not None
        if published:
            rows = list(workspace.rows("root"))
            assert len(rows) == 1 and rows[0][1] == entity
        assert published == ("refusal" not in observed)
    return {"encoded_record_bytes": record_bytes, "json_value_bytes": len(canonical_value_bytes(entity.value.value)),
            "record_framing_bytes": overhead - 2, "published_and_reopened": published, "metrics": metrics, **observed}


def _inline_attempt(path, record_bytes):
    """Publish one record of exactly ``record_bytes`` and report what the owners did.

    An over-limit attempt records the refusal instead of failing the probe, so
    the caller can assert which byte ceiling it hit.
    """

    identity = "boundary:inline"
    entity, overhead = _sized_entity(identity, record_bytes)
    observed, metrics, original_encode = {}, {}, ledger_module._encode
    def encode(value):
        payload = original_encode(value)
        if (isinstance(value, dict) and set(value) == {"records", "retained", "candidates", "links"}
                and any(record[:2] == ["entity", identity] for record in value["records"])):
            observed["commit_receipt_bytes"] = len(payload)
        return payload
    with CoreWorkspace(path) as workspace, core_runtime_experiment.observations(workspace, metrics):
        with patch.object(ledger_module, "_encode", encode):
            try:
                with workspace.publisher.session() as session:
                    session.publish(MetadataBatch("boundary:inline", records=(entity,), retained=(("entity", identity),)))
            except LimitExceededError as error:
                observed["refusal"] = str(error)
    with CoreWorkspace(path) as workspace:
        row = next(workspace.ledger.read_records((("entity", identity),)))[0]
        published = row is not None
        if published:
            assert row.retained and row.available and row.value == entity
        assert published == ("refusal" not in observed)
    return {"encoded_record_bytes": record_bytes, "json_value_bytes": len(canonical_value_bytes(entity.value.value)),
            "record_framing_bytes": overhead - 2, "published_and_reopened": published, "metrics": metrics, **observed}


def boundary(directory, _count=None):
    """Distinguish native record, inline publication and ContentRef value limits."""
    native = _bulk_attempt(directory / "native-record-ceiling", BATCH_BYTES)
    above_record = _bulk_attempt(directory / "native-record-over", BATCH_BYTES + 1)
    assert native["published_and_reopened"] and not above_record["published_and_reopened"]
    exact = _inline_attempt(directory / "inline-record-ceiling", BATCH_BYTES)
    assert not exact["published_and_reopened"]
    # Measure the actual owner-produced receipt; do not reproduce ledger framing.
    supported_size = BATCH_BYTES - exact["commit_receipt_bytes"]
    supported = _inline_attempt(directory / "inline-publication-ceiling", supported_size)
    next_byte = _inline_attempt(directory / "inline-publication-over", supported_size + 1)
    assert supported["published_and_reopened"] and not next_byte["published_and_reopened"]
    value = "x" * (BATCH_BYTES - 2)
    assert len(canonical_value_bytes(value)) == BATCH_BYTES
    path, metrics = directory / "content-value-ceiling", {}
    with CoreWorkspace(path) as workspace, core_runtime_experiment.observations(workspace, metrics):
        with workspace.publisher.session() as session:
            content = session.retain_value(value)
            entity = core.Entity(format_version=1, entity_id="boundary:content", entity_type="occurrence", value=content)
            session.publish(MetadataBatch("boundary:content", records=(entity,), retained=(("entity", entity.entity_id),)))
            try:
                session.retain_value(value + "x")
            except LimitExceededError as error:
                refusal = str(error)
            else:
                raise AssertionError("over-limit JSON value retained")
    with CoreWorkspace(path) as workspace, workspace.publisher.session() as session:
        row = next(workspace.ledger.read_records((("entity", entity.entity_id),)))[0]
        assert row.retained and row.available and row.value.value == content
        assert session.read_json(content, label="boundary value") == value
    return {"ceiling_bytes": BATCH_BYTES, "native_record_ceiling": native, "native_record_plus_one": above_record,
            "inline_record_ceiling": exact, "inline_publication_ceiling": supported, "inline_publication_plus_one": next_byte,
            "content_reference_value": {"encoded_json_value_bytes": BATCH_BYTES, "encoded_record_bytes": len(encode_record(entity)),
                "published_and_reopened": True, "plus_one_refusal": refusal, "metrics": metrics},
            "scope": "State members, native encoded records and JSON ContentRef values each permit 8 MiB; "
                     "an explicitly published record shares 8 MiB with its commit receipt and guards."}


def run_stage(directory, stage, *, count=1024):
    """Run one boundary stage in a fresh directory, refusing a count outside the fixed workload."""

    directory.mkdir(parents=True, exist_ok=True)
    if not 1 <= count <= core_workload.CORE_MEMBER_COUNT:
        raise ValueError("count outside fixed Core workload")
    return {"nested": nested, "schema": schema, "boundary": boundary}[stage](directory, count)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("nested", "schema", "boundary"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--count", type=int, default=1024)
    args = parser.parse_args()
    files = [Path(__file__), Path(core_workload.__file__), Path(core_runtime_experiment.__file__), *Path(docspec.__file__).parent.rglob("*.py")]
    pins = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    started = time.perf_counter()
    try:
        result = run_stage(args.directory, args.stage, count=args.count)
    except Exception as error:
        result = {"error": {"type": type(error).__name__, "message": str(error)}}
    result.update(stage=args.stage, elapsed_seconds=time.perf_counter() - started,
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
        versions={name: importlib.metadata.version(name) for name in ("docspec", "duckdb", "pyarrow", "msgspec", "jsonschema-rs")},
        source_sha256=pins, sources_unchanged=all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in pins.items()),
        limitations=["Fresh-process RSS includes setup, operation and independent verification.",
                    "Schema timings include per-call clock overhead; conversion metrics reuse the Core probe observer."])
    args.directory.mkdir(parents=True, exist_ok=True)
    with (args.directory / (args.stage + "-report.json")).open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
    if "error" in result:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
