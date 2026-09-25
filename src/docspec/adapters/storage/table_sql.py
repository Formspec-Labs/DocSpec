"""Native SQL for docspec-table-row/1 and declared member keys, beside spicy-docs' key references.

No Python row callbacks. A direct column comparison between generations must
compare DOUBLE through its spelling: SQL equates 0.0 with -0.0, whose
canonical spellings differ.
"""

from collections.abc import Callable
from dataclasses import dataclass

from spicy_docs.sources.federal_register.native import federal_register_source_record_id

from docspec.adapters.storage.iceberg import identifier, literal
from docspec.domain.identity import canonical_value_bytes, require_text
from docspec.domain.table_rows import SAFE_INTEGER, table_columns
from docspec.errors import IntegrityError


OCCURRENCE_PREFIX = "urn:docspec:table-occurrence:v1:"
_SHORT_ESCAPES = {8: "\\b", 9: "\\t", 10: "\\n", 12: "\\f", 13: "\\r"}


@dataclass(frozen=True, slots=True)
class _Spelling:
    fields: tuple[str, ...] | None  # None: any one field
    sql: Callable[[list[str]], str]
    reference: Callable[[tuple[str, ...]], str]
    components: Callable[[str], tuple[tuple[str, ...], ...]]


_FEDERAL_REGISTER = ("document_number", "publication_date")
# Each declared spelling compiles to SQL over VARCHAR components, next to
# spicy-docs' reference and the component tuples that could spell a key.
_SPELLINGS = {
    ("value", "1"): _Spelling(None, lambda parts: parts[0], lambda values: values[0], lambda key: ((key,),)),
    ("federal-register-source-record-id", "1"): _Spelling(
        _FEDERAL_REGISTER, lambda parts: f"{parts[0]} || '@' || {parts[1]}",
        lambda values: federal_register_source_record_id(dict(zip(_FEDERAL_REGISTER, values, strict=True))),
        lambda key: tuple((key[:index], key[index + 1:]) for index, char in enumerate(key) if char == "@")),
}


def _declared(spelling):
    declared = _SPELLINGS.get((spelling.spelling_id, spelling.version))
    if declared is None or (len(spelling.fields) != 1 if declared.fields is None else spelling.fields != declared.fields):
        raise IntegrityError("member-key spelling is not declared for these fields")
    return declared


def reference_member_key(spelling, row) -> str:
    """spicy-docs' reference key of one row; a NULL, empty or non-text component refuses."""
    values = tuple(row[field] for field in spelling.fields)
    if any(type(value) is not str or not value for value in values):
        raise ValueError("member key components must be nonempty strings")
    return _declared(spelling).reference(values)


def key_components(spelling, key: str) -> tuple[tuple[str, ...], ...]:
    """Every component tuple that could spell ``key``; lookups push them into scans."""
    return _declared(spelling).components(key)


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
    elif kind in {"BOOLEAN", "INTEGER"}:
        value = f"CAST(({expression}) AS VARCHAR)"
    elif kind == "BIGINT":
        value = (f"CASE WHEN ({expression}) BETWEEN {-SAFE_INTEGER} AND {SAFE_INTEGER} "
                 f"THEN CAST(({expression}) AS VARCHAR) ELSE {json_string_sql(f'CAST(({expression}) AS VARCHAR)')} END")
    elif kind == "DOUBLE":
        # DuckDB 1.5.5 casts ±2^81, ±2^91 and ±2^807 to another value's spelling.
        # A spelling that reads back as its own value is unique to it; any other
        # refuses the row rather than give two values one digest.
        text = f"CAST(({expression}) AS VARCHAR)"
        value = (f"CASE WHEN isnan(({expression})) THEN '\"nan\"' "
                 f"WHEN TRY_CAST({text} AS DOUBLE) IS DISTINCT FROM ({expression}) "
                 "THEN error('table DOUBLE value has no round-trip spelling') "
                 f"ELSE '\"' || {text} || '\"' END")
    elif kind in {"DATE", "TIMESTAMP", "TIMESTAMPTZ"}:
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


def member_key_sql(spelling, *, qualifier=None):
    """Spell a declared member key; a NULL or empty component raises while the pass runs."""
    declared = _declared(spelling)
    parts = [_column(field, qualifier) for field in spelling.fields]
    invalid = " OR ".join(f"{part} IS NULL OR {part} = ''" for part in parts)
    return (f"CASE WHEN {invalid} THEN error('table member key has a NULL or empty component') "
            f"ELSE {declared.sql(parts)} END")


def _occurrence_frame(family, table, key, digest_json):
    """The exact canonical bytes stable_urn hashes: [family, table, key, "sha256:<row digest>"]."""
    require_text(family, "table family")
    require_text(table, "logical table")
    prefix = canonical_value_bytes([family, table]).decode()[:-1] + ","
    return f"{literal(prefix)} || {json_string_sql(key)} || ',' || {digest_json} || ']'"


def occurrence_urn_sql(occurrence_hash):
    """Spell an occurrence URN from its 32-byte hash column."""
    return f"'{OCCURRENCE_PREFIX}' || lower(hex({occurrence_hash}))"


def identity_relation(rows, identity):
    """Project member_key, row_digest BLOB(32) and occurrence_hash BLOB(32) over ``rows``.

    The inner projection spells each row's canonical JSON once; the outer one
    frames the occurrence from that digest, so no row is spelled or hashed twice.
    """
    inner = (f"{member_key_sql(identity.key)} AS member_key, "
             f"sha256({row_json_sql(identity.columns)}) AS row_hex")
    frame = _occurrence_frame(identity.family, identity.table, "member_key", "'\"sha256:' || row_hex || '\"'")
    return rows.project(inner).project(f"member_key, unhex(row_hex) AS row_digest, unhex(sha256({frame})) AS occurrence_hash")


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
