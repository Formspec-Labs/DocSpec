"""Python reference for the versioned canonical spelling of typed table rows."""

from collections.abc import Mapping
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


def table_columns(columns):
    """Validate declared native types and return columns in canonical UTF-16 order.

    Names may contain control characters except NUL, which SQL identifiers
    cannot represent. Nanosecond timestamps are refused: the Python reference
    and the supported Iceberg timestamp profile retain microseconds exactly.
    """
    result, names = [], set()
    for name, kind in columns:
        if not isinstance(name, str) or not name or "\0" in name:
            raise ValueError("table column names must be nonempty strings without NUL")
        canonical_value_bytes(name)  # Shared owner refuses invalid Unicode.
        if name in names or name.casefold() in {value.casefold() for value in names}:
            raise ValueError("table column names must be distinct, including SQL case folding")
        names.add(name)
        result.append((name, table_type(kind)))
    if not result:
        raise ValueError("table rows require at least one column")
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
    """An unchanged row keeps its occurrence across generations of one table."""
    require_text(family, "table family")
    require_text(table, "logical table")
    require_text(member_key, "member key")
    require_sha256(row_digest, "table row digest")
    return stable_urn("table-occurrence", [family, table, member_key, row_digest])
