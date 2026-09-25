"""Table occurrence identity: the one native identity pass, the minted-occurrence index and its bounded lookup."""

from collections.abc import Iterable, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import reduce
from pathlib import Path
import re
import struct
from tempfile import TemporaryDirectory

import duckdb
import pyarrow as pa

from docspec.adapters.storage.batches import table_arrow_schema
from docspec.adapters.storage.records import native_columns
from docspec.adapters.storage.table_sql import (OCCURRENCE_PREFIX, identity_relation, key_components, member_key_sql,
    reference_member_key, row_json_sql)
from docspec.domain.identity import require_text, sha256_digest
from docspec.domain.references import LayerRef
from docspec.domain.storage import TableSchema
from docspec.domain.table_rows import SAFE_INTEGER, KeySpelling, TableIdentity, table_occurrence_id, table_row_digest
from docspec.errors import IntegrityError
from docspec.ports.record_storage import BATCH_ROWS


OCCURRENCE_INDEX = TableSchema("core-table-occurrences:1", (
    ("occurrence_hash", "BLOB"), ("member_key", "VARCHAR"), ("row_digest", "BLOB"), ("first_state_id", "VARCHAR")))
INDEX_KIND = "core-table-occurrences"
# As for record identities: a literal list keeps row-group pruning.
_LOOKUP_CHUNK = 256
_HEX64 = re.compile(r"[0-9a-f]{64}")

# The fixed oracle corpus: every docspec-table-row/1 type, controls in keys,
# names and strings, literal escapes, a NaN with its sign bit set, signed zero,
# BIGINT beyond 2^53, both date bounds and a zoned timestamp.
_ORACLE_COLUMNS = (("document_number", "VARCHAR"), ("publication_date", "VARCHAR"), ("text\x1f", "VARCHAR"),
                   ("flag", "BOOLEAN"), ("count", "INTEGER"), ("big", "BIGINT"), ("ratio", "DOUBLE"),
                   ("day", "DATE"), ("moment", "TIMESTAMP"), ("zoned", "TIMESTAMPTZ"), ("items", "VARCHAR[]"))
_ORACLE_ROWS = (
    ("2026-\x1f\"1\\u001F", "2026-09-25", "quote\" slash\\ \x00\n\x7f é😀", True, -(2**31), -(2**63),
     struct.unpack(">d", bytes.fromhex("fff8000000000001"))[0], date(1, 1, 1), datetime(1, 1, 1),
     datetime(2026, 9, 25, 1, 2, 3, 123400, tzinfo=timezone(timedelta(hours=5, minutes=30))), ["\x00\x1f", None, "😀"]),
    (" ", "\u2028", "", False, 2**31 - 1, SAFE_INTEGER + 1, -0.0, date(9999, 12, 31),
     datetime(2026, 9, 25, 1, 2, 3, 1), datetime(2026, 9, 25, tzinfo=timezone.utc), []),
    ("x@y", "z", None, None, None, SAFE_INTEGER, float("inf"), None, None, None, None),
)
_FEDERAL_REGISTER_KEY = KeySpelling("federal-register-source-record-id", "1", ("document_number", "publication_date"))
# Dated keys: a producer may type publication_date as DATE, which must spell
# the key its VARCHAR form had, at both year bounds.
_DATED_COLUMNS = (("document_number", "VARCHAR"), ("publication_date", "DATE"), ("title", "VARCHAR"))
_DATED_ROWS = (("2026-\x1f1", date(2026, 9, 25), "dated"), ("x@y", date(1, 1, 1), None), (" ", date(9999, 12, 31), ""))
_ORACLE_CASES = (
    (_ORACLE_COLUMNS, _ORACLE_ROWS, (
        TableIdentity("oracle\x1ffamily", 'oracle"table', _FEDERAL_REGISTER_KEY, _ORACLE_COLUMNS),
        TableIdentity("oracle", "table", KeySpelling("value", "1", ("document_number",)), _ORACLE_COLUMNS))),
    (_DATED_COLUMNS, _DATED_ROWS, (
        TableIdentity("oracle", "dated", _FEDERAL_REGISTER_KEY, _DATED_COLUMNS),
        TableIdentity("oracle", "dates", KeySpelling("value", "1", ("publication_date",)), _DATED_COLUMNS))),
)
_ORACLE_PASSED = set()


def reference_identity(identity: TableIdentity, row: Mapping) -> tuple[str, str, str]:
    """The Python reference for one row: its member key, row digest and occurrence URN."""
    key = reference_member_key(identity, row)
    digest = table_row_digest({name: row[name] for name, _ in identity.columns}, identity.columns)
    return key, digest, table_occurrence_id(identity.family, identity.table, key, digest)


def check_native_spelling(cursor) -> None:
    """Refuse to mint or resolve when this DuckDB spells the fixed corpus unlike the Python reference.

    ``duckdb`` is pinned by range, not version, and a release can change a
    spelling. The check runs once per DuckDB version in a process.
    """
    if duckdb.__version__ in _ORACLE_PASSED:
        return
    for columns, values, identities in _ORACLE_CASES:
        names = [name for name, _ in columns]
        rows = [dict(zip(names, row, strict=True)) for row in values]
        for identity in identities:
            source = cursor.from_arrow(pa.Table.from_pylist(rows, schema=table_arrow_schema(columns)))
            native = identity_relation(source, identity).fetchall()
            expected = [(key, bytes.fromhex(digest[7:]), bytes.fromhex(occurrence[len(OCCURRENCE_PREFIX):]))
                        for key, digest, occurrence in (reference_identity(identity, row) for row in rows)]
            if sorted(native) != sorted(expected):
                raise IntegrityError(f"DuckDB {duckdb.__version__} spells docspec-table-row/1 unlike its Python reference")
    _ORACLE_PASSED.add(duckdb.__version__)


@dataclass(frozen=True, slots=True)
class MintedIdentities:
    """One identity pass spilled to a scratch Parquet file, valid while ``mint_identities`` is open.

    Columns: member_key VARCHAR (unique), row_digest BLOB(32) and
    occurrence_hash BLOB(32), in no order.
    """

    path: Path
    row_count: int

    def relation(self, cursor):
        """Read the spilled identities on any cursor of the record store."""
        return cursor.read_parquet(str(self.path))


@contextmanager
def mint_identities(records, rows, identity: TableIdentity, *, cursor):
    """Give every row its member key, row digest and occurrence hash in one streamed native pass.

    ``rows`` is a relation on ``cursor``, a record-store cursor, holding the
    identity's columns with their declared types: a table layer's relation,
    or a later generation's added and changed rows. Each row is spelled once
    and streamed to a scratch Parquet file under the store's scratch root,
    with no sort and no temporary table, so memory stays bounded by the pass,
    not the table. A NULL or empty key component aborts the pass; one GROUP BY
    over the spilled keys, which may spill, refuses a duplicate key. The
    spelling oracle runs first. The file is removed on exit; consumers such as
    the membership writer and ``append_occurrences`` read it through
    ``MintedIdentities.relation``.
    """
    check_native_spelling(cursor)
    available = dict(native_columns(rows))
    if any(available.get(name) != kind for name, kind in identity.columns):
        raise IntegrityError("table rows differ from the declared identity projection")
    with TemporaryDirectory(prefix="docspec-table-identities-", dir=records.merge_scratch_root) as scratch:
        path = Path(scratch) / "identities.parquet"
        identity_relation(rows, identity).write_parquet(str(path))
        spilled = cursor.read_parquet(str(path))
        if spilled.aggregate("member_key, count(*) AS copies", "member_key").filter("copies > 1").limit(1).fetchone():
            raise IntegrityError("table contains a duplicate member key")
        yield MintedIdentities(path, spilled.aggregate("count(*)").fetchone()[0])


def _index(records, index):
    layer = records.admitted(index) if isinstance(index, LayerRef) else index
    if getattr(layer, "schema", None) != OCCURRENCE_INDEX or layer.reference.layer_kind != INDEX_KIND:
        raise IntegrityError("layer is not a minted-occurrence index")
    return layer


def append_occurrences(records, index, minted: MintedIdentities, *, first_state_id: str):
    """Append the minted occurrences the index lacks as one file sorted by occurrence_hash.

    ``index`` is the dataset's current index, or None to start one. The index
    is append-only and outlives the states that fill it. An occurrence absent
    before is generated by ``first_state_id``; one already present is adopted
    and keeps its first state (ruling R1(b)). Returns the new index layer
    (``index`` itself when nothing is new) and the number generated.
    """
    require_text(first_state_id, "first_state_id")
    with records._cursor() as cursor:
        rows = minted.relation(cursor).project(
            duckdb.ColumnExpression("occurrence_hash"), duckdb.ColumnExpression("member_key"),
            duckdb.ColumnExpression("row_digest"), duckdb.ConstantExpression(first_state_id).alias("first_state_id"))
        if index is None:
            with closing(rows.to_arrow_reader(BATCH_ROWS)) as reader:
                layer = records.write_table(reader, layer_kind=INDEX_KIND, schema=OCCURRENCE_INDEX,
                                            sort_by=("occurrence_hash",))
            return layer, layer.reference.record_count
        index = _index(records, index)
        with records.relations({"index": index}, cursor=cursor) as relations:
            known = relations["index"].project("occurrence_hash AS known_hash")
            fresh = rows.join(known, "occurrence_hash = known_hash", how="anti")
            with closing(fresh.to_arrow_reader(BATCH_ROWS)) as reader:
                layer = records.append_table(index, reader, sort_by=("occurrence_hash",))
        return layer, layer.reference.record_count - index.reference.record_count


@dataclass(frozen=True, slots=True)
class IndexedOccurrence:
    """What the index recorded when an occurrence was first minted."""

    member_key: str
    row_digest: str
    first_state_id: str


def lookup_occurrences(records, index, identity: TableIdentity, occurrence_ids: Iterable[str]) -> dict[str, IndexedOccurrence]:
    """Resolve each requested table occurrence the index holds to its member key and row digest.

    Each chunk of up to 256 hashes is one IN filter pushed into the scan,
    which DuckDB prunes per value: it reads only row groups whose
    occurrence_hash bounds admit a requested hash, at most one per hash in
    each appended file. (A semi-join prunes by the overall range beyond 50
    values.) Each found row must hash back to its occurrence under
    ``identity``. Identities of other kinds, and unknown ones, are omitted;
    memory grows with the requested count.
    """
    wanted = {}
    for occurrence_id in occurrence_ids:
        if isinstance(occurrence_id, str) and occurrence_id.startswith(OCCURRENCE_PREFIX) \
                and _HEX64.fullmatch(suffix := occurrence_id[len(OCCURRENCE_PREFIX):]):
            wanted[bytes.fromhex(suffix)] = occurrence_id
    hashes, found = list(wanted), {}
    with records._cursor() as cursor, records.relations({"index": _index(records, index)}, cursor=cursor) as relations:
        for start in range(0, len(hashes), _LOOKUP_CHUNK):
            chunk = (duckdb.ConstantExpression(value) for value in hashes[start:start + _LOOKUP_CHUNK])
            selected = relations["index"].filter(duckdb.ColumnExpression("occurrence_hash").isin(*chunk))
            for occurrence_hash, member_key, row_digest, first_state_id in selected.fetchall():
                occurrence_id, digest = wanted[occurrence_hash], "sha256:" + row_digest.hex()
                if table_occurrence_id(identity.family, identity.table, member_key, digest) != occurrence_id:
                    raise IntegrityError("minted-occurrence index row differs from its occurrence hash")
                found[occurrence_id] = IndexedOccurrence(member_key, digest, first_state_id)
    return found


def _component_value(text, kind):
    """A candidate component as its column type holds it, or None when no such value spells ``text``."""
    if kind != "DATE":
        return text
    try:
        value = date.fromisoformat(text)
    except ValueError:
        return None
    return value if value.isoformat() == text else None


def key_rows(rows, identity: TableIdentity, keys) -> "duckdb.DuckDBPyRelation":
    """Keep the rows of ``rows`` whose member key is one of ``keys``, reading only candidate row groups.

    Every component tuple that could spell a wanted key is pushed into the
    scan as IN filters, which prune row groups by their statistics; the exact
    spelled key then filters the superset. Callers bound ``keys``.
    """
    keys = set(keys)
    candidates = {tuple(_component_value(part, kind) for part, kind in zip(parts, identity.key_kinds, strict=True))
                  for key in keys for parts in key_components(identity.key, key) if all(parts)}
    candidates = [parts for parts in candidates if all(part is not None for part in parts)]
    if not candidates:
        return rows.filter("false")
    superset = reduce(lambda left, right: left & right, (
        duckdb.ColumnExpression(field).isin(*(duckdb.ConstantExpression(value) for value in {parts[index] for parts in candidates}))
        for index, field in enumerate(identity.key.fields)))
    exact = duckdb.SQLExpression(member_key_sql(identity)).isin(*(duckdb.ConstantExpression(key) for key in keys))
    return rows.filter(superset).filter(exact)


def read_occurrences(records, table, identity: TableIdentity, occurrences: Mapping[str, IndexedOccurrence]) -> dict[str, bytes]:
    """Read the rows holding ``occurrences`` from ``table`` and return each one's canonical row bytes.

    Per chunk of up to 256 keys, the key columns' candidate values are pushed
    into the scan and the exact key filters the rest, so only matching rows
    are spelled. A missing row, or one whose digest differs from the index,
    refuses: the caller named a table whose membership does not hold that
    occurrence. The bytes are the occurrence's inline value.
    """
    items, result = list(occurrences.items()), {}
    with records._cursor() as cursor, records.relations({"table": table}, cursor=cursor) as relations:
        check_native_spelling(cursor)
        available = dict(native_columns(relations["table"]))
        if any(available.get(name) != kind for name, kind in identity.columns):
            raise IntegrityError("table rows differ from the declared identity projection")
        spelled = f"{member_key_sql(identity)} AS member_key, {row_json_sql(identity.columns)} AS row_json"
        for start in range(0, len(items), _LOOKUP_CHUNK):
            chunk = items[start:start + _LOOKUP_CHUNK]
            keys = {entry.member_key for _, entry in chunk}
            payloads = {}
            for key, row_json in key_rows(relations["table"], identity, keys).project(spelled).fetchall():
                if key in payloads:
                    raise IntegrityError("table contains a duplicate member key")
                payloads[key] = row_json.encode()
            for occurrence_id, entry in chunk:
                payload = payloads.get(entry.member_key)
                if payload is None or sha256_digest(payload) != entry.row_digest:
                    raise IntegrityError("table row differs from its minted occurrence")
                result[occurrence_id] = payload
    return result
