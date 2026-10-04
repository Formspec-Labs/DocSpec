"""Native ETL values retain exact row identity through Python, SQL, Arrow and Iceberg."""
from datetime import date
from decimal import Decimal

import duckdb
import pyarrow as pa
import pytest
from pyiceberg.io.pyarrow import _pyarrow_to_schema_without_ids

from docspec.adapters.storage.batches import table_arrow_schema
from docspec.adapters.storage.iceberg import table_columns as iceberg_columns
from docspec.adapters.storage.table_sql import row_json_sql, rows_differ_sql
from docspec.domain.storage import TableSchema
from docspec.domain.table_rows import table_row_bytes, table_type

COLUMNS = (("id", "VARCHAR"), ("amount", "DECIMAL(38,8)"), ("cycles", "INTEGER[]"),
           ("nested", 'STRUCT("name" VARCHAR, "values" STRUCT("x" DOUBLE, "day" DATE, "money" DECIMAL(20,2))[])[]'))


def test_nested_exact_values_share_one_row_spelling():
    rows = [
        {"id": "one", "amount": Decimal("123456789012345678901234567890.12345678"), "cycles": [2024, None, 2026],
         "nested": [{"name": "escape\u0001é", "values": [{"x": -0.0, "day": date(2026, 10, 3), "money": Decimal("1.20")}, None]}, None]},
        {"id": "empty", "amount": Decimal("-0.00000000"), "cycles": [], "nested": []},
        {"id": "null", "amount": None, "cycles": None, "nested": None},
    ]
    schema = TableSchema("native", COLUMNS)
    table = pa.Table.from_pylist(rows, schema=table_arrow_schema(schema.columns))
    with duckdb.connect() as con:
        con.register("rows", table)
        got = con.execute("SELECT " + row_json_sql(schema.columns) + " FROM rows").fetchall()
    assert [value.encode() for value, in got] == [table_row_bytes(row, schema.columns) for row in rows]
    assert iceberg_columns(_pyarrow_to_schema_without_ids(table.schema)) == schema.columns


def test_nested_negative_zero_is_a_distinct_version():
    columns = (("items", "DOUBLE[]"),)
    with duckdb.connect() as con:
        con.execute("CREATE TABLE a(items DOUBLE[])")
        con.execute("CREATE TABLE b(items DOUBLE[])")
        con.execute("INSERT INTO a VALUES ([-0.0::DOUBLE])")
        con.execute("INSERT INTO b VALUES ([0.0::DOUBLE])")
        assert con.execute("SELECT " + rows_differ_sql(columns, "a", "b") + " FROM a,b").fetchone() == (True,)


@pytest.mark.parametrize("kind", ['STRUCT("x" INTEGER, "X" INTEGER)', 'DECIMAL(39,2)', 'STRUCT(x VARCHAR); DROP TABLE x'])
def test_invalid_nested_types_refuse(kind):
    with pytest.raises(ValueError):
        table_type(kind)


@pytest.mark.parametrize("value", [Decimal("1.001"), Decimal("1000"), Decimal("NaN")])
def test_decimal_never_rounds_or_overflows(value):
    with pytest.raises(ValueError):
        table_row_bytes({"money": value}, (("money", "DECIMAL(5,2)"),))


def test_native_nested_storage_round_trip(tmp_path):
    from contextlib import closing
    from docspec.adapters.storage import IcebergRecordStorage

    schema = TableSchema("nested-storage", COLUMNS)
    row = {"id": "held", "amount": Decimal("123.12345678"), "cycles": [2024, None, 2026],
           "nested": [{"name": "repeat", "values": [{"x": -0.0, "day": date(2026, 10, 3), "money": Decimal("-9.01")}]}, None]}
    table = pa.Table.from_pylist([row], schema=table_arrow_schema(schema.columns))
    with closing(IcebergRecordStorage(tmp_path)) as storage:
        layer = storage.write_table(table.to_batches(), layer_kind="native-etl", schema=schema)
        storage.verify(layer.reference)
        with layer.relation() as relation:
            got = relation.to_arrow_table().to_pylist()
        assert got == [row]
        assert table_row_bytes(got[0], schema.columns) == table_row_bytes(row, schema.columns)
