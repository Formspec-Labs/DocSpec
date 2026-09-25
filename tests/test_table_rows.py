"""Independent Python and DuckDB agreement for versioned table identities."""

from datetime import date, datetime, timedelta, timezone
import random
import struct

import duckdb
import pytest

from docspec.adapters.storage.iceberg import identifier
from docspec.adapters.storage.table_sql import (
    json_string_sql, membership_json_sql, occurrence_id_sql, occurrence_json_sql,
    row_digest_sql, row_json_sql,
)
from docspec.domain import core
from docspec.domain.core_admission import inline_occurrence_payload, record_value
from docspec.domain.identity import canonical_value_bytes
from docspec.domain.table_rows import (
    ROW_RULE, SAFE_INTEGER, table_columns, table_occurrence_id, table_row_bytes,
    table_row_digest, table_row_value, table_type,
)


def native_rows(columns, rows, *, session_timezone="UTC"):
    """Bind typed source values separately from the generated SQL expressions."""
    with duckdb.connect() as connection:
        connection.execute("SET TimeZone = ?", [session_timezone])
        declarations = ", ".join(f"{identifier(name)} {table_type(kind)}" for name, kind in columns)
        connection.execute("CREATE TABLE source (" + declarations + ")")
        connection.executemany("INSERT INTO source VALUES (" + ",".join("?" for _ in columns) + ")",
                               [[row[name] for name, _ in columns] for row in rows])
        return connection.execute("SELECT " + row_json_sql(columns, qualifier="s") + ", "
                                  + row_digest_sql(columns, qualifier="s") + " FROM source s").fetchall()


def test_supported_typed_corpus_matches_independent_reference():
    columns = (("text\x1f", "VARCHAR"), ("small", "SMALLINT"), ("integer", "INTEGER"),
               ("big", "BIGINT"), ("double", "DOUBLE"), ("boolean", "BOOLEAN"),
               ("day", "DATE"), ("moment", "TIMESTAMP"), ("zoned", "TIMESTAMPTZ"),
               ("items", "VARCHAR[]"), ("\ue000", "VARCHAR"), ("\U00010000", "VARCHAR"))
    values = [
        ("quote\" slash\\ literal\\u001F\n\x00" + "é😀", -32768, -(2**31), -2**63, -0.0, False,
         date(1, 1, 1), datetime(1, 1, 1), datetime(2026, 9, 25, 1, 2, 3, 123400, tzinfo=timezone(timedelta(hours=5, minutes=30))),
         ["\x00\x1f", None, "back\\u001F", "😀"], "bmp", "supplementary"),
        ("", 32767, 2**31 - 1, SAFE_INTEGER, float("inf"), True,
         date(9999, 12, 31), datetime(2026, 9, 25, 1, 2, 3, 1), datetime(2026, 9, 25, tzinfo=timezone.utc),
         [], "last", "first"),
        (None, None, None, SAFE_INTEGER + 1, struct.unpack(">d", bytes.fromhex("fff8000000000001"))[0], None,
         None, None, None, None, None, None),
        (None, 0, 0, -(SAFE_INTEGER + 1), float("-inf"), True,
         date(2024, 2, 29), datetime(2026, 9, 25, 1, 2, 3, 999999), None,
         [None], "\u2028", "\u2029"),
    ]
    rows = [dict(zip((name for name, _ in columns), row, strict=True)) for row in values]
    expected = [(table_row_bytes(row, columns).decode(), table_row_digest(row, columns)) for row in rows]
    assert native_rows(columns, rows) == expected
    assert native_rows(columns, rows, session_timezone="America/New_York") == expected
    assert ROW_RULE == "docspec-table-row/1"
    assert table_row_value(rows[0], columns)["big"] == str(-(2**63))


def test_all_controls_and_literal_escapes_have_exact_string_bytes():
    values = [chr(code) for code in range(32)] + ["\\u001F", "\\n", "\"\\/", "é😀", "\u2028\u2029", ""]
    values += ["prefix" + value + "suffix" for value in values]
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE strings(value VARCHAR)")
        connection.executemany("INSERT INTO strings VALUES (?)", [(value,) for value in values])
        actual = connection.execute("SELECT " + json_string_sql('"value"') + " FROM strings").fetchall()
    assert [row[0].encode() for row in actual] == [canonical_value_bytes(value) for value in values]


def test_keys_use_utf16_order_and_safe_sql_quoting():
    names = ["\U00010000", "\ue000", 'quote"key', "apostrophe'key", "newline\nkey"]
    names += ["control" + chr(code) for code in range(1, 32)]
    columns = tuple((name, "VARCHAR") for name in reversed(names))
    row = {name: "value\x1f" + name for name in names}
    actual = native_rows(columns, [row])[0][0].encode()
    assert actual == table_row_bytes(row, columns)
    assert actual.index("𐀀".encode()) < actual.index("".encode())


def test_double_shortest_round_trip_strings_match_repr():
    rng = random.Random(27)
    values = [0.0, -0.0, 1e-7, 1e-6, 1e-5, 1e-4, 1e15, 1e16, 1e20, 1e21,
              5e-324, 2.2250738585072014e-308, 1.7976931348623157e308,
              float("nan"), float("inf"), float("-inf")]
    values += [struct.unpack(">d", rng.getrandbits(64).to_bytes(8))[0] for _ in range(512)]
    rows = [{"number": value} for value in values]
    columns = (("number", "DOUBLE"),)
    assert native_rows(columns, rows) == [(table_row_bytes(row, columns).decode(), table_row_digest(row, columns)) for row in rows]


def test_timestamp_precision_is_preserved_or_refused():
    columns = (("second", "TIMESTAMP_S"), ("millisecond", "TIMESTAMP_MS"))
    row = {"second": datetime(2026, 9, 25, 12, 34, 56), "millisecond": datetime(2026, 9, 25, 12, 34, 56, 123000)}
    assert native_rows(columns, [row])[0][0].encode() == table_row_bytes(row, columns)
    for kind in ("TIMESTAMP_S", "TIMESTAMP_MS"):
        with pytest.raises(ValueError, match="precision"):
            table_row_bytes({"value": datetime(2026, 1, 1, microsecond=1)}, (("value", kind),))
    for value, kind in [(datetime(2026, 1, 1), "TIMESTAMPTZ"), (datetime(2026, 1, 1, tzinfo=timezone.utc), "TIMESTAMP")]:
        with pytest.raises(ValueError, match="timezone"):
            table_row_bytes({"value": value}, (("value", kind),))


def test_native_identity_membership_and_occurrence_match_existing_owners():
    columns = (("field", "VARCHAR"),)
    value = {"field": "row\x1f😀"}
    payload, digest = table_row_bytes(value, columns), table_row_digest(value, columns)
    family, table, key = "source\x00family", 'logical\n"table', "key\x1f\\u001F😀"
    expected = table_occurrence_id(family, table, key, digest)
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE source(member_key VARCHAR, row_digest VARCHAR, row_json VARCHAR)")
        connection.execute("INSERT INTO source VALUES (?,?,?)", [key, digest, payload.decode()])
        relation = "SELECT *, " + occurrence_id_sql(family, table) + " AS occurrence_id FROM source"
        occurrence, membership, entity = connection.execute("SELECT occurrence_id, " + membership_json_sql()
            + ", " + occurrence_json_sql('"row_json"') + " FROM (" + relation + ")").fetchone()
    assert occurrence == expected
    assert membership.encode() == canonical_value_bytes(record_value(core.Membership(member_key=key, occurrence_id=expected), core.Membership))
    assert entity.encode() == inline_occurrence_payload(expected, payload)
    assert table_occurrence_id(family, "other", key, digest) != expected
    assert table_occurrence_id(family, table, "other", digest) != expected


@pytest.mark.parametrize("kind", ["FLOAT", "HUGEINT", "UBIGINT", "DECIMAL(20,2)", "BLOB", "STRUCT(a INTEGER)", "INTEGER[]", "TIMESTAMP_NS"])
def test_unsupported_types_refuse_even_null_rows(kind):
    with pytest.raises(ValueError, match="unsupported"):
        table_row_bytes({"value": None}, (("value", kind),))
    with pytest.raises(ValueError, match="unsupported"):
        row_json_sql((("value", kind),))


@pytest.mark.parametrize("kind,value", [("BOOLEAN", 1), ("INTEGER", True), ("SMALLINT", 2**15),
    ("INTEGER", 2**31), ("BIGINT", 2**63), ("DOUBLE", 1), ("DATE", "2026-09-25"),
    ("VARCHAR[]", [1]), ("VARCHAR", b"bytes")])
def test_reference_refuses_type_coercion(kind, value):
    with pytest.raises(ValueError):
        table_row_bytes({"value": value}, (("value", kind),))


def test_closed_rows_column_names_and_temporal_bounds():
    for columns in [(), (("a", "VARCHAR"), ("a", "VARCHAR")), (("a", "VARCHAR"), ("A", "VARCHAR")),
                    (("", "VARCHAR"),), (("\0", "VARCHAR"),), (("\ud800", "VARCHAR"),)]:
        with pytest.raises(ValueError):
            table_columns(columns)
    for row in [{}, {"a": "yes", "extra": 1}]:
        with pytest.raises(ValueError, match="exactly"):
            table_row_bytes(row, (("a", "VARCHAR"),))
    with duckdb.connect() as connection:
        for value in ("infinity", "-infinity", "10000-01-01"):
            with pytest.raises(duckdb.InvalidInputException, match="outside years"):
                connection.execute("SELECT " + row_json_sql((("value", "DATE"),)) + " FROM (SELECT ?::DATE AS value)", [value])
