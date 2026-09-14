# Core architectural performance review — 2026-09-14

**DuckDB now writes retained Iceberg snapshots.** The earlier simplifications
below removed repeated work within the former Parquet backend. The subsequent
cutover removes touched-bucket rewrites and flat file inventories. SQLite remains
the logical publication and retention ledger; canonical encoding and comparison
semantics remain unchanged. See the [storage guide](../record-storage.md).

## Iceberg production writer

The [production-path probe](probes/2026-09-14-iceberg-core-writer.py) and
[receipt](probes/2026-09-14-iceberg-core-writer.json) used 100,000 members with
compressible 256-byte bodies, DuckDB 1.5.5, PyIceberg 0.12.0 and the local Apache
REST fixture. One native thread, warm filesystem, one run:

| Operation | Measured result |
| --- | --- |
| Create and publish the complete state | 9.38 s |
| Remove one member and publish its revision | 0.140 s |
| Compare original and changed states exactly | 0.196 s |
| Physical work for the removal | No new data files or data rows; one delete file, one delete row, 1,182 bytes |
| Retained membership root | 861 bytes before, 860 bytes after |

The original data file remained byte-identical. Reopened reads recovered both
complete states with their exact counts. Separate regression cases cover
updates plus insertion, independent branches, lost publication responses,
checksum tampering, compaction, shared-file cleanup and portable exports.

This demonstrates reduced write amplification. It is not an equivalent rerun of
the earlier million-member, 8 GiB workload or a framework throughput comparison.
Fresh availability checks and native joins can still examine complete snapshots.
A changed full-sequence SHA-256 comparison still consumes the complete byte
stream. Iceberg metadata/history and delete files need physical maintenance;
there is no independent purge or automatic orphan sweep.

The [required regression report](probes/2026-09-14-iceberg-regression.xml.gz)
records **1,293 passing tests**, zero failures/errors/skips, and 291.02 seconds;
one live-service test is deselected. Eighteen additional focused checks cover
the final storage cleanup and relocated-snapshot behavior. The
[verification receipt](probes/2026-09-14-iceberg-verification.json) pins the built
wheel and final source. All 129 packaged files match the source. Ruff, dependency
lock and whitespace checks pass; the spec is unchanged. These are local checks;
CI has not run. The temporary catalog containers were removed.

The subsequent [retained catalogue check](probes/2026-09-14-iceberg-catalog-reimport.md)
uses the existing Federal Register and Regulations.gov rows, including every
disposition. It records complete value comparisons, physical file sharing,
initial import costs and small-revision timings on the current Iceberg writer.
Its receipts preserve the input pins and execution limits.

The [import and comparison follow-up](probes/2026-09-14-core-import-compare.md)
then restricts value reads to comparison samples and replaces recursive snapshot
copies with native decoding. Its report owns the subsequent measurements.

All earlier measurements below retain their original implementation and scope.

## Native storage simplification verification

The [final strict regression report](probes/2026-09-14-core-native-simplification-regression.xml.gz)
records **1,293 passing tests**, zero failures/errors/skips, in 192.33 seconds.
One live-service test outside the required map is deselected. The run includes
installed-package checks. Ruff, lockfile validation, and patch whitespace checks
pass. The Core spec remains byte-for-byte unchanged.

The installed wheel SHA-256 is
`a50295357e83cbaee9c4648b2d27f1e63fb93df5a9ee622ef267cac4790eb53f`;
all 128 packaged files match the final source. The sorted source-file digest
manifest SHA-256 is
`64910bad10cf730a2cf1072b532a8e11370a2ff9f9fa59f8821b9b78b05aab5a`.
Its entries are `<file SHA-256>  <path relative to src>\n`, excluding
`__pycache__`, sorted by path.

Production checks cover 2,047, 2,048, and 2,049 changed keys, with the last case
exceeding 8 MiB of selected bytes. They establish exact unchanged-multiset reuse
without another complete hash, updated member origins, duplicate-count changes,
reopened evaluation, and rejection of false supplied evidence. Packing checks
exercise reversed input, nonoverlapping file ranges, small row groups, exact
lookups, and full layer verification. Existing checkpoint, interrupted-publication,
cleanup, and independent-export checks pass through the same owners.

The first full run exposed two obsolete test assumptions. Checkpoint corruption
now enters through the copy writer so the real compaction equality check runs;
wide selection tests allow every packed file to contain requested rows. Both
focused cases and the final full gate pass. These checks establish behavior,
not new million-member timings or larger-than-memory performance.

## First-pass verification

The [strict regression report](probes/2026-09-14-core-simplification-regression.xml.gz)
records **1,289 passing tests**, zero failures/errors/skips, and 272.31 s console
elapsed time. One live-service test outside the required map is deselected.
The run includes the repository's installed-wheel runtime, packaged-resource,
source-catalog and provider-example checks. Its wheel SHA-256 is
`75471255143c1bfab620c363d0e3c32c354a69ebda26d603b84594a4271b5e85`;
all 128 packaged files matched that first-pass source snapshot. The separate earlier
393-case installed-only receipt still applies to its original wheel.
`uv run --no-sync pytest --require-regression-map` produced this run; Ruff and
the patch whitespace check also pass. Core spec bytes are unchanged.

Focused checks establish one parent scan, ten native read batches coalesced into
two publication units for 2,305 entities, stable publication retries across read
chunk sizes, exact receipt limits, complete typed-column byte accounting and
compaction, missing/aliased member semantics, rejection of inconsistent external
routing, and protection of shared opaque blobs during cleanup. Publication and
cleanup now use the same selected-content reference reader.

The [small paired diagnostic](probes/2026-09-14-core-simplification.json), with its
[reproducible fixture](probes/2026-09-14-core-simplification-probe.py), compares
HEAD `9736e97` with the first-pass source snapshot using the same interpreter and dependencies.
It processes 8,192 values containing 38,188,884 canonical input bytes.

| Operation | Before | After |
| --- | --- | --- |
| Build and durably publish | 1.420 s | 1.243 s |
| Retain fields and compute evidence | 0.486 s | 0.378 s |
| Retain whole values and compute evidence | 1.021 s | 0.667 s |

Every selected value passes an independent expected-value check outside the
timer; both revisions produce identical evidence digests and byte counts. This
is one sequential pair with uncontrolled OS cache and machine load, one native
thread and a 6 GiB engine allowance. It measures the combined changes; it does
not establish million-member throughput, memory use, incremental latency or an
engine comparison. The earlier capacity receipts remain pinned historical results.

That first pass introduced record root version 4/profile version 3 and
selected-member manifest version 2. The subsequent implementation uses state
manifest version 2 and selected-member manifest version 3. Comparison framing and logical meaning remain unchanged.
There is one current reader and no compatibility path or added dependency.

## Structural fix proposals: reuse established solutions

**Decision: remove work introduced by DocSpec, reuse database and Parquet
capabilities, and reject a custom tree or sorted-run manager as the default.**
Three parallel investigations covered state updates/equality, file inventories/
packing, and incremental selections. They reviewed the working tree qualified
above. The table below records the investigation. The non-Iceberg changes were subsequently
implemented as described in the current [storage guide](../record-storage.md);
the table-format replacement remains outside this change.

The earlier recommendation to design shared indexed pages is superseded as the
default. Sorted runs also reproduce storage-engine responsibilities: scheduling,
deletions, indexes, compaction, snapshots and cleanup. An established algorithm
does not, by itself, justify implementing another copy of it.

### Findings and established solutions

| Issue | Classification | Recommended fix and precedent |
| --- | --- | --- |
| Fixed-bucket membership replacement | DocSpec introduced this write amplification. Immutable history does not require rewriting every touched bucket. | Delegate row updates and snapshot file management to an established table implementation. Iceberg supports separate row-deletion records and reusable snapshots; verify the selected writer actually uses that mechanism. [Iceberg specification](https://iceberg.apache.org/spec/#row-level-deletes) |
| Eager full membership hash and repeated change discovery | DocSpec introduced both. The membership digest checks representations of the same state; it is not the operation correspondence digest. | Remove eager membership hashing from ordinary revisions. Check exact equivalence when admitting another representation of an existing state. Have the resolver retain its actual normalized changes so selections do not rediscover them. [Current consumers](../../src/docspec/application/core_publication.py#L232) |
| Full file inventory copied into every state | DocSpec's flat root format introduced this. | Use the same table implementation's snapshot/manifest sharing; do not add a separate descriptor database. Iceberg explicitly permits unchanged manifests to be shared across snapshots. [Snapshots and manifests](https://iceberg.apache.org/spec/#snapshots) |
| Tiny files used to limit selective reads | A defensible original tradeoff, created by coupling file size to read granularity. | Sort before assigning file/group boundaries, then pack small row groups into larger files with the existing PyArrow writer. DuckDB can prune row groups within one file. Replace the shared final writer step rather than add another writer. [DuckDB guidance](https://duckdb.org/docs/current/data/parquet/tips), [PyArrow writer](https://arrow.apache.org/docs/python/generated/pyarrow.parquet.ParquetWriter.html) |
| 2,048-key/8 MiB selection fallback | DocSpec introduced a complete-working-set limit where only batch boundaries need it. | Keep changed addresses/results in connection-owned DuckDB temporary tables, with native joins, grouping and configured spill. Delete the Python overlay cutoff and partial-work restart. [Temporary tables](https://duckdb.org/docs/current/sql/statements/create_table#temporary-tables) |

The first and third findings are one storage decision. The selection and packing
fixes do not depend on introducing a new snapshot format.

### Concrete changes using current owners

**Selection:** replace `_Members.changes` and its repeated Arrow reconstruction
with native temporary relations owned for the entire evaluation. Let the state
reader consume a native changed-address relation; repeatedly invoking the public
small named-key path would introduce repeated planning/scans. Keep per-value,
Arrow-batch, native-memory and scratch limits. Normalize away net reverted changes.
Remove the unrelated 2,048-hop ancestry cutoff; use scoped lookup reuse rather
than another persistent ancestry cache.

**Comparison evidence:** persist checked evidence with a retained selected-member
manifest. Compute it once, then reuse it when exact affected-row checks establish
an unchanged comparison. For unordered values, compare complete canonical bytes
with signed counts, preserving duplicates. For ordered values, unchanged bytes
and ordering tokens at affected keys are a safe sufficient proof; otherwise use
the existing complete comparison. Update origins even when immaterial occurrence
identities change. These are narrow incremental-view-maintenance operations that
DuckDB joins/grouping can express; a new general view-maintenance service is
unnecessary. [Established signed-update approach](https://materialize.com/blog/self-correcting-materialized-views/)

**State admission:** initial external admission still establishes complete valid
membership. Local revision admission validates every edit in order, then produces
exact effective changes bound to the admitted base/result roots. Raw revision
metadata cannot manufacture this proof. Admitting another representation requires
exact membership equivalence; compaction can transfer its existing checked proof.
Already admitted reads need no repeated equivalence check. If all comparable
representations are gone, restoration requires previously admitted exact content
or surviving equivalence evidence. The current membership digest itself lives in
a removable manifest, so an alternate-layout restoration capability after full
removal is not established by the present code.

**Ownership:** `CoreStateStorage` owns membership/edit proof; `CoreSelectionStorage`
owns selected-value meaning and checked evidence; `RecordStorage` owns physical
tables/files and its replacement adapter; the publisher/ledger remain the only
successful-retention admission and metadata commit path. Maintenance and export
continue through the record store's complete physical-reference traversal. Arrow
does not supply transactions: keep the existing durable publication and cleanup
owners. [Arrow's stated boundary](https://arrow.apache.org/docs/python/dataset.html#a-note-on-transactions-acid-guarantees)

### Existing storage implementations: actual fit and gaps

| Candidate | Verified relevant capability | Concrete limit or unresolved fit |
| --- | --- | --- |
| Iceberg through DuckDB | The local check below verifies updates/deletes preserve unchanged data files, share manifests, and remain readable through both DuckDB and PyIceberg. | Writes require an attached REST catalog. Writes from independently retained bases, portable export and DocSpec retention/publication integration remain untested. [Writer documentation](https://duckdb.org/docs/current/core_extensions/iceberg/writing) |
| PyIceberg | Branch-targeted writes and historical snapshots; substantial overlap with the proposed custom snapshot manager. | Current `upsert` routes updates through overwrite, and delete falls back to copy-on-write. Adopting it alone does not establish removal of write amplification. [Published implementation](https://py.iceberg.apache.org/reference/pyiceberg/table/) |
| DuckLake | Native DuckDB snapshots/time travel and SQL-managed file metadata. | Its roadmap lists branching and protected snapshots as future work. Those touch DocSpec's arbitrary retained base revisions and retention directly. Do not equate time travel with independent writable branches. [Time travel](https://ducklake.select/docs/stable/duckdb/usage/time_travel), [roadmap](https://ducklake.select/roadmap) |

**Recommendation:** Iceberg is the leading replacement candidate for ordinary
snapshot/file management; the exact writer/catalog combination remains unqualified.
Do not approve either a replacement adapter or custom run manager from feature
lists alone. The decisive fit check is a tiny two-branch state: independently
update/delete from the same retained base, reopen both, compact one, protect the
other from cleanup, export/reopen independently, and interrupt between table and
ledger publication. Count rewritten bytes to ensure the chosen writer actually
avoids copying untouched membership. This is a concrete adapter decision, not a
new capacity matrix. The isolated checks below leave DocSpec's runtime and project
dependencies unchanged.

### Minimal local Iceberg check

A subsequent [4,096-row smoke test](probes/2026-09-14-iceberg-smoke.py), with its
[actual receipt](probes/2026-09-14-iceberg-smoke.json), used PyIceberg 0.12.0,
PyArrow 25.0.1, and an isolated local SQLite catalog. Two independently writable
branches, exact values, fresh-process reopen, catalog-free metadata reads, and
refusal to expire the protected base all passed. One unchanged data file and
one manifest were shared across each edit.

The single-row update wrote 2,048 data rows in two new files; the single-row
delete wrote 2,047 rows in one new file. Neither wrote a separate delete file.
Inspection of the installed `Transaction.delete` confirms that requesting
merge-on-read warns and falls back to copy-on-write; `Transaction.upsert` uses
overwrite. Thus this simplest local writer does not remove file rewrite
amplification. This is a result about PyIceberg's writer, not every Iceberg writer.
No DocSpec dependency or runtime code changed. Portable relocation and DocSpec
publication/crash integration remain untested; the next check tests DuckDB's writer.
Timings in the receipt include post-write inventory and are diagnostic only.

### DuckDB Iceberg writer check

The [second probe](probes/2026-09-14-iceberg-duckdb-smoke.py) reuses the same
4,096-row fixture and inventory helpers. DuckDB 1.5.5 with Iceberg extension
`45163a28` performs every data write through Apache's REST fixture 1.10.1, with
local files and a SQLite catalog. The [receipt](probes/2026-09-14-iceberg-duckdb-smoke.json)
pins the container digest and records the complete file inventories.

| Sequential operation | New data rows | New positional delete records | New data/delete bytes, excluding metadata | Reused data files |
| --- | ---: | ---: | ---: | ---: |
| Update one row | 1 | 1 | 584 + 981 | 2 |
| Delete another row | 0 | 1 | 0 + 981 | 3 |

Both original files remain byte-identical. The update shares both original
manifests, and the delete shares all four preceding manifests. DuckDB and
PyIceberg independently recover exact current and historical values, including
through fresh processes and direct metadata reads without a catalog lookup.
The original files remain in place; this does not test portable relocation.

**This writer avoids the unchanged-row rewrite observed with PyIceberg.** It
passes the immediate storage-mechanism check and remains a viable replacement
candidate. These are sequential writes to `main`; independently writable bases,
compaction, concurrency, scale and DocSpec publication/crash semantics remain
unqualified. Separate delete files also add read and maintenance work. The tiny
test does not establish end-to-end speed or asymptotic lookup cost. Operation
times exclude inventory and setup and are not comparable benchmarks. The test
removes its local server on exit; it retains the receipt and scratch data.

### Evidence, preserved requirements and remaining costs

A small agent diagnostic packed 8,192 sorted roughly 8 KiB values into one
Parquet file with 128 groups. Fresh DuckDB connections returned the correct exact,
adjacent and dispersed key selections. Engine-reported bytes read were 795 for
one key, 795 for three adjacent keys, 3,015 for three dispersed keys, and 80,513
for a full scan. This confirms row-group pruning; values were highly compressible
and these counters are not disk I/O or throughput. The raw profiles and physical
group counts are retained in the [diagnostic receipt](probes/2026-09-14-core-rowgroup-pruning.json.gz).

The selection agent also checked a 4,097-row unordered value rotation, ordered
differences, duplicate counts, absent/null and number/string distinctions. Those
initial checks supported the equivalence rules. Production regressions now cover
the native path above the former row and byte thresholds; they do not establish
spill performance or a new million-member throughput claim.

Let N be members, E affected keys, F physical files and Q selected bytes. Native
change evaluation removes the arbitrary switch from E-sized extraction to
N-sized extraction. Trustworthy change evidence can avoid the full membership
diff; unrelated/untrusted endpoints still require a real comparison. A proved
unchanged selected value can reuse its digest. **A genuinely changed `members-v1`
digest still requires the complete ordered comparison stream, O(Q) byte work.**
Avoiding that pass requires a new comparison representation, not combining
ordinary SHA-256 chunk hashes.

Full output, independent import/audit/export, and preserving still-retained
history have real data-size costs. Narrow reads may check visited paths but
cannot thereby certify whole-state availability. A table adapter must preserve
exact occurrence identities, ordered edit preconditions, multiplicity, relevant
ordering, canonical bytes, actual provenance, complete retained inputs and
policy-controlled deletion. These obligations come from [Core §§3, 5–8](../core-model.md),
which explicitly allows incremental correspondence and alternative storage.

**Kill criteria:** reject packing if generated/scattered identifiers defeat group
pruning; reject evidence shortcuts if they accept a false imported change claim
or collapse a required distinction; reject a table adapter if it needs a parallel
DocSpec snapshot/cleanup authority to preserve the model. Retaining historical
files is not a storage leak when a retained state still needs them.

**Verdict: RECONSIDER the custom physical design.** The causes are supported by
current code and established precedents. Use existing SQL/Arrow features for the
narrow fixes; establish the table-adapter gap before inventing storage machinery.
Confidence is high in the classifications and proposed removal of redundant
work, moderate in integration scope, and unmeasured for end-to-end speedup.

## Baseline artifact summary and evidence

Reviewed HEAD `9736e97` and runtime commit `f2c8066`; category: product processing
and retention infrastructure. The beneficiaries are bulk importers, consumers
selecting fields, and users editing large retained datasets. This review traces
the baseline code and existing receipts. The implementation follow-up above and
its verification below record the subsequent changes.

The [baseline receipt](probes/2026-09-14-core-bounded-writer-capacity.json) measures
8 GiB import/publication at 281.73 s, whole-value evaluation at 203.87 s, fields
across 1,048,576 members at 66.98 s, and 1,025 named members at 6.12 s. Those
selection times exclude independent checking. The contention writer averaged
10.32 s per batch; 100 batches plus reconciliation took 1,155.26 s. These are
102,400 edit events across the same 1,024 addresses. Correctness passed; original
time and memory targets did not. These observations establish costs, not their
complete attribution.

## Lineage and relationships

| Artifact | Constraint or decision |
| --- | --- |
| [Core §1, §3.2, §8](../core-model.md) | Recoverable identities, complete membership and retention; no prescribed physical record layout or hashing scheme. Compaction and shared representations are permitted. |
| [Implementation plan §3.1](../core-model-implementation-plan.md) | Chooses a complete checkpoint after every revision, touched-partition replacement and zero pending edits. This is a changeable implementation decision. |
| [Earlier complexity audit](2026-09-14-core-batching-complexity-audit.md) | Identified file discovery; its pre-pruning timings are historical, not current bottleneck attribution. |
| [Current assessment](2026-09-14-core-performance-assessment.md) | Correctly separates application throughput from disk bandwidth and framework comparisons. This review supplies owner-level explanations. |

| Named seam and owner | Input → work → output → check |
| --- | --- |
| `CoreStateStorage.create`, `core_states.py:91` | Occurrences/members → canonical rows and files → retained state → completeness plus publisher admission. |
| `CoreSelectionStorage`, `core_selections.py:87` | State and field definition → typed extraction/canonical bytes → retained selected values → exact comparison evidence. |
| `CorePublisher`, `core_publication.py:123` | Logical publication unit → obligations → ledger commit → immutable identities, availability and provenance. |
| `RecordStorage`, `ports/record_storage.py:124` | Admitted layers → union/compaction → physical representation → exact logical equivalence. |

Paths in the table are under `src/docspec/application`, `adapters/storage`, or the
explicit `ports` directory; findings below give complete paths. Ownership is
already centralized. Reshapes should stay within these owners.

## Baseline findings, ranked by practical value

### 1. RESHAPE: route one selected-input scan

**CONCERN — avoidable work; medium effort, high confidence in work reduction,
unmeasured elapsed benefit.**

[`core_selections.py:87`](../../src/docspec/adapters/storage/core_selections.py)
creates three separately consumed branches from the same lazy state relation:
inline values, external JSON, and opaque content. Each branch executes even when
its output is empty. The current named receipt confirms three parent scans of
26 files each. Its 30,397,359-byte payload-column bound is conservative, not
physical I/O. Identity bounds already prune before opening files
(`src/docspec/adapters/storage/records.py:315`); no current evidence supports the
old claim that each named lookup opens all 23,269 files.

Let S be selected source rows and P their payload bytes. Branching repeats the
source relation's scan/join cost a constant number of times; it adds no new
asymptotic exponent. For full selections, S=N. It nevertheless makes the engine revisit
large JSON records solely to discover that other representations are absent.

**Smallest reshape:** use one bounded scan, classify its rows once, and route
inline/external/opaque batches through the existing typed evaluator. Preserve
missing-member rows, aliases, content checks and exact field semantics. Do not
materialize the entire source merely to share it. This recommendation loses
priority if owner measurements show that the repeated branches are negligible,
or routing overhead erases their saving. File-visit counters can falsify the
claimed work reduction without changing correctness requirements.

### 2. RESHAPE: retain canonical selected bytes as a typed column

**CONCERN — representation overhead; medium effort and benefit confidence.**

`src/docspec/adapters/storage/core_selections.py:50` embeds canonical comparison
JSON as a string inside another canonical JSON record. `_write_members` at 264
stores those records through the generic writer; `_comparison_rows` at 305 parses
them to recover the same bytes and orders them for hashing. Publication at 473
also parses selected records to discover external content references. Whole-value
selection therefore carries large values through multiple representation stages.

For Q canonical selected bytes and S rows, wrapping/unwrapping adds O(Q) byte
work; ordering may require O(S log S) comparisons and spill. These are distinct
required-purpose encodings under today's format, not proof of an accidentally
duplicated encoder call. Aggregate codec counters can include the oracle and do
not establish Python's share of the evaluation timer.

**Smallest reshape:** replace the selected-row storage format with typed columns
for member key, occurrence ID, comparison bytes, sort key and content reference.
Keep the shared canonical encoder and evidence framing. Sort/hash that byte
column directly; derive content obligations while consuming the same admitted
stream. Reuse the record writer's durability and descriptor machinery rather than
add another store. This changes the storage profile, not the selected-value
meaning. Preserve unordered multiplicity, declared ordering, absent/null/type
distinctions and fresh validation of externally supplied rows. If parse/escape
work is a small portion of 203.87 s, the latency benefit will be small; that is
the falsifier, not a reason to predict a speedup factor now.

### 3. RESHAPE: stop treating each read batch as a publication unit

**CONCERN — import overhead; modest initial effort, medium confidence in fewer
transactions, low confidence in elapsed attribution.**

`src/docspec/adapters/storage/records.py:362` reads at most 256 rows before byte
slicing. `src/docspec/adapters/storage/core_states.py:139` publishes every yielded
batch separately. For the million-row fixture that creates thousands of units;
`src/docspec/application/core_publication.py:390` repeats closure checks and
`src/docspec/adapters/storage/ledger.py:429` opens a write transaction per commit.
The build receipt records 16,395 total SQLite transactions and 4,358,241 traced
statements. Total transactions include reads; traced statements include row
bindings. Neither number measures lock waiting or proves database dominance.

**Smallest reshape:** coalesce adjacent admitted rows up to the existing
publication byte/row limits before calling the same publisher. Stream larger
transaction groups only if that owner can preserve bounded memory and one clear
atomic unit. First coalescing reduces repeated fixed work without a database or
connection-pool replacement. The initial Parquet readback also decodes all entity
rows for metadata admission; a future shared writer/admission stream could avoid
that pass, but must not buffer an entire import or bypass immutable-row checks.

Keep content durable before successful-retention publication, idempotent units,
authoritative failures and stale-version checks. Core §5.4 and §8 allow grouped
publication. Byte-heavy records limit coalescing, and larger units can hold the
single writer longer. A negligible transaction/setup share or worsened reader
latency would falsify its priority. Python `fsync` timings exclude SQLite/native
syncs; weakening durability is not justified by these counters.

### 4. RESHAPE: use shared indexed pages for checkpoint updates

**CONCERN — structural incremental cost; highest design effort, high confidence
in the growth mechanism, uncertain end-to-end gain.**

`src/docspec/adapters/storage/core_states.py:372` replaces touched membership
buckets; 1,024 distributed keys reach essentially all 64. At 157, every state
hashes all N compact membership rows, with native ordering at
`src/docspec/adapters/storage/records.py:362`. Entity union at 526 copies the full
file list, and `_retain_root` at 779 serializes it. All-member incremental selected-value
reuse also compares complete memberships (`core_selections.py:188`); named scopes
filter the requested keys. These costs
remain even while unchanged document bodies are shared.

For E edits, N_t rows in touched buckets, F files and H retained revisions:
membership rewriting processes N_t+E rows, hashing requires Ω(N) bytes, and flat
descriptors require O(F) space per state. If each revision adds f files, retained
descriptor volume is O(HF + H²f). These are work/storage mechanisms, not a claim
of quadratic elapsed time or peak memory.

**Smallest coherent structural replacement:** shared immutable indexed pages
with changed-path copying, used for both membership and file descriptors. An
eagerly resolved root can preserve zero history replay while sharing unchanged
pages. A deterministic authenticated key tree can replace the full-stream
membership digest; arbitrary file-chunk hashes cannot preserve equality across
compaction. This requires an explicit format/plan change, not a second legacy
reader. Trustworthy page identities can also narrow actual-membership comparisons
without trusting user-declared edits alone.

Preserve complete recovery, immutable old states, occurrence identity, exact edit
semantics and representation-independent equality. The
[bounds and paired-diagnostic receipt](probes/2026-09-14-core-identity-bounds-capacity.json)'s
bucket experiment already found no compelling net gain from simply changing
64 buckets to 1,024/4,096. Its adopted hash control took about **0.40 s** for one
million memberships: eliminating that alone cannot remove a 10-second batch.
Root metadata, rewrite and overlap work must justify this larger change together.
If fixed-E latency/storage do not improve as N/H rise, the replacement fails its
user-value test. Reuse existing compaction for physical repacking; increasing file
size alone trades broad-scan efficiency against selective-read amplification.

## Invariants, counterfactuals and verdict

Any reshape must **preserve** the currently verified invariants: complete retained states (Core
§3.2); distinct occurrence/value identity (§3.1); exact selected values and input
retention (§5); recoverable, valid provenance (§2); complete keyed edit semantics
(§9). Removing those checks would change the product. Removing their repeated
physical execution does not, when equivalent evidence remains with the owner.

Memory is not constant: bounded row buffers coexist with O(F) descriptors,
O(V+G) relevant provenance graphs and native sort/join state. The successful
16 GiB selection at 8.24 GiB peak is positive finite evidence. It does not rank
DuckDB versus Polars asymptotically. `src/docspec/adapters/storage/provenance.py:76`
visits the relevant graph;
the historical H=256 control had flat median latency despite cumulative graph
growth. Keep graphlib and current provenance admission; a new ancestry cache has
no demonstrated value. Likewise, necessary first materialization and previously
removed duplicate publication encoding are not remaining defects.

**Baseline verdict: RECONSIDER the four physical choices above; keep the engine, shared
codec, publisher and ledger owners.** Intent and semantics match; measured
incremental speed diverges from the original target. Findings 1–3 offer narrower
debt-reducing changes; finding 4 is the structural option if scalable small
updates justify it. None promises an unmeasured speedup. A bare dataframe query
omits retention/provenance work, while an engine port retains these outer costs.
The strongest contrary evidence would be equivalent owner timings showing those
costs are negligible.
