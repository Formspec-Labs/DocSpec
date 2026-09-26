# Read keys, not payloads (cuts 1 and 6)

**Sorting only the compact member addresses, joining every payload once, and
restoring key order one spilled window at a time reads the full Federal Register
catalogue at the default 6 GiB allowance with the same rows, order, bytes and
batch boundaries.** On the committed tree the read took 16.3 s at 2.55 GB peak
RSS and spilled 1.01 GB of LZ4-compressed Arrow IPC. Main's global sort took
20.0 s at 15.41 GB, with 2.65 GB spilled, at a 14 GiB allowance; at the default
6 GiB it fails in 12.3 s ("record query exceeds its native memory allowance",
`/Users/mikewolfd/Work/spicy-stack/output/cutover-20260925/attempt2-fr/derive-fr.log`).
The engine default stays at one thread: with more threads, DuckDB pulls
Python-fed Arrow input on its worker threads, where lazy SQLite sources refuse.

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
CREATE OR REPLACE TEMP TABLE ordered_members AS
  SELECT member_key, occurrence_id, row_number() OVER (ORDER BY member_key) AS position FROM <addresses>;
-- Payloads within one window (a sixteenth of the allowance, from Parquet footers): one ordered query.
-- Larger: one pass, building the hash table on the compact side.
SELECT position, record_json AS occurrence_record
  FROM (SELECT occurrence_id, position FROM ordered_members) LEFT JOIN entities ON occurrence_id = record_identity;
```

`batches.spilled_order` writes each joined row once to an LZ4-compressed Arrow
IPC file for its window of positions, then reads, combines and sorts each window
alone, checking that every position arrives exactly once. Member keys rejoin
each window from `ordered_members`. Windows are whole multiples of 2,048 rows,
so batch boundaries match one global order. At 6 GiB the catalogue spills into
20 windows of 51,200 rows. The entity layer is scanned once, and total work is
linear in payload bytes.

The join carries only `(occurrence_id, position)` because of how DuckDB picks its
build side (EXPLAIN on the catalogue). With `member_key` in the join, DuckDB
builds on the payload scan, even when the join is written as a RIGHT join.
`disabled_optimizers` could force the choice, but it is connection-wide and
reaches every other cursor.

Neither option the brief named fits this data. Both layer partition policies
have one bucket, so `partition_bucket` cannot split the work. A payload join per
2,048-row batch would look up 2,048 random identities across 1,984 row groups,
touching about 64% of them per batch, or roughly 490 near-full scans.

The reader now checks its session once per batch, before fetching the next one,
instead of once per row. Inline values need no check; retained content still
checks before each read. After the session closes, rows of an already fetched
batch can still be delivered from memory, but no retained file is read. Staging
tables are created with `CREATE OR REPLACE` and close with their cursor, so no
SQL runs when a stream closes. A paused stream therefore closes cleanly after
its workspace closed.

## Results

| Run | Design | Allowance | Threads | Read (s) | First batch (s) | Wall (s) | Max RSS (GB) | Peak spill (GB) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| before | main `3133850`, global sort | 14 GiB | 1 | 19.99 | 11.83 | 21.80 | 15.41 | 2.65 |
| review round | per-window rescans, 5 windows | 6 GiB | 1 | 17.77 | 2.48 | 18.90 | 3.14 | 0 |
| review round | per-window rescans, 5 windows | 6 GiB | 4 | 8.33 | 0.71 | 9.58 | 3.43 | 0 |
| review round | per-window rescans, 5 windows | 6 GiB | 8 | 7.38 | 0.48 | 8.96 | 3.41 | 0 |
| trial | one pass, quarter windows, raw IPC | 6 GiB | 1 | 17.42 | 8.58 | 18.74 | 6.24 | 7.86 |
| trial | one pass, sixteenth windows, raw IPC | 6 GiB | 1 | 16.11 | 8.15 | 17.01 | 2.09 | 7.87 |
| trial | one pass, sixteenth windows, LZ4 IPC | 6 GiB | 1 | 16.64 | 10.04 | 17.57 | 2.58 | 1.01 |
| **committed** | one pass, sixteenth windows, LZ4 IPC | 6 GiB | 1 | **16.34** | 9.98 | 17.53 | **2.55** | **1.01** |
| committed | one pass, sixteenth windows, LZ4 IPC | 6 GiB | 4 | 16.04 | 9.61 | 17.10 | 2.56 | 1.01 |

Every read delivered 1,007,639 rows in 985 batches with fingerprint
`sha256:da9dc9c6…c50` over (member_key, occurrence_record) pairs and
batch-boundary digest `sha256:52be1069…672`. Wall and max RSS come from
`/usr/bin/time -l`. Spill is the engine's scratch directory, sampled every
0.2 s. The committed runs measured the committed `src` (source digest in the
receipt).

Each trial changed one thing. Quarter-allowance windows held two copies of a
1.6 GB window in memory. Sixteenth windows cost no extra scan and cut peak RSS
to 2.1 GB. LZ4 cut the spill 7.8× for about half a second. One pass also waits
about 10 s for its first batch, against 2.5 s for rescanned windows, but does
not rescan: the review round's design needed one entity scan per window,
O(P²/A) in payload bytes P and allowance A. The one-pass design is O(P).
`test_spilled_payload_windows_deliver_the_pinned_global_order` pins a digest
taken from main's global sort over three spilled windows.

## Threads (cut 6)

The rewrite streams the catalogue's exact entity bytes into a fresh store under
the Iceberg fixture: an incoming table, a duplicate check, then
`INSERT … ORDER BY record_identity`. This is the write path's wide sort, and this
change leaves it untouched.

| Workload at 6 GiB | 1 thread | 4 threads |
| --- | --- | --- |
| Entity-layer rewrite | 32.48 s, 7.13 GB, 8.50 GB spill | 12.21 s, 7.72 GB, 7.90 GB spill |
| Ordered read, per-window rescans | 17.77 s, 3.14 GB | 8.33 s, 3.43 GB |
| Ordered read, one pass (committed) | 16.34 s, 2.55 GB | 16.04 s, 2.56 GB |

The default stays at one thread. At four threads the full gate failed
`test_parquet_arrow_stream.py`'s caller-thread cases and
`test_bulk_revision_uses_two_state_inputs_beyond_the_metadata_row_limit`:
DuckDB pulled Python-fed Arrow streams on worker threads, and SQLite-backed
sources refused. The one-pass read gains nothing from threads, because its
Arrow routing, LZ4 and the harness's own hashing are single-threaded. The
rewrite gains 2.7×. The [2026-09-13 note](../../core-model-implementation-map.md)
that four threads failed a wide sort at 4 GiB does not recur at 6 GiB.

**Next step.** Pull every registered Python stream on its caller's thread in
bounded batches before raising the default. These are `IcebergRecordStorage._incoming`
(`records.py:627`), the selection updates and computed rows
(`core_selections.py:257` and `:335`), and `CoreStateStorage.changed_keys`
(`core_states.py:792`). Then four threads would speed DuckDB-bound work such as
this rewrite; the ordered read would also need its routing parallelized.

## Also fixed

In DuckDB 1.5, a view made with a relation's `create_view` is visible to every
cursor of one connection, while temporary tables stay per cursor. Nested ordered
reads therefore replaced and dropped each other's staging view; five
installed-wheel and example tests failed on that during the review round.
Staging goes through `IcebergRecordStorage.temp_table`, which names each view
uniquely and drops it after its single statement. The three older staging sites,
in state changes, selection addresses and selection deltas, share the helper and
lose the same latent race.

## Costs and limits

- A full read writes its payloads once to scratch, LZ4-compressed: 1.01 GB for
  this catalogue. The spill counts against `max_merge_scratch_bytes`, and a read
  whose spill exceeds it refuses with `LimitExceededError`.
- Member keys rejoin each window through a range filter on the compact
  `ordered_members` table; 20 such queries for this catalogue.
- Window sizes come from the footers of every stored row, including rows that
  delete files remove; they size work and never count records. A layer whose
  footers show no payload bytes reads as one window, ordered within the engine
  allowance.
- These are single observations on a busy shared host. The before run launched at
  19.9 GB reclaimable memory, just under the 20 GB rule; it had read 24.2 GB a
  minute earlier. Every later run waited for at least 20 GB.
- Payload sorts outside this cut remain: `AdmittedRecordLayer.batches()`, used by
  compaction, the export writer and selections; `segment_relation` in
  `application/document_processors.py`; and checkpoint's entity reorder.
  `bounded_batches` still finds byte cut points with a per-row Python loop,
  about a million iterations per catalogue read.
- `test_member_rows_from_0_9_1_read_pin_and_protect_until_removed` failed on main
  `3133850` in any fresh checkout: git cannot keep the fixture's empty
  `blobs/.staging` directory. `ae42970` recreates it in the test; this branch
  carries it.
