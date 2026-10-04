"""Native SQL for docspec-table-row/1 and declared member keys, checked against the keys' Python references.

No Python row callbacks. A direct column comparison between generations must
compare DOUBLE through its spelling: SQL equates 0.0 with -0.0, whose
canonical spellings differ.
"""

from collections.abc import Callable
from dataclasses import dataclass
import sys
import json

from spicy_docs.schemas.tables import KEY_SPELLINGS
from spicy_docs.sources.federal_register.native import federal_register_source_record_id

from docspec.adapters.storage.iceberg import identifier, literal
from docspec.domain.identity import canonical_value_bytes, require_text
from docspec.domain.storage import type_tree, type_name
from docspec.domain.table_rows import KEY_TYPES, SAFE_INTEGER, key_component, table_columns
from docspec.errors import IntegrityError


OCCURRENCE_PREFIX = "urn:docspec:table-occurrence:v1:"
_SHORT_ESCAPES = {8: "\\b", 9: "\\t", 10: "\\n", 12: "\\f", 13: "\\r"}


@dataclass(frozen=True, slots=True)
class _Spelling:
    fields: tuple[str, ...] | range  # exactly these fields, or any fields, as many as the range holds
    kinds: tuple[frozenset[str], ...] | frozenset[str]  # each component's column types, or every component's
    sql: Callable[[list[str]], str]
    reference: Callable[[tuple[str, ...]], str]
    components: Callable[[str], tuple[tuple[str, ...], ...]]


_TEXT = frozenset({"VARCHAR", "DATE"})
_FEDERAL_REGISTER = ("document_number", "publication_date")


def _native_components(key):
    try:
        parts = json.loads(key)
    except (ValueError, TypeError):
        return ()
    if not isinstance(parts, list) or any(part is not None and not isinstance(part, str) for part in parts):
        return ()
    return (tuple(parts),) if canonical_value_bytes(parts).decode() == key else ()


def _native_tuple_sql(parts):
    return "'[' || " + " || ',' || ".join(f"coalesce({json_string_sql(part)}, 'null')" for part in parts) + " || ']'"


def _segment_components(key):
    """A derived key splits at its last "#": the suffix is a canonical decimal integer, which holds no "#"."""
    head, mark, tail = key.rpartition("#")
    try:
        canonical = mark and str(int(tail)) == tail
    except ValueError:
        canonical = False
    return ((head, tail),) if canonical and head else ()


def _producer(name, fallback=None):
    """A producer spelling's Python reference: its entry in spicy-docs' ``KEY_SPELLINGS``, read when called.

    ``fallback`` serves only while the installed spicy-docs registers no such
    entry: decision 0003's ``number@date`` before spicy-docs declared it.
    """
    def reference(values):
        function = KEY_SPELLINGS.get(name, fallback)
        if function is None:
            raise IntegrityError(f"spicy-docs declares no key spelling {name}")
        return function(tuple(values))
    return reference


def _at_joined_sql(parts):
    """Components joined by "@"; one holding "@" would make the key ambiguous, so its row refuses."""
    holds, joined = " OR ".join(f"contains({part}, '@')" for part in parts), " || '@' || ".join(parts)
    return f"CASE WHEN {holds} THEN error('table member key component holds the @ joiner') ELSE {joined} END"


# Each declared spelling compiles to SQL over its components' text, next to
# its Python reference and the component tuples that could spell a key.
# spicy-docs owns the producer spellings and their references (KEY_SPELLINGS),
# which the spelling oracle holds this SQL to. at-joined/1 serves any identity
# of two or more components, and splits back at every "@" because no
# component may hold one. DocSpec owns member-segment/1, a one-to-many
# derived layer's key (C29), injective because the segment index spells as a
# canonical integer after the last "#".
_SPELLINGS = {
    ("native-tuple", "1"): _Spelling(range(1, sys.maxsize), KEY_TYPES, _native_tuple_sql,
                                      lambda parts: canonical_value_bytes(list(parts)).decode(), _native_components),
    ("value", "1"): _Spelling(range(1, 2), _TEXT, lambda parts: parts[0], _producer("value/1"), lambda key: ((key,),)),
    ("federal-register-source-record-id", "1"): _Spelling(
        _FEDERAL_REGISTER, (_TEXT, _TEXT), lambda parts: f"{parts[0]} || '@' || {parts[1]}",
        _producer("federal-register-source-record-id/1", fallback=lambda values: federal_register_source_record_id(
            dict(zip(_FEDERAL_REGISTER, values, strict=True)))),
        lambda key: tuple((key[:index], key[index + 1:]) for index, char in enumerate(key) if char == "@")),
    ("member-segment", "1"): _Spelling(
        ("member_key", "segment_index"), (frozenset({"VARCHAR"}), frozenset({"INTEGER", "BIGINT"})),
        lambda parts: f"{parts[0]} || '#' || {parts[1]}", lambda values: values[0] + "#" + values[1],
        _segment_components),
    ("at-joined", "1"): _Spelling(range(2, sys.maxsize), KEY_TYPES, _at_joined_sql, _producer("at-joined/1"),
                                  lambda key: (tuple(key.split("@")),)),
}


def declared_spelling(spelling, kinds=None):
    """The spelling DocSpec compiles for these fields, refusing an unknown one or a column type it does not take."""
    declared = _SPELLINGS.get((spelling.spelling_id, spelling.version))
    if declared is None or (len(spelling.fields) not in declared.fields if isinstance(declared.fields, range)
                            else spelling.fields != declared.fields):
        raise IntegrityError("member-key spelling is not declared for these fields")
    allowed = (declared.kinds,) * len(spelling.fields) if isinstance(declared.kinds, frozenset) else declared.kinds
    if kinds is not None and any(kind not in types for kind, types in zip(kinds, allowed, strict=True)):
        raise IntegrityError("member-key spelling is not declared for these fields' types")
    return declared


def reference_member_key(identity, row) -> str:
    """The reference key of one row; a NULL, empty or mistyped component refuses.

    A DATE component reaches the reference as its ISO 8601 text, an integer as
    its decimal text.
    """
    declared = declared_spelling(identity.key, identity.key_kinds)
    if identity.key.spelling_id == "native-tuple":
        return declared.reference(tuple(None if row[field] is None else row[field] if kind == "VARCHAR" else key_component(row[field], kind)
                                        for field, kind in zip(identity.key.fields, identity.key_kinds, strict=True)))
    return declared.reference(tuple(key_component(row[field], kind)
                                    for field, kind in zip(identity.key.fields, identity.key_kinds, strict=True)))


def key_components(spelling, key: str) -> tuple[tuple[str, ...], ...]:
    """Every component tuple that could spell ``key``, one nonempty component per field; lookups push them into scans."""
    return tuple(parts for parts in declared_spelling(spelling).components(key)
                 if len(parts) == len(spelling.fields) and (spelling.spelling_id == "native-tuple" or all(parts)))


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


def json_array_sql(*expressions):
    """Spell a canonical JSON array of trusted VARCHAR SQL expressions; a NULL element makes the whole array NULL.

    Engine's prepared-row id is the sha256 of one: [source_id, member_key].
    """
    return "'[' || " + " || ',' || ".join(json_string_sql(expression) for expression in expressions) + " || ']'"


def _cell(expression, kind, depth=0):
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
    else:
        tree = type_tree(kind)
        if tree[0] == "LIST":
            item = f"table_item_{depth}"
            element = _cell(item, type_name(tree[1]), depth + 1)
            value = f"'[' || coalesce(array_to_string(list_transform(({expression}), {item} -> {element}), ','), '') || ']'"
        elif tree[0] == "STRUCT":
            fields = sorted(tree[1], key=lambda pair: pair[0].encode("utf-16-be"))
            parts = [literal(canonical_value_bytes(name).decode() + ':') + ' || ' +
                     _cell(f"({expression}).{identifier(name)}", type_name(child), depth + 1) for name, child in fields]
            value = "'{' || " + " || ',' || ".join(parts) + " || '}'"
        elif tree[0] == "DECIMAL":
            value = json_string_sql(f"CAST(({expression}) AS VARCHAR)")
        else:
            raise ValueError(f"No canonical SQL spelling for {kind}")
    return f"CASE WHEN ({expression}) IS NULL THEN 'null' ELSE {value} END"


def row_json_sql(columns, *, qualifier=None):
    """Compile a canonical row expression over a relation matching the schema."""
    parts = [literal(canonical_value_bytes(name).decode() + ":") + " || " + _cell(_column(name, qualifier), kind)
             for name, kind in table_columns(columns)]
    return "'{' || " + " || ',' || ".join(parts) + " || '}'"


def rows_differ_sql(columns, left, right):
    """Whether two relations' rows, qualified ``left`` and ``right``, spell differently under docspec-table-row/1.

    A direct comparison between generations: every type but DOUBLE spells
    injectively, so SQL distinctness is spelling distinctness, NULLs and list
    elements included. SQL equates 0.0 with -0.0, whose spellings differ, and
    every NaN with every other, which all spell ``nan``.
    """
    differs = []
    for name, kind in table_columns(columns):
        a, b = _column(name, left), _column(name, right)
        if type_tree(kind)[0] in {"LIST", "STRUCT"}:
            differs.append(f"{_cell(a, kind)} IS DISTINCT FROM {_cell(b, kind)}")
            continue
        differs.append(f"{a} IS DISTINCT FROM {b}" + (f" OR ({a} = 0 AND signbit({a}) <> signbit({b}))" if kind == "DOUBLE" else ""))
    return " OR ".join(f"({condition})" for condition in differs)


def member_key_sql(identity, *, qualifier=None):
    """Spell a declared member key; a NULL or empty component raises while the pass runs.

    A DATE component spells ISO 8601, like the row rule, and refuses outside
    years 1 through 9999 as it does; an integer spells its decimal text.
    """
    declared = declared_spelling(identity.key, identity.key_kinds)
    columns = [_column(field, qualifier) for field in identity.key.fields]
    invalid = " OR ".join(f"{column} IS NULL OR {column} = ''" if kind == "VARCHAR" else f"{column} IS NULL"
                          for column, kind in zip(columns, identity.key_kinds, strict=True))
    dates = " OR ".join(f"NOT isfinite({column}) OR year({column}) NOT BETWEEN 1 AND 9999"
                        for column, kind in zip(columns, identity.key_kinds, strict=True) if kind == "DATE")
    parts = [f"strftime({column}, '%Y-%m-%d')" if kind == "DATE" else column if kind == "VARCHAR"
             else f"CAST({column} AS VARCHAR)" for column, kind in zip(columns, identity.key_kinds, strict=True)]
    if identity.key.spelling_id == "native-tuple":
        return f"CASE WHEN {dates} THEN error('table date is outside years 1 through 9999') ELSE {declared.sql(parts)} END" if dates else declared.sql(parts)
    return (f"CASE WHEN {invalid} THEN error('table member key has a NULL or empty component') "
            + (f"WHEN {dates} THEN error('table date is outside years 1 through 9999') " if dates else "")
            + f"ELSE {declared.sql(parts)} END")


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
    inner = (f"{member_key_sql(identity)} AS member_key, "
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
