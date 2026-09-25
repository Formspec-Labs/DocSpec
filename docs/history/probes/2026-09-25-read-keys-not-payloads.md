# Read keys, not payloads (cuts 1 and 6)

**Ordering only the compact member addresses, then joining payloads one
memory-sized window at a time, reads the full Federal Register catalogue at the
default 6 GiB allowance with the same rows, order, bytes and batch boundaries.**
The read took 17.8 s at 3.14 GB peak RSS with nothing spilled. Main's global sort
took 20.0 s at 15.41 GB, with 2.65 GB spilled, at a 14 GiB allowance; at the
default 6 GiB it fails in 12.3 s ("record query exceeds its native memory
allowance", `/Users/mikewolfd/Work/spicy-stack/output/cutover-20260925/attempt2-fr/derive-fr.log`).
Four engine threads would
halve the read and cut a full rewrite by 2.7×, but the default stays at one
thread. With more threads DuckDB pulls Python-fed Arrow input on its worker
threads, where lazy SQLite sources refuse.

The [harness](2026-09-25-read-keys-not-payloads.py) and
[receipt](2026-09-25-read-keys-not-payloads.json) record every number below; raw
logs, `watch.sh` metadata and memory-gate readings are under
`/Users/mikewolfd/Work/spicy-stack/output/read-efficiency-20260925/`. Software:
DuckDB 1.5.5, PyArrow 25.0.1 and Python 3.12.9 through `uv run --frozen`, on the
shared 14-CPU, 48 GB arm64 host. Each run was a fresh process under `watch.sh`,
one at a time, while other lanes kept load averages between 12 and 31.

## Fixture

The retained Federal Register workspace
`/Users/mikewolfd/Work/corpora/docspec-iceberg-reimport-2173b92-20260914/federal-register/workspace`
opened with `create=False`; the harness never derives, admits or writes there.
State `catalogue` at pin `sha256:b456349d…a61` has 1,007,639 members. Its entity
layer holds 7,855,642,895 uncompressed `record_json` bytes (490.6 MB with ZSTD) in
1,984 row groups, averaging 7,796 bytes per occurrence. Occurrence IDs are
SHA-256 URNs, unrelated to member-key order.

## What changed

Before, `CoreStateReader.batches()` and `CoreStateStorage.rows()` ran
`relation.order("member_key")` over the membership⋈entities join. DuckDB planned
a LEFT hash join that built on the entity scan, so every payload sat in the hash
table, and the ORDER BY then carried every payload again.

Now `CoreStateStorage.ordered_batches` serves `rows`, `values`, `batches` and
`changes`:

```sql
CREATE TEMP TABLE ordered_members AS
  SELECT member_key, occurrence_id, row_number() OVER (ORDER BY member_key) AS position FROM <addresses>;
-- per window of whole BATCH_ROWS multiples, sized to a quarter of the engine allowance from Parquet footers:
CREATE TEMP TABLE member_window AS
  SELECT member_key, occurrence_id FROM ordered_members WHERE position > :lo AND position <= :hi;
SELECT member_key, occurrence_id, record_json AS occurrence_record
  FROM member_window LEFT JOIN entities ON occurrence_id = record_identity ORDER BY member_key;
```

The exact window cardinality keeps the entity scan on the probe side. A filtered
relation would win the build-side choice only through DuckDB's default 20%
estimate. Windows are whole multiples of 2,048 rows, so batch boundaries match
one global order. At 6 GiB the catalogue reads in five windows of 204,800 rows;
at 14 GiB, in three.

Neither option the brief named fits this data. Both layer partition policies
have one bucket, so `partition_bucket` cannot split the work. A payload join per
2,048-row batch would look up 2,048 random identities across 1,984 row groups,
touching about 64% of them per batch, or roughly 490 near-full scans.

The reader now checks its session once per batch, before fetching the next one,
instead of once per row. Inline values need no check; retained content still
checks before each read. After the session closes, rows of an already fetched
batch can still be delivered from memory, but no retained file is read.

## Results

| Run | Code | Allowance | Threads | Read (s) | First batch (s) | Wall (s) | Max RSS (GB) | Peak spill (GB) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| before | main `3133850` | 14 GiB | 1 | 19.99 | 11.83 | 21.80 | 15.41 | 2.65 |
| after | measured tree | 6 GiB | 1 | 17.77 | 2.48 | 18.90 | 3.14 | 0 |
| after | measured tree | 6 GiB | 4 | 8.33 | 0.71 | 9.58 | 3.43 | 0 |
| after | measured tree | 6 GiB | 8 | 7.38 | 0.48 | 8.96 | 3.41 | 0 |
| after, defaults | committed tree | 6 GiB | 1 | 16.33 | 2.42 | 17.80 | 3.33 | 0 |

Every read delivered 1,007,639 rows in 985 batches with fingerprint
`sha256:da9dc9c6…c50` over (member_key, occurrence_record) pairs and
batch-boundary digest `sha256:52be1069…672`. Wall and max RSS come from
`/usr/bin/time -l`. Spill is the engine temporary directory, sampled every
0.2 s. The measured and committed trees differ only in staging-view naming,
below, and comments.

## Threads (cut 6)

The rewrite streams the catalogue's exact entity bytes into a fresh store under
the Iceberg fixture: an incoming table, a duplicate check, then
`INSERT … ORDER BY record_identity`. This is the write path's wide sort, and this
change leaves it untouched.

| Workload at 6 GiB | 1 thread | 4 threads | 8 threads |
| --- | --- | --- | --- |
| Ordered read | 17.77 s, 3.14 GB | 8.33 s, 3.43 GB | 7.38 s, 3.41 GB |
| Entity-layer rewrite | 32.48 s, 7.13 GB, 8.50 GB spill | 12.21 s, 7.72 GB, 7.90 GB spill | not run |

Four threads are clearly faster within bounded memory. The
[2026-09-13 note](../../core-model-implementation-map.md) that four threads
failed a wide sort at 4 GiB does not recur at 6 GiB. The default still stays at
one: at four threads, the full gate failed `test_parquet_arrow_stream.py`'s
caller-thread cases and
`test_bulk_revision_uses_two_state_inputs_beyond_the_metadata_row_limit`. DuckDB
pulled Python-fed Arrow streams on worker threads, and SQLite-backed sources
refused. Raising the default first requires pulling that input on its caller's
thread, for example by giving native-only reads their own multi-threaded
connection. Eight threads add little over four: the harness's own serial SHA-256
over 7.85 GB bounds the consumer.

## Also fixed

In DuckDB 1.5, a view made with a relation's `create_view` is visible to every
cursor of one connection, while temporary tables stay per cursor. Nested ordered
reads therefore replaced and dropped each other's staging view; five
installed-wheel and example tests failed on that before the fix. Staging now
goes through `IcebergRecordStorage.temp_table`, which names each view uniquely
and drops it after its single `CREATE TEMP TABLE`. The three older staging sites,
in state changes, selection addresses and selection deltas, share the helper and
lose the same latent race.

## Costs and limits

- Each window scans the entity layer once, so total work grows with the number of
  windows, which is the payload bytes divided by a quarter of the allowance. For
  a fixed allowance that is superlinear in state size. A state with ten times
  this catalogue's payloads would take about 50 scans at 6 GiB; there, a one-pass
  compressed partition spill or a larger allowance is the better path.
- Window sizes come from the footers of every stored row, including rows that
  delete files remove. The estimate sizes the work and never counts records; a
  skewed window can still spill within the engine allowance.
- These are single observations on a busy shared host. The before run launched at
  19.9 GB reclaimable memory, just under the 20 GB rule; it had read 24.2 GB a
  minute earlier. Every later run waited for at least 20 GB.
- Payload sorts outside this cut remain: `AdmittedRecordLayer.batches()`, used by
  compaction, the export writer and selections; `segment_relation` in
  `application/document_processors.py`; and checkpoint's entity reorder.
  `bounded_batches` still finds byte cut points with a per-row Python loop,
  about a million iterations per catalogue read.
- `tests/test_core_older_workspace.py::test_member_rows_from_0_9_1_read_pin_and_protect_until_removed`
  fails on main `3133850` in a fresh checkout. The fixture cannot track its empty
  `blobs/.staging` directory, and a store opened with `create=False` does not
  create it.
