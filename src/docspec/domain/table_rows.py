"""Python reference for docspec-table-row/1 spellings and occurrence identity, and a table's identity rules."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
import re
import struct

from docspec.domain.identity import canonical_value_bytes, require_sha256, require_text, sha256_digest, stable_urn
from docspec.domain.storage import TABLE_TYPES, TableSchema, check_column_names


ROW_RULE = "docspec-table-row/1"
# A derived layer's reserved columns (C29): the source member's key, the
# occurrence(s) each row was derived from, and a one-to-many row's position.
DERIVED_MEMBER, SOURCE_OCCURRENCE, SEGMENT_INDEX = "member_key", "source_occurrence_id", "segment_index"
SAFE_INTEGER = 2**53 - 1
_INTEGER_BITS = {"INTEGER": 32, "BIGINT": 64}
# Every type a table layer holds, except BLOB, which no digest spells.
_TYPES = TABLE_TYPES - {"BLOB"}
_ALIASES = {"BOOL": "BOOLEAN", "INT": "INTEGER", "INT4": "INTEGER", "INT8": "BIGINT", "STRING": "VARCHAR",
            "TEXT": "VARCHAR", "TIMESTAMP WITH TIME ZONE": "TIMESTAMPTZ", "LIST<VARCHAR>": "VARCHAR[]"}


def table_type(kind):
    """Normalize a supported logical type without importing a storage engine."""
    if not isinstance(kind, str):
        raise ValueError("table column type must be a native type name")
    normalized = " ".join(kind.upper().strip().split())
    normalized = _ALIASES.get(normalized, normalized)
    if normalized not in _TYPES:
        raise ValueError(f"unsupported table row type: {kind}")
    return normalized


def table_columns(columns):
    """Validate declared native types and return columns in canonical UTF-16 order.

    Nanosecond timestamps are refused: the Python reference and Iceberg
    timestamps retain microseconds exactly. DOUBLE spells as its shortest
    round-trip decimal, Python's ``repr``.
    """
    result = tuple((name, table_type(kind)) for name, kind in columns)
    check_column_names(name for name, _ in result)
    return tuple(sorted(result, key=lambda column: column[0].encode("utf-16-be")))


def _value(value, kind):
    if value is None:
        return None
    if kind == "VARCHAR" and type(value) is str:
        return value
    if kind == "BOOLEAN" and type(value) is bool:
        return value
    if kind in _INTEGER_BITS and type(value) is int:
        bound = 2 ** (_INTEGER_BITS[kind] - 1)
        if not -bound <= value < bound:
            raise ValueError(f"table value is outside {kind}")
        return str(value) if kind == "BIGINT" and abs(value) > SAFE_INTEGER else value
    if kind == "DOUBLE" and type(value) is float:
        return "nan" if math.isnan(value) else repr(value)
    if kind == "DATE" and type(value) is date:
        return value.isoformat()
    if kind in {"TIMESTAMP", "TIMESTAMPTZ"} and type(value) is datetime:
        aware = value.utcoffset() is not None
        if aware != (kind == "TIMESTAMPTZ"):
            raise ValueError("timestamp timezone must match its declared table type")
        utc = value.astimezone(timezone.utc) if aware else value.replace(tzinfo=timezone.utc)
        return utc.isoformat().replace("+00:00", "Z")
    if kind == "VARCHAR[]" and type(value) in (list, tuple):
        if all(item is None or type(item) is str for item in value):
            return list(value)
    raise ValueError(f"table value does not match {kind}")


def table_row_value(row, columns):
    """Convert one complete typed row to the JSON value covered by the rule."""
    return _row_value(row, table_columns(columns))


def _row_value(row, columns):
    if not isinstance(row, Mapping) or set(row) != {name for name, _ in columns}:
        raise ValueError("table row must contain exactly its declared columns")
    return {name: _value(row[name], kind) for name, kind in columns}


def table_row_bytes(row, columns):
    """Emit the independent Rulespec canonical bytes used by the native oracle."""
    return canonical_value_bytes(table_row_value(row, columns))


def table_rows_bytes(rows, columns):
    """Emit each complete typed row's canonical bytes, as ``table_row_bytes`` does, validating ``columns`` once."""
    columns = table_columns(columns)
    for row in rows:
        yield canonical_value_bytes(_row_value(row, columns))


def table_row_digest(row, columns):
    return sha256_digest(table_row_bytes(row, columns))


def table_occurrence_id(family, table, member_key, row_digest):
    """An unchanged row keeps its occurrence across generations of one table.

    Any nonempty key is valid, including whitespace; its components were
    already refused when NULL or empty.
    """
    require_text(family, "table family")
    require_text(table, "logical table")
    if not isinstance(member_key, str) or not member_key:
        raise ValueError("member key must be a nonempty string")
    require_sha256(row_digest, "table row digest")
    return stable_urn("table-occurrence", [family, table, member_key, row_digest])


# Ruling R5: a column recording when a row was observed or fetched can change
# on an unchanged row and re-mint it every generation. Such a table is
# admitted only through a declared projection without those columns, and no
# table declares one yet. The 17 fork-host tables R5 counted match by name.
_VOLATILE_COLUMN = re.compile(r"(?:^|_)(?:observed|fetched)_at$", re.IGNORECASE)


def volatile_columns(names) -> tuple[str, ...]:
    """The columns among ``names`` that record when a row was observed or fetched (ruling R5)."""
    return tuple(name for name in names if _VOLATILE_COLUMN.search(name))


# A key component is nonempty text; a DATE spells ISO 8601, as ``str(date)``
# does in spicy-docs' references, so a producer that types a date column keeps
# every key its VARCHAR spelling had. An integer spells its decimal text; only
# a derived layer's segment index is one (C29).
KEY_TYPES = frozenset({"VARCHAR", "DATE", "INTEGER", "BIGINT"})


def key_component(value, kind) -> str:
    """One member-key component's text: nonempty VARCHAR text, a DATE's ISO 8601 spelling or an integer's decimal."""
    if kind == "VARCHAR" and type(value) is str and value:
        return value
    if kind == "DATE" and type(value) is date:
        return value.isoformat()
    if kind in _INTEGER_BITS and type(value) is int:
        return str(value)
    raise ValueError("member key components must be nonempty text, dates or integers")


@dataclass(frozen=True, slots=True)
class KeySpelling:
    """A declared, versioned member-key spelling over nonempty VARCHAR, DATE or integer components.

    The table SQL adapter compiles the declared spellings, each beside its
    Python reference and the component types it takes, and refuses any other.
    ``value/1`` is one VARCHAR or DATE field's own text;
    ``federal-register-source-record-id/1`` is spicy-docs' ``number@date`` over
    document_number and publication_date; spicy-docs' ``at-joined/1`` is any
    composite identity's components in declared order joined by ``@``, none
    empty or holding ``@``; DocSpec's ``member-segment/1`` is a derived layer's
    ``member_key#segment_index``. The ID and version enter the state identity,
    so a new spelling is an explicit re-key; a composite identity admits only
    through a spelling spicy-docs declares (ruling R6).
    """

    spelling_id: str
    version: str
    fields: tuple[str, ...]

    def __post_init__(self) -> None:
        require_text(self.spelling_id, "member-key spelling")
        require_text(self.version, "member-key spelling version")
        fields = tuple(self.fields)
        if not fields or len(set(fields)) != len(fields) or any(not isinstance(field, str) or not field for field in fields):
            raise ValueError("member-key fields must be distinct nonempty column names")
        object.__setattr__(self, "fields", fields)


@dataclass(frozen=True, slots=True)
class TableIdentity:
    """How rows of one logical table get member keys and occurrences under docspec-table-row/1.

    ``columns`` is the declared projection the row digest covers (ruling R5),
    kept in canonical order; the key fields are VARCHAR, DATE or integer
    columns of it, as their spelling declares. The table name is the logical
    name, never a file name. A derived layer's family is its definition ID and
    its table the caller's schema ID (C29).
    """

    family: str
    table: str
    key: KeySpelling
    columns: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        require_text(self.family, "table family")
        require_text(self.table, "logical table")
        columns = table_columns(self.columns)
        kinds = dict(columns)
        if any(kinds.get(field) not in KEY_TYPES for field in self.key.fields):
            raise ValueError("member-key fields must be VARCHAR, DATE or integer columns of the declared projection")
        object.__setattr__(self, "columns", columns)

    @property
    def key_kinds(self) -> tuple[str, ...]:
        """The declared type of each member-key field, in key order."""
        kinds = dict(self.columns)
        return tuple(kinds[field] for field in self.key.fields)

    def to_dict(self) -> dict:
        """Return the closed rules value a table state records and its identity covers."""
        return {"row": ROW_RULE, "family": self.family, "table": self.table,
                "key": {"id": self.key.spelling_id, "version": self.key.version, "fields": list(self.key.fields)},
                "columns": [list(column) for column in self.columns]}

    @classmethod
    def derived(cls, definition_id, schema: TableSchema) -> "TableIdentity":
        """A derived layer's rules (C29): its definition scopes the occurrences and its schema ID names the table.

        The schema declares member_key VARCHAR and source_occurrence_id VARCHAR
        (the source row) or VARCHAR[] (every row a fusion joined); a
        one-to-many layer adds segment_index INTEGER or BIGINT and is keyed by
        member-segment/1, any other by value/1 over member_key. No column may
        take the reader's occurrence_id.
        """
        kinds = dict(schema.columns)
        if kinds.get(DERIVED_MEMBER) != "VARCHAR" or kinds.get(SOURCE_OCCURRENCE) not in {"VARCHAR", "VARCHAR[]"}:
            raise ValueError("a derived layer declares member_key VARCHAR and source_occurrence_id VARCHAR or VARCHAR[]")
        if kinds.get(SEGMENT_INDEX, "INTEGER") not in _INTEGER_BITS:
            raise ValueError("a one-to-many layer's segment_index is INTEGER or BIGINT")
        if any(name.casefold() == "occurrence_id" for name in kinds):
            raise ValueError("a derived layer's columns must not take the reader's occurrence_id")
        key = (KeySpelling("member-segment", "1", (DERIVED_MEMBER, SEGMENT_INDEX)) if SEGMENT_INDEX in kinds
               else KeySpelling("value", "1", (DERIVED_MEMBER,)))
        return cls(definition_id, schema.schema_id, key, schema.columns)

    @classmethod
    def from_dict(cls, value) -> "TableIdentity":
        if (not isinstance(value, dict) or set(value) != {"row", "family", "table", "key", "columns"}
                or value["row"] != ROW_RULE or not isinstance(value["key"], dict)
                or set(value["key"]) != {"id", "version", "fields"}):
            raise ValueError("table identity rules have an invalid closed shape")
        key = value["key"]
        return cls(value["family"], value["table"], KeySpelling(key["id"], key["version"], tuple(key["fields"])),
                   tuple(tuple(column) for column in value["columns"]))


# The fixed corpus every native spelling must reproduce before an identity is
# minted, shared by the unit test and the runtime oracle. It covers every type;
# strings on both native paths (with a control character, and without one but
# with quote, backslash, solidus, DEL and non-ASCII text); ordinary doubles,
# signed zero, infinities and a NaN with its sign bit set; integers around 2^53;
# both date bounds; zoned timestamps; list edge cases; and member keys for both
# declared spellings, including one with an "@" inside a component.
SPELLING_COLUMNS = (("document_number", "VARCHAR"), ("publication_date", "VARCHAR"), ("text\x1f", "VARCHAR"),
                    ("flag", "BOOLEAN"), ("count", "INTEGER"), ("big", "BIGINT"), ("ratio", "DOUBLE"),
                    ("day", "DATE"), ("moment", "TIMESTAMP"), ("zoned", "TIMESTAMPTZ"), ("items", "VARCHAR[]"))
_NEGATIVE_NAN = struct.unpack(">d", bytes.fromhex("fff8000000000001"))[0]
_INDIA = timezone(timedelta(hours=5, minutes=30))
SPELLING_ROWS = tuple(dict(zip((name for name, _ in SPELLING_COLUMNS), values, strict=True)) for values in (
    ("2026-\x1f\"1\\u001F", "2026-09-25", "control \x00\n\x1f \"quoted\" \\u001F", True, -(2**31), -(2**63),
     _NEGATIVE_NAN, date(1, 1, 1), datetime(1, 1, 1), datetime(2026, 9, 25, 1, 2, 3, 123400, tzinfo=_INDIA),
     ["\x00\x1f", None, "😀"]),
    ("quote\"key", "back\\slash/key", "plain \" \\ / \x7f é 😀   \\u001F", False, 2**31 - 1, SAFE_INTEGER + 1,
     -0.0, date(9999, 12, 31), datetime(2026, 9, 25, 1, 2, 3, 1), datetime(2026, 9, 25, tzinfo=timezone.utc), []),
    (" ", " ", "", None, 0, SAFE_INTEGER, 0.1, date(2024, 2, 29), datetime(2026, 9, 25, 1, 2, 3, 999999),
     None, ["\"", "\\", "/\x7f", "é"]),
    ("x@y", "z", None, None, None, -(SAFE_INTEGER + 1), 1.7976931348623157e308, None, None, None, None),
    ("2026-00005", "2026-01-02", "a", True, 1, 2**63 - 1, 5e-324, date(1970, 1, 1), None,
     datetime(1970, 1, 1, tzinfo=timezone.utc), [None]),
    ("2026-00006", "2026-01-02", "b", False, -1, -SAFE_INTEGER, 123456.789, None, None, None, ["a", "b"]),
    ("2026-00007", "2026-01-02", "c", None, None, 2**53, 1e21, None, None, None, None),
    ("2026-00008", "2026-01-02", "d", None, None, None, -1e-7, None, None, None, None),
    ("2026-00009", "2026-01-02", "e", None, None, None, float("inf"), None, None, None, None),
    ("2026-00010", "2026-01-02", "f", None, None, None, float("-inf"), None, None, None, None),
))
# Keys with a DATE component, as a producer may type publication_date: each
# spells the key its ISO 8601 text had, at both year bounds.
DATED_KEY_COLUMNS = (("document_number", "VARCHAR"), ("publication_date", "DATE"), ("title", "VARCHAR"))
DATED_KEY_ROWS = tuple(dict(zip((name for name, _ in DATED_KEY_COLUMNS), values, strict=True)) for values in (
    ("2026-\x1f1", date(2026, 9, 25), "dated"), ("x@y", date(1, 1, 1), None), (" ", date(9999, 12, 31), "")))
# Derived one-to-many keys (member-segment/1): a source member key holding
# "#", a control character or a digit run, with INTEGER and then BIGINT
# segment indexes at their bounds and below zero. ("a#1", 2) and ("a", 12)
# must spell distinct keys.
SEGMENT_KEY_COLUMNS = (("member_key", "VARCHAR"), ("segment_index", "INTEGER"), ("text", "VARCHAR"))
SEGMENT_KEY_ROWS = tuple(dict(zip((name for name, _ in SEGMENT_KEY_COLUMNS), values, strict=True)) for values in (
    ("a#1", 2, "segment"), ("a", 12, None), ("2026-\x1f1@2026-09-25", -(2**31), ""), ("#", 2**31 - 1, "hash"),
    (" ", 0, "space")))
WIDE_SEGMENT_KEY_COLUMNS = (("member_key", "VARCHAR"), ("segment_index", "BIGINT"), ("text", "VARCHAR"))
WIDE_SEGMENT_KEY_ROWS = tuple(dict(zip((name for name, _ in WIDE_SEGMENT_KEY_COLUMNS), values, strict=True))
                              for values in (("a#1", 2**63 - 1, "wide"), ("a", -(2**63), None), ("b", SAFE_INTEGER + 1, "")))
# Composite keys (at-joined/1) over every component type at its bounds: text
# with controls, quotes, a backslash, whitespace and non-BMP characters (none
# may hold "@"), DATE at both year bounds, INTEGER and BIGINT past 2^53.
AT_JOINED_KEY_COLUMNS = (("bill_id", "VARCHAR"), ("day", "DATE"), ("seq", "INTEGER"), ("wide", "BIGINT"),
                         ("body", "VARCHAR"))
AT_JOINED_KEY_ROWS = tuple(dict(zip((name for name, _ in AT_JOINED_KEY_COLUMNS), values, strict=True)) for values in (
    ("118-hr-1", date(2026, 9, 25), 1, 2**63 - 1, "one"),
    ("\x00\x1f\"\\/ é😀", date(1, 1, 1), -(2**31), -(2**63), None),
    (" ", date(9999, 12, 31), 2**31 - 1, SAFE_INTEGER + 1, ""),
    ("#1", date(1970, 1, 1), 0, -1, "hash"),
))
# Doubles DuckDB 1.5.5 casts to another double's spelling: 2^81 prints as
# 2^82's shortest decimal, and 2^807 with a hexadecimal digit. A native spelling
# must refuse them or match the reference, never spell them otherwise.
ROUND_TRIP_TRAPS = tuple(sign * 2.0 ** exponent for exponent in (81, 91, 807) for sign in (1.0, -1.0))
