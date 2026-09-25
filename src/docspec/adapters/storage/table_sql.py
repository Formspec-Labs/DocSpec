"""Native SQL spelling of docspec-table-row/1; no Python row callbacks."""

from docspec.adapters.storage.iceberg import identifier, literal
from docspec.domain.identity import canonical_value_bytes, require_text
from docspec.domain.table_rows import SAFE_INTEGER, table_columns


_SHORT_ESCAPES = {8: "\\b", 9: "\\t", 10: "\\n", 12: "\\f", 13: "\\r"}


def _column(name, qualifier=None):
    for value in (name, qualifier):
        if value is not None and (not isinstance(value, str) or not value or "\0" in value):
            raise ValueError("SQL table names must be nonempty strings without NUL")
    return (identifier(qualifier) + "." if qualifier else "") + identifier(name)


def json_string_sql(expression):
    """Spell a trusted VARCHAR SQL expression, including canonical controls.

    DuckDB's JSON writer uses uppercase hex for control escapes. The raw-text
    branch preserves literal backslash sequences and uses lowercase escapes;
    ordinary strings keep the fast native JSON writer. NULL remains SQL NULL.
    """
    escaped = f"replace(replace(({expression}), {literal(chr(92))}, {literal(chr(92) * 2)}), {literal(chr(34))}, {literal(chr(92) + chr(34))})"
    for code in range(32):
        escaped = f"replace({escaped}, chr({code}), {literal(_SHORT_ESCAPES.get(code, f'\\u{code:04x}'))})"
    return (f"CASE WHEN regexp_matches(({expression}), '[\\x00-\\x1f]') THEN "
            f"'\"' || {escaped} || '\"' ELSE CAST(to_json(({expression})) AS VARCHAR) END")


def _cell(expression, kind):
    if kind == "VARCHAR":
        value = json_string_sql(expression)
    elif kind in {"BOOLEAN", "SMALLINT", "INTEGER"}:
        value = f"CAST(({expression}) AS VARCHAR)"
    elif kind == "BIGINT":
        value = (f"CASE WHEN ({expression}) BETWEEN {-SAFE_INTEGER} AND {SAFE_INTEGER} "
                 f"THEN CAST(({expression}) AS VARCHAR) ELSE {json_string_sql(f'CAST(({expression}) AS VARCHAR)')} END")
    elif kind == "DOUBLE":
        value = json_string_sql(f"CASE WHEN isnan(({expression})) THEN 'nan' ELSE CAST(({expression}) AS VARCHAR) END")
    elif kind in {"DATE", "TIMESTAMP", "TIMESTAMP_S", "TIMESTAMP_MS", "TIMESTAMPTZ"}:
        utc = f"timezone('UTC', ({expression}))" if kind == "TIMESTAMPTZ" else f"({expression})"
        text = f"strftime({utc}, '%Y-%m-%d')"
        if kind != "DATE":
            text = (f"strftime({utc}, '%Y-%m-%dT%H:%M:%S') || "
                    f"CASE WHEN strftime({utc}, '%f') = '000000' THEN '' ELSE '.' || strftime({utc}, '%f') END || 'Z'")
        value = (f"CASE WHEN NOT isfinite({utc}) OR year({utc}) NOT BETWEEN 1 AND 9999 "
                 f"THEN error('table date is outside years 1 through 9999') ELSE {json_string_sql(text)} END")
    else:  # table_columns admits only VARCHAR[] here.
        element = f"coalesce({json_string_sql('table_item')}, 'null')"
        value = f"'[' || coalesce(array_to_string(list_transform(({expression}), table_item -> {element}), ','), '') || ']'"
    return f"CASE WHEN ({expression}) IS NULL THEN 'null' ELSE {value} END"


def row_json_sql(columns, *, qualifier=None):
    """Compile a canonical row expression over a relation matching the schema."""
    parts = [literal(canonical_value_bytes(name).decode() + ":") + " || " + _cell(_column(name, qualifier), kind)
             for name, kind in table_columns(columns)]
    return "'{' || " + " || ',' || ".join(parts) + " || '}'"


def row_digest_sql(columns, *, qualifier=None):
    return "'sha256:' || sha256(" + row_json_sql(columns, qualifier=qualifier) + ")"


def occurrence_id_sql(family, table, *, member_key="member_key", row_digest="row_digest", qualifier=None):
    """Hash the exact stable_urn input, using a sha256:-prefixed row digest."""
    require_text(family, "table family")
    require_text(table, "logical table")
    prefix = canonical_value_bytes([family, table]).decode()[:-1] + ","
    key, digest = _column(member_key, qualifier), _column(row_digest, qualifier)
    framed = f"{literal(prefix)} || {json_string_sql(key)} || ',' || {json_string_sql(digest)} || ']'"
    return "'urn:docspec:table-occurrence:v1:' || sha256(" + framed + ")"


def membership_json_sql(*, member_key="member_key", occurrence_id="occurrence_id", qualifier=None):
    key, occurrence = _column(member_key, qualifier), _column(occurrence_id, qualifier)
    return ("'{\"kind\":\"Membership\",\"member_key\":' || " + json_string_sql(key)
            + " || ',\"occurrence_id\":' || " + json_string_sql(occurrence) + " || '}'")


def occurrence_json_sql(row_json_expression, *, occurrence_id="occurrence_id", qualifier=None):
    """Frame canonical row JSON as an exact inline occurrence Entity record."""
    return ("'{\"entity_id\":' || " + json_string_sql(_column(occurrence_id, qualifier))
            + " || ',\"entity_type\":\"occurrence\",\"format_version\":1,\"kind\":\"entity\","
              "\"value\":{\"codec\":\"json-v1\",\"kind\":\"inline\",\"value\":' || ("
            + row_json_expression + ") || '}}'")
