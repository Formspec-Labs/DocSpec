"""Independent Python and DuckDB agreement for versioned table identities.

The shared spelling corpus, bound through DuckDB parameters rather than the
runtime oracle's Arrow tables, must spell exactly as the Python reference.
Every power of two and a random sample of doubles must spell as repr or
refuse, never as another value.
"""

from datetime import date, datetime, timedelta, timezone
import hashlib
import math
import random
import struct
import sys

import duckdb
import pytest

from docspec.adapters.storage.iceberg import identifier
from docspec.adapters.storage.table_occurrences import reference_identity
from docspec.adapters.storage.table_sql import (declared_spelling, identity_relation, json_array_sql, json_string_sql,
    key_components, membership_json_sql, occurrence_json_sql, occurrence_urn_sql, reference_member_key, row_json_sql)
from docspec.domain import core
from docspec.domain.core_admission import inline_occurrence_payload, record_value
from docspec.domain.identity import canonical_value_bytes
from docspec.domain.table_rows import (AT_JOINED_KEY_COLUMNS, AT_JOINED_KEY_ROWS, DATED_KEY_COLUMNS, DATED_KEY_ROWS,
    KEY_TYPES, ROUND_TRIP_TRAPS, ROW_RULE, SEGMENT_KEY_COLUMNS, SEGMENT_KEY_ROWS, SPELLING_COLUMNS, SPELLING_ROWS,
    WIDE_SEGMENT_KEY_COLUMNS, WIDE_SEGMENT_KEY_ROWS, KeySpelling, TableIdentity, table_columns, table_occurrence_id,
    table_row_bytes, table_row_digest, table_row_value, table_type)
from docspec.errors import IntegrityError


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


def native_identities(columns, rows, identity):
    """The identity pass over ``rows`` bound through DuckDB parameters into a typed table."""
    with duckdb.connect() as connection:
        declarations = ", ".join(f"{identifier(name)} {table_type(kind)}" for name, kind in columns)
        connection.execute(f"CREATE TABLE source ({declarations})")
        connection.executemany(f"INSERT INTO source VALUES ({', '.join('?' for _ in columns)})",
                               [[row[name] for name, _ in columns] for row in rows])
        return identity_relation(connection.table("source"), identity).fetchall()


def test_dated_key_components_spell_the_reference_key():
    identity = TableIdentity("family", "table", KeySpelling("federal-register-source-record-id", "1",
                                                            ("document_number", "publication_date")), DATED_KEY_COLUMNS)
    native = native_identities(DATED_KEY_COLUMNS, DATED_KEY_ROWS, identity)
    references = [reference_identity(identity, row) for row in DATED_KEY_ROWS]
    assert sorted(native) == sorted((key, bytes.fromhex(digest[7:]), bytes.fromhex(urn.rsplit(":", 1)[1]))
                                    for key, digest, urn in references)
    assert {key for key, _, _ in references} == {"2026-\x1f1@2026-09-25", "x@y@0001-01-01", " @9999-12-31"}


@pytest.mark.parametrize("columns,rows", [(SEGMENT_KEY_COLUMNS, SEGMENT_KEY_ROWS),
                                          (WIDE_SEGMENT_KEY_COLUMNS, WIDE_SEGMENT_KEY_ROWS)])
def test_segment_keys_spell_the_reference_and_split_back_into_their_components(columns, rows):
    spelling = KeySpelling("member-segment", "1", ("member_key", "segment_index"))
    identity = TableIdentity("urn:definition", "segments", spelling, columns)
    native = native_identities(columns, rows, identity)
    references = [reference_identity(identity, row) for row in rows]
    assert sorted(native) == sorted((key, bytes.fromhex(digest[7:]), bytes.fromhex(urn.rsplit(":", 1)[1]))
                                    for key, digest, urn in references)
    # The index after the last "#" is a canonical integer, so each key splits back into its one pair.
    assert [key_components(spelling, key) for key, _, _ in references] == [
        ((row["member_key"], str(row["segment_index"])),) for row in rows]
    assert len({key for key, _, _ in references}) == len(rows)
    for key in ("a", "a#", "#3", "a#03", "a#-0", "a#+3", "a# 3", "a#3.0"):
        assert key_components(spelling, key) == ()



# Awkward text: controls, quotes, a backslash, whitespace, DEL, separators and non-BMP characters; never "@".
_AWKWARD = [chr(code) for code in range(32)] + list("\"\\/ #-:é  \x7f😀𝄞") + ["ab", "EPA-HQ", "118"]
_JOINED_FIELDS = (("bill_id", "day", "seq", "wide"), ("wide", "bill_id"), ("day", "seq", "bill_id", "wide", "body"))


def generated_joined_rows(count, seed):
    rng = random.Random(seed)

    def text():
        return "".join(rng.choice(_AWKWARD) for _ in range(rng.randrange(1, 6)))
    return [dict(zip((name for name, _ in AT_JOINED_KEY_COLUMNS), values, strict=True)) for values in (
        (text(), date(1, 1, 1) + timedelta(days=rng.randrange(3_652_059)), rng.randrange(-(2**31), 2**31),
         rng.choice([rng.randrange(-(2**63), 2**63), rng.randrange(-(2**53), 2**53)]), text())
        for _ in range(count))]


@pytest.mark.parametrize("fields", _JOINED_FIELDS)
def test_at_joined_keys_spell_the_reference_over_text_dates_and_integers(fields):
    """The generic composite spelling over any two or more VARCHAR, DATE, INTEGER or BIGINT fields, in declared order.

    Its SQL must equal spicy-docs' at_joined_key on every row, and each key must split back into exactly its
    components, so a lookup pushes one component tuple into the scan.
    """
    rows = generated_joined_rows(300, 20260928) + [row for row in AT_JOINED_KEY_ROWS if row["body"]]
    identity = TableIdentity("oracle", "joined", KeySpelling("at-joined", "1", fields), AT_JOINED_KEY_COLUMNS)
    references = [reference_identity(identity, row) for row in rows]
    assert sorted(native_identities(AT_JOINED_KEY_COLUMNS, rows, identity)) == sorted(
        (key, bytes.fromhex(digest[7:]), bytes.fromhex(urn.rsplit(":", 1)[1])) for key, digest, urn in references)
    text = {"VARCHAR": str, "DATE": date.isoformat, "INTEGER": str, "BIGINT": str}
    kinds = dict(AT_JOINED_KEY_COLUMNS)
    assert [key_components(identity.key, key) for key, _, _ in references] == [
        (tuple(text[kinds[field]](row[field]) for field in fields),) for row in rows]
    assert references[0][0] == "@".join(text[kinds[field]](rows[0][field]) for field in fields)
    for key in ("", "a", "@".join("a" * (len(fields) - 1)), "@".join("a" * (len(fields) + 1)),
                "@" + "@".join("a" * (len(fields) - 1)), "@".join("a" * (len(fields) - 1)) + "@"):
        assert key_components(identity.key, key) == ()  # wrong arity or an empty component


@pytest.mark.parametrize("change,reference_refusal,native_refusal", [
    ({"bill_id": "118@hr-1"}, "refuses a component holding '@'", "holds the @ joiner"),
    ({"bill_id": "@"}, "refuses a component holding '@'", "holds the @ joiner"),
    ({"bill_id": ""}, "member key components must be nonempty", "NULL or empty component"),
    ({"bill_id": None}, "member key components must be nonempty", "NULL or empty component"),
    ({"day": None}, "member key components must be nonempty", "NULL or empty component"),
    ({"seq": None}, "member key components must be nonempty", "NULL or empty component"),
])
def test_at_joined_refuses_an_empty_component_or_one_holding_its_joiner(change, reference_refusal, native_refusal):
    """A component holding "@" would make the key ambiguous; both spellings refuse the row rather than escape it."""
    identity = TableIdentity("oracle", "joined", KeySpelling("at-joined", "1", _JOINED_FIELDS[0]), AT_JOINED_KEY_COLUMNS)
    row = {**AT_JOINED_KEY_ROWS[0], **change}
    with pytest.raises(ValueError, match=reference_refusal):
        reference_member_key(identity, row)
    with pytest.raises(duckdb.InvalidInputException, match=native_refusal):
        native_identities(AT_JOINED_KEY_COLUMNS, [AT_JOINED_KEY_ROWS[2], row], identity)


def test_at_joined_serves_any_composite_identity_of_key_types_and_nothing_else():
    for count in (2, 3, 8):
        fields = tuple(f"c{index}" for index in range(count))
        for kind in sorted(KEY_TYPES):
            assert declared_spelling(KeySpelling("at-joined", "1", fields), (kind,) * count).fields == range(2, sys.maxsize)
    with pytest.raises(IntegrityError, match="not declared for these fields$"):
        declared_spelling(KeySpelling("at-joined", "1", ("bill_id",)), ("VARCHAR",))
    for kind in ("DOUBLE", "BOOLEAN", "TIMESTAMP", "TIMESTAMPTZ", "VARCHAR[]"):
        with pytest.raises(IntegrityError, match="not declared for these fields' types"):
            declared_spelling(KeySpelling("at-joined", "1", ("bill_id", "other")), ("VARCHAR", kind))
    # The Federal Register keeps its own sealed name for the same two-component bytes.
    fr = KeySpelling("federal-register-source-record-id", "1", ("document_number", "publication_date"))
    with pytest.raises(IntegrityError, match="not declared for these fields$"):
        declared_spelling(KeySpelling("federal-register-source-record-id", "1", ("document_number", "day")))
    assert declared_spelling(fr, ("VARCHAR", "DATE")).fields == ("document_number", "publication_date")


def test_all_controls_and_literal_escapes_have_exact_string_bytes():
    values = [chr(code) for code in range(32)] + ["\\u001F", "\\n", "\"\\/", "é😀", "\u2028\u2029", ""]
    values += ["prefix" + value + "suffix" for value in values]
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE strings(value VARCHAR)")
        connection.executemany("INSERT INTO strings VALUES (?)", [(value,) for value in values])
        actual = connection.execute("SELECT " + json_string_sql('"value"') + " FROM strings").fetchall()
    assert [row[0].encode() for row in actual] == [canonical_value_bytes(value) for value in values]


# Engine's prepared-row IDs, from its own stable_id (spicyengine 71a325b, indexing/prepared_table.py): the sha256 of
# json.dumps([source_id, member_key], ensure_ascii=False, sort_keys=True, separators=(",", ":")).
ENGINE_IDS = (
    ("federal-register", "2015-03474@2015-02-19", "4a80f235e44ce487fef32cfab8db4c0f51331841f185f6148497d974be774c8b"),
    ("regulations-gov", "EPA-HQ-OAR-2021-0317-0001", "d69afe458a9c3ea2f2c1d5796d54c2ccfc92fb5de637e6f2c089eea1f796fa35"),
    ("federal-register", "quote\"back\\slash\x00\x1f\n\u2028é😀",
     "2666cb3c3530d68974b6c3f86bdd85e1564e902722fd19f65b226eddc8143d8c"),
    ("federal-register", "doc#1#12", "24b9b5cd82fe4a3e1204ecddf6d904443101d4888efd00526ebbb8577997ef08"),
)


def test_engine_prepared_ids_are_the_sha256_of_a_canonical_json_array():
    with duckdb.connect() as connection:
        connection.execute("CREATE TABLE ids (source_id VARCHAR, member_key VARCHAR)")
        connection.executemany("INSERT INTO ids VALUES (?, ?)", [(source, key) for source, key, _ in ENGINE_IDS])
        native = dict(connection.execute("SELECT member_key, sha256(" + json_array_sql("source_id", "member_key")
                                         + ") FROM ids").fetchall())
    for source, key, expected in ENGINE_IDS:
        assert native[key] == expected
        assert hashlib.sha256(canonical_value_bytes([source, key])).hexdigest() == expected


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


@pytest.mark.parametrize("kind", ["FLOAT", "HUGEINT", "UBIGINT", "DECIMAL(39,2)", "BLOB", "STRUCT(a UNKNOWN)", "HUGEINT[]",
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
