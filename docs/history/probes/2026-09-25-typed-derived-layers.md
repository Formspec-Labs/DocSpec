# C29 gate: typed derived layers

**Passed, for DocSpec's part.** `CoreWorkspace.derive_table` wrote PM01's
10,000 real Federal Register prepared values as a typed layer holding the
columns Engine indexes, and a fresh process read back **0 differing (member
key, field) values of 490,000** against the same values read by Python's
`json`. Every row's occurrence equals DocSpec's Python reference, and Engine's
`id` equals Engine's own `stable_id`. Over PM01's second source revision, the
incremental derive **wrote exactly the 50 rewritten members**, skipped the 5
unchanged controls it was also given, removed the 1 removed member, and its
`changes` (51) equal the source's (51) and the members whose prepared value
differs (51). Each derive adds a constant 7–9 ledger records, whatever its
size. At full scale, all **1,007,639 rows derived in 49.9 s at 3.6 GiB**
(0.0495 ms per record), beside PM01's 0.81 ms: the falsifier does not fire.
Like for like, C26's JSON derive of the same values, cut to the same fields,
took 0.173 ms per record at 6.4 GiB, so the typed path is 3.5 times cheaper at
scale. At 10,000 records, where fixed costs dominate, the margin is 1.15 times.

The [harness](2026-09-25-typed-derived-layers.py) and
[receipt](2026-09-25-typed-derived-layers.json) record every number below;
per-step receipts, logs and RSS samples are in
`~/Work/corpora/c29-gate-20260925/receipts/`. Code: DocSpec `7843812`
(lane/typed-derive on main `cdae377`), DuckDB 1.5.5, PyArrow 25.0.1, Python
3.12.9, one DuckDB thread and a 6 GiB engine limit, on a 14-CPU arm64 Mac at
load 4–9 shared with other lanes. Each step ran once, in its own process. The
full-scale steps ran one at a time under the PM01 watch wrapper with a 12 GiB
cap, each started once free plus reclaimable memory (`vm_stat` free, inactive,
speculative and purgeable pages) reached 20 GB. A first run on `42e9a81`
(`receipts/first-run-42e9a81/`) gave the same values and counts. It sorted the
table by key, which this run's code no longer does (below).

## Fixture

| Input | What | Rows |
| --- | --- | ---: |
| PM01 `compare/fr-10k.prepared-{1,2}.jsonl` | Search `b150fdd`'s prepared values for the md5-ordered FR sample, before and after the second source revision | 10,000 / 9,999 |
| PM01 `workspaces/fr-10k` (copied) | the source state and its revision: 50 titles rewritten, 1 member removed, 5 untouched controls named | 10,000 / 9,999 |
| PM01 `compare/rg-10k.prepared-1.jsonl` and one synthetic member | Regulations.gov values, and a member with all 18 filters and all 21 dates populated, zoned times, control characters and a `#` in its key | 10,001 |
| cutover FR prepared state `13e28a19…` (pin `sha256:445789ab…`) and `catalogue` (pin `sha256:b456349d…`) | read only, through a read-only ledger and DocSpec's key-ordered reader, pins verified | 1,007,639 |

**Typing.** DuckDB's JSON reader parses each value once (`from_json`) into
Engine's columns: `id` (Engine's `stable_id`), `source_id`, `member_key`,
`source_occurrence_id`, the four texts, the display `metadata` JSON,
`identifiers` and the 18 filters as sorted distinct `VARCHAR[]`,
`filter_agency_scope`, and the 21 dates as `DATE` or UTC `TIMESTAMPTZ`: 50
columns. Engine's `retained_ref`, `content_sha256` and `publication_date` are
not stored (spike, section 1). Each member's `source_occurrence_id` comes from
DocSpec's reader of the source state. The typing stands in for Search's future
preparer. It shares only Engine's `row()` rules with the reference, which is
plain Python over `json`.

## Checks

| Threshold | Result |
| --- | --- |
| Engine's `id` | Engine's own `stable_id`, read from spicyengine `71a325b` and run without importing spicyengine, equals the native `sha256` of the canonical `[source_id, member_key]` for all 10,000 keys and 42 adversarial strings (every control character, quotes, backslash, U+2028) |
| Zero differing (member key, field) values, fresh process | 0 of 490,000 (FR, first revision); 0 of 489,951 after the incremental derive; 0 of 490,049 for coverage, where every filter and date field is populated at least once |
| Occurrences recomputed independently | DocSpec's Python reference, run on each row read back, gives every stored `occurrence_id`: 10,000, 9,999 and 10,001 of them |
| Incremental derive writes only changed members; `changes` at most the source's | 55 rows supplied (50 rewrites, 5 unchanged controls) and 1 removal: 50 written, 5 unchanged, 1 removed. The table's one new data file holds exactly the 50 rewrites. Derived `changes` 51 = source `changes` 51 = members whose prepared value differs, by `json` (51) |
| Ledger grows by a constant number of records per derive | +8 records, +3 units (the derive's unit, its identity mark and the dataset pointer) for the first 10k derive; +7 for the incremental one, whose definition already existed; +9 at 1M, where the bound lookup entity was pinned on first reference; +7 for the 1M incremental derive. C26's JSON derive of the same values adds 8–9 records in 8 units |
| Milliseconds per record beside PM01's 0.81 | 0.151 at 10k, 0.0495 at 1M (below) |
| 1M typed layer equals its typed input, fresh process | two-way `EXCEPT ALL` over all 50 columns: 0 and 0; 1,007,639 rows and 1,007,639 distinct occurrences |

## Cost

| Derive | Rows | Seconds | ms/record | Peak RSS | Written |
| --- | ---: | ---: | ---: | ---: | ---: |
| typed, FR 10k | 10,000 | 1.51 | 0.151 | 0.97 GiB | 7.27 MB |
| typed, incremental (55 rows, 1 removal) | 55 | 0.94 | — | 1.1 GiB | 169 KB (72 KB Parquet) |
| C26 JSON, the same 10k values whole | 10,000 | 2.47 | 0.247 | 0.95 GiB | 7.88 MB |
| C26 JSON, cut to the typed fields | 10,000 | 1.73 | 0.173 | 1.0 GiB | 5.79 MB |
| PM01 JSON derive (Search end to end, with `prepare()`) | 10,000 | — | 0.81 | — | — |
| **typed, FR full** | **1,007,639** | **49.9** | **0.0495** | **3.6 GiB** | **634 MB** |
| typed, incremental over 1M (50 rewrites, 1 removal) | 50 | 1.64 | — | 0.8 GiB | 148 KB |
| C26 JSON, full, cut to the typed fields | 1,007,639 | 174.1 | 0.173 | 6.4 GiB | 565 MB |
| cutover JSON derive (Search 0.4.2, whole values, with `prepare()`) | 1,007,639 | 1,164 | 1.155 | 12.5 GB | — |

The C26 baselines time `derive` alone. At 1M, 10.3 s of building the caller's
Python values is taken out of its 184.4 s. The typed derive spends its time
natively: staging the batches once to scratch, one spelling and hashing pass,
the table write, the membership and the index. At 10k, loading the in-process
catalog (0.24 s) and four Iceberg commits dominate both paths.

At full scale the table holds 515 MB (511 B per row, four files), the
membership 46 MB and the occurrence index 73 MB (72.6 B per row, as C27
measured). The JSON path's entity layer holds 519 MB and its membership 46 MB.
The typed table carries Engine's `id`, `source_id` and the source occurrence
per row besides the same fields; the index is the typed layer's only extra
cost. Typing all 1,007,639 values natively took 16.7 s, plus 8.2 s to add each
row's catalogue occurrence by a positional join checked key by key, and 472 MB
of Parquet.

## What a clean result could hide

- **Both sides share `prepare()`.** Equality shows the typed round trip, not
  that preparation is correct.
- **A shared typing step.** The typing is DuckDB's JSON functions, the
  reference is plain Python over `json`; they share only `row()`'s rules.
- **Empty fields.** FR fills 13 filters and 5 dates. The coverage set fills all
  18 filters and all 21 dates: Regulations.gov fills every filter and 8 of the
  10 zoned timestamps, and the synthetic member fills the other 12 dates.
- **Digests checked against themselves.** DocSpec's Python reference recomputes
  every occurrence from the row read back; Engine's `stable_id` is Engine's own
  code.
- **Row counts alone.** Bytes written are recorded: the 10k incremental derive
  adds one 50-row data file and one delete file, sharing every base file. That
  is DocSpec's merge-on-read table. Engine's copy-on-write republish, which the
  C29 gate also asks for, is Engine's to measure when it reads this layer.
- **The 0.81 baseline includes Search's `prepare()` and predates C28.**
  C26's JSON derive of the same values is measured beside it, at 10k and at 1M.
- **`metadata` bytes.** Engine keeps display JSON as one string. The native
  typing writes it as Engine's `encoded()` would for 29,999 of the 30,000 rows
  compared. The
  synthetic member's control character comes out as `\u001F` where Python
  writes `\u001f`; the values are equal. This is the harness's typing, not DocSpec's.

## Findings

1. **A point read costs about 0.2 s of native spelling compilation per query
   on a 50-column layer.** `read_value` took 540 ms per key on the 1M layer;
   a bounded group of 256 keys took 2.6 s. On the first run's table, the
   filtered table and membership scans took 8 and 2 ms and the row's spelled
   record 200 ms of the rest: DuckDB binding its `docspec-table-row/1`
   expression. This is C27's reader, shared by admitted generations, and does
   not depend on row count or order. Bulk reads amortize it.
2. **Sorting the derived table by key cost 4.7 GB for no read benefit.** The
   first run sorted it: 53.6 s at 8.4 GiB, against 49.9 s at 3.7 GiB unsorted,
   and point reads took 569 against 578 ms. Derived tables now keep the
   caller's order (`c62bb6f`); the index keeps its hash sort, on which its lookups
   do prune.
3. **A derived row records its source occurrence, so a changed source member
   always rewrites its row,** even when its other fields are unchanged; the
   row's lineage changed. Here the two coincide, since every PM01 rewrite
   changes the title. A source change that leaves every prepared field unchanged
   would still count as a derived change, and Engine would rewrite that row's
   source reference.
4. **`changes` between two 1M derived states took 3.9 s:** one native pass over
   both memberships, since table-shaped states record no revision certificates.

## Rerun

From `~/Work/corpora/c29-gate-20260925`, `./run-gate.sh` runs every step in
order, each in its own process (`uv run --frozen --project $WT python $G
COMMAND`), with the full-scale steps under
`~/Work/corpora/pm01-gate-2026-09-23/tools/watch.sh LOG 12 -- …`. The first
run's sorted-table comparison used `scratch/profile/sortless.py`
(`logs/first-run-42e9a81/scale-unsorted.log`, `scale-lookups.log`).
