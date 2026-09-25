"""Python reference for docspec-table-row/1 spellings and occurrence identity, and a table's identity rules."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
import math

from docspec.domain.identity import canonical_value_bytes, require_sha256, require_text, sha256_digest, stable_urn


ROW_RULE = "docspec-table-row/1"
SAFE_INTEGER = 2**53 - 1
_INTEGER_BITS = {"SMALLINT": 16, "INTEGER": 32, "BIGINT": 64}
_TYPES = {"VARCHAR", "BOOLEAN", *_INTEGER_BITS, "DOUBLE", "DATE", "TIMESTAMP",
          "TIMESTAMP_S", "TIMESTAMP_MS", "TIMESTAMPTZ", "VARCHAR[]"}
_ALIASES = {"BOOL": "BOOLEAN", "INT": "INTEGER", "INT2": "SMALLINT", "INT4": "INTEGER",
            "INT8": "BIGINT", "STRING": "VARCHAR", "TEXT": "VARCHAR",
            "TIMESTAMP WITH TIME ZONE": "TIMESTAMPTZ", "LIST<VARCHAR>": "VARCHAR[]"}


def table_type(kind):
    """Normalize a supported logical type without importing a storage engine."""
    if not isinstance(kind, str):
        raise ValueError("table column type must be a native type name")
    normalized = " ".join(kind.upper().strip().split())
    normalized = _ALIASES.get(normalized, normalized)
    if normalized not in _TYPES:
        raise ValueError(f"unsupported table row type: {kind}")
    return normalized


def check_column_names(names: Iterable[str]) -> None:
    """Refuse no columns, an empty or NUL-bearing name, invalid Unicode, or a case-folded duplicate.

    SQL identifiers cannot carry NUL, and DuckDB folds identifier case.
    """
    folded = set()
    for name in names:
        if not isinstance(name, str) or not name or "\0" in name:
            raise ValueError("table column names must be nonempty strings without NUL")
        canonical_value_bytes(name)  # Shared owner refuses invalid Unicode.
        if name.casefold() in folded:
            raise ValueError("table column names must be distinct, including SQL case folding")
        folded.add(name.casefold())
    if not folded:
        raise ValueError("table rows require at least one column")


def table_columns(columns):
    """Validate declared native types and return columns in canonical UTF-16 order.

    Nanosecond timestamps are refused: the Python reference and the supported
    Iceberg timestamp profile retain microseconds exactly.
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
    if kind in {"TIMESTAMP", "TIMESTAMP_S", "TIMESTAMP_MS", "TIMESTAMPTZ"} and type(value) is datetime:
        aware = value.utcoffset() is not None
        if aware != (kind == "TIMESTAMPTZ"):
            raise ValueError("timestamp timezone must match its declared table type")
        if (kind == "TIMESTAMP_S" and value.microsecond) or (kind == "TIMESTAMP_MS" and value.microsecond % 1000):
            raise ValueError("timestamp precision exceeds its declared table type")
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


@dataclass(frozen=True, slots=True)
class KeySpelling:
    """A declared, versioned member-key spelling over nonempty VARCHAR components.

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
    kept in canonical order; the key fields are VARCHAR columns of it. The
    table name is the logical name, never a file name.
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
        if any(kinds.get(field) != "VARCHAR" for field in self.key.fields):
            raise ValueError("member-key fields must be VARCHAR columns of the declared projection")
        object.__setattr__(self, "columns", columns)

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
