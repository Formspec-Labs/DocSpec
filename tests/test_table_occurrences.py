"""Table occurrence identity: the native pass against the Python reference, the minted index and its lookup.

Native member keys, row digests and occurrence hashes must equal the Python
reference on a corpus of control characters, a NaN with its sign bit set,
BIGINT beyond 2^53, dates, timestamps and string lists, whichever writer
produced the table. The index generates each occurrence once and adopts it
after (ruling R1(b)); a lookup must not read index row groups that cannot hold
the hashes it asks for, and must refuse a row whose digest changed.
"""

from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import struct

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from docspec.adapters.storage import IcebergRecordStorage, table_occurrences, table_sql
from docspec.adapters.storage.batches import table_arrow_schema
from docspec.adapters.storage.table_occurrences import (INDEX_KIND, OCCURRENCE_INDEX, append_occurrences,
    lookup_occurrences, mint_identities, read_occurrences, reference_identity)
from docspec.adapters.storage.table_sql import OCCURRENCE_PREFIX, occurrence_urn_sql
from docspec.domain.storage import TableSchema
from docspec.domain.table_rows import KeySpelling, TableIdentity, table_row_bytes
from docspec.errors import IntegrityError

COLUMNS = (("document_number", "VARCHAR"), ("publication_date", "VARCHAR"), ("title", "VARCHAR"),
           ("pages", "BIGINT"), ("ratio", "DOUBLE"), ("signed", "DATE"), ("seen", "TIMESTAMP"),
           ("zoned", "TIMESTAMPTZ"), ("flag", "BOOLEAN"), ("count", "INTEGER"), ("topics", "VARCHAR[]"))
FEDERAL_REGISTER = KeySpelling("federal-register-source-record-id", "1", ("document_number", "publication_date"))
IDENTITY = TableIdentity("federal-register", "federal_register", FEDERAL_REGISTER, COLUMNS)
VALUE_IDENTITY = replace(IDENTITY, key=KeySpelling("value", "1", ("document_number",)))
CORPUS = [
    {"document_number": "2026-\x1f\"1\\u001F", "publication_date": "2026-09-25",
     "title": "quote\" slash\\ \x00\n\x7f é😀", "pages": 2**53 + 1,
     "ratio": struct.unpack(">d", bytes.fromhex("fff8000000000001"))[0], "signed": date(1, 1, 1),
     "seen": datetime(2026, 9, 25, 1, 2, 3, 4),
     "zoned": datetime(2026, 9, 25, 1, 2, 3, 123400, tzinfo=timezone(timedelta(hours=5, minutes=30))),
     "flag": True, "count": -(2**31), "topics": ["\x00\x1f", None, "😀"]},
    {"document_number": " ", "publication_date": "\u2028", "title": "", "pages": -(2**63), "ratio": -0.0,
     "signed": date(9999, 12, 31), "seen": datetime(1, 1, 1), "zoned": None, "flag": False, "count": 2**31 - 1,
     "topics": []},
    {"document_number": "x@y", "publication_date": "z", "title": None, "pages": None, "ratio": float("inf"),
     "signed": None, "seen": None, "zoned": datetime(2026, 1, 1, tzinfo=timezone.utc), "flag": None,
     "count": None, "topics": None},
]


def table_layer(records, rows, *, columns=COLUMNS, writer="native"):
    """Retain ``rows`` natively, or as a producer's pyarrow Parquet file registered by reference."""
    data = pa.Table.from_pylist(rows, schema=table_arrow_schema(columns))
    schema = TableSchema("corpus:1", columns)
    if writer == "native":
        return records.write_table(data.to_batches(), layer_kind="test-table", schema=schema)
    staged = records.root / "staging" / (hashlib.sha256(repr(rows).encode()).hexdigest()[:16] + ".parquet")
    staged.parent.mkdir(exist_ok=True)
    pq.write_table(data, staged)
    digest = "sha256:" + hashlib.sha256(staged.read_bytes()).hexdigest()
    return records.register_parquet(staged, layer_kind="producer-table", schema=schema, member_digest=digest)


@contextmanager
def minted(records, layer, identity=IDENTITY):
    with records._cursor() as cursor, records.relations({"table": layer.reference}, cursor=cursor) as relations, \
            mint_identities(records, relations["table"], identity, cursor=cursor) as identities:
        yield cursor, identities


def expected_identities(identity, rows):
    return sorted((key, bytes.fromhex(digest[7:]), bytes.fromhex(urn[len(OCCURRENCE_PREFIX):]))
                  for key, digest, urn in (reference_identity(identity, row) for row in rows))


@pytest.mark.parametrize("writer", ["native", "registered"])
def test_native_identities_equal_the_python_reference(tmp_path, writer):
    with closing(IcebergRecordStorage(tmp_path)) as records:
        layer = table_layer(records, CORPUS, writer=writer)
        for identity in (IDENTITY, VALUE_IDENTITY):
            with minted(records, layer, identity) as (cursor, identities):
                # One streamed pass: nothing of the table is materialized in DuckDB.
                assert cursor.sql("SELECT count(*) FROM duckdb_tables() WHERE temporary").fetchone() == (0,)
                native = identities.relation(cursor).project("member_key, row_digest, occurrence_hash").fetchall()
                urns = identities.relation(cursor).project(occurrence_urn_sql("occurrence_hash")).fetchall()
                assert identities.row_count == len(CORPUS)
            assert sorted(native) == expected_identities(identity, CORPUS)
            assert sorted(urn for (urn,) in urns) == sorted(reference_identity(identity, row)[2] for row in CORPUS)
            assert not identities.path.exists()


@pytest.mark.parametrize("change", [{"publication_date": None}, {"document_number": ""}, {"document_number": None}])
def test_identity_pass_refuses_null_or_empty_key_components(tmp_path, change):
    with closing(IcebergRecordStorage(tmp_path)) as records:
        layer = table_layer(records, [CORPUS[0], {**CORPUS[1], **change}])
        with pytest.raises(IntegrityError, match="NULL or empty component"):
            with minted(records, layer):
                pytest.fail("a NULL or empty key component was minted")


def test_identity_pass_refuses_duplicate_keys_including_spelling_collisions(tmp_path):
    with closing(IcebergRecordStorage(tmp_path)) as records:
        # ("x@y", "z") and ("x", "y@z") both spell x@y@z.
        layer = table_layer(records, [CORPUS[2], {**CORPUS[2], "document_number": "x", "publication_date": "y@z"}])
        with pytest.raises(IntegrityError, match="duplicate member key"):
            with minted(records, layer):
                pytest.fail("a duplicate member key was minted")


def test_identity_pass_refuses_undeclared_spellings_and_foreign_projections(tmp_path):
    with closing(IcebergRecordStorage(tmp_path)) as records:
        layer = table_layer(records, CORPUS)
        for identity, match in [
            (replace(IDENTITY, columns=tuple(("pages", "INTEGER") if name == "pages" else (name, kind)
                                             for name, kind in COLUMNS)), "identity projection"),
            (replace(IDENTITY, columns=(*COLUMNS, ("absent", "VARCHAR"))), "identity projection"),
            (replace(IDENTITY, key=KeySpelling("value", "2", ("document_number",))), "not declared"),
            (replace(IDENTITY, key=KeySpelling("federal-register-source-record-id", "1", ("title", "document_number"))),
             "not declared"),
        ]:
            with pytest.raises(IntegrityError, match=match):
                with minted(records, layer, identity):
                    pytest.fail("an identity the rows cannot hold was minted")
    with pytest.raises(ValueError, match="VARCHAR columns"):
        replace(IDENTITY, key=KeySpelling("value", "1", ("pages",)))
    assert TableIdentity.from_dict(IDENTITY.to_dict()) == IDENTITY
    for broken in ({**IDENTITY.to_dict(), "row": "docspec-table-row/2"}, {**IDENTITY.to_dict(), "extra": 1}):
        with pytest.raises(ValueError, match="closed shape"):
            TableIdentity.from_dict(broken)


def test_the_spelling_oracle_refuses_to_mint_after_native_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(table_occurrences, "_ORACLE_PASSED", set())
    # A DuckDB release spelling a newline as \u000a would change every digest.
    monkeypatch.setitem(table_sql._SHORT_ESCAPES, 10, "\\u000a")
    with closing(IcebergRecordStorage(tmp_path)) as records:
        layer = table_layer(records, CORPUS[1:])
        with pytest.raises(IntegrityError, match="unlike its Python reference"):
            with minted(records, layer):
                pytest.fail("a drifted spelling was minted")


def admit(records, rows, index, state_id):
    """Mint a generation and append what the index lacks, as C27's first and later admissions do."""
    layer = table_layer(records, rows)
    with minted(records, layer) as (_, identities):
        index, generated = append_occurrences(records, index, identities, first_state_id=state_id)
    return layer, index, generated


def test_the_index_generates_each_occurrence_once_and_adopts_it_after(tmp_path):
    changed = [{**CORPUS[0], "title": "amended"}, *CORPUS[1:],
               {**CORPUS[1], "document_number": "2026-00002"}]
    urns = [reference_identity(IDENTITY, row)[2] for row in (*CORPUS, changed[0], changed[3])]
    with closing(IcebergRecordStorage(tmp_path)) as records:
        first, index_a, generated_a = admit(records, CORPUS, None, "state-a")
        _, index_b, generated_b = admit(records, changed, index_a, "state-b")
        _, index_again, generated_again = admit(records, CORPUS, index_b, "state-a-again")
        assert (generated_a, generated_b, generated_again) == (3, 2, 0) and index_again is index_b
        assert (index_a.reference.record_count, index_b.reference.record_count) == (3, 5)
        found = lookup_occurrences(records, index_again.reference, IDENTITY, urns)
        assert {urn: entry.first_state_id for urn, entry in found.items()} == dict(zip(
            urns, ["state-a", "state-a", "state-a", "state-b", "state-b"], strict=True))
        # A->B->A: the reappearing row resolves to its first occurrence and reads back exactly.
        rows = read_occurrences(records, first.reference, IDENTITY, {urn: found[urn] for urn in urns[:3]})
        assert rows == {urn: table_row_bytes(row, COLUMNS) for urn, row in zip(urns, CORPUS)}
        assert set(records.data_files(index_a.reference)) < set(records.data_files(index_b.reference))
        for locator in records.data_files(index_b.reference):
            hashes = pq.read_table(tmp_path / locator).column("occurrence_hash").to_pylist()
            assert hashes == sorted(hashes)
        records.verify(index_b.reference)


def test_lookups_prune_the_index_and_refuse_rows_whose_digest_changed(tmp_path):
    columns = (("document_number", "VARCHAR"), ("publication_date", "VARCHAR"), ("title", "VARCHAR"))
    identity = TableIdentity("federal-register", "federal_register", FEDERAL_REGISTER, columns)
    rows = [{"document_number": f"2026-{index:06d}", "publication_date": "2026-01-02",
             "title": hashlib.sha256(str(index).encode()).hexdigest()} for index in range(60_000)]
    with closing(IcebergRecordStorage(tmp_path)) as records:
        layer = table_layer(records, rows, columns=columns)
        with minted(records, layer, identity) as (cursor, identities):
            index, _ = append_occurrences(records, None, identities, first_state_id="state-1")
            # The 60 lowest hashes and the highest: more values than DuckDB turns a
            # join into an IN filter for (50), spanning the whole hash range, so
            # pruning by the range alone would read every row group.
            ordered = identities.relation(cursor).order("occurrence_hash").project("member_key").fetchall()
        spread = {key for (key,) in (*ordered[:60], ordered[-1])}
        targets = {reference_identity(identity, row)[2]: row for row in rows
                   if row["document_number"] + "@2026-01-02" in spread}
        assert len(targets) == 61

        # Break the first page header of every row group that holds no target.
        [locator] = records.data_files(index.reference)
        metadata, path = pq.ParquetFile(tmp_path / locator).metadata, tmp_path / locator
        payload, damaged = bytearray(path.read_bytes()), 0
        wanted = {bytes.fromhex(urn[len(OCCURRENCE_PREFIX):]) for urn in targets}
        for group in range(metadata.num_row_groups):
            chunk = metadata.row_group(group).column(0)
            if not any(chunk.statistics.min <= value <= chunk.statistics.max for value in wanted):
                start = chunk.dictionary_page_offset or chunk.data_page_offset
                payload[start:start + 16] = b"\xff" * 16
                damaged += 1
        assert damaged >= 2
        path.write_bytes(payload)
        with pytest.raises(IntegrityError), records.relations({"index": index.reference}) as relations:
            relations["index"].aggregate("bit_xor(hash(occurrence_hash))").fetchone()

        others = ["urn:docspec:entity:v1:" + "0" * 64, OCCURRENCE_PREFIX + "zz", OCCURRENCE_PREFIX + "0" * 64, 7]
        found = lookup_occurrences(records, index.reference, identity, [*targets, *others])
        assert set(found) == set(targets)
        assert all(entry.member_key == row["document_number"] + "@2026-01-02" and entry.first_state_id == "state-1"
                   for entry, row in ((found[urn], row) for urn, row in targets.items()))
        assert read_occurrences(records, layer.reference, identity, found) == {
            urn: table_row_bytes(row, columns) for urn, row in targets.items()}

        # A table whose row at that key changed, or that lacks the key, does not hold the occurrence.
        changed = [{**row, "title": "changed"} for row in targets.values()]
        unrelated = [{**rows[0], "document_number": "2025-000001"}]
        for other in (changed, unrelated):
            with pytest.raises(IntegrityError, match="differs from its minted occurrence"):
                read_occurrences(records, table_layer(records, other, columns=columns).reference, identity, found)


def test_a_lookup_refuses_an_index_row_that_does_not_hash_to_its_occurrence(tmp_path):
    _, digest, urn = reference_identity(IDENTITY, CORPUS[0])
    forged = pa.table({"occurrence_hash": [bytes.fromhex(urn[len(OCCURRENCE_PREFIX):])], "member_key": ["another"],
                       "row_digest": [bytes.fromhex(digest[7:])], "first_state_id": ["state-a"]},
                      schema=table_arrow_schema(OCCURRENCE_INDEX.columns))
    with closing(IcebergRecordStorage(tmp_path)) as records:
        index = records.write_table(forged.to_batches(), layer_kind=INDEX_KIND, schema=OCCURRENCE_INDEX)
        with pytest.raises(IntegrityError, match="differs from its occurrence hash"):
            lookup_occurrences(records, index.reference, IDENTITY, [urn])
        other = records.write_table(forged.to_batches(), layer_kind="another-kind", schema=OCCURRENCE_INDEX)
        with pytest.raises(IntegrityError, match="not a minted-occurrence index"):
            lookup_occurrences(records, other.reference, IDENTITY, [urn])

