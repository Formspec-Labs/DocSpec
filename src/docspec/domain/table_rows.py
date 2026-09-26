"""Python reference for docspec-table-row/1 spellings and occurrence identity, and a table's identity rules."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
import struct

from docspec.domain.identity import canonical_value_bytes, require_sha256, require_text, sha256_digest, stable_urn
from docspec.domain.storage import TABLE_TYPES, check_column_names


ROW_RULE = "docspec-table-row/1"
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
    columns = table_columns(columns)
    if not isinstance(row, Mapping) or set(row) != {name for name, _ in columns}:
        raise ValueError("table row must contain exactly its declared columns")
    return {name: _value(row[name], kind) for name, kind in columns}


def table_row_bytes(row, columns):
    """Emit the independent Rulespec canonical bytes used by the native oracle."""
    return canonical_value_bytes(table_row_value(row, columns))


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


# A key component is nonempty text; a DATE spells ISO 8601, as ``str(date)``
# does in spicy-docs' references, so a producer that types a date column keeps
# every key its VARCHAR spelling had.
KEY_TYPES = frozenset({"VARCHAR", "DATE"})


def key_component(value, kind) -> str:
    """One member-key component's text: nonempty VARCHAR text, or a DATE's ISO 8601 spelling."""
    if kind == "VARCHAR" and type(value) is str and value:
        return value
    if kind == "DATE" and type(value) is date:
        return value.isoformat()
    raise ValueError("member key components must be nonempty text or dates")


@dataclass(frozen=True, slots=True)
class KeySpelling:
    """A declared, versioned member-key spelling over nonempty VARCHAR or DATE components.

    spicy-docs owns each spelling's Python reference; the table SQL adapter
    compiles the declared ones and refuses any other. ``value/1`` is one
    field's own text; ``federal-register-source-record-id/1`` is ``number@date``
    over document_number and publication_date. The ID and version enter the
    state identity, so a new spelling is an explicit re-key; composite
    spellings wait for spicy-docs to declare them (ruling R6).
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
    kept in canonical order; the key fields are VARCHAR or DATE columns of it.
    The table name is the logical name, never a file name.
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
            raise ValueError("member-key fields must be VARCHAR or DATE columns of the declared projection")
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
# Doubles DuckDB 1.5.5 casts to another double's spelling: 2^81 prints as
# 2^82's shortest decimal, and 2^807 with a hexadecimal digit. A native spelling
# must refuse them or match the reference, never spell them otherwise.
ROUND_TRIP_TRAPS = tuple(sign * 2.0 ** exponent for exponent in (81, 91, 807) for sign in (1.0, -1.0))
