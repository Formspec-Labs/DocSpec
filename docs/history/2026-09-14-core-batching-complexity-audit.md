# Core batching and complexity audit — 2026-09-14

Batching is implemented, but bounded input batches do not make every operation proportional to the changed data. The principal remaining cost is repeatedly discovering a few entity rows across many Parquet files. The best next change is to retain truthful per-file identity bounds in the existing record descriptors and use them before native file discovery and union checks. Keep the current membership digest, patch verification, provenance owner, and incoming-state materialization.

This is a source trace with two independent empirical validators, not completion of C03/C25. Production code and acceptance limits were unchanged during this audit. The [capacity limits](../core-model-implementation-map.md#capacity-targets-fixed-before-tuning) still require the full 1,000-revision and 100-batch/four-reader runs, including their correctness, memory and durability checks.

## Scope and terms

All 128 current package files were checked against wheel SHA-256 `6fd4443ff8e6afac08b2f545da577c50cc63b4449564fbbedd381aecbba51bc5`; none differed. Its source manifest is `ec79a15cb36a4fc7cae235ec66af3ac537300d27cecad507a6fbf9dce31a31d0`. The preceding `974d4765d27cbf6e2d09d329cad1386d00a916085094788fdab94251d2792d79` package differs only in the SQLite URI correction. The installed environment uses Python 3.12.9, DuckDB 1.5.5, PyArrow 25.0.1, msgspec 0.21.1, jsonschema-rs 0.52.1 and rulespec-artifacts 1.0.12. Entity storage has eight hash buckets and a 384 KiB shard target; membership has 64 buckets. Native metadata caching is disabled.

| Symbol | Meaning |
| --- | --- |
| N | Members in the current state. The entity layer can contain more than N historical occurrences. |
| F | Physical files in a layer; F_j denotes the particular source layer used by lookup j. |
| E | Occurrences changed by one logical revision. |
| B | Value-edit activities needed for E changes: at least ceiling(E/256), subject to byte limits. |
| H | Revision history depth. |
| G | Provenance edges in the affected ancestor/descendant graph; graph checks also visit its vertices. |
| P | Canonical bytes of the edited values. This matters independently of row count. |

“One revision” is not one operation request. For 1,024 edits, the current helper produces four value-edit requests, publishes their results, then runs a membership-revision request. A subsequent reuse decision adds its own request. SQL/Arrow handoffs are limited to 2,048 rows and 8 MiB; the value-edit owner limits an activity to 256 changes and separately checks instruction, input, output and publication bytes. The native 6 GiB setting is not a promise about process resident memory; the operation-specific process limits still require measurement.

## Request-to-result trace

| Stage and existing owner | Work and scaling boundary |
| --- | --- |
| [Record admission and lookup](../../src/docspec/adapters/storage/records.py), [ledger read](../../src/docspec/adapters/storage/ledger.py) | A fresh layer-availability check reads its descriptor and checks F file addresses/sizes. Full import additionally checks physical bytes and logical rows. The shared publication guard reuses up to eight admitted layer handles; it does not cache every native Parquet footer. A bounded ledger read groups entity IDs by source layer, then locates matching files using compact identity columns before reading payloads. |
| [Value editing](../../src/docspec/application/core_edits.py), [operation lifecycle](../../src/docspec/application/core_execution.py) | Each of B activities durably starts, prefetches sources, applies patches, records individual usage/generation/derivation events, and publishes through the common owner. Patch/value processing is proportional to P, with a bounded number of copies and encodings. Queries and transactions are grouped, not issued once per occurrence. |
| [Publication](../../src/docspec/application/core_publication.py) | The result requires its execution, request, bound inputs and outputs. Existing whole-input retention therefore reads source entities again. The owner validates a bounded closure and passes the same `AdmittedRecord` bytes to the ledger; that duplicate encoding pass has already been removed. Progress records still use separate transactions, so a `ledger.commit` counter is not a count of every write transaction. |
| [`from_occurrences`](../../src/docspec/adapters/storage/core_states.py) | The new output entities initially have inline JSON payloads in the ledger. This step performs their first Parquet materialization, builds the small input state, and updates their physical locations through ordinary immutable admission. It costs O(E + P), plus native ordering/writes; it is not simply rewriting an existing Parquet layer. |
| [`prepare_revision`](../../src/docspec/application/core_edits.py) | The revision checks the retained producing results and independently reapplies every supplied patch to compare exact values. Per-result provenance sets are already built once, avoiding repeated linear event searches. Source/result prefetch uses bounded groups and subdivides on byte limits. An implementation label alone cannot justify skipping these checks on supplied revisions. |
| [`_resolve_edits`](../../src/docspec/adapters/storage/core_states.py) | Only touched membership buckets are replaced. If they contain N_t rows, native joins/order/write process N_t + E rows. One edit typically touches about N/64 members; 1,024 distributed keys touch essentially all 64 buckets. This is physical bucket replacement, not an O(E) delta representation. |
| [`_check_puts`, `union_disjoint`](../../src/docspec/adapters/storage/core_states.py) | Every put, including a later-removed put, must resolve in the incoming or base entity layer. The union checks ID overlap using compact columns and shares payload files. For changes spanning all eight entity buckets, its overlap proof still considers the full base entity population and file set. It then serializes a complete F-sized descriptor. |
| [Membership digest](../../src/docspec/domain/core_encoding.py) | Each representation obtains the same SHA-256 over the complete ordered canonical membership stream. Hashing requires all membership bytes; the current reader also globally orders the compact rows. This is O(N) byte work plus native ordering, worst-case O(N log N), even for one edit. It does not reread every document body. |
| [Provenance admission](../../src/docspec/adapters/storage/provenance.py) | SQL gathers the relevant ancestors/descendants once per admitted result batch, and graphlib checks that subgraph. Cost depends on its vertices and G, not the whole unrelated ledger. Per-occurrence events remain necessary; they do not imply per-occurrence transactions. |
| [Reuse](../../src/docspec/application/core_reuse.py), [dependency assessment](../../src/docspec/application/core_dependencies.py), [selected values](../../src/docspec/adapters/storage/core_selections.py) | Requests are assessed before policy, candidates stream from SQL, and publication rechecks exact correspondence/evidence versions. Retained comparison snapshots avoid needing lost old input bytes. An incremental selected-value plan compares actual compact memberships and computes changed values; it does not trust the declared edit list alone. Full comparison still orders/hashes the selected stream. Finding an earlier certified selection can walk up to the bounded history-search limit. |

For root-relative edits, three distinct stages read the old source values: transformation, result input retention, and revision verification. With B=4, validator A observed 12 source-file discovery passes. Their cost is closer to the sum of those lookups' file-discovery costs than to E alone. Importantly, F_j is the layer pinned for that particular occurrence: after the first contention batch, newly generated sources normally live in small incoming layers. It would be wrong to charge all 12 lookups against the full original F for every later revision.

## Ranked findings and decisions

### 1. Confirmed: native file discovery amplifies small edit batches

Validator A held E=1,024 and H=0, editing original root occurrences in three populations. Each observed revision made 25 relation entries, 16 record-layer lookups, one union and one put check. Twelve entries received the complete parent file list. Warm runs reused admission within one publisher session; they had zero fresh availability checks.

| N / parent F | Warm revision | Parent discovery inside that time | Union | Unobserved warm control |
| --- | ---: | ---: | ---: | ---: |
| 16,384 / 361 | 3.217 s | 0.578 s | 0.033 s | 3.260 s |
| 131,072 / 2,878 | 5.648 s | 2.831 s | 0.187 s | 5.784 s |
| 1,048,576 / 23,269 | 60.130 s | 52.800 s | 2.118 s | 60.209 s |

All 1,024 changed values and the complete membership domain passed their oracles. This confirms repeated discovery as the dominant owner for root-relative edits. It does not independently identify an asymptotic exponent: N and F change together, the operating-system cache is uncontrolled, and there is one observed warm trial plus its control per size. These are diagnostic timings, not the complete edit-through-reuse or contention acceptance measurement. See [validator A](probes/2026-09-14-core-file-scaling-validator-a.json).

**Recommended simplification:** the existing writer already visits every validated identity and assigns its physical shard. Track each shard's exact minimum and maximum identity there, retain them in the existing closed descriptor, and verify enclosure in the existing full logical-admission pass. Reuse one conservative bounds predicate before `_relation` file discovery and `union_disjoint` overlap checks. Keep every base file in the resulting union.

This adds no store, long-lived cache or alternate reader. It retains O(F) or O(F log E) cheap descriptor comparisons, but can avoid opening nearly all irrelevant files. Generated `urn:docspec:entity:` IDs are outside the original fixture IDs' prefix range, making the union case especially promising. Overlapping ranges remain a worst case; improvement is a falsifiable prediction, not an adopted performance result.

Use writer-owned bounds rather than optional native statistics: the pinned native writer omits min/max for sufficiently long strings. No normalization or case folding is permitted. Fresh Core imports already use `records.admit` and an unfiltered logical scan; retained reads use the existing prior-admission precondition. There is no demonstrated need for an additional trust subsystem. Admission regressions must reject false bounds, preserve duplicate/alias checks, and cover long/Unicode IDs before any pruning benefit is claimed.

### 2. Confirmed: later revisions retain full-state costs, but incoming materialization is not the priority

Validator B held N=1,048,576 and prior depth H=5, varying E. These sources were previously generated occurrences, so their physical lookup locations differ from validator A's original root sources.

| E | Full revision | Incoming-state materialization, inclusive | Membership resolution | Membership digest, full plus tiny input state | Union |
| --- | ---: | ---: | ---: | ---: | ---: |
| 64 | 13.493 s | 0.095 s | 3.063 s | 0.513 s | 8.293 s |
| 256 | 6.521 s | 0.201 s | 1.663 s | 0.507 s | 2.121 s |
| 1,024 | 11.081 s | 0.572 s | 1.829 s | 0.504 s | 3.034 s |

The first run has a large cold union outlier; these totals cannot support a simple fit against E. Owner times overlap and must not be summed. The measurements falsify prioritizing removal of `from_occurrences`: it owns necessary first materialization and is a minority cost. Keep it, along with the already-shared canonical encoding and publication owners. Bounds should first target the remaining global union check. The roughly half-second full membership digest is not the leading bottleneck and currently enforces exact membership equality across physical representations.

Each revision wrote about 7.78 MB of layer descriptors regardless of E. Parquet growth was 8.27 / 14.75 / 18.32 MB; journal/blob growth was 0.66 / 2.61 / 10.42 MB. Logical events and derivations grew with E. SQLite file length sometimes stayed unchanged because free pages were reused; this does not mean no metadata was written. All declared edits and named values, including the absent-key case, passed. See the [validator B receipt](/tmp/docspec-core-edit-scaling-20260914/validation.json).

Changing SEMI to INNER is not a useful substitute: the pinned native plan already uses `RIGHT_SEMI` with the small input on the build side. Six read-only paired queries returned the same empty overlap; SEMI took 1.59–1.69 s and INNER 1.63–1.65 s. See the [paired query receipt](/tmp/docspec-core-overlap-join-20260914/runtime.json).

### 3. Confirmed growth mechanism; not a measured history-latency failure

Every revision retains complete membership and a complete entity-file descriptor. If each step adds f files, descriptor volume over H retained revisions is O(HF + H²f); current membership rewriting also accumulates the bytes of touched buckets. These are retained-storage costs, not peak Python memory. Checkpoints can compact the current physical representation without erasing older retained states.

The provenance scope of a state-revision chain also grows with H. Validator B's separate fixed N=16,384, E=1 run completed H=256 in 36.013 s, with a complete physical/schema/value oracle afterward. State provenance scopes contained exactly 2H edges: cumulative inspected edge volume was 65,792, or H(H+1). Yet median revision latency did not rise: 0.145 / 0.138 / 0.133 s in the H16 / H64 / H256 windows. State-provenance time rose only from 6.51 to 9.19 ms. Descriptors accumulated 47.20 MB; median new descriptor size grew from 145,425 to 220,894 bytes between the first and last windows.

Therefore do not replace graphlib, add an ancestry cache, or claim quadratic elapsed time from the graph-volume formula. The existing full history qualification is the appropriate check after the file-discovery change. This small run does not establish million-member performance, and its first 256 changes use distinct keys, so value-occurrence ancestry stays shallow. See the [history-axis receipt](/tmp/docspec-core-history-axis-20260914/runtime.json).

## Peak space, contention and required follow-through

Python row buffers are bounded, but total space is not O(1): admitted descriptors require O(F) space per retained handle, the relevant provenance graph requires its vertices plus G, and native joins/orderings may hold or spill compact N-row relations. Full value processing additionally depends on payload bytes. A cache capped at eight handles bounds the number of descriptors, not their individual size. Final independent oracles can raise process peak above the timed revision phase and must be reported separately.

History holds one publication session across its revisions. The contention writer opens a fresh session per batch, so it pays fresh admission after the previous scope ends. Four reader processes have separate native connections and repeat named-field reads; bounded writer batches do not eliminate their metadata/file work. SQLite WAL permits readers alongside the single writer, but CPU, file metadata, storage I/O and durability work still compete. The 600-second contention limit includes all 100 real 1,024-member batches, current-state/stale-update checks, acknowledgements and reopen reconciliation. A standalone warm query or revision is not that acceptance result.

Implement the shared per-file bounds change, verify its admission and pruning semantics, then rerun the same two distinguishing cases: original-root edits and later generated-source edits. Confirm both exact values and actual files visited. Continue with the existing full history and four-reader contention recipes on the frozen package. Preserve the 64 MiB edited-parent payload bound, authoritative failures, exact provenance, full SHA comparison, `synchronous=FULL`, and all existing time/memory limits. No new benchmark framework or alternative lifecycle is needed.

## Evidence pins

Hashes below identify the evidence read for this audit; raw receipts also retain source/helper pins and timing caveats.

| Evidence | SHA-256 |
| --- | --- |
| [Validator A](probes/2026-09-14-core-file-scaling-validator-a.json) | `b552bdd1868fc744e36105268e22bc27010e27b604233770c716a2040c26abb2` |
| [Validator B](/tmp/docspec-core-edit-scaling-20260914/validation.json) | `000134c37eec4e3a25465d1f26e2d95cff59b15a4d9815a838e6a1b357ed632b` |
| [History-axis measurement](/tmp/docspec-core-history-axis-20260914/runtime.json) | `df7cd66e752a08fb39cc9eb9ee2721dbe348f2d694942f55c7b146740f272ef1` |

The current-package [installed acceptance](probes/2026-09-14-core-installed-uri-fixed.json), earlier [layout measurements](probes/2026-09-14-core-entity-bucket-layout.json), and [admission/contention measurements](probes/2026-09-14-core-history-admission-reuse.json) remain separate evidence. This audit neither supersedes their limitations nor marks the remaining task list complete.
