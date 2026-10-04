"""Independent admission of SpicyRegs' native subject and ETL receipt joins.

The producer declares Arrow schemas and complete field policies in its pinned
artifact. This adapter checks the protocol without importing producer code.
"""
from base64 import b64decode, b64encode
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory

import pyarrow as pa
import pyarrow.parquet as pq

from docspec.errors import IntegrityError

KEY = "etl_receipts.parquet"
WITNESS = pa.struct([(name, pa.string()) for name in ("source_id", "source_uri", "sha256", "locator", "body_version")])
SCHEMA = pa.schema([(name, pa.string()) for name in (
    "receipt_id", "dataset", "policy_version", "generation_id", "record_id", "subject_version", "identity_json",
    "attempt_id", "outcome", "processor")]
    + [("witnesses", pa.list_(WITNESS)), ("processing_json", pa.string()), ("diagnostic_json", pa.string())])
POLICY_FIELDS = {"dataset", "policy_version", "subject_schema", "identity_fields", "receipt_fields",
                 "receipt_only", "nullable_identity_fields"}


def _pack(value):
    if value is None:
        return ["null", None]
    if type(value) in (str, bool, int):
        return [type(value).__name__, value]
    if isinstance(value, Decimal):
        return ["decimal", str(value)]
    if isinstance(value, datetime):
        return ["datetime", value.isoformat()]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if isinstance(value, bytes):
        return ["bytes", b64encode(value).decode()]
    if isinstance(value, float) and math.isfinite(value):
        return ["float", value.hex()]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return ["dict", [[key, _pack(child)] for key, child in sorted(value.items())]]
    if isinstance(value, (list, tuple)):
        return ["list", [_pack(child) for child in value]]
    raise IntegrityError("Unsupported exact receipt value")


def _exact(value):
    return json.dumps(_pack(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return "sha256:" + sha256(_exact(value).encode()).hexdigest()


def _decode(text):
    def unpack(value):
        tag, body = value
        if tag in {"null", "str", "bool", "int"}:
            return body
        if tag == "dict":
            return {key: unpack(child) for key, child in body}
        if tag == "list":
            return [unpack(child) for child in body]
        return {"decimal": Decimal, "datetime": datetime.fromisoformat, "date": date.fromisoformat,
                "bytes": b64decode, "float": float.fromhex}[tag](body)
    result = unpack(json.loads(text))
    if _exact(result) != text:
        raise IntegrityError("Receipt evidence is not in its exact canonical spelling")
    return result


def _rows(path):
    with pq.ParquetFile(path) as source:
        for batch in source.iter_batches(batch_size=2048):
            yield from batch.to_pylist()


def _policies(declarations):
    policies = {}
    for value in declarations:
        if set(value) != POLICY_FIELDS:
            raise IntegrityError("Unknown receipt policy shape")
        dataset = value["dataset"]
        schema = pa.ipc.read_schema(pa.BufferReader(b64decode(value["subject_schema"], validate=True)))
        names = schema.names
        identity, processing = value["identity_fields"], value["receipt_fields"]
        if (not isinstance(dataset, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", dataset)
                or dataset in policies or not isinstance(value["policy_version"], str) or not value["policy_version"]
                or type(value["receipt_only"]) is not bool or value["receipt_only"] != (not names)
                or len(set(identity)) != len(identity) or len(set(processing)) != len(processing)
                or len({name.casefold() for name in names}) != len(names)
                or not set(identity) <= set(names) or set(processing) & set(names)
                or not set(value["nullable_identity_fields"]) <= set(identity)
                or (names and not identity)):
            raise IntegrityError("Invalid receipt field classification")
        policies[dataset] = {**value, "schema": schema}
    return policies


def verify_receipts(staging, declaration, tables, grouped, member, *, indexed=None):
    """Validate every family row and receipt with a disk-backed one-to-one index."""
    if (set(declaration) != {"key", "generationId", "policies", "columns", "rows"}
            or declaration["key"] != KEY or not isinstance(declaration["generationId"], str)
            or not declaration["generationId"]):
        raise IntegrityError("Invalid ETL receipt declaration")
    expected_index = {"key": KEY, "sha256": member.sha256, "byteSize": member.byte_size,
                      "rows": member.record_count, "columns": declaration["columns"],
                      "generationId": declaration["generationId"],
                      "datasets": [policy["dataset"] for policy in declaration["policies"]]}
    if indexed is not None and indexed != expected_index:
        raise IntegrityError("Publication receipt selection differs from pinned artifact")
    policies = _policies(declaration["policies"])
    if {name + ".parquet" for name, policy in policies.items() if not policy["receipt_only"]} != set(tables):
        raise IntegrityError("Receipt policies must classify every subject table")
    path = staging / KEY
    import duckdb
    with duckdb.connect() as connection:
        columns = [[name, kind] for name, kind, *_ in connection.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()]
    if columns != declaration["columns"] or member.record_count != declaration["rows"]:
        raise IntegrityError("Receipt declared columns or count differs from its member")
    if not pq.read_schema(path).equals(SCHEMA) or pq.ParquetFile(path).metadata.num_rows != declaration["rows"]:
        raise IntegrityError("Receipt schema or count differs from its declaration")
    with TemporaryDirectory(prefix="docspec-receipts-") as temp, sqlite3.connect(str(Path(temp) / "joins.db")) as con:
        con.execute("CREATE TABLE receipts(dataset TEXT, record_id TEXT, version TEXT, identity_json TEXT, "
                    "receipt_id TEXT UNIQUE, outcome TEXT, used INTEGER DEFAULT 0)")
        con.execute("CREATE UNIQUE INDEX accepted_key ON receipts(dataset, record_id) WHERE outcome='accepted'")
        for row in _rows(path):
            policy = policies.get(row["dataset"])
            if (policy is None or policy["policy_version"] != row["policy_version"]
                    or row["generation_id"] != declaration["generationId"]
                    or _digest({key: value for key, value in row.items() if key != "receipt_id"}) != row["receipt_id"]):
                raise IntegrityError("Receipt policy, generation or content digest differs")
            accepted = row["outcome"] == "accepted"
            if (row["outcome"] not in {"accepted", "refused", "error", "rejected", "observed"}
                    or accepted != (row["subject_version"] is not None)
                    or accepted and (policy["receipt_only"] or not row["record_id"] or not row["identity_json"])):
                raise IntegrityError("Receipt outcome differs from subject presence")
            if not row["attempt_id"] or not row["processor"] or not row["witnesses"]:
                raise IntegrityError("Receipt has no processing identity or source witnesses")
            for witness in row["witnesses"]:
                if (witness is None or not witness["source_id"] or not (witness["sha256"] or witness["body_version"])
                        or witness["sha256"] is not None and not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", witness["sha256"])):
                    raise IntegrityError("Receipt source witness is incomplete")
            processing = _decode(row["processing_json"])
            diagnostics = _decode(row["diagnostic_json"])
            allowed = set(policy["receipt_fields"])
            if row["outcome"] in {"refused", "error", "rejected"}:
                allowed.update(policy["schema"].names)
            if not isinstance(processing, dict) or set(processing) - allowed or not isinstance(diagnostics, dict):
                raise IntegrityError("Unclassified receipt processing fields")
            try:
                con.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?,0)", [row[key] for key in (
                    "dataset", "record_id", "subject_version", "identity_json", "receipt_id", "outcome")])
            except sqlite3.IntegrityError as error:
                raise IntegrityError("Duplicate or ambiguous receipt") from error
        for name, members in grouped.items():
            dataset = name.removesuffix(".parquet")
            policy = policies[dataset]
            for member in members:
                subject_path = staging / member.object_key
                if not pq.read_schema(subject_path).equals(policy["schema"]):
                    raise IntegrityError("Subject schema differs from receipt policy")
                for row in _rows(subject_path):
                    identity = [[key, row[key]] for key in policy["identity_fields"]]
                    if any(value is None and key not in policy["nullable_identity_fields"] for key, value in identity):
                        raise IntegrityError("Subject has a null identity")
                    join = (dataset, _digest([dataset, identity]), _digest([dataset, row]), _exact(identity))
                    found = con.execute("SELECT receipt_id,used FROM receipts WHERE dataset=? AND record_id=? "
                                        "AND version=? AND identity_json=? AND outcome='accepted'", join).fetchone()
                    if found is None or found[1]:
                        raise IntegrityError("Subject has no unique selected receipt for its exact version")
                    con.execute("UPDATE receipts SET used=1 WHERE receipt_id=?", [found[0]])
        if con.execute("SELECT 1 FROM receipts WHERE outcome='accepted' AND used=0 LIMIT 1").fetchone():
            raise IntegrityError("Accepted receipt has no matching subject")
