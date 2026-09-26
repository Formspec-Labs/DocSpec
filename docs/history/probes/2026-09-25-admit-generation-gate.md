# C27 gate: admit Federal Register generations by reference

**Passed.** `CoreWorkspace.admit_generation` admitted the fork-host prior
generation (`sha256:6c215859…`, 1,008,903 rows) in **14.4 s at 0.95 GiB**, then
the current one (`sha256:731984ca…`, 1,009,005 rows) over it in **3.2 s at
2.1 GiB**. `changes` between the two states equals pyarrow's direct
column-by-column comparison exactly: 102 added, 2 changed, 0 removed, and
1,008,901 occurrences carried forward. A synthetic third generation that
restores the two changed rows resolves them to their **first** occurrences,
recorded by the prior state (ruling R1(b)). The contract columns equal the
retained catalogue on all 1,007,639 of its keys, beyond 54 listed keys whose
producer and catalogue captures of the API differ. Neither falsifier fires.

The [harness](2026-09-25-admit-generation-gate.py) and
[receipt](2026-09-25-admit-generation-gate.json) record every number below;
per-step receipts, logs and RSS samples are in
`~/Work/corpora/c27-gate-20260925/receipts/`. Code: DocSpec `e61576b`
(lane/admission, with the table-storage lane's 0188ff1 merged), DuckDB 1.5.5,
PyArrow 25.0.1, Python 3.12.9, one DuckDB thread and a 6 GiB engine limit, on
a 14-CPU arm64 Mac at load 7–10. Each step ran once, in its own process, under
the PM01 watch wrapper with a 12 GiB cap; writes used the `tools/with_iceberg.py`
REST fixture.

## Fixture

| Input | What | Rows |
| --- | --- | ---: |
| `fork-fr-generation-2026-09-23/prior` | fork-host generation `sha256:6c215859…`, member `18afcd6e…` | 1,008,903 |
| `fork-fr-generation-2026-09-23/current` | fork-host generation `sha256:731984ca…`, member `47ad1212…` | 1,009,005 |
| `synthetic/fr-g3` | current, with its 2 changed rows restored to their prior values and one row keyed `2026-\x1fC27@2026-09-25` | 1,009,006 |
| `synthetic/typed-a`, `typed-b`, `typed-a2` | a typed FR-keyed table: DATE key component, a NaN with its sign bit set, -0.0, ±inf, BOOLEAN, INTEGER bounds, BIGINT beyond 2^53, TIMESTAMP, VARCHAR[], controls in keys and text; B changes row 7, A′ restores it and adds a row | 2,000 / 2,000 / 2,001 |
| retained `catalogue` (pin `sha256:b456349d…`) | decoded through DocSpec's JSON decoder, projected with spicy-docs' `project_federal_register_document` | 1,007,639 |

FR's generation is all VARCHAR, so typed values cannot ride in an FR
generation without changing every row digest; they ride in the companion
typed dataset instead, admitted A→B→A through the same code path.

## Admission

| Generation | Seconds | Peak RSS | Member | Membership written | Index written | Ledger records | Ledger bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| prior (first) | 14.35 | 0.95 GiB | 155,941,795 | 46,197,668 | 73,211,062 | +9 | 110,592 (new file) |
| current | 3.22 | 2.12 GiB | 155,941,204 | 28,743 | 17,856 | +8 | +32,768 |
| fr-g3 (synthetic) | 3.37 | 2.09 GiB | 324,471,238 | 27,975 | 12,118 | +8 | +28,672 |

Seconds are the `admit_generation` call; each process, with interpreter
start-up and the fixture, took 16.0, 5.1 and 5.1 s. Member bytes include its
Iceberg metadata; fr-g3's member is larger because pyarrow wrote it. Bytes
downloaded: none (local sources); each admission staged its root, manifest and
member once. Every generation adds one admission unit, its identity mark and
one dataset-pointer unit: 3 units, 8 records (9 when its operation definition
is new) and 9 links, whatever its row count. The typed admissions follow the
same pattern (+9, +8, +8).

Beside the baselines: the spike's first admission took 12.1 s at 1.09 GiB; the
row-copy reimport of different content took 16 min 49 s at 8.80 GiB; the
like-for-like C26 derive is estimated at about 9 min. Admission is 37× below
the estimate on a first generation and 170× below it on a later one.

| Report counts | generated | adopted | added | changed | removed | carried |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| prior | 1,008,903 | 0 | 1,008,903 | 0 | 0 | 0 |
| current | 104 | 1,008,901 | 102 | 2 | 0 | 1,008,901 |
| fr-g3 | 1 | 1,009,005 | 1 | 2 | 0 | 1,009,003 |

## Checks, in a fresh process

| Check | Result |
| --- | --- |
| Admitted count equals `recordCount`; pins equal `pins.json` | 1,008,903 and 1,009,005; both pins match |
| `changes` (reader) against pyarrow's direct comparison of the two members | equal key sets: 102 added, 2 changed (`2026-19240@2026-09-21`, `2026-19258@2026-09-21`), 0 removed; `compare` counts equal |
| Carried forward: same occurrence at the same key in both states | 1,008,901 |
| fr-g3's restored rows | both equal the prior state's occurrences, differ from the current's, and the index names the prior state as their first; only the control-character row was generated |
| Typed A→B→A | row 7 in A′ is A's occurrence, first admitted by A; rules equal those built from the pyarrow schema |
| Python reference, every row | 0 mismatches in key and occurrence over 1,009,005 current rows; 0 in key, occurrence and full occurrence record over 6,001 typed rows |
| Catalogue keys | 1,007,639 compared, equal to the reference; 0 catalogue-only keys; 1,366 generation-only keys, listed |
| Two-way `EXCEPT` over the 22 shared contract columns (`topics_json` absent, `rin` extra) | 54 rows each way, all in `cfr_references_json`, listed by key; 0 in the other 21 columns |
| `modify_date` | NULL on every row of both sides |

The comparison read the current state through `CoreStateReader.table()`, took
72 s and peaked at 10.5 GiB, mostly pyarrow holding both members and DuckDB's
buffers; that is the check's cost, not admission's.

## Adjudications

- **JSON columns compare as JSON values.** DocSpec's canonical JSON storage
  sorts object keys, so the catalogue's decoded API records reproject
  `agencies_json` with sorted keys while the producer kept the API's order:
  1,001,753 rows differ as text and 0 as JSON. The four `*_json` columns are
  compared after both sides are re-encoded with sorted keys by `json.dumps`,
  an encoder independent of DocSpec's.
- **54 `cfr_references_json` exceptions are two captures of the API.** In 53,
  the producer's `part` is a string (`"17"`) where the catalogue's capture holds
  an integer (`17`); in one (`2017-27683@2017-12-26`) `chapter` is `"I"`
  against `0`. All are 2001–2019 documents. The admitted rows equal the
  producer's member byte for byte (sealed digest) and row for row (Python
  oracle), so these are producer-versus-catalogue source differences, not
  admission's.
- **Generation-only keys post-date the catalogue's supply, not its reimport.**
  All 1,366 are dated 2026-09-03 through 2026-09-22, after the catalogue's
  2026-09-02 supply; 777 fall on or before 2026-09-14, the reimport date that
  the task's threshold names.

## Findings

- **The index costs 72.6 B per occurrence, not 35–50 B.** It stores two
  32-byte hashes per row (occurrence and row digest), which do not compress;
  the estimate took one digest from the spike's key-plus-digest file. The
  first FR admission's index is 73.2 MB. Deriving the row digest at lookup,
  rather than storing it, would halve it; that is the index schema's owner's
  call.
- **A later admission peaks at about 2.1 GiB,** twice the first: the direct
  all-column join holds the base table's rows in its hash table. It spills
  under the engine limit, but its memory grows with the table, not the delta.
- **Scaling.** The first admission is one pass over the rows plus a sorted
  membership write and a sorted index write, O(n log n) in DuckDB's spillable
  sorts. A later one is two key passes and one join over both tables, O(n),
  plus O(changes) minting. Resolving an occurrence by identity is one index
  lookup, one membership lookup and one pruned table read per table-shaped
  state searched, newest first; with R3 there is one per dataset.

## What would make this clean result wrong, and the limits

- **Comparing in the admitting session:** every check ran in a fresh process
  that reopened the existing workspace and published nothing.
- **Digests checked only against themselves:** the Python oracle recomputed
  every key, digest and occurrence with pyarrow's values and Rulespec's
  encoder; the direct comparison used pyarrow compute, not DuckDB.
- **A narrowed key set:** both key counts are asserted before the `EXCEPT`.
- **One repetition** at load 7–10; timings are descriptive. The synthetic FR
  member was written by pyarrow, not the producer.
- **Not measured here:** HTTPS transfer (the admission tests cover the HTTPS
  path with a mocked transport), tables above 1 M rows, concurrent admissions.

## Reproduce

From `~/Work/corpora/c27-gate-20260925`, each under
`pm01-gate-2026-09-23/tools/watch.sh LOG 12 --`:

```sh
WT=~/Work/spicy-stack-worktrees/docspec-admission; G=$WT/docs/history/probes/2026-09-25-admit-generation-gate.py
uv run --frozen --project $WT python $G reference
uv run --frozen --project $WT python $G synthesize
uv run --frozen --project $WT python $WT/tools/with_iceberg.py $WT/.venv/bin/python $G admit prior ~/Work/corpora/fork-fr-generation-2026-09-23/prior federal-register
# ... current, then synthetic/fr-g3 into federal-register; synthetic/typed-a, typed-b, typed-a2 into typed
uv run --frozen --project $WT python $G compare
```
