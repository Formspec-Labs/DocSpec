"""Independent Python and DuckDB agreement for versioned table identities.

The shared spelling corpus, bound through DuckDB parameters rather than the
runtime oracle's Arrow tables, must spell exactly as the Python reference.
Every power of two and a random sample of doubles must spell as repr or
refuse, never as another value.
"""

from datetime import datetime, timezone
import math
import random
import struct

import duckdb
import pytest

from docspec.adapters.storage.iceberg import identifier
from docspec.adapters.storage.table_occurrences import reference_identity
from docspec.adapters.storage.table_sql import (identity_relation, json_string_sql, membership_json_sql,
    occurrence_json_sql, occurrence_urn_sql, row_json_sql)
from docspec.domain import core
from docspec.domain.core_admission import inline_occurrence_payload, record_value
from docspec.domain.identity import canonical_value_bytes
from docspec.domain.table_rows import (ROUND_TRIP_TRAPS, ROW_RULE, SPELLING_COLUMNS, SPELLING_ROWS, KeySpelling,
    TableIdentity, table_columns, table_occurrence_id, table_row_bytes, table_row_digest, table_row_value, table_type)


def native_rows(columns, rows, *, session_timezone="UTC"):
    """Bind typed source values separately from the generated SQL expressions."""
    with duckdb.connect() as connection:
        connection.execute("SET TimeZone = ?", [session_timezone])
        declarations = ", ".join(f"{identifier(name)} {table_type(kind)}" for name, kind in columns)
        connection.execute("CREATE TABLE source (" + declarations + ")")
        connection.executemany("INSERT INTO source VALUES (" + ",".join("?" for _ in columns) + ")",
                               [[row[name] for name, _ in columns] for row in rows])
        return connection.execute("SELECT row_json, 'sha256:' || sha256(row_json) FROM (SELECT "
                                  + row_json_sql(columns, qualifier="s") + " AS row_json FROM source s)").fetchall()


def test_shared_corpus_matches_the_independent_reference():
    expected = [(table_row_bytes(row, SPELLING_COLUMNS).decode(), table_row_digest(row, SPELLING_COLUMNS))
                for row in SPELLING_ROWS]
    assert native_rows(SPELLING_COLUMNS, SPELLING_ROWS) == expected
    assert native_rows(SPELLING_COLUMNS, SPELLING_ROWS, session_timezone="America/New_York") == expected
    assert ROW_RULE == "docspec-table-row/1"
    assert table_row_value(SPELLING_ROWS[0], SPELLING_COLUMNS)["big"] == str(-(2**63))


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


def test_doubles_spell_as_repr_or_refuse_never_as_another_value():
    rng = random.Random(27)
    values = [0.0, -0.0, 1e-7, 1e-6, 1e-5, 1e-4, 1e15, 1e16, 1e20, 1e21, 0.1, 123456.789,
              5e-324, 2.2250738585072014e-308, 1.7976931348623157e308,
              float("nan"), float("inf"), float("-inf"), *ROUND_TRIP_TRAPS]
    values += [sign * math.ldexp(1.0, exponent) for exponent in range(-1074, 1024) for sign in (1.0, -1.0)]
    values += [struct.unpack(">d", rng.getrandbits(64).to_bytes(8))[0] for _ in range(512)]
    column, refused = (("number", "DOUBLE"),), set()
    with duckdb.connect() as connection:
        spell = f"SELECT {row_json_sql(column)} FROM (SELECT ?::DOUBLE AS number)"
        for value in values:
            try:
                native = connection.execute(spell, [value]).fetchone()[0]
            except duckdb.InvalidInputException as error:
                assert "no round-trip spelling" in str(error)
                refused.add(struct.pack(">d", value))
                continue
            assert native.encode() == table_row_bytes({"number": value}, column), value
    # Only the known traps may refuse; a fixed DuckDB may spell them instead.
    assert refused <= {struct.pack(">d", value) for value in ROUND_TRIP_TRAPS}


def test_timestamps_must_match_their_declared_zone():
    for value, kind in [(datetime(2026, 1, 1), "TIMESTAMPTZ"), (datetime(2026, 1, 1, tzinfo=timezone.utc), "TIMESTAMP")]:
        with pytest.raises(ValueError, match="timezone"):
            table_row_bytes({"value": value}, (("value", kind),))


def test_native_identity_membership_and_occurrence_match_existing_owners():
    columns = (("field", "VARCHAR"), ("key", "VARCHAR"))
    identity = TableIdentity("source\x00family", 'logical\n"table', KeySpelling("value", "1", ("key",)), columns)
    row = {"field": "row\x1f😀", "key": "key\x1f\\u001F😀"}
    key, digest, expected = reference_identity(identity, row)
    payload = table_row_bytes(row, columns)
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE source(field VARCHAR, key VARCHAR)")
        connection.execute("INSERT INTO source VALUES (?, ?)", [row["field"], row["key"]])
        identity_relation(connection.table("source"), identity).project(
            f"member_key, row_digest, {occurrence_urn_sql('occurrence_hash')} AS occurrence_id").create_view("minted")
        member_key, row_digest, occurrence, membership, entity = connection.execute(
            "SELECT member_key, row_digest, occurrence_id, " + membership_json_sql() + ", "
            + occurrence_json_sql(row_json_sql(columns, qualifier="s")) + " FROM source s, minted").fetchone()
    assert (member_key, row_digest, occurrence) == (key, bytes.fromhex(digest[7:]), expected)
    assert membership.encode() == canonical_value_bytes(record_value(core.Membership(member_key=key, occurrence_id=expected), core.Membership))
    assert entity.encode() == inline_occurrence_payload(expected, payload)
    assert table_occurrence_id(identity.family, "other", key, digest) != expected
    assert table_occurrence_id(identity.family, identity.table, "other", digest) != expected


@pytest.mark.parametrize("kind", ["FLOAT", "HUGEINT", "UBIGINT", "DECIMAL(20,2)", "BLOB", "STRUCT(a INTEGER)", "INTEGER[]",
                                  "TIMESTAMP_NS", "SMALLINT", "TIMESTAMP_S", "TIMESTAMP_MS"])
def test_unsupported_types_refuse_even_null_rows(kind):
    with pytest.raises(ValueError, match="unsupported"):
        table_row_bytes({"value": None}, (("value", kind),))
    with pytest.raises(ValueError, match="unsupported"):
        row_json_sql((("value", kind),))


@pytest.mark.parametrize("kind,value", [("BOOLEAN", 1), ("INTEGER", True), ("INTEGER", -(2**31) - 1),
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
