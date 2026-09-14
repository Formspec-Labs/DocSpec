# Core performance assessment — 2026-09-14

The implementation works on substantial datasets, but its measured bulk throughput
is modest and its incremental updates miss our original concurrency targets.
That supports caution about advertising speed. It does not establish that DuckDB
is slower than Polars, or that Python is the dominant cost. No equivalent engine
comparison has been run.

This assessment reconciles the existing measurements and two independent source
audits. It adds no benchmark runs. The [current receipt](probes/2026-09-14-core-bounded-writer-capacity.json)
pins wheel `bc73eb47c51b4fd359b43af9a9412b39058998307e1948aa1b31aaeae64200bc`,
inputs, dependencies, machine and raw evidence. The earlier
[complexity audit](2026-09-14-core-batching-complexity-audit.md) describes the
pre-pruning implementation; its old file-discovery timings are historical.

## What the measurements establish

The main population contains 1,048,576 members with 8 GiB of logical body bytes.
Runs used an Apple M4 Pro with 48 GiB RAM, an internal APFS SSD, one native engine
thread per process, a 6 GiB engine allowance, and SQLite WAL with
`synchronous=FULL`. Instrumentation was enabled. Processes were fresh; the OS
cache was not cleared. Concurrent readers used their own processes and engines.

| Operation | Measured result | Practical meaning |
| --- | --- | --- |
| Import and durably publish | 281.73 s; 29.08 logical-body MiB/s | Modest application throughput; this is not measured disk bandwidth. |
| Select, encode, hash and retain whole values | 203.87 s evaluation; 40.18 logical-body MiB/s | Substantial processing cost beyond reading bytes. Independent checking took another 137.86 s. |
| Extract fields across all members | 66.98 s evaluation | About 15,656 members/s; independent checking is excluded from this time. |
| Select 1,025 named members | 6.12 s evaluation after 0.86 s admission | Selective payload reads work, but the operation still has material overhead. |
| Revise 1,024 members, with four readers | Mean writer batch 10.32 s; 100 batches plus reopen reconciliation 1,155.26 s | Correct results, but the full run exceeds the original 600 s target. |

The writer performed 102,400 edit events across the **same 1,024 member addresses**,
not 102,400 distinct documents. Early batch median was 8.17 s and the final ten
median was 12.29 s. That is observed slowdown, not proof of quadratic runtime or
one particular cause. Writer peak memory was 2.73 GiB, above its 2 GiB target.
The 9.34 GiB sum of individual process peaks also exceeded its target, but is a
conservative accounting measure, not observed simultaneous memory. Reader
metadata p95 stayed below 6 ms; named-read p95 was about 1.6 s.

There is positive capacity evidence too. The two-copy population contains
2,097,152 members and 16 GiB of logical bodies. Build took 669.24 s at 11.88 GiB
peak memory. Whole-value selection took 435.12 s at 8.24 GiB, retaining
17,523,951,228 canonical bytes. A separate process checked every value and key,
matched the exact digest, and peaked at 9.60 GiB. Selection and verification
therefore handled more logical bytes than their 12 GiB process allowance.
This is one successful workload, not a universal memory bound. Storage peaks
were sampled, and process peaks may include setup and independent checking.

## Where the work grows

**Small changes still incur population-sized membership work.** The
[revision resolver](../../src/docspec/adapters/storage/core_states.py) replaces
touched membership buckets. With 64 buckets, one key typically affects about
N/64 membership rows; 1,024 distributed keys can touch all buckets. The shared
membership digest still hashes the complete ordered membership stream for every
revision. Hashing alone requires O(N) compact-row work, with additional ordering cost.
It does **not** mean rereading every document body. Batching reduces the amount
held at once; it does not turn this operation into O(number of changes).

**Fragmentation is real, but the worst lookup behavior was already fixed.**
The root has 23,269 occurrence files. Admitted per-file identity bounds now
[prune inputs before opening Parquet](../../src/docspec/adapters/storage/records.py).
The named-read receipt shows three profiled scans of 26 files each and a
conservative 30.4 MB payload-column bound, not a scan of every payload file.
File descriptors and broad scans still carry costs. Small files also enable
selective reads and immutable sharing, so making every file large is not an
automatic improvement. DuckDB's general analytical guidance recommends
100 MB–10 GB Parquet files; that is context, not a measured optimum for this
update workload. Its published row-group benchmark factors cannot be applied
to DocSpec. [DuckDB file-layout guidance](https://duckdb.org/docs/current/guides/performance/file_formats)

**Conversions matter; their share of current elapsed time is unproven.**
Native relations already do bulk joins, filtering and ordering. Python owns
semantic control and the shared canonical encoding path. Duplicate publication
encoding has been removed. Remaining decode/encode counts alone do not show
that these operations dominate latency. Durable publication and provenance
admission also impose real boundaries between stages.

**Bounded batches do not imply constant total memory.** Descriptors grow with
file count, provenance checks with the relevant graph, and native sorts/joins
can hold or spill population-sized data. Retaining cumulative descriptors can
also amplify storage across many revisions. The source trace identifies these
growth mechanisms; the finite measurements do not establish a quadratic
elapsed-time curve or a DuckDB-versus-Polars memory-complexity ranking.

## What a framework comparison can honestly say

Polars can optimize filters, column selection, shared scans and join order
within a lazy query. DuckDB also pushes filters and column selection into
Parquet scans. Neither engine can automatically remove DocSpec's full-stream
membership digest or optimize across every separately committed semantic step.
[Polars optimizations](https://docs.pola.rs/user-guide/lazy/optimizations/),
[DuckDB Parquet execution](https://duckdb.org/docs/current/guides/performance/file_formats)

Polars streaming is not a guarantee that every operation has bounded memory;
unsupported operations can fall back to its in-memory engine.
[Polars streaming documentation](https://docs.pola.rs/user-guide/concepts/streaming/)
Consequently, the relevant comparison is the actual execution plan and total
application work, not the language of the API or a framework-wide Big O label.

A fair speed comparison would preserve input layout, JSON/type semantics,
canonical bytes, provenance, durability, threads and cache conditions. Comparing
this implementation against a plain dataframe query would omit much of its
required work. Engine choice could change meaningful constant costs; we have
not measured the gain or established that a port is worthwhile.

The strongest architectural concern is work per revision. If faster updates
become necessary, reduce full-membership work and file/metadata amplification
before assuming an engine replacement will solve them. That is a priority for
a future performance requirement, not additional work required to close this
refactor. Under the user's [implementation acceptance amendment](../core-model-implementation-map.md#implementation-acceptance--2026-09-14),
testing stops here. Failed targets and unrun capacity claims remain unqualified.
