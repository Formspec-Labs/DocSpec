"""Table-shaped Core states: a producer's registered table, its membership and the dataset's minted-occurrence index.

Decision 0007. A version-3 state manifest names the producer member registered
by reference (``table``), ``core-membership:1`` rows as every keyed state has
(``membership``), the dataset's append-only minted-occurrence index as of the
admission (``occurrences``) and the identity ``rules``. Occurrence records are
spelled natively from the table's rows when read and never stored; the index
resolves an occurrence to its key without a scan.
"""

from contextlib import closing, contextmanager
from dataclasses import dataclass
from uuid import uuid4

import pyarrow as pa

from docspec.adapters.storage.core_entities import MEMBERSHIP_ADDRESSES, MEMBERSHIP_POLICY, MEMBERSHIP_SCHEMA
from docspec.adapters.storage.iceberg import identifier
from docspec.adapters.storage.records import identity_filter
from docspec.adapters.storage.table_occurrences import (IndexedOccurrence, append_occurrences, candidate_rows,
    check_native_spelling, lookup_occurrences, mint_identities, read_occurrences)
from docspec.adapters.streams import owned_iterator
from docspec.adapters.storage.table_sql import (OCCURRENCE_PREFIX, identity_relation, member_key_sql, membership_json_sql,
    occurrence_json_sql, occurrence_urn_sql, row_json_sql, rows_differ_sql)
from docspec.domain.core_admission import inline_occurrence_payload
from docspec.domain.identity import sha256_digest
from docspec.domain.references import LayerRef
from docspec.domain.storage import TableSchema
from docspec.domain.table_rows import TableIdentity, table_type
from docspec.errors import IntegrityError
from docspec.ports.record_storage import BATCH_ROWS


TABLE_KIND = "core-table"
TABLE_SCHEMA_ID = "core-table:1"
_READER_COLUMNS = frozenset({"member_key", "occurrence_id"})


@dataclass(frozen=True, slots=True)
class TableStateView:
    """A table-shaped state as a member search sees it: its pinned index, table and membership.

    ``committed_ms`` is its table's commit time; every admission registers its
    own table, while unchanged generations share their index and membership.
    """

    reference: LayerRef
    committed_ms: int
    table: LayerRef
    membership: LayerRef
    identity: TableIdentity


def table_schema(columns) -> TableSchema:
    """A producer table's closed schema in its physical column order, refusing a type the profile cannot hold."""
    try:
        return TableSchema(TABLE_SCHEMA_ID, tuple((name, table_type(kind)) for name, kind in columns))
    except ValueError as error:
        raise IntegrityError(f"generation table has an unsupported column type: {error}") from error


def _membership_rows(minted):
    """Membership records natively from spilled identities: key routing and canonical Membership bytes."""
    addresses = minted.project(f"member_key, {occurrence_urn_sql('occurrence_hash')} AS occurrence_id")
    return addresses.project("member_key AS record_identity, member_key AS partition_value, "
                             f"encode({membership_json_sql()}) AS record_json")


# A delta applies through apply_changes, which admits each changed row in
# Python and adds files; a larger delta, or a membership already spread over
# this many data files, is rewritten natively instead, which also compacts it.
DELTA_ROWS = 32 * BATCH_ROWS
MEMBERSHIP_FILES = 16


@contextmanager
def admit_layers(records, path, identity: TableIdentity, schema: TableSchema, *, member_digest, state_id,
                 base=None, base_identity=None):
    """Mint a staged producer member's identities, register it, then retain its membership and index.

    ``base`` holds the admitted layers of the dataset's current state, whose
    rules ``base_identity`` share this identity's family, table and key
    spelling. Without a base, one native pass mints every occurrence. With a
    base of the same columns, one direct all-column join finds the added,
    removed and changed keys; only those rows are spelled and minted,
    unchanged keys keep their occurrence, and the membership takes the delta
    (``_apply_delta``). Other columns re-mint every row against the base's
    index. Every key refusal precedes registration.

    Yields the admitted layers, the counts and the minted identities, which
    stay readable until the caller has published: ``generated`` occurrences
    are new to the index and ``adopted`` ones it already held (ruling R1(b));
    ``added``, ``removed`` and ``changed`` compare memberships with the base,
    and ``carried`` keys kept their base occurrence.
    """
    delta = base is not None and base_identity.columns == identity.columns
    views = {name: f"admission_{name}_{uuid4().hex}" for name in ("new", "old")}
    new_key, old_key = member_key_sql(identity, qualifier="n"), member_key_sql(identity, qualifier="o")
    with records._cursor() as cursor, records.relations({} if base is None else {"old": base["table"]},
                                                        cursor=cursor) as relations:
        check_native_spelling()
        cursor.read_parquet(str(path)).create_view(views["new"])
        if base is not None:
            relations["old"].create_view(views["old"])
        try:
            rows = cursor.sql(f"SELECT * FROM {views['new']}")
            if delta:
                if cursor.sql(f"SELECT count(*) - count(DISTINCT {new_key}) FROM {views['new']} n").fetchone()[0]:
                    raise IntegrityError("table contains a duplicate member key")
                # One direct all-column join; only added and changed rows are spelled.
                rows = cursor.sql(f"SELECT n.* FROM {views['new']} n LEFT JOIN {views['old']} o ON {new_key} = {old_key} "
                                  f"WHERE o.{identifier(identity.key.fields[0])} IS NULL OR "
                                  f"{rows_differ_sql(identity.columns, 'n', 'o')}")
            with mint_identities(records, rows, identity, cursor=cursor) as minted:
                table = records.register_parquet(path, layer_kind=TABLE_KIND, schema=schema, member_digest=member_digest)
                counts = {"rows": table.reference.record_count}
                if delta:
                    membership = _apply_delta(records, cursor, table, identity, base, minted, counts)
                else:
                    membership = records.retain_relation(_membership_rows(minted.relation(cursor)), cursor=cursor,
                                                         layer_kind="core-membership", schema=MEMBERSHIP_SCHEMA,
                                                         partition_policy=MEMBERSHIP_POLICY)
                index, generated = append_occurrences(records, None if base is None else base["occurrences"], minted,
                                                      first_state_id=state_id)
                if base is None:
                    counts.update(added=counts["rows"], removed=0, changed=0)
                elif not delta:
                    with records.relations({"old": base["membership"], "new": membership}, cursor=cursor) as memberships:
                        counts.update(_membership_counts(memberships["old"], memberships["new"]))
                rows = counts["rows"]
                counts.update(generated=generated, adopted=rows - generated, reminted=base is not None and not delta)
                counts.setdefault("carried", rows - counts["added"] - counts["changed"])
                yield {"table": table, "membership": membership, "occurrences": index}, counts, minted
        finally:
            for view in views.values():
                cursor.execute(f"DROP VIEW IF EXISTS {view}")


def _apply_delta(records, cursor, table, identity, base, minted, counts):
    """Give the base membership the minted rows and drop the removed keys; count them into ``counts``.

    Up to ``DELTA_ROWS`` changes on a membership of fewer than
    ``MEMBERSHIP_FILES`` data files go through ``apply_changes``, sharing
    every base file. Anything larger is one native rewrite of the whole
    membership (``retain_relation``), so no delta admits rows in Python beyond
    that bound and the files a dataset's generations accumulate are compacted.
    """
    fresh = minted.relation(cursor)
    with records.relations({"base": base["membership"], "new": table}, cursor=cursor) as relations:
        base_rows = relations["base"].project("record_identity, partition_value, record_json")
        changed = fresh.join(base_rows.project("record_identity AS base_key"), "member_key = base_key", how="semi") \
            .aggregate("count(*)").fetchone()[0]
        added = minted.row_count - changed
        removed = base["membership"].reference.record_count + added - counts["rows"]
        counts.update(added=added, changed=changed, carried=counts["rows"] - minted.row_count, removed=removed)
        new_keys = relations["new"].project(f"{member_key_sql(identity)} AS new_key")
        minted_keys = fresh.project("member_key AS minted_key")
        if minted.row_count + removed <= DELTA_ROWS and len(records.data_files(base["membership"].reference)) < MEMBERSHIP_FILES:
            gone = base_rows.join(new_keys, "record_identity = new_key", how="anti").project(
                "record_identity, partition_value, NULL::BLOB AS record_json")
            with closing(_membership_rows(fresh).union(gone).to_arrow_reader(BATCH_ROWS)) as delta:
                membership = records.apply_changes(base["membership"], delta)
        else:
            kept = base_rows.join(new_keys, "record_identity = new_key", how="semi").join(
                minted_keys, "record_identity = minted_key", how="anti")
            membership = records.retain_relation(kept.union(_membership_rows(fresh)), cursor=cursor,
                                                 layer_kind="core-membership", schema=MEMBERSHIP_SCHEMA,
                                                 partition_policy=MEMBERSHIP_POLICY)
    if membership.reference.record_count != counts["rows"]:
        raise IntegrityError("table-shaped membership differs from its table's rows")
    return membership


def check_minted_copies(records, ledger, table, identity: TableIdentity, minted):
    """Refuse an occurrence this admission minted whose identity the ledger holds with other bytes, or as a state.

    A ledger row takes precedence over layers when read by identity, so only
    minted identities can collide. The ledger's table-occurrence rows are read
    by one range scan up to the minted count and matched to the minted
    identities natively; when they outnumber the minted rows, the minted
    identities are looked up instead (``data_identities``). Python work is
    bounded by the smaller side, and only matches have their rows read.
    """
    pinned = []
    with owned_iterator(ledger.data_identities_with_prefix(OCCURRENCE_PREFIX)) as batches:
        for batch in batches:
            pinned.extend(batch)
            if len(pinned) > minted.row_count:
                pinned = None
                break
    if pinned == []:
        return
    with records._cursor() as cursor:
        identities = minted.relation(cursor).project(f"{occurrence_urn_sql('occurrence_hash')} AS occurrence_id")
        if pinned is None:
            with closing(identities.to_arrow_reader(BATCH_ROWS)) as reader, owned_iterator(ledger.data_identities(
                    occurrence for batch in reader for occurrence in batch.column(0).to_pylist())) as groups:
                matches = [row for group in groups for row in group]
        else:
            rows = cursor.from_arrow(pa.table(dict(zip(("ledger_kind", "ledger_id", "ledger_digest"), zip(*pinned)))))
            matches = identities.join(rows, "occurrence_id = ledger_id").project(
                "ledger_kind, ledger_id, ledger_digest").fetchall()
        if any(kind == "state" for kind, _, _ in matches):
            raise IntegrityError("data identity is ambiguous between an entity and a state")
        held, found = {occurrence: digest for _, occurrence, digest in matches}, {}
        for start in range(0, len(held), BATCH_ROWS):
            hashes = [bytes.fromhex(occurrence[len(OCCURRENCE_PREFIX):]) for occurrence in list(held)[start:start + BATCH_ROWS]]
            selected = identity_filter(cursor, minted.relation(cursor), hashes, column="occurrence_hash")
            for member_key, row_digest, occurrence_hash in selected.project("member_key, row_digest, occurrence_hash").fetchall():
                found[OCCURRENCE_PREFIX + occurrence_hash.hex()] = IndexedOccurrence(member_key, "sha256:" + row_digest.hex(), "")
    for occurrence, row in read_occurrences(records, table, identity, found).items():
        if held[occurrence] != sha256_digest(inline_occurrence_payload(occurrence, row)):
            raise IntegrityError("state member conflicts with an immutable retained record")


def _membership_counts(old, new):
    """Added, removed and changed keys between two membership relations, by their canonical bytes."""
    before = old.project("record_identity AS old_key, record_json AS old_member")
    after = new.project("record_identity AS new_key, record_json AS new_member")
    changes = before.join(after, "old_key = new_key", how="outer").filter("old_member IS DISTINCT FROM new_member").project(
        "CASE WHEN old_key IS NULL THEN 'added' WHEN new_key IS NULL THEN 'removed' ELSE 'changed' END AS change")
    counts = dict(changes.aggregate("change, count(*)", "change").fetchall())
    return {name: counts.get(name, 0) for name in ("added", "removed", "changed")}


def _fresh(taken, base):
    """A column name no producer column spells, even under SQL case folding."""
    name, index = base, 0
    while name.casefold() in taken:
        index += 1
        name = f"{base}_{index}"
    taken.add(name.casefold())
    return name


def _keyed(table, identity, members):
    """Join table rows to their (member_key, occurrence_id) addresses by spelled key, under names no column takes.

    The rows are the probe side and the compact addresses the build side, so
    the join holds addresses, never rows. Returns the joined relation and the
    reserved names of its key and occurrence columns.
    """
    taken = {name.casefold() for name, _ in identity.columns}
    key, occurrence, matched = (_fresh(taken, base) for base in ("docspec_member_key", "docspec_occurrence_id", "docspec_row_key"))
    rows = table.project(f"*, {member_key_sql(identity)} AS {identifier(matched)}")
    addresses = members.project(f"member_key AS {identifier(key)}, occurrence_id AS {identifier(occurrence)}")
    return rows.join(addresses, f"{identifier(matched)} = {identifier(key)}", how="inner"), key, occurrence


# An occurrence record frames its row's spelling with a fixed-length URN.
_FRAMING_BYTES = len(inline_occurrence_payload(OCCURRENCE_PREFIX + "0" * 64, b""))


def spelled_payload(records, table, identity: TableIdentity):
    """(estimated occurrence-record bytes, rows) of a table-shaped state's table, for sizing ordered reads.

    Footers size encoded columns, which dictionaries shrink far below the
    JSON readers spell, so the mean spelling of one batch of rows sizes the
    records instead.
    """
    rows = table.reference.record_count
    with records.relations({"table": table}) as relations:
        mean = relations["table"].limit(BATCH_ROWS).aggregate(
            f"avg(strlen({row_json_sql(identity.columns)}))").fetchone()[0]
    return int(((mean or 0) + _FRAMING_BYTES) * rows), rows


@contextmanager
def occurrence_addressed(records, layers, identity: TableIdentity, *, cursor=None, scope=None, addresses=None):
    """Yield a table-shaped state's (member_key, occurrence_id) addresses and the values they name.

    Values are (entity_id, occurrence_record): the exact inline occurrence
    entities ``inline_occurrence_payload`` spells around each addressed row's
    canonical bytes, built natively from the rows. ``scope`` names bounded
    keys, absent ones addressing nothing, and reads only the table's
    candidate row groups; ``addresses`` is a relation of wanted_key on
    ``cursor``, materialized once as compact addresses.
    """
    tables = {} if scope is None else {"wanted": pa.table({"wanted_key": pa.array(scope, type=pa.string())})}
    identities = None if scope is None else {"membership": list(scope)}
    with records.relations({"membership": layers["membership"], "table": layers["table"]}, tables=tables,
                           identities=identities, cursor=cursor) as relations:
        members, table = relations["membership"].project(MEMBERSHIP_ADDRESSES), relations["table"]
        wanted = relations["wanted"] if scope is not None else addresses
        if wanted is not None:
            members = wanted.join(members, "wanted_key = member_key", how="left").project("wanted_key AS member_key, occurrence_id")
        if addresses is not None:
            members = records.temp_table(cursor, members, "selection_addresses")
        if scope is not None:
            table = candidate_rows(table, identity, scope)
        joined, _, occurrence = _keyed(table, identity, members.filter("occurrence_id IS NOT NULL"))
        record = occurrence_json_sql(row_json_sql(identity.columns), occurrence_id=occurrence)
        yield members, joined.project(f"{identifier(occurrence)} AS entity_id, encode({record}) AS occurrence_record")


@contextmanager
def typed_relation(records, layers, identity: TableIdentity, *, cursor=None):
    """Yield (member_key, occurrence_id, <the producer's columns>) for a table-shaped state, without JSON."""
    fields = layers["table"].schema.fields
    if {name.casefold() for name in fields} & _READER_COLUMNS:
        raise IntegrityError("table columns clash with the reader's member_key or occurrence_id")
    with records.relations({"membership": layers["membership"], "table": layers["table"]}, cursor=cursor) as relations:
        joined, key, occurrence = _keyed(relations["table"], identity, relations["membership"].project(MEMBERSHIP_ADDRESSES))
        yield joined.project(f"{identifier(key)} AS member_key, {identifier(occurrence)} AS occurrence_id, "
                             + ", ".join(identifier(name) for name in fields))


def _held(records, membership, keys):
    """Map each of ``keys`` that ``membership`` holds to its occurrence, in bounded groups."""
    keys, held = sorted(keys), {}
    for start in range(0, len(keys), BATCH_ROWS):
        chunk = keys[start:start + BATCH_ROWS]
        with records.relations({"membership": membership}, identities={"membership": chunk}) as relations:
            held.update(relations["membership"].project(MEMBERSHIP_ADDRESSES).fetchall())
    return held


def find_table_members(records, identities, views):
    """Resolve table occurrences through each view's pinned index, newest view first.

    Returns identity -> (view, byte size, exact record bytes, sha256 hex). An
    occurrence is read from the newest state whose membership holds it at its
    key; only its row is spelled, and the index digest check refuses a row
    that changed. Other identities are skipped without a query.
    """
    remaining = {identity for identity in identities if isinstance(identity, str) and identity.startswith(OCCURRENCE_PREFIX)}
    found = {}
    for view in views:
        if not remaining:
            break
        indexed = lookup_occurrences(records, view.reference, view.identity, remaining)
        if not indexed:
            continue
        held = _held(records, view.membership, {entry.member_key for entry in indexed.values()})
        wanted = {occurrence: entry for occurrence, entry in indexed.items() if held.get(entry.member_key) == occurrence}
        for occurrence, row in (read_occurrences(records, view.table, view.identity, wanted) if wanted else {}).items():
            payload = inline_occurrence_payload(occurrence, row)
            found[occurrence] = view, len(payload), payload, sha256_digest(payload)[7:]
        remaining -= found.keys()
    return found


def check_table_membership(records, layers, identity: TableIdentity):
    """Refuse a membership that does not hold exactly its table's rows, each at its minted occurrence.

    Admission establishes this by construction; this full native identity
    pass is for a manifest this session did not write.
    """
    with records._cursor() as cursor, records.relations(
            {"membership": layers["membership"], "table": layers["table"]}, cursor=cursor) as relations:
        check_native_spelling()
        table = relations["table"]
        if table.aggregate(f"count(*) - count(DISTINCT {member_key_sql(identity)})").fetchone()[0]:
            raise IntegrityError("table contains a duplicate member key")
        minted = identity_relation(table, identity).project(
            f"member_key AS row_key, {occurrence_urn_sql('occurrence_hash')} AS row_occurrence")
        members = relations["membership"].project(MEMBERSHIP_ADDRESSES)
        if minted.join(members, "row_key = member_key", how="outer").filter(
                "row_key IS NULL OR member_key IS NULL OR row_occurrence IS DISTINCT FROM occurrence_id").limit(1).fetchone():
            raise IntegrityError("table-shaped membership differs from its table rows")
