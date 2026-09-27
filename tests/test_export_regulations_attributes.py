"""The Regulations.gov attribute export: native typing equals a Python reference, and real records round-trip.

The reference is ``expected_row``: Python's ``json`` and plain type checks,
with spicy-docs' ``json_column`` for JSON text. The tool's rows come from
DuckDB alone, so the two share only the column list and the instant pattern.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest
from spicy_docs.schemas import TABLE_CONTRACTS
from spicy_docs.schemas.tables import json_column

from docspec.runtime import CoreWorkspace
from tools import export_regulations_attributes as tool
from tools.export_regulations_attributes import BOOLEAN, INTEGER, JSON, LIST, TABLES, TIMESTAMPTZ, VARCHAR, Table

RECORDS = Path(__file__).parent / "fixtures" / "json" / "regulations-gov-catalogue-records.json"
PROBE = Table("probe", "documents", "document_id", (("text", VARCHAR), ("flag", BOOLEAN), ("count", INTEGER),
                                                    ("at", TIMESTAMPTZ), ("items", LIST), ("displayProperties", JSON)))
CONTROLS = "".join(map(chr, range(32))) + "\x7f"
HAZARDS = ["plain", "quote\" back\\slash /solidus", CONTROLS, "\\u001F is text", "é😀  ", ""]
DISPLAY = {"label": "Page Count", "name": "pageCount", "tooltip": 'Pages "in" the content file'}


def record(key, attributes, kind="documents"):
    """A catalogue value whose own fact holds ``attributes``, beside a joined fact under another id."""
    own = {"fields": {"data": {"attributes": attributes, "id": key, "type": kind}}, "scopeId": f"regulations-gov-{kind}"}
    joined = {"fields": {"data": {"attributes": {"text": 7}, "id": key + "-docket", "type": "dockets"}},
              "scopeId": "regulations-gov-dockets"}
    return {"sourceItemId": key, "sourceNativeFacts": [joined, own, {"fields": {"abstract": "a"}, "scopeId": "fr"}]}


def native_rows(table, values):
    """The tool's typed rows for (key, value) pairs: own facts selected and typed by DuckDB alone."""
    with duckdb.connect(config={"TimeZone": "UTC"}) as connection:
        connection.execute("CREATE TABLE catalogue (member_key VARCHAR, occurrence_id VARCHAR, value JSON)")
        connection.executemany("INSERT INTO catalogue VALUES (?, ?, ?)",
                               [(key, f"occurrence:{key}", json.dumps(value)) for key, value in values])
        tool.own_facts(connection.table("catalogue")).create_view("own")
        return {row["member_key"]: row for row in connection.sql(tool.typed_sql(table, "own")).to_arrow_table()
                .to_pylist()}


def test_native_typing_equals_the_python_reference():
    cases = [*({"text": text} for text in [*HAZARDS, None]), {"flag": True}, {"flag": False},
             *({"count": count} for count in (0, -5, 2**31 - 1, -2**31)),
             *({"at": at} for at in ("2005-10-24T04:00:00Z", "2026-09-02T23:59:59Z", "0001-01-01T00:00:00Z",
                                     "9999-12-31T23:59:59Z")),
             {"items": HAZARDS}, {"items": []}, {"displayProperties": [DISPLAY, {**DISPLAY, "label": None}]},
             {"displayProperties": []}, {}]
    values = [(f"k{index}", record(f"k{index}", attributes)) for index, attributes in enumerate(cases)]
    native = native_rows(PROBE, values)
    for key, value in values:
        expected = tool.expected_row(PROBE, key, value)
        assert {name: native[key][name] for name in expected if name != "document_id"} == \
            {name: wanted for name, wanted in expected.items() if name != "document_id"}, key
        assert native[key]["source_occurrence_id"] == f"occurrence:{key}"
    assert native["k7"]["flag"] is True and native["k11"]["count"] == 2**31 - 1
    assert native["k14"]["at"] == datetime(2026, 9, 2, 23, 59, 59, tzinfo=timezone.utc)
    assert native["k17"]["items"] == HAZARDS
    assert native["k19"]["display_properties_json"] == json_column([DISPLAY, {**DISPLAY, "label": None}])


@pytest.mark.parametrize(("attribute", "stated"), [
    ("text", 7), ("text", True), ("text", ["a"]), ("text", {"a": "b"}), ("text", 1.5),
    ("flag", "true"), ("flag", 1),
    ("count", 1.5), ("count", "5"), ("count", 2**31), ("count", -2**31 - 1), ("count", 2**64), ("count", True),
    ("at", "2005-10-24"), ("at", "2005-10-24T04:00:00+01:00"), ("at", "2005-10-24T04:00:00.5Z"),
    ("at", "2005-10-24T04:00:00.123456Z"),
    ("at", "2005-10-24 04:00:00Z"), ("at", "2005-02-30T00:00:00Z"), ("at", "0000-01-01T00:00:00Z"), ("at", 5),
    ("items", ["a", None]), ("items", ["a", 1]), ("items", [["a"]]), ("items", [{"a": "b"}]), ("items", "a"),
    ("displayProperties", [{**DISPLAY, "label": "é"}]), ("displayProperties", [{**DISPLAY, "label": "a\\b"}]),
    ("displayProperties", [{"name": "n", "label": "l", "tooltip": "t"}]), ("displayProperties", [{"label": "l"}]),
    ("displayProperties", [{**DISPLAY, "label": 1}]), ("displayProperties", [None]), ("displayProperties", DISPLAY),
])
def test_values_the_contract_type_does_not_describe_are_refused(attribute, stated):
    values = [("k", record("k", {attribute: stated}))]
    with pytest.raises(duckdb.Error, match=f"documents {attribute} "):
        native_rows(PROBE, values)
    if attribute != "displayProperties":
        with pytest.raises(ValueError, match=attribute):
            tool.expected_row(PROBE, "k", values[0][1])


def test_attribute_columns_follow_the_contract_rules():
    for table in TABLES.values():
        names = [name for name, _, _ in table.columns]
        assert len(set(names)) == len(names)
        assert not set(names) & set(TABLE_CONTRACTS[table.kind].columns), table.name
        assert all(name.endswith("_json") == (kind == JSON) for name, _, kind in table.columns)
        assert [kind for _, kind in tool.layer_schema(table).columns[:2]] == ["VARCHAR", "VARCHAR"]


def _workspace(tmp_path, values):
    workspace = tmp_path / "workspace"
    with CoreWorkspace(workspace) as opened:
        opened.create("catalogue", values)
        with opened.open_state("catalogue") as reader:
            return workspace, reader.pin


def test_real_records_derive_export_and_match_the_catalogue(tmp_path):
    records = json.loads(RECORDS.read_text(encoding="utf-8"))
    workspace, pin = _workspace(tmp_path, [(key, value) for key, value in records])
    receipts, members, own, memory = tmp_path / "receipts", tmp_path / "members", tmp_path / "own.parquet", 2**30
    receipts.mkdir()
    extracted = tool.extract(workspace, "catalogue", pin, own, engine_memory_bytes=memory)
    assert extracted["kinds"] == {"documents": 3, "dockets": 2}
    census = tool.census(own)
    assert census["dockets"]["fields"]['$.attributes."keywords"']["non_null"] == 1
    assert census["documents"]["fields"]['$.links."self"']["non_null"] == 3
    for name, table in TABLES.items():
        derived = tool.derive(workspace, table, own, pin, state_id="catalogue", engine_memory_bytes=memory)
        (receipts / f"derive-{name}.json").write_text(json.dumps(derived))
    receipt = tool.publish(workspace, receipts, members, {"state": "catalogue"}, engine_memory_bytes=memory)

    assert receipt["source"] == {"state": "catalogue", "pin": pin}
    for name, table in TABLES.items():
        member = receipt["tables"][name]["member"]
        assert member["rows"] == extracted["kinds"][table.kind] == member["rowGroupRows"]
        assert dict(member["columns"])["effective_date"] == "TIMESTAMP WITH TIME ZONE"
        assert pq.read_table(members / member["path"]).column(table.key).to_pylist() == sorted(
            key for key, value in records if tool.expected_row(table, key, value) is not None)
        checked = tool.check(workspace, pin, "catalogue", table, members / member["path"],
                             json.loads((receipts / f"derive-{name}.json").read_text()), sample=10,
                             engine_memory_bytes=memory)
        assert checked["rows_equal"] and checked["differing"] == []
        assert checked["compared_keys_values"][0] == member["rows"]
        with CoreWorkspace(workspace, create=False) as opened:
            request = opened.generating_request(receipt["tables"][name]["derivedState"])
        assert {getattr(item, "state_id", None) for item in request.inputs} >= {"catalogue"}
    documents = dict(receipt["tables"]["document_attributes"]["member"]["columns"])
    assert (documents["allow_late_comments"], documents["page_count"], documents["authors"]) == \
        ("BOOLEAN", "INTEGER", "VARCHAR[]")

    every = tool.compare_all(workspace, pin, "catalogue", members, engine_memory_bytes=memory)["tables"]
    assert {name: (result["rows"], result["differing"]) for name, result in every.items()} == \
        {"document_attributes": (3, 0), "docket_attributes": (2, 0)}
    dockets = {row["docket_id"]: row for row in pq.read_table(members / "docket_attributes.parquet").to_pylist()}
    assert dockets["EPA-HQ-OPP-2022-0153"]["keywords"] == ["Glycoprotein", "•\tPZP  •\tPorcine Zona Pellucida"]
    assert dockets["ACF-2006-0004"]["display_properties_json"] == "[]"


SMALL = Table("small", "documents", "document_id", (("flag", BOOLEAN), ("items", LIST)))


def _small_member(path, *, order="document_id", options="", items="['a', document_id]"):
    """Write a small member of SMALL; return the layer digest its rows would have."""
    rows = f"SELECT i::VARCHAR AS document_id, i % 2 = 0 AS flag, {items} AS items FROM range(10, 20) t(i)"
    with duckdb.connect() as connection:
        connection.execute(f"COPY ({rows} ORDER BY {order}) TO '{path}' (FORMAT parquet{options})")
        return connection.execute(f"SELECT {tool._multiset_sql(['document_id', 'flag', 'items'])} FROM ({rows})").fetchone()


@pytest.mark.parametrize(("writer", "bound", "problem"), [
    ({}, None, None),
    ({"order": "document_id DESC"}, None, "out of key order"),
    ({"options": ", COMPRESSION snappy"}, None, "codecs"),
    ({"options": ", COMPRESSION zstd, FIELD_IDS auto"}, None, "field IDs"),
    ({"items": "'a'"}, None, "columns"),
    ({}, ("MAX_ROW_GROUP_BYTES", 1), "row group"),
    ({}, ("MAX_MEMBER_BYTES", 1), "over 1 GiB"),
])
def test_the_member_check_refuses_each_broken_bound(tmp_path, monkeypatch, writer, bound, problem):
    path = tmp_path / "small.parquet"
    digest = _small_member(path, **{"options": ", COMPRESSION zstd", **writer})
    if bound:
        monkeypatch.setattr(tool, *bound)
    if problem is None:
        assert tool._member(path, SMALL, 10, digest)["rows"] == 10
        with pytest.raises(SystemExit, match="where the layer holds 11"):
            tool._member(path, SMALL, 11, digest)
    else:
        with pytest.raises(SystemExit, match=problem):
            tool._member(path, SMALL, 10, None if problem == "columns" else digest)


def test_row_groups_are_sized_from_measured_widths_before_writing():
    with duckdb.connect() as connection:
        uniform = connection.sql("SELECT i, 1000 AS w FROM range(10000) t(i)")
        assert tool.group_rows(uniform, 4096 * 1000) == 4096
        assert tool.group_rows(uniform, 4096 * 1000 - 1) == 2048
        skewed = connection.sql("SELECT i, CASE WHEN i = 5000 THEN 2000000 ELSE 1000 END AS w FROM range(10000) t(i)")
        assert tool.group_rows(skewed, 5_000_000) == 2048
        with pytest.raises(SystemExit, match="a group of 2048 rows"):
            tool.group_rows(skewed, 2_000_000)


def test_extract_refuses_a_member_without_one_own_fact(tmp_path):
    workspace, pin = _workspace(tmp_path, [("k", record("other", {}))])
    with pytest.raises(SystemExit, match="no single own"):
        tool.extract(workspace, "catalogue", pin, tmp_path / "own.parquet", engine_memory_bytes=2**30)


def test_publish_refuses_layers_derived_from_different_pins(tmp_path):
    for index, name in enumerate(TABLES):
        (tmp_path / f"derive-{name}.json").write_text(json.dumps({"catalogue_pin": f"sha256:{index}"}))
    with pytest.raises(SystemExit, match="different catalogue pins"):
        tool.publish(tmp_path / "workspace", tmp_path, tmp_path / "members", {}, engine_memory_bytes=2**30)
