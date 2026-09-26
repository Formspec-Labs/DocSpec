"""Table-shaped Core states: a registered producer table or a derived one, its membership and the minted-occurrence index.

Decision 0007. A version-3 state manifest names the table (a producer member
registered by reference, or a derived layer DocSpec writes natively, C29),
``core-membership:1`` rows as every keyed state has (``membership``), the
dataset's append-only minted-occurrence index as of the state (``occurrences``)
and the identity ``rules``. Occurrence records are spelled natively from the
table's rows when read and never stored; the index resolves an occurrence to
its key without a scan.
"""

from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
import hashlib
from pathlib import Path
from uuid import uuid4

import pyarrow as pa

from docspec.adapters.storage.core_entities import MEMBERSHIP_ADDRESSES, MEMBERSHIP_POLICY, MEMBERSHIP_SCHEMA
from docspec.adapters.storage.iceberg import identifier
from docspec.adapters.storage.records import identity_filter
from docspec.adapters.storage.table_occurrences import (IndexedOccurrence, MintedIdentities, append_occurrences,
    check_native_spelling, lookup_occurrences, mint_identities, read_occurrences, spell_rows, spilled_identities)
from docspec.adapters.streams import owned_iterator
from docspec.adapters.storage.table_sql import (OCCURRENCE_PREFIX, identity_relation, member_key_sql, membership_json_sql,
    occurrence_json_sql, occurrence_urn_sql, row_json_sql, rows_differ_sql)
from docspec.domain.core_admission import inline_occurrence_payload
from docspec.domain.identity import sha256_digest
from docspec.domain.references import LayerRef
from docspec.domain.storage import TableSchema
from docspec.domain.table_rows import DERIVED_MEMBER, SOURCE_OCCURRENCE, TableIdentity, table_occurrence_id, table_type
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
    """Give the base membership the minted rows and drop the removed keys; count them into ``counts``."""
    fresh = minted.relation(cursor)
    with records.relations({"base": base["membership"], "new": table}, cursor=cursor) as relations:
        changed = fresh.join(relations["base"].project("record_identity AS base_key"), "member_key = base_key",
                             how="semi").aggregate("count(*)").fetchone()[0]
        added = minted.row_count - changed
        removed = base["membership"].reference.record_count + added - counts["rows"]
        counts.update(added=added, changed=changed, carried=counts["rows"] - minted.row_count, removed=removed)
        gone = relations["base"].project("record_identity AS gone_key").join(
            relations["new"].project(f"{member_key_sql(identity)} AS new_key"), "gone_key = new_key", how="anti")
        membership = _revise_membership(records, cursor, base["membership"], fresh, gone,
                                        changes=minted.row_count + removed)
    if membership.reference.record_count != counts["rows"]:
        raise IntegrityError("table-shaped membership differs from its table's rows")
    return membership


def _revise_membership(records, cursor, base, fresh, gone, *, changes):
    """Give the membership ``base`` the minted ``fresh`` rows and drop the keys ``gone`` names (``gone_key``).

    Up to ``DELTA_ROWS`` changes on a membership of fewer than
    ``MEMBERSHIP_FILES`` data files go through ``apply_changes``, sharing
    every base file. Anything larger is one native rewrite of the whole
    membership (``retain_relation``), so no delta admits rows in Python beyond
    that bound and the files a dataset's states accumulate are compacted.
    """
    if changes <= DELTA_ROWS and len(records.data_files(base.reference)) < MEMBERSHIP_FILES:
        removals = gone.project("gone_key AS record_identity, gone_key AS partition_value, NULL::BLOB AS record_json")
        with closing(_membership_rows(fresh).union(removals).to_arrow_reader(BATCH_ROWS)) as delta:
            return records.apply_changes(base, delta)
    with records.relations({"base": base}, cursor=cursor) as relations:
        kept = relations["base"].project("record_identity, partition_value, record_json").join(
            gone, "record_identity = gone_key", how="anti").join(
            fresh.project("member_key AS minted_key"), "record_identity = minted_key", how="anti")
        return records.retain_relation(kept.union(_membership_rows(fresh)), cursor=cursor, layer_kind="core-membership",
                                       schema=MEMBERSHIP_SCHEMA, partition_policy=MEMBERSHIP_POLICY)


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


@dataclass(frozen=True, slots=True)
class StagedRows:
    """A derive's rows spilled once to scratch with their minted identities, valid while ``staged_rows`` is open.

    ``digest`` covers the rows as a set: the sha256 of their occurrence hashes
    in hash order, each of which binds the definition, schema, key and row.
    """

    identity: TableIdentity
    schema: TableSchema
    path: Path
    minted: MintedIdentities
    digest: str

    @property
    def count(self) -> int:
        return self.minted.row_count

    def rows(self, cursor):
        """Read the staged rows on any cursor of the record store."""
        return cursor.read_parquet(str(self.path))


@contextmanager
def staged_rows(records, batches, schema: TableSchema, identity: TableIdentity):
    """Spill a derive's typed batches once and mint every row natively; nothing reaches the store.

    ``schema`` holds the columns in the caller's order. A NULL, empty or
    repeated key refuses here, before any layer is written.
    """
    stored = table_schema(schema.columns)
    with records.staged_table(batches, schema=stored) as (path, _), records._cursor() as cursor, \
            mint_identities(records, cursor.read_parquet(str(path)), identity, cursor=cursor) as minted:
        yield StagedRows(identity, stored, path, minted, _set_digest(minted.relation(cursor)))


def _set_digest(minted):
    """The sha256 of the occurrence hashes in hash order, streamed in Arrow batches: the same rows in any order digest alike."""
    digest = hashlib.sha256()
    with closing(minted.project("occurrence_hash").order("occurrence_hash").to_arrow_reader(BATCH_ROWS)) as reader:
        for batch in reader:
            hashes = batch.column(0).cast(pa.binary(32))
            digest.update(hashes.buffers()[1][hashes.offset * 32:(hashes.offset + len(hashes)) * 32])
    return "sha256:" + digest.hexdigest()


def derive_layers(records, staged: StagedRows, *, state_id, base=None, removals=()):
    """Write a derived layer's table, membership and index from staged rows; over a base, only the changed rows.

    Without a base the rows become a new table in the caller's order (a key
    sort would hold the wide rows in memory, 4.7 GB at 1 M, and point reads
    are bound by spelling, not pruning), the membership is written from their
    identities and a new index starts. With one, every source
    member the rows or ``removals`` name is touched: a staged row whose key
    already holds its occurrence is unchanged and not written, and a base row of
    a touched member that no staged row keys is removed. The table and the
    membership take that delta through ``apply_changes``, sharing the base's
    files, and only written rows reach the index. Returns the layers and counts:
    ``unchanged`` staged rows were not written; ``generated`` occurrences are new
    to the index and ``adopted`` ones it already held.
    """
    with records._cursor() as cursor:
        if base is None:
            table = records.write_table_relation(staged.rows(cursor), cursor=cursor, layer_kind=TABLE_KIND,
                                                 schema=staged.schema)
            membership = records.retain_relation(_membership_rows(staged.minted.relation(cursor)), cursor=cursor,
                                                 layer_kind="core-membership", schema=MEMBERSHIP_SCHEMA,
                                                 partition_policy=MEMBERSHIP_POLICY)
            index, generated = append_occurrences(records, None, staged.minted, first_state_id=state_id)
            counts = {"rows": staged.count, "added": staged.count, "changed": 0, "removed": 0, "unchanged": 0}
        else:
            table, membership, index, generated, counts = _apply_derivation(records, cursor, staged, base, removals,
                                                                            state_id)
    if not table.reference.record_count == membership.reference.record_count == counts["rows"]:
        raise IntegrityError("derived table and membership disagree about their rows")
    counts.update(generated=generated, adopted=counts["added"] + counts["changed"] - generated)
    return {"table": table, "membership": membership, "occurrences": index}, counts


def _apply_derivation(records, cursor, staged, base, removals, state_id):
    """Replace the base's rows of every touched source member, writing only the rows whose occurrence changed.

    One native scan of the base's key columns finds the touched members'
    rows; they, the staged rows whose (key, occurrence) the base does not
    hold, and the base keys no staged row holds are materialized once, each
    as small as the change.
    """
    fields, key, member = staged.identity.key.fields, member_key_sql(staged.identity), identifier(DERIVED_MEMBER)
    names = ", ".join(identifier(field) for field in fields)
    row_key = identifier(_fresh({name.casefold() for name in staged.schema.fields}, "docspec_row_key"))
    rows, minted = staged.rows(cursor), staged.minted.relation(cursor)
    tables = {"removals": pa.table({"removed_member": pa.array(sorted(removals), type=pa.string())})}
    with records.relations({"table": base["table"], "membership": base["membership"]}, tables=tables,
                           cursor=cursor) as relations:
        if rows.join(relations["removals"], f"{member} = removed_member", how="semi").limit(1).fetchone():
            raise IntegrityError("derive removal repeats a member key its rows hold")
        touched = rows.project(f"{member} AS touched").union(relations["removals"].project("removed_member")).distinct()
        old = records.temp_table(cursor, relations["table"].join(touched, f"{member} = touched", how="semi").project(
            f"{names}, {key} AS old_key"), "derived_old")
        held = relations["membership"].join(old.project("old_key"), "record_identity = old_key", how="semi").project(
            MEMBERSHIP_ADDRESSES).project("member_key AS held_key, occurrence_id AS held_occurrence")
        changed = records.temp_table(cursor, minted.join(
            held, f"member_key = held_key AND {occurrence_urn_sql('occurrence_hash')} = held_occurrence", how="anti"),
            "derived_changed")
        removed = records.temp_table(cursor, old.join(minted.project("member_key AS new_key"), "old_key = new_key",
                                                      how="anti"), "derived_removed")
        rewritten = rows.project(f"*, {key} AS {row_key}").join(changed.project("member_key AS changed_key"),
                                                                 f"{row_key} = changed_key", how="semi")
        table = records.apply_changes(base["table"], rewritten.project(", ".join(map(identifier, staged.schema.fields))),
                                      key=fields, removed=removed.project(names), cursor=cursor)
        written, replaced = changed.join(old.project("old_key"), "member_key = old_key", how="left").aggregate(
            "count(*), count(old_key)").fetchone()
        dropped = removed.aggregate("count(*)").fetchone()[0]
        membership = _revise_membership(records, cursor, base["membership"], changed,
                                        removed.project("old_key AS gone_key"), changes=written + dropped)
        with spilled_identities(records, changed, cursor=cursor) as fresh:
            index, generated = append_occurrences(records, base["occurrences"], fresh, first_state_id=state_id)
    counts = {"rows": base["table"].reference.record_count + written - replaced - dropped, "added": written - replaced,
              "changed": replaced, "removed": dropped, "unchanged": staged.count - written}
    return table, membership, index, generated, counts


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
    canonical bytes. ``scope`` names bounded keys, absent ones addressing
    nothing: their rows are read as ``spell_rows`` reads them, spelled by the
    Python reference and each checked against its occurrence, so a point read
    binds no row expression. A whole state, or ``addresses`` (a relation of
    wanted_key on ``cursor``, materialized once as compact addresses), is
    spelled natively.
    """
    tables = {} if scope is None else {"wanted": pa.table({"wanted_key": pa.array(scope, type=pa.string())})}
    identities = None if scope is None else {"membership": list(scope)}
    with (records._cursor() if cursor is None else nullcontext(cursor)) as cursor, records.relations(
            {"membership": layers["membership"], "table": layers["table"]}, tables=tables, identities=identities,
            cursor=cursor) as relations:
        members, table = relations["membership"].project(MEMBERSHIP_ADDRESSES), relations["table"]
        wanted = relations["wanted"] if scope is not None else addresses
        if wanted is not None:
            members = wanted.join(members, "wanted_key = member_key", how="left").project("wanted_key AS member_key, occurrence_id")
        if scope is not None:
            held = dict(members.filter("occurrence_id IS NOT NULL").fetchall())
            yield members, cursor.from_arrow(_spelled_values(cursor, table, identity, held))
            return
        if addresses is not None:
            members = records.temp_table(cursor, members, "selection_addresses")
        joined, _, occurrence = _keyed(table, identity, members.filter("occurrence_id IS NOT NULL"))
        record = occurrence_json_sql(row_json_sql(identity.columns), occurrence_id=occurrence)
        yield members, joined.project(f"{identifier(occurrence)} AS entity_id, encode({record}) AS occurrence_record")


def _spelled_values(cursor, table, identity: TableIdentity, held):
    """The (entity_id, occurrence_record) values of the occurrences ``held`` maps keys to, spelled in Python.

    ``table`` is the state's table relation on ``cursor``. Each row must hash
    back to the occurrence its membership names: a missing or changed row
    refuses, as an index lookup's digest check does.
    """
    rows = spell_rows(cursor, table, identity, held)
    records_ = []
    for key, occurrence in held.items():
        payload = rows.get(key)
        if payload is None or table_occurrence_id(identity.family, identity.table, key, sha256_digest(payload)) != occurrence:
            raise IntegrityError("table row differs from its minted occurrence")
        records_.append(inline_occurrence_payload(occurrence, payload))
    return pa.table({"entity_id": pa.array(list(held.values()), type=pa.string()),
                     "occurrence_record": pa.array(records_, type=pa.binary())})


@contextmanager
def typed_relation(records, layers, identity: TableIdentity, *, cursor=None):
    """Yield (member_key, occurrence_id, <the table's columns>) for a table-shaped state, without JSON.

    A key spelled from a member_key column, as every derived layer's is, keeps
    that column as member_key: the state's key for a one-to-one layer, the
    source member's for a one-to-many one, keyed member_key#segment_index.
    """
    fields = layers["table"].schema.fields
    own = identity.key.fields[0] == DERIVED_MEMBER
    if {name.casefold() for name in fields} & (_READER_COLUMNS - {DERIVED_MEMBER} if own else _READER_COLUMNS):
        raise IntegrityError("table columns clash with the reader's member_key or occurrence_id")
    with records.relations({"membership": layers["membership"], "table": layers["table"]}, cursor=cursor) as relations:
        joined, key, occurrence = _keyed(relations["table"], identity, relations["membership"].project(MEMBERSHIP_ADDRESSES))
        head = identifier(DERIVED_MEMBER) if own else f"{identifier(key)} AS member_key"
        yield joined.project(", ".join([head, f"{identifier(occurrence)} AS occurrence_id",
                                        *(identifier(name) for name in fields if not (own and name == DERIVED_MEMBER))]))


@contextmanager
def affected_rows(records, layers, identity: TableIdentity, older, newer):
    """Yield a derived state's typed rows, as ``typed_relation`` does, that were derived from a row ``newer`` no longer holds.

    ``older`` and ``newer`` are the membership layers of two states of one input,
    in this store. Every earlier occurrence of a changed or removed member comes
    out of one anti-join of their canonical membership bytes; the rows whose
    source_occurrence_id, or any element of it for a fusion, names one come out
    of one semi-join. C16's affected-result query, natively and per row.
    """
    if dict(layers["table"].schema.columns).get(SOURCE_OCCURRENCE) not in {"VARCHAR", "VARCHAR[]"} \
            or identity.key.fields[0] != DERIVED_MEMBER:
        raise IntegrityError("only a derived layer names the rows it was derived from")
    listed = dict(layers["table"].schema.columns)[SOURCE_OCCURRENCE] == "VARCHAR[]"
    taken = {name.casefold() for name in layers["table"].schema.fields}
    hits = {field: _fresh(taken, "docspec_hit_" + field) for field in identity.key.fields}
    with records._cursor() as cursor, records.relations({"old": older, "new": newer, "table": layers["table"]},
                                                        cursor=cursor) as inputs, \
            typed_relation(records, layers, identity, cursor=cursor) as rows:
        changed = inputs["old"].project("record_json AS old_member").join(
            inputs["new"].project("record_json AS new_member"), "old_member = new_member", how="anti").project(
            "json_extract_string(decode(old_member), '/occurrence_id') AS changed_occurrence")
        source = identifier(SOURCE_OCCURRENCE)
        references = inputs["table"].project(", ".join(f"{identifier(field)} AS {identifier(hit)}" for field, hit in hits.items())
                                             + f", {f'unnest({source})' if listed else source} AS docspec_reference")
        found = references.join(changed, "docspec_reference = changed_occurrence", how="semi").project(
            ", ".join(identifier(hit) for hit in hits.values())).distinct()
        yield rows.join(found, " AND ".join(f"{identifier(field)} = {identifier(hit)}" for field, hit in hits.items()),
                        how="semi")


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
