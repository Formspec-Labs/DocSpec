"""Table occurrence identity: the one native identity pass, the minted-occurrence index and its bounded lookup."""

from collections.abc import Iterable, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date
from functools import reduce
from pathlib import Path
import re
from tempfile import TemporaryDirectory

import duckdb
import pyarrow as pa

from docspec.adapters.storage.batches import table_arrow_schema
from docspec.adapters.storage.iceberg import identifier
from docspec.adapters.storage.records import LITERAL_IDENTITIES, identity_filter, native_columns
from docspec.adapters.storage.table_sql import (OCCURRENCE_PREFIX, identity_relation, key_components, member_key_sql,
    reference_member_key, row_json_sql)
from docspec.domain.identity import require_text, sha256_digest
from docspec.domain.references import LayerRef
from docspec.domain.storage import TableSchema
from docspec.domain.table_rows import (DATED_KEY_COLUMNS, DATED_KEY_ROWS, ROUND_TRIP_TRAPS, SEGMENT_KEY_COLUMNS,
    SEGMENT_KEY_ROWS, SPELLING_COLUMNS, SPELLING_ROWS, WIDE_SEGMENT_KEY_COLUMNS, WIDE_SEGMENT_KEY_ROWS, KeySpelling,
    TableIdentity, table_occurrence_id, table_row_bytes, table_row_digest)
from docspec.errors import IntegrityError
from docspec.ports.record_storage import BATCH_ROWS


OCCURRENCE_INDEX = TableSchema("core-table-occurrences:1", (
    ("occurrence_hash", "BLOB"), ("member_key", "VARCHAR"), ("row_digest", "BLOB"), ("first_state_id", "VARCHAR")))
INDEX_KIND = "core-table-occurrences"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_FEDERAL_REGISTER_KEY = KeySpelling("federal-register-source-record-id", "1", ("document_number", "publication_date"))
_SEGMENT_KEY = KeySpelling("member-segment", "1", ("member_key", "segment_index"))
# Each shared corpus with the identities it must mint exactly as the reference.
_ORACLE_CASES = (
    (SPELLING_COLUMNS, SPELLING_ROWS, (
        TableIdentity("oracle\x1ffamily", 'oracle"table', _FEDERAL_REGISTER_KEY, SPELLING_COLUMNS),
        TableIdentity("oracle", "table", KeySpelling("value", "1", ("document_number",)), SPELLING_COLUMNS))),
    (DATED_KEY_COLUMNS, DATED_KEY_ROWS, (
        TableIdentity("oracle", "dated", _FEDERAL_REGISTER_KEY, DATED_KEY_COLUMNS),
        TableIdentity("oracle", "dates", KeySpelling("value", "1", ("publication_date",)), DATED_KEY_COLUMNS))),
    (SEGMENT_KEY_COLUMNS, SEGMENT_KEY_ROWS, (
        TableIdentity("urn:oracle:definition", "segments", _SEGMENT_KEY, SEGMENT_KEY_COLUMNS),
        TableIdentity("urn:oracle:definition", "texts", KeySpelling("value", "1", ("member_key",)), SEGMENT_KEY_COLUMNS))),
    (WIDE_SEGMENT_KEY_COLUMNS, WIDE_SEGMENT_KEY_ROWS, (
        TableIdentity("urn:oracle:definition", "wide", _SEGMENT_KEY, WIDE_SEGMENT_KEY_COLUMNS),)),
)
_ORACLE_PASSED = set()


def reference_identity(identity: TableIdentity, row: Mapping) -> tuple[str, str, str]:
    """The Python reference for one row: its member key, row digest and occurrence URN."""
    key = reference_member_key(identity, row)
    digest = table_row_digest({name: row[name] for name, _ in identity.columns}, identity.columns)
    return key, digest, table_occurrence_id(identity.family, identity.table, key, digest)


def check_native_spelling() -> None:
    """Refuse to mint or resolve when this DuckDB spells the shared corpus unlike the Python reference.

    ``duckdb`` is pinned by range, not version, and a release can change a
    spelling. Every corpus row must match; each round-trip trap must refuse or
    match. The check runs once per DuckDB version in a process, on its own
    connection.
    """
    if duckdb.__version__ in _ORACLE_PASSED:
        return
    with duckdb.connect() as connection:
        for columns, rows, identities in _ORACLE_CASES:
            source = connection.from_arrow(pa.Table.from_pylist(list(rows), schema=table_arrow_schema(columns)))
            for identity in identities:
                expected = [(key, bytes.fromhex(digest[7:]), bytes.fromhex(occurrence[len(OCCURRENCE_PREFIX):]))
                            for key, digest, occurrence in (reference_identity(identity, row) for row in rows)]
                if sorted(identity_relation(source, identity).fetchall()) != sorted(expected):
                    raise IntegrityError(f"DuckDB {duckdb.__version__} spells docspec-table-row/1 unlike its Python reference")
        column = (("value", "DOUBLE"),)
        for value in ROUND_TRIP_TRAPS:
            try:
                native = connection.execute(f"SELECT {row_json_sql(column)} FROM (SELECT ?::DOUBLE AS value)", [value]).fetchone()[0]
            except duckdb.InvalidInputException as error:
                if "no round-trip spelling" not in str(error):
                    raise
                continue
            if native.encode() != table_row_bytes({"value": value}, column):
                raise IntegrityError(f"DuckDB {duckdb.__version__} spells DOUBLE {value!r} as another value")
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
    check_native_spelling()
    available = dict(native_columns(rows))
    if any(available.get(name) != kind for name, kind in identity.columns):
        raise IntegrityError("table rows differ from the declared identity projection")
    with spilled_identities(records, identity_relation(rows, identity), cursor=cursor) as minted:
        if minted.relation(cursor).aggregate("member_key, count(*) AS copies", "member_key").filter("copies > 1").limit(1).fetchone():
            raise IntegrityError("table contains a duplicate member key")
        yield minted


@contextmanager
def spilled_identities(records, identities, *, cursor):
    """Spill a relation of (member_key, row_digest, occurrence_hash) on ``cursor`` to scratch Parquet, removed on exit."""
    if tuple(identities.columns) != ("member_key", "row_digest", "occurrence_hash"):
        raise IntegrityError("spilled identities hold member_key, row_digest and occurrence_hash")
    with TemporaryDirectory(prefix="docspec-table-identities-", dir=records.merge_scratch_root) as scratch:
        path = Path(scratch) / "identities.parquet"
        identities.write_parquet(str(path))
        yield MintedIdentities(path, cursor.read_parquet(str(path)).aggregate("count(*)").fetchone()[0])


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

    Up to 256 hashes are one IN filter, which DuckDB prunes per value: it
    reads at most one row group per hash in each appended file. More are one
    semi-join, a single scan of the index. Each found row must hash back to
    its occurrence under ``identity``. Identities of other kinds, and unknown
    ones, are omitted; memory grows with the requested count.
    """
    wanted = {}
    for occurrence_id in occurrence_ids:
        if isinstance(occurrence_id, str) and occurrence_id.startswith(OCCURRENCE_PREFIX) \
                and _HEX64.fullmatch(suffix := occurrence_id[len(OCCURRENCE_PREFIX):]):
            wanted[bytes.fromhex(suffix)] = occurrence_id
    found = {}
    with records._cursor() as cursor, records.relations({"index": _index(records, index)}, cursor=cursor) as relations:
        selected = identity_filter(cursor, relations["index"], list(wanted), column="occurrence_hash")
        for occurrence_hash, member_key, row_digest, first_state_id in selected.fetchall():
            occurrence_id, digest = wanted[occurrence_hash], "sha256:" + row_digest.hex()
            if table_occurrence_id(identity.family, identity.table, member_key, digest) != occurrence_id:
                raise IntegrityError("minted-occurrence index row differs from its occurrence hash")
            found[occurrence_id] = IndexedOccurrence(member_key, digest, first_state_id)
    return found


def _component_value(text, kind):
    """A candidate component as its column holds it, or None when no such value spells ``text``."""
    if kind == "VARCHAR":
        return text
    try:
        value = date.fromisoformat(text) if kind == "DATE" else int(text)
    except ValueError:
        return None
    return value if (value.isoformat() if kind == "DATE" else str(value)) == text else None


def candidate_rows(rows, identity: TableIdentity, keys):
    """Narrow ``rows`` to a superset of the rows spelling one of ``keys``, pruning row groups.

    Up to 256 keys push every component tuple that could spell one of them
    into the scan as IN filters on the key columns, typed as the columns
    hold them; beyond that ``rows`` is returned whole. Callers match the
    spelled key exactly.
    """
    keys = set(keys)
    if not keys or len(keys) > LITERAL_IDENTITIES:
        return rows if keys else rows.filter("false")
    candidates = {tuple(_component_value(part, kind) for part, kind in zip(parts, identity.key_kinds, strict=True))
                  for key in keys for parts in key_components(identity.key, key) if all(parts)}
    candidates = [parts for parts in candidates if all(part is not None for part in parts)]
    if not candidates:
        return rows.filter("false")
    return rows.filter(reduce(lambda left, right: left & right, (
        duckdb.ColumnExpression(field).isin(*map(duckdb.ConstantExpression, {parts[index] for parts in candidates}))
        for index, field in enumerate(identity.key.fields))))


def read_occurrences(records, table, identity: TableIdentity, occurrences: Mapping[str, IndexedOccurrence]) -> dict[str, bytes]:
    """Read the rows holding ``occurrences`` from ``table`` and return each one's canonical row bytes.

    One query: up to 256 keys push their key columns' candidate values into
    the scan, more become one semi-join, and only the matching rows are
    spelled. A missing row, or one whose digest differs from the index,
    refuses: the caller named a table whose membership does not hold that
    occurrence. The bytes are the occurrence's inline value.
    """
    keys = {entry.member_key for entry in occurrences.values()}
    payloads = {}
    with records._cursor() as cursor, records.relations({"table": table}, cursor=cursor) as relations:
        check_native_spelling()
        rows = relations["table"]
        available = dict(native_columns(rows))
        if any(available.get(name) != kind for name, kind in identity.columns):
            raise IntegrityError("table rows differ from the declared identity projection")
        keyed = candidate_rows(rows, identity, keys).project(f"{member_key_sql(identity)} AS __docspec_key, "
                             + ", ".join(identifier(name) for name, _ in identity.columns))
        selected = identity_filter(cursor, keyed, keys, column="__docspec_key").project(
            f"__docspec_key, {row_json_sql(identity.columns)} AS __docspec_row")
        for key, row_json in selected.fetchall():
            if key in payloads:
                raise IntegrityError("table contains a duplicate member key")
            payloads[key] = row_json.encode()
    result = {}
    for occurrence_id, entry in occurrences.items():
        payload = payloads.get(entry.member_key)
        if payload is None or sha256_digest(payload) != entry.row_digest:
            raise IntegrityError("table row differs from its minted occurrence")
        result[occurrence_id] = payload
    return result
