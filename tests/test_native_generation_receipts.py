"""Real producer generations keep useful native values and independently checked receipts."""
from decimal import Decimal
import json
from pathlib import Path
import shutil

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from rulespec_artifacts import (LocalMemberSource, Producer, build_artifact_root,
                               canonical_json_bytes, describe_member, write_member_manifest)

from docspec.adapters.generation_source import stage_generation
from docspec.adapters.storage.table_occurrences import candidate_rows
from docspec.adapters.storage.table_sql import member_key_sql, reference_member_key
from docspec.domain.table_rows import KeySpelling, TableIdentity
from docspec.errors import IntegrityError
from docspec.runtime import CoreWorkspace

FIXTURE = Path(__file__).parent / "fixtures/generations/native-receipts"


def reseal(path):
    """Restamp changed bytes: failures must come from semantic admission, not just outer digests."""
    root = json.loads((path / "artifact.json").read_bytes())
    members = [describe_member(LocalMemberSource(path), object_key=p.name, role="table",
                               media_type="application/vnd.apache.parquet", record_count=pq.ParquetFile(p).metadata.num_rows)
               for p in sorted(path.glob("*.parquet"))]
    with (path / "members.json").open("wb") as stream:
        manifest = write_member_manifest(stream, scope_kind="global", scope_id="fixture-native", object_key="members.json", members=members)
    root = build_artifact_root(kind=root["kind"], spec=root["spec"], producer=Producer.from_dict(root["producer"], path="fixture/producer"), manifests=[manifest])
    (path / "artifact.json").write_bytes(canonical_json_bytes(root))


def test_actual_producer_generation_admits_native_values_and_retains_receipts(tmp_path):
    with stage_generation(FIXTURE, family="fixture-native", table="fixture_native") as staged:
        assert staged.receipts[0].path.read_bytes() == (FIXTURE / "etl_receipts.parquet").read_bytes()
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        admitted = workspace.admit_generation(FIXTURE, family="fixture-native", table="fixture_native", dataset="native")
        request = workspace.generating_request(admitted.state_id)
        assert [item.label for item in request.inputs] == ["root", "members", "etl-receipts-0"]
        descriptor = admitted.report["etlReceipts"][0]
        with workspace.publisher.session() as session:
            [entry] = next(session.read_records([("entity", request.inputs[-1].entity_id)]))
            assert entry.value.value.digest == descriptor["sha256"]
        with workspace.open_state(admitted.state_id) as reader:
            with reader.table() as table:
                row = table.to_arrow_table().to_pylist()[0]
            assert row["money"] == Decimal("123456789012345678.90")
            assert row["cycles"] == [2024, None, 2026]
            assert row["facts"] == [{"label": "one", "value": 2**60}, None]
            value = reader.read_value(row["member_key"])
            assert value["facts"][0]["value"] == str(2**60)
            assert value["money"] == "123456789012345678.90"
            assert "source_text" not in value
        assert workspace.admit_generation(FIXTURE, family="fixture-native", table="fixture_native", dataset="native").state_id == admitted.state_id


@pytest.mark.parametrize("attack", ["subject", "receipt", "missing", "wrong-policy"])
def test_resealed_invalid_receipt_join_is_rejected(tmp_path, attack):
    source = tmp_path / "source"
    shutil.copytree(FIXTURE, source)
    if attack == "subject":
        table = pq.read_table(source / "fixture_native.parquet")
        rows = table.to_pylist()
        rows[0]["money"] += Decimal("0.01")
        pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), source / "fixture_native.parquet")
    elif attack == "missing":
        (source / "etl_receipts.parquet").unlink()
    elif attack == "wrong-policy":
        root = json.loads((source / "artifact.json").read_bytes())
        root["spec"]["etlReceipts"]["policies"][0]["receipt_fields"] = []
        (source / "artifact.json").write_text(json.dumps(root))
    else:
        table = pq.read_table(source / "etl_receipts.parquet")
        rows = table.to_pylist()
        rows[0]["generation_id"] = "different"
        pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), source / "etl_receipts.parquet")
    reseal(source)
    with pytest.raises(IntegrityError):
        with stage_generation(source, family="fixture-native", table="fixture_native"):
            pytest.fail("invalid receipt generation admitted")


def test_native_composite_identity_preserves_null_empty_and_delimiters():
    columns = (("agency", "VARCHAR"), ("cycle", "BIGINT"))
    identity = TableIdentity("native", "aggregate", KeySpelling("native-tuple", "1", ("agency", "cycle")), columns)
    rows = [{"agency": "a@b", "cycle": None}, {"agency": "", "cycle": 2**60}, {"agency": None, "cycle": 2024}]
    data = pa.Table.from_pylist(rows, schema=pa.schema([("agency", pa.string()), ("cycle", pa.int64())]))
    with duckdb.connect() as con:
        relation = con.from_arrow(data)
        assert [key for key, in relation.project(member_key_sql(identity)).fetchall()] == [reference_member_key(identity, row) for row in rows]
        for row in rows:
            key = reference_member_key(identity, row)
            assert candidate_rows(relation, identity, [key]).to_arrow_table().to_pylist() == [row]


@pytest.mark.parametrize("key", ["", "plain", "a@b", "null"])
def test_single_native_text_identity_admits_every_policy_permitted_value(tmp_path, key):
    from docspec.adapters.generation_receipts import _digest, _exact

    source = tmp_path / "source"
    shutil.copytree(FIXTURE, source)
    table = pq.read_table(source / "fixture_native.parquet")
    [row] = table.to_pylist()
    row["id"] = key
    pq.write_table(pa.Table.from_pylist([row], schema=table.schema), source / "fixture_native.parquet")
    receipts = pq.read_table(source / "etl_receipts.parquet")
    [receipt] = receipts.to_pylist()
    identity = [["id", key]]
    receipt.update(record_id=_digest(["fixture_native", identity]),
                   subject_version=_digest(["fixture_native", row]), identity_json=_exact(identity))
    receipt["receipt_id"] = _digest({name: value for name, value in receipt.items() if name != "receipt_id"})
    pq.write_table(pa.Table.from_pylist([receipt], schema=receipts.schema), source / "etl_receipts.parquet")
    reseal(source)
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        admitted = workspace.admit_generation(source, family="fixture-native", table="fixture_native", dataset="native")
        with workspace.open_state(admitted.state_id) as reader:
            with reader.table() as rows:
                [stored] = rows.to_arrow_table().to_pylist()
            assert reader.read_value(stored["member_key"])["id"] == key
