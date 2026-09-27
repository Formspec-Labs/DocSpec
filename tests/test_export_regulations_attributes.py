"""The Regulations.gov attribute export: native spellings equal spicy-docs' helpers, and real records round-trip.

The reference is ``expected_row``: Python's ``json`` and spicy-docs' ``text``
and ``json_column`` over the catalogue value. The tool's rows come from DuckDB
alone, so the two share only the column list.
"""

import json
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest
from spicy_docs.schemas import TABLE_CONTRACTS
from spicy_docs.schemas.tables import json_column

from docspec.runtime import CoreWorkspace
from tools import export_regulations_attributes as tool
from tools.export_regulations_attributes import JSON, TABLES, TEXT, Table

RECORDS = Path(__file__).parent / "fixtures" / "json" / "regulations-gov-catalogue-records.json"
PROBE = Table("probe", "documents", "document_id", (("scalar", TEXT), ("list", JSON)))
CONTROLS = "".join(map(chr, range(32))) + "\x7f"
HAZARDS = ["plain", "quote\" back\\slash /solidus", CONTROLS, "\\u001F is text", "é😀  ", ""]


def record(key, attributes, kind="documents"):
    """A catalogue value whose own fact holds ``attributes``, beside a joined fact under another id."""
    own = {"fields": {"data": {"attributes": attributes, "id": key, "type": kind}}, "scopeId": f"regulations-gov-{kind}"}
    joined = {"fields": {"data": {"attributes": {"list": ["joined"]}, "id": key + "-docket", "type": "dockets"}},
              "scopeId": "regulations-gov-dockets"}
    return {"sourceItemId": key, "sourceNativeFacts": [joined, own]}


def native_rows(table, values):
    """The tool's typed rows for (key, value) pairs: own facts selected and spelled by DuckDB alone."""
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE catalogue (member_key VARCHAR, occurrence_id VARCHAR, value JSON)")
        connection.executemany("INSERT INTO catalogue VALUES (?, ?, ?)",
                               [(key, f"occurrence:{key}", json.dumps(value)) for key, value in values])
        connection.execute(f"CREATE VIEW own AS SELECT member_key, occurrence_id, own[1]->>'$.fields.data.type' AS kind, "
                           f"own[1]->'$.fields.data' AS data FROM (SELECT *, {tool._OWN_FACT} AS own FROM catalogue)")
        relation = connection.sql(tool.typed_sql(table, "own"))
        return {row[0]: dict(zip(relation.columns, row, strict=True)) for row in relation.fetchall()}


def test_native_spellings_equal_spicy_docs_text_and_json_column():
    lists = [HAZARDS, [], [None, True, False, 0, 2**53 + 1, -5], [{"tooltip": CONTROLS, "name": "n", "label": "é"}],
             [{}, {"b": None, "a": 7, "A_1": "x"}], None]
    scalars = [*HAZARDS, True, False, 0, 2**63, -5, None]
    cases = [{"scalar": scalar} for scalar in scalars] + [{"list": items} for items in lists] + [{}]
    values = [(f"k{index}", record(f"k{index}", attributes)) for index, attributes in enumerate(cases)]
    native = native_rows(PROBE, values)
    assert set(native) == {key for key, _ in values}
    for key, value in values:
        expected = tool.expected_row(PROBE, key, value)
        assert {name: native[key][name] for name in ("scalar", "list_json")} == \
            {name: expected[name] for name in ("scalar", "list_json")}, key
        assert native[key]["source_occurrence_id"] == f"occurrence:{key}"
    hazards = native[f"k{len(scalars)}"]["list_json"]
    assert hazards == json_column(HAZARDS) and "\\u007f" in hazards and "\\ud83d\\ude00" in hazards


@pytest.mark.parametrize("attributes", [{"scalar": 1.5}, {"scalar": {"a": "b"}}, {"scalar": ["a"]}, {"list": [1.5]},
                                        {"list": [["nested"]]}, {"list": {"a": "b"}}, {"list": [{"a": {"b": 1}}]},
                                        {"list": [{"a b": 1}]}])
def test_values_the_spellings_cannot_reproduce_refuse(attributes):
    with pytest.raises(duckdb.Error, match="documents (scalar|list)"):
        native_rows(PROBE, [("k", record("k", attributes))])


def test_attribute_columns_do_not_repeat_the_thin_tables():
    for table in TABLES.values():
        names = [name for name, _, _ in table.columns]
        assert len(set(names)) == len(names)
        assert not set(names) & set(TABLE_CONTRACTS[table.kind].columns), table.name
        assert all(name == name.lower() and (spelling == JSON) == name.endswith("_json")
                   for name, _, spelling in table.columns)


def test_real_records_derive_export_and_match_the_catalogue(tmp_path):
    records = json.loads(RECORDS.read_text(encoding="utf-8"))
    workspace, receipts, members = tmp_path / "workspace", tmp_path / "receipts", tmp_path / "members"
    receipts.mkdir()
    with CoreWorkspace(workspace) as opened:
        opened.create("catalogue", [(key, value) for key, value in records])
        with opened.open_state("catalogue") as reader:
            pin = reader.pin
    memory = 2**30
    own = tmp_path / "own.parquet"
    extracted = tool.extract(workspace, "catalogue", pin, own, engine_memory_bytes=memory)
    assert extracted["kinds"] == {"documents": 3, "dockets": 2}
    for name, table in TABLES.items():
        derived = tool.derive(workspace, table, own, pin, state_id="catalogue", engine_memory_bytes=memory)
        (receipts / f"derive-{name}.json").write_text(json.dumps(derived))
    receipt = tool.publish(workspace, receipts, members, {"state": "catalogue"}, engine_memory_bytes=memory)

    assert receipt["source"] == {"state": "catalogue", "pin": pin}
    for name, table in TABLES.items():
        member = receipt["tables"][name]["member"]
        assert member["rows"] == extracted["kinds"][table.kind]
        assert member["columns"] == [[table.key, "VARCHAR"], *([column, "VARCHAR"] for column, _, _ in table.columns)]
        parquet = pq.ParquetFile(members / member["path"])
        assert not any(field.metadata for field in parquet.schema_arrow)
        assert parquet.read().column(table.key).to_pylist() == sorted(
            key for key, value in records if tool.expected_row(table, key, value) is not None)
        checked = tool.check(workspace, pin, "catalogue", table, members / member["path"],
                             json.loads((receipts / f"derive-{name}.json").read_text()), sample=10,
                             engine_memory_bytes=memory)
        assert checked["rows_equal"] and checked["differing"] == []
        assert checked["compared_keys_values"][0] == member["rows"]
        with CoreWorkspace(workspace, create=False) as opened:
            request = opened.generating_request(receipt["tables"][name]["derivedState"])
        assert {getattr(item, "state_id", None) for item in request.inputs} >= {"catalogue"}

    every = tool.compare_all(workspace, pin, "catalogue", members, engine_memory_bytes=memory)["tables"]
    assert {name: (result["rows"], result["differing"]) for name, result in every.items()} == \
        {"document_attributes": (3, 0), "docket_attributes": (2, 0)}
    dockets = pq.read_table(members / "docket_attributes.parquet").to_pylist()
    keywords = {row["docket_id"]: row["keywords_json"] for row in dockets}
    assert keywords["EPA-HQ-OPP-2022-0153"] == '["Glycoprotein","\\u2022\\tPZP  \\u2022\\tPorcine Zona Pellucida"]'
    assert {row["docket_id"]: row["display_properties_json"] for row in dockets}["ACF-2006-0004"] == "[]"

