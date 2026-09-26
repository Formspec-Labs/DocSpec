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
from docspec.adapters.storage.table_occurrences import (append_occurrences, candidate_rows, check_native_spelling,
    lookup_occurrences, mint_identities, read_occurrences)
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


def admit_layers(records, path, identity: TableIdentity, schema: TableSchema, *, member_digest, state_id,
                 base=None, base_identity=None):
    """Mint a staged producer member's identities, register it, then retain its membership and index.

    ``base`` holds the admitted layers of the dataset's current state, whose
    rules ``base_identity`` share this identity's family, table and key
    spelling. Without a base, one native pass mints every occurrence. With a
    base of the same columns, one direct all-column join finds the added,
    removed and changed keys; only those rows are spelled and minted,
    unchanged keys keep their occurrence, and the membership applies the delta
    to the base's, sharing its files. Other columns re-mint every row against
    the base's index. Every key refusal precedes registration.
    Returns the admitted layers and counts: ``generated`` occurrences are new
    to the index and ``adopted`` ones it already held (ruling R1(b));
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
                    membership = _apply_delta(records, cursor, table, identity, base, minted, views["old"], counts)
                else:
                    membership = records.retain_relation(_membership_rows(minted.relation(cursor)), cursor=cursor,
                                                         layer_kind="core-membership", schema=MEMBERSHIP_SCHEMA,
                                                         partition_policy=MEMBERSHIP_POLICY)
                index, generated = append_occurrences(records, None if base is None else base["occurrences"], minted,
                                                      first_state_id=state_id)
        finally:
            for view in views.values():
                cursor.execute(f"DROP VIEW IF EXISTS {view}")
        if base is None:
            counts.update(added=counts["rows"], removed=0, changed=0)
        elif not delta:
            with records.relations({"old": base["membership"], "new": membership}, cursor=cursor) as memberships:
                counts.update(_membership_counts(memberships["old"], memberships["new"]))
    rows = counts["rows"]
    counts.update(generated=generated, adopted=rows - generated, reminted=base is not None and not delta)
    counts.setdefault("carried", rows - counts["added"] - counts["changed"])
    return {"table": table, "membership": membership, "occurrences": index}, counts


def _apply_delta(records, cursor, table, identity, base, minted, old_view, counts):
    """Apply minted rows and removed keys to the base membership; count them into ``counts``."""
    new_view = f"admission_registered_{uuid4().hex}"
    old_key, new_key = member_key_sql(identity, qualifier="o"), member_key_sql(identity, qualifier="n")
    fresh = minted.relation(cursor)
    with records.relations({"new": table}, cursor=cursor) as relations:
        relations["new"].create_view(new_view)
        try:
            removed = cursor.sql(f"SELECT {old_key} AS record_identity, {old_key} AS partition_value, "
                                 f"NULL::BLOB AS record_json FROM {old_view} o ANTI JOIN {new_view} n ON {old_key} = {new_key}")
            with closing(_membership_rows(fresh).union(removed).to_arrow_reader(BATCH_ROWS)) as delta:
                membership = records.apply_changes(base["membership"], delta)
        finally:
            cursor.execute(f"DROP VIEW IF EXISTS {new_view}")
    with records.relations({"base": base["membership"]}, cursor=cursor) as memberships:
        existing = memberships["base"].project("record_identity AS base_key")
        changed = fresh.join(existing, "member_key = base_key", how="semi").aggregate("count(*)").fetchone()[0]
    added = minted.row_count - changed
    counts.update(added=added, changed=changed, carried=counts["rows"] - minted.row_count,
                  removed=base["membership"].reference.record_count + added - membership.reference.record_count)
    return membership


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


def _keyed(table, identity, members, *, how):
    """Join addresses (member_key, occurrence_id) to table rows by spelled key, under names no column takes.

    Returns the joined relation and the reserved names of its key, occurrence
    and row-match columns.
    """
    taken = {name.casefold() for name, _ in identity.columns}
    key, occurrence, matched = (_fresh(taken, base) for base in ("docspec_member_key", "docspec_occurrence_id", "docspec_row_key"))
    left = members.project(f"member_key AS {identifier(key)}, occurrence_id AS {identifier(occurrence)}")
    right = table.project(f"*, {member_key_sql(identity)} AS {identifier(matched)}")
    return left.join(right, f"{identifier(key)} = {identifier(matched)}", how=how), key, occurrence, matched


def _occurrences(members, table, identity):
    """Spell each address's inline occurrence record from its table row; an absent row or address yields NULL."""
    joined, key, occurrence, matched = _keyed(table, identity, members, how="left")
    record = occurrence_json_sql(row_json_sql(identity.columns), occurrence_id=occurrence)
    return joined.project(f"{identifier(key)} AS member_key, {identifier(occurrence)} AS occurrence_id, "
                          f"CASE WHEN {identifier(matched)} IS NULL OR {identifier(occurrence)} IS NULL THEN NULL "
                          f"ELSE encode({record}) END AS occurrence_record")


@contextmanager
def occurrence_relation(records, layers, identity: TableIdentity, *, cursor=None, scope=None, addresses=None):
    """Yield (member_key, occurrence_id, occurrence_record) for a table-shaped state.

    Records are the exact inline occurrence entities ``inline_occurrence_payload``
    spells around each row's canonical bytes, built natively from the rows.
    ``scope`` names bounded keys, absent ones yielding NULLs, and reads only the
    table's candidate row groups; ``addresses`` is a relation of wanted_key on
    ``cursor``.
    """
    tables = {} if scope is None else {"wanted": pa.table({"wanted_key": pa.array(scope, type=pa.string())})}
    identities = None if scope is None else {"membership": list(scope)}
    with records.relations({"membership": layers["membership"], "table": layers["table"]}, tables=tables,
                           identities=identities, cursor=cursor) as relations:
        members, table = relations["membership"].project(MEMBERSHIP_ADDRESSES), relations["table"]
        wanted = relations["wanted"] if scope is not None else addresses
        if wanted is not None:
            members = wanted.join(members, "wanted_key = member_key", how="left").project("wanted_key AS member_key, occurrence_id")
        if scope is not None:
            table = candidate_rows(table, identity, scope)
        yield _occurrences(members, table, identity)


@contextmanager
def typed_relation(records, layers, identity: TableIdentity, *, cursor=None):
    """Yield (member_key, occurrence_id, <the producer's columns>) for a table-shaped state, without JSON."""
    fields = layers["table"].schema.fields
    if {name.casefold() for name in fields} & _READER_COLUMNS:
        raise IntegrityError("table columns clash with the reader's member_key or occurrence_id")
    with records.relations({"membership": layers["membership"], "table": layers["table"]}, cursor=cursor) as relations:
        joined, key, occurrence, _ = _keyed(relations["table"], identity,
                                            relations["membership"].project(MEMBERSHIP_ADDRESSES), how="inner")
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
