# Core implementation tasks

Updated 2026-09-14. These **24 tasks** (C22 is dropped; its identifier is retained so earlier records resolve) implement the [Core spec](core-model.md)
and [implementation plan](core-model-implementation-plan.md), informed by the
[current-code comparison](history/2026-09-13-core-model-implementation-comparison.md).
Each task section below records its current status and delivered evidence.
The Core lifecycle and document integration are implemented. The user’s
[acceptance amendment](core-model-implementation-map.md#implementation-acceptance--2026-09-14)
ends the expanded performance matrix; all 24 live tasks are complete under that scope. C03/C25 retain the evidence and performance limitations. Task IDs are local planning labels, not newly created
jobs or tracker issues.

The [consensus record](history/2026-09-13-core-model-consensus.md#current-adopted-decisions)
records the current adopted scope; the
[recursive validation](history/2026-09-13-core-plan-swarm-validation.md) records the
earlier review and its checked versions. C10 admits roots before C11 executes
operations; task IDs below preserve that dependency order.

The outcome is one simpler shared lifecycle, with the existing document features
using it, and a bounded bulk data path through DuckDB and Arrow. Python coordinates
operations and batches. The SQLite ledger owns logical publication; the existing content-addressed blob store owns bytes.
Nothing is deferred behind a trigger; a capability that turns out to be needed is a new decision. Tests accompany each task; the final validation tasks
complete the coverage and qualify the assembled implementation.

Legacy support is out of scope. Adopt the new APIs and storage formats directly;
build no old-format importer, compatibility layer, migration framework, or dual-write
path. Preserve useful product behavior through the new implementation. Retention
and recovery requirements apply to data admitted by that implementation.

The subsequent storage simplification replaces bounded Python selection overlays
with native temporary tables, retains checked comparison evidence, removes eager
membership hashes, and packs small row groups into larger Parquet files. Resolver
certificates narrow actual change checks; exact comparison still protects
representation equivalence and untrusted data. See [record storage](record-storage.md)
for the current behavior. The [final regression report](history/2026-09-14-core-architectural-performance-review.md#native-storage-simplification-verification)
records 1,293 passing tests for that earlier revision. The subsequent Iceberg
cutover replaces bucket rewrites and flat file inventories with DuckDB row-level
writes and pinned snapshots. SQLite still owns logical publication and retention.
See the [Iceberg measurement](history/probes/2026-09-14-iceberg-core-writer.json)
and [catalog setup](record-storage.md#configure-writes). The
[Iceberg regression report](history/probes/2026-09-14-iceberg-regression.xml.gz)
records 1,293 passing tests; historical performance
receipts remain tied to the implementation they measured.

## Completion rules shared by every task

- Deliver the named observable behavior, its meaningful checks, and its caller changes together.
- Keep dataset-scale data in bounded native batches wherever the operation permits;
  preserve required sequential edit semantics and account for unavoidable Python work.
- Reuse unchanged payloads and verified references within their integrity scope.
  A small local edit should not force decoding or encoding every unaffected record.
- Preserve bulk field-level dependencies across members, meaningful positions as
  data, and deterministic sorting rules where consumed order affects results.
- Use the shared JSON decoder and encoder for new or changed values. Measure the
  bounded Python validation/encoding calls as part of the complete bulk path.
- Preserve useful document functionality, exact source evidence, retained history,
  failure accounting, and inspectable result selections.
- Name the old rule or path being replaced and remove it when callers move. New Core
  behavior uses the existing architectural boundaries; it does not justify a second
  permanent lifecycle or an abstraction for every conceptual relationship.
- Pin actual tested dependency versions and preserve optional package boundaries.
  Keep behavior, capacity evidence, package build, and publication status distinct.

## Dependency-ordered overview

Dependencies name prerequisite deliverables, not a demand to serialize all work.
Related tasks may be implemented together when that makes a smaller coherent change.

| Task | Deliverable | Depends on |
| --- | --- | --- |
| C01 | Map ownership, preservation, and retirement | — |
| C02 | Specify records, identity rules, and independent fixtures | C01 |
| C03 | Qualify the required bulk operations | C02 |
| C04 | Implement typed records and one admission path | C02 |
| C05 | Implement canonical value and correspondence encoding | C03, C04 |
| C06 | Make record storage and interchange batch-native | C03, C04, C05 |
| C07 | Connect the existing blob store to publication and cleanup | C04, C05 |
| C08 | Implement the authoritative SQLite metadata backend | C04, C05 |
| C09 | Implement durable publication and recovery | C06, C07, C08 |
| C10 | Implement general root states and occurrence membership | C09 |
| C11 | Implement the common operation lifecycle | C10 |
| C12 | Implement immutable partial-value transformations | C11 |
| C13 | Implement membership revision resolution | C12 |
| C14 | Implement typed selected-value evaluation | C13 |
| C15 | Implement checkpoints and compaction without new logical states | C14 |
| C16 | Implement dependency adequacy and affected-result queries | C14, C08 |
| C17 | Implement multi-result correspondence and exact reuse selection | C16, C11 |
| C18 | Implement current selection and policy-authorized cleanup | C15, C17 |
| C19 | Move the document pipeline onto Core and bulk data flow | C13, C14, C17 |
| C20 | Consolidate Python API, CLI, and inspection | C18, C19 |
| C21 | Preserve scheduler, remote storage, and independent exports | C20 |
| C22 | Dropped; Keyed-State checks move to C24 | — |
| C23 | Retire superseded code, schemas, configuration, and dependencies | C20, C21 |
| C24 | Complete conformance, regression, and installed-package checks | C23 |
| C25 | Qualify full-path performance and finish implementation docs | C24 |

After C02, C03 and C04 can progress independently. After C05, batch I/O,
content storage, and the ledger can progress independently. After C17, document integration can proceed when its listed prerequisites are ready.

## A. Fix the scope and acceptance criteria

The “Start from” entries identify the original baseline. Retired paths remain
as code text; links point to files that still exist. The implementation map
names the current owners and records retirement coverage.

### C01 · Map ownership, preservation, and retirement

**Depends on:** none. **Status:** complete (2026-09-13; mapping and acceptance targets only).

Map each required Core behavior to its current owner, target owner, useful user behavior, and code to retire. Reconcile overlapping items in the existing dataset and maintainability checklists so each change has one task owner. D48/D54's shared lifecycle and cleanup work belongs to C11, C19, C21, and C23; their earlier Dagster-only ownership is superseded. External search-recipe qualification remains separate. Record the direct replacement of old APIs and storage formats and define full-workload capacity targets.

**Done when:** A coverage and retirement map names the shared owners, document workflows to preserve, old entry points and formats to remove, and measurable time/memory/scan limits. Targets and test inputs are recorded before tuning. Legacy compatibility and data migration are excluded.

**Delivered:** The [ownership and acceptance map](core-model-implementation-map.md)
names current and target owners, direct API/format replacements, preserved
document workflows and their checks, checklist routing, and fixed workload
time/memory/scan budgets. D31/D37/D48/D54 route to Core; maintainability E5 routes
to D40. C02 fixes executable fixtures; C03/C25 measure the selected implementation.
Source inventory, task dependencies and local links were checked; no runtime or
capacity result is claimed by C01.

**Simplification:** Keep the existing runtime → application → ports/domain organization. No new framework, service, or repository for every concept in the spec.

**Start from:** [docs/dataset-experiments-todo.md](dataset-experiments-todo.md), [docs/maintainability-todo.md](maintainability-todo.md), [CONTRIBUTING.md](../CONTRIBUTING.md).

### C02 · Specify records, identity rules, and independent fixtures

**Depends on:** C01. **Status:** complete (2026-09-13; records and independent fixtures).

Specify the versioned occurrence, state, revision, operation definition, execution, input/result binding, selected-value, resource, and selection records. Separate member keys, occurrence/entity IDs, content digests, request IDs, and attempt IDs. Define contextual raw/derived roles, new/adopted entities, uncertainty, empty/null/failure outcomes, and physical versus logical state identity.

**Done when:** Known-answer fixtures cover every distinction and invalid combination, including generation cycles/self-dependence, duplicate generation, and usage timing for streams. A small independent state/selection reference model and Hypothesis generators exist for later implementation checks. Record the actual PROV interpretation from the beginning; C24 checks it against the ledger.

**Delivered, 2026-09-13:** Added versioned [Core records](../src/docspec/domain/core.py),
the [lifecycle fixtures and requirement mapping](../tests/fixtures/core/README.md),
and the eager, production-independent
[state/selection/provenance oracle](../tests/support/core_reference.py),
[Hypothesis generators](../tests/support/core_strategies.py), and
[selected-value known answers](../tests/fixtures/core/selected-values.json).
The streamed [Core workload generator](../tests/support/core_workload.py) fixes
the capacity input's keys, duplicate pairs, bodies and typed metadata cycle.
The fixtures cover all 12 top-level record families, retention scopes, exact
reuse selections, contextual roles, outcomes and qualified provenance events.
Reference checks reject invalid edit prefixes, cycles and impossible usage or
generation ordering. The focused Core/catalog/package run passed **215 checks**.
Hypothesis 6.168.0 is locked as a development dependency. These are foundation
checks, not full-size capacity or assembled-runtime conformance evidence.

**Simplification:** Several concepts may share one record or manifest. The reference model is test code; production has one implementation of each rule.

**Start from:** [docs/core-model.md](core-model.md), [docs/core-model-implementation-plan.md](core-model-implementation-plan.md), [src/docspec/domain/content.py](../src/docspec/domain/content.py), `src/docspec/domain/processors.py`.

### C03 · Qualify the required bulk operations

**Depends on:** C02. **Status:** complete (2026-09-14; operation baselines and independent larger-than-memory verification).

Exercise DuckDB/Arrow on the actual selected-value encodings, nested JSON edits, schema validation, and hashing requirements. Cover all/named-member field selection, duplicate counts, deterministic sorting, whole-state framing, absent/null/type/composite cases, duplicate-key and pointer-syntax admission, and canonical encoding of all newly extracted or changed values. Use input larger than the allowed working memory, avoiding unbounded list/string aggregates. Measure shared decoder/encoder calls, Python allocations/conversions, scratch use, and output streaming.

**Done when:** The canonical-bytes path for extracted values passes its fixtures across the admitted domain, and reproducible baselines exist for each required operation without treating the existing membership probe as a complete resolver or production benchmark.

**Simplification:** One engine, no native component.

**Progress, 2026-09-13:** The [bounded bulk experiment](../tests/support/core_bulk_experiment.py)
exercises DuckDB typed field/whole extraction, named-member joins, canonical
member encoding, external byte sorting and framed SHA-256 streaming. Its
[42 checks](../tests/test_core_bulk_experiment.py) compare independent known
answers and generated values, including absence/null, object tokens, material
keys/identities, duplicate multiplicity and chunk-independent digests. The
full 1,048,576-member whole-value path completed in **75.9 seconds** at **9.94 GiB**
peak RSS; named fields completed in **4.0 seconds**. Whole, field and named
digests match independently regenerated full answers. The user authorized more
memory and prioritized finishing the implementation; the setting for that historical probe was
8 GiB/one native thread, without a custom sort or an engine comparison project.
[Exact receipts](history/probes/2026-09-13-core-bulk-observations.json) preserve
the initial memory failures, successful settings and file pins. These are historical operation baselines, not current-package full-path timings.

**Additional baselines, 2026-09-14:** The
[installed boundary recipe](history/probes/2026-09-14-core-boundary-qualification.json)
uses the exact locked dependencies in separate processes. It verifies 1,024
nested-value edits through 16 actual activities, 1,025 supplied-schema checks
including refusal, and exact byte boundaries through publication and reopening.
The native record ceiling is 8 MiB. Commit framing reduces the largest inline
record in this fixture to 8,388,423 bytes plus a 185-byte receipt; a `ContentRef`
supports the full 8 MiB encoded JSON value. Each next-byte case refuses. This receipt remains pinned to its historical package.

**Completion, 2026-09-14:** The [current receipt](history/probes/2026-09-14-core-bounded-writer-capacity.json)
adds full whole/field/named/ordered selection and scan evidence through the
installed production owners. Its two-copy case independently checks every one
of 2,097,152 members: exact keys, values and digest agree; selection peaks at
8.24 GiB and verification at 9.60 GiB for 16 GiB of logical body data.
Current runs use 6 GiB/one native thread. Historical boundary and allocation
probes retain their own pins. C24 owns current semantic fixtures; C25 owns
full-path measurements and explicitly unqualified capacity claims.

**Start from:** [docs/history/probes/2026-09-13-engine-resolver-probe.py](history/probes/2026-09-13-engine-resolver-probe.py), [docs/capacity-workloads.md](capacity-workloads.md), [src/docspec/adapters/storage/records.py](../src/docspec/adapters/storage/records.py).

## B. Build the shared data and operation path

### C04 · Implement typed records and one admission path

**Depends on:** C02. **Status:** complete (2026-09-13; record and schema admission).

Implement fixed records with msgspec. Admit raw JSON through the shared Rulespec decoder to reject duplicate keys, then use strict typed conversion or the supplied payload schema without decoding the same bytes again. Generate fixed-record schemas from the same types. Compile and reuse jsonschema-rs validators with pinned draft, references, formats, numeric behavior, and structured errors. Keep JSON Patch's ignored extra operation members as its defined exception to unknown-field rejection.

**Done when:** Valid values round-trip; duplicate keys, unknown fixed-record fields, malformed references, ambiguous presence, unsupported numeric values, and invalid outcomes refuse consistently. Supplied payload schemas remain authoritative for payloads. Fixed schemas and decoding agree on the acceptance fixtures.

**Simplification:** Retire corresponding handwritten shape parsers and duplicate structural validators as their callers move. Keep cross-record semantic checks with their owners.

**Delivered:** [Core admission](../src/docspec/domain/core_admission.py) uses the
shared canonical decoder, strict msgspec conversion and the types' generated
schema. Payload values retain their original Python types until the shared
codec accepts or refuses them. [Payload schema validation](../src/docspec/adapters/schema_validation.py)
pins Draft 2020-12, reference snapshots, explicit format behavior and structured
error locations. Existing catalog callers now use this one compiled validator
for both acceptance and diagnostics; the Python jsonschema path and direct
dependency are removed. msgspec 0.21.1 is required. Record, schema and catalog
checks passed in the 215-test focused run. The full regression gate subsequently
passed **1,343 tests** with Dagster and S3 enabled, including installed-package
checks (one live-service test deliberately deselected). Ruff and lock consistency
passed. Ledger-wide semantics belong to C09; assembled Core qualification remains
C24/C25.

**Start from:** [src/docspec/domain](../src/docspec/domain), [src/docspec/adapters/catalog_artifact/schemas.py](../src/docspec/adapters/catalog_artifact/schemas.py), [src/docspec/cli_io.py](../src/docspec/cli_io.py), [pyproject.toml](../pyproject.toml).

### C05 · Implement canonical value and correspondence encoding

**Depends on:** C03, C04. **Status:** complete (2026-09-13; shared encoding primitives).

Reuse the shared JSON decoder, canonical encoder, and SHA-256. Encode new or changed JSON values, including extracted fields and edited values; reuse unchanged admitted canonical bytes. Implement versioned operation/dependency encodings, explicit absent/present values, labeled composites, state-member multisets or sorted sequences, and material keys/identities/resources. Preserve exact opaque binary bytes and distinct logical identities.

**Done when:** Every encoding has known-answer bytes and digests. Number/string, null/absence, missing member versus missing field, duplicate counts, Unicode/control-character escaping, object ordering, and malformed numeric cases match across paths. Every new/changed extracted JSON value uses the shared encoder; no escape-only shortcut remains. Large selections preserve encoded bytes/digests across bounded chunk sizes. Encoding version changes cannot silently reinterpret retained identities.

**Simplification:** One canonical rule set per codec and one hash family. Ordinary JSON serialization and engine-internal hashes do not become alternate identity encoders.

**Delivered:** [Core comparison encoding](../src/docspec/domain/core_encoding.py)
owns field/member presence, material keys and identities, normalized dependency
definitions, resource uncertainty and versioned correspondence bytes. The
existing canonical-array digester now supports fixed purpose/version prefixes
and admitted bytes; both existing document callers and the bulk experiment use
that owner. The experiment's duplicate member encoder and hasher were removed.
The focused Core/codec/domain/execution/release checks passed **223 tests**.
Batch-storage and lifecycle integration proceed through C06, C14 and C17;
C03 records the completed operation baselines.

**Start from:** [src/docspec/domain/identity.py](../src/docspec/domain/identity.py), [src/docspec/adapters/framing.py](../src/docspec/adapters/framing.py), [src/docspec/adapters/catalog_artifact/digests.py](../src/docspec/adapters/catalog_artifact/digests.py), [docs/canonical-json.md](canonical-json.md).

### C06 · Make record storage and interchange batch-native

**Depends on:** C03, C04, C05. **Status:** complete (2026-09-14; integrated Core owners and regression checks).

Expose bounded Arrow record-batch readers/writers and DuckDB relations for internal work. Carry queryable keys, types, presence, references, and digests alongside encoded payloads. Stream writes and reads; preserve partition selection, physical admission, producer exceptions, cancellation, and iterator closure. Keep decoding at explicit admission, changed-value encoding, and consumer boundaries.

**Done when:** Relational handoffs stay in Arrow/DuckDB and do not parse or re-encode unchanged payloads. Required Python admission/encoding work stays bounded and measured. Read/write results match independent fixtures; early cancellation, oversized values, and producer failures close resources correctly. Row and byte ceilings are enforced.

**Simplification:** Replace internal fetch-row/parse/encode handoffs and per-item workspace spooling where relational batches express the same work. Keep a small row convenience API over the shared path.

**Progress, 2026-09-13:** The existing Parquet writer now owns both row and
Arrow input. Row admission calls the batch writer; native input preserves
already admitted canonical payload buffers. Operation-scoped admission exposes
bounded batch readers and DuckDB relations. `retain_batches` returns the new
write's admission directly, avoiding a second payload scan; incremental callers
carry their admitted base. Unordered imports use the same writer with native
uniqueness checks before physical publication. Keyed lookups and multi-layer
relations keep joins in DuckDB. The ledger, Arrow encoder and lookup parameters
share one row/byte batching helper. Routing accepts arbitrary string keys, including
empty and whitespace member keys. Existing logical readers share that
query path, and the workload tool uses the same row/byte batching implementation.
[Native handoff checks](../tests/test_record_batches.py) cover unchanged-byte
copies, native queries, partition selection, empty input, row/byte ceilings,
early cancellation, producer failure and closure. The native connection remains
shared and configurable. Core consumer migration continues with C10/C14/C19;
C06 remains open until those relational handoffs replace the old internal ones.

The retrospective review also identified repeated admission/encoding between
Core root writing and entity ledger admission. Carry admitted canonical rows
through that existing boundary when completing this task; measure actual
conversion counts through C25 before claiming the duplication is removed.

**Integrated verification (2026-09-14):** All live consumers now use the
shared batch path. A validated canonical-record snapshot carries the same bytes
through publisher and ledger admission. Eight real root entities required
16 schema checks, eight canonical encodes and eight canonical decodes, down from
40/24/16. Validation still occurs before storage and on persisted-row admission;
there is no unchecked caller flag. The 38-test storage/admission gate and
114-test publication/reuse/provenance gate pass. Full workload measurements
remain C25; these counts establish the removed repeated conversions.

**Start from:** [src/docspec/ports/record_storage.py](../src/docspec/ports/record_storage.py), [src/docspec/adapters/storage/records.py](../src/docspec/adapters/storage/records.py), [src/docspec/ports/record_workspace.py](../src/docspec/ports/record_workspace.py), `src/docspec/adapters/reconciliation.py`, [tests/test_parquet_arrow_stream.py](../tests/test_parquet_arrow_stream.py).

### C07 · Connect the existing blob store to publication and cleanup

**Depends on:** C04, C05. **Status:** complete (2026-09-14; integrated Core owners and regression checks).

Keep the existing content-addressed blob store and its S3 adapter. Preserve logical artifact IDs independently of storage addresses. Implement backend deletion and content-readiness/protection hooks; the blob interface now exposes backend deletion, with policy integration still required. Route removal through C18's policy owner. Establish local file and directory durability before content can be published by the ledger, and qualify equivalent readiness for S3.

**Done when:** Exact bytes survive write and reopen; equal bytes can back distinct entities. Integrity, concurrent creation, stream closure, interruption, and file/directory durability checks pass. New and reused objects stay protected through publication. Deletion is bounded, retryable, and callable only through the policy owner; the existing inventory is not treated as a deletion implementation.

**Simplification:** No new storage library. Preserve DocSpec retention rules and the provider-neutral content boundary.

**Progress, 2026-09-13:** Local and S3 writes now use
[one staging and stream-ownership implementation](../src/docspec/adapters/streams.py)
and one contained materializer. Local blobs and Parquet members share immutable
file linking and directory durability. Existing content has an explicit readiness
check. Both backends expose bounded, retryable deletion for the policy owner;
S3 uses conditional deletion and confirms the active address disappeared.
[S3's deletion semantics](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteObject.html)
distinguish removal of an active address from erasure of provider archival
versions; the backend makes no archival-erasure claim. Fault tests exercise
interrupted directory flushing, retries, exact producer errors, closure and
provider conflicts. The focused storage/Core workload group passed **139 checks**. The full local
regression run with the prepared Dagster/S3 extras then passed **1,423 tests**
in 255.70 seconds; the live-service test was deselected. This is local regression
evidence, including installed-package checks, rather than live-provider qualification.
C09 now protects new and reused bytes through publication with the shared
ledger lock. Policy-controlled cleanup remains C18 integration work; C07 stays
open until that owner routes deletion and verifies its complete race behavior.

**Integrated verification (2026-09-14):** C18 now routes actual deletion
through its policy owner, with durable outcomes and recovery. Shared content,
pending publication and suspended checkpoints remain protected. Local and S3
adapter checks, process-death protection and cleanup races pass in the strict
1,172-test regression gate. Archival S3 version erasure remains outside the
backend's active-address deletion guarantee.

**Start from:** [src/docspec/ports/blob_store.py](../src/docspec/ports/blob_store.py), [src/docspec/adapters/storage/blobs.py](../src/docspec/adapters/storage/blobs.py), `src/docspec/runtime/storage.py`, [tests/test_storage_adapters.py](../tests/test_storage_adapters.py).

### C08 · Implement the authoritative SQLite metadata backend

**Depends on:** C04, C05. **Status:** complete (2026-09-13; metadata backend primitives).

Implement versioned metadata tables, initialization, constraints, indexes, bounded parameter/result batches, and explicit connection ownership. Use sqlite3 with WAL, FULL durability, foreign-key checks, short transactions, and bounded busy/retry handling. Implement the shared storage primitives for all six operations in plan §4, plus bounded internal writes for attempt progress/failure, dependency evidence, immutable policies, and imported entities/states. Define stable update identities and retry semantics. Reject unsupported schema versions explicitly.

**Done when:** Initialization, write/close/reopen, unsupported-version refusal, rollback, consistent batch lookup, multiple candidate rows per key, and concurrent writer checks pass. Failure/progress and added evidence survive reopening without implying successful retention or overwriting original records. Large requests use set-based joins or bounded request tables rather than one query per item.

**Simplification:** The old processor cache stays a derived cache during transition; it never becomes a competing authority. DuckDB reads metadata through controlled snapshots/batches; ledger writes remain owned here.

**Delivered, 2026-09-13:** The [Core ledger](../src/docspec/adapters/storage/ledger.py)
implements the [bounded metadata port](../src/docspec/ports/core_ledger.py) through
SQLite WAL/FULL, foreign keys, explicit connection ownership, a bounded busy wait,
version refusal and atomic unit receipts. Core admission and the shared encoder
prepare immutable records before the write transaction. The backend distinguishes
recorded outcomes, successful retention, current availability and evidence versions.
One commit primitive supports result/selection/import metadata; the same owner
provides candidate joins, relationship reads, guarded current selection, progress
and failure writes, and recoverable policy-removal intent/completion. New omission
or supplement records append evidence and advance versions without rewriting
original outcomes. Requests use temporary tables and set-based queries; both
parameters and returned rows observe the shared row/byte limits.
[Ledger checks](../tests/test_core_ledger.py) exercise reopen, rollback, conflicting
identities, uncertain commits, multiple candidates, failure/evidence preservation,
concurrent writers, stale current/evidence guards, read snapshots, byte limits and
removal recovery. The focused Core/storage/package group passed **185 tests**.
These are backend guarantees; content readiness, protection and PROV publication
admission remain C09, and policy decisions and deletion integration remain C18.

**Start from:** `src/docspec/adapters/processor_cache.py`, `src/docspec/adapters/storage/controls.py`, `src/docspec/adapters/storage/catalog.py`, `src/docspec/runtime/storage.py`.

### C09 · Implement durable publication and recovery

**Depends on:** C06, C07, C08. **Status:** complete (2026-09-14; integrated Core owners and regression checks).

Implement successful-retention checks and publication units: retain required bytes/descriptions, establish recoverability, then atomically publish results and associations. Use the same admission checks for imported entities and complete states without fabricating a producing execution. Validate actual generation/usage/derivation relationships across incoming and retained records. Protect referenced content from cleanup through publication. Separate completion, historical retention, current availability, and reuse eligibility. Reconcile uncertain commits using stable identities.

**Done when:** Fault injection before/after content retention and metadata commit yields complete published units or recoverable unpublished work. Publication retry does not duplicate logical results. Generation self-dependence, cycles, conflicting generation, and invalid stream usage timing refuse at admission. Whole-value, state, and selected-value bindings enforce their distinct retention duties. Reuse selections use the same publisher and recheck evidence versions and availability against concurrent omissions or cleanup.

**Simplification:** Consolidate release and result publication rules into one owner. No per-record transaction requirement or mandatory storage reread is introduced.

**Progress, 2026-09-13:** The [shared publisher](../src/docspec/application/core_publication.py)
uses one retention walk for imported data, new results and exact output
selections. Whole, state and selected-value input bindings impose their own
retention duties. Selecting one output preserves the original result descriptions
without requiring unselected outputs or the original input bytes to remain
available. New content carries its readiness proof directly; already admitted
content receives an availability check. Stable unit receipts reconcile lost
commit responses, while fresh selections recheck dependency evidence versions.
The [provenance index](../src/docspec/adapters/storage/provenance.py) checks actual
generation, usage and derivation in the same transaction, using scoped SQL and
`graphlib`. Shared publication/exclusive cleanup protection reuses the same
[file-lock helper](../src/docspec/adapters/locks.py) as source-catalog succession.
[Publisher tests](../tests/test_core_publisher.py) cover retention, missing content,
exact reuse, stale evidence, and failures before/after commit. The
[provenance tests](../tests/test_core_publication_provenance.py) compare generated
cases against the independent oracle; the
[protection tests](../tests/test_core_content_protection.py) include process death.
The complete local regression gate passed **1,459 tests** in 255.95 seconds,
including installed-package checks with Dagster/S3 prepared; one live-service
test was deselected. Ruff and documentation link/task-graph checks passed.
Bulk roots, revised states, retained-parent value evaluation and checkpoints now
use this publisher through C10 and C13–C15. C09 stays open for the remaining
dependency/reuse checks, policy cleanup and document caller replacement in
C16–C19.

**Integrated verification (2026-09-14):** Dependency guards, exact reuse,
policy cleanup and document callers now share publication. The strict regression
gate passes. The 1,024-edit capacity probe exposed a retained-history limit:
new records remain limited to 2,048, while the complete metadata scope now has
an 8 MiB bound and database parameters use bounded groups. The original
interrupted publication recovered in 3.26 seconds without repeating edits;
all 1,024 values and physical layers passed independent audit. A regression
checks a scope exceeding 2,048 records, bounded bindings and exact retry.
This recovery time is not an end-to-end edit performance claim.

**Start from:** `src/docspec/application/commit.py`, `src/docspec/application/execution_checkpoints.py`, `src/docspec/adapters/storage/catalog.py`, `tests/test_stage_checkpoint_recovery.py`, `tests/test_checkpoint_blob_admission.py`.

### C10 · Implement general root states and occurrence membership

**Depends on:** C09. **Status:** complete (2026-09-13; general root storage and admission).

Create and read keyed, unordered root states with scalar or structured admitted values and opaque references. Keep source/member keys separate from immutable occurrence IDs. Store full membership, multiplicity, and values in retained batches, including ordinary position fields where needed to preserve source meaning. Use no separate state-order vector or `move` operation.

**Done when:** Equal values remain distinct occurrences; full roots recover after reopen, including meaningful source positions. A source metadata change can keep its member key while receiving a new occurrence identity. Imported roots do not invent construction activities or silently discard relevant source order. Single-member and batch APIs use the same implementation.

**Simplification:** General roots no longer require a document candidate, extraction, or segmentation. Existing document records become application payloads instead of defining every dataset's shape.

**Delivered:** [Core root storage](../src/docspec/adapters/storage/core_states.py)
imports occurrence and membership streams through the existing Parquet writer.
It admits arbitrary input order, scalar/structured values and opaque references;
native joins reject unresolved members. Equal values can retain distinct
occurrence identities, repeated membership preserves multiplicity, and source
positions remain ordinary payload data. Inline and bulk representations share
one logical membership encoding. Each complete root publishes through C09 after
its content and entity descriptions are retained; imported roots invent no
execution or provenance events.

The ledger stores entity identities, canonical row digests and physical layer
references, with payloads in Parquet. Its existing record API resolves requested
entities in bounded native lookups and checks the pinned bytes. (Since
[C28](#c28--register-bulk-state-members-per-layer), a state's members get these
rows only when a publication references them by identity.) A validated
physical copy can replace an inline SQLite payload without changing the entity.
Complete state reads join native membership and entity relations; they do not
build an entire state in Python. [Root checks](../tests/test_core_states.py) cover
reopen, empty/single/bulk roots, 4,097 members, arbitrary input order, empty keys,
distinct equal values, changed values under stable source keys, opaque bytes,
large-row byte limits, conflicting identities and failed producers. The focused
Core/storage/package group passed **84 tests**; Ruff passed. Document consumer
replacement remains C19, and full-path capacity qualification remains C25.

**Start from:** [src/docspec/domain/content.py](../src/docspec/domain/content.py), [src/docspec/domain/source_catalog.py](../src/docspec/domain/source_catalog.py), [src/docspec/domain/storage.py](../src/docspec/domain/storage.py), [src/docspec/adapters/catalog_artifact/reader.py](../src/docspec/adapters/catalog_artifact/reader.py).

### C11 · Implement the common operation lifecycle

**Depends on:** C10. **Status:** complete (2026-09-13).

Expose direct Python operation entry points for captures, transformations, and adoption of existing artifacts. Record particular attempts, input/result bindings, actual usage/generation/derivation, resources, outcomes, and retry/recovery behavior. Support explicit fresh execution independently of recovery. Use graphlib for the bounded operation graph and shared bounded work scheduling.

**Done when:** Fresh identical requests produce distinct attempts. Publication retries retain stable identities; verified continuation may retain an unfinished attempt, while invoking the producer again records a new attempt and preserves the earlier one. Empty/null success differs from failure/interruption. Adopted artifacts preserve provenance. Streaming and fused execution preserve declared operation boundaries without a persistence barrier between every step.

**Simplification:** Use one lifecycle for direct calls and adapters. C17 adds reuse to this same lifecycle; it does not introduce a second executor or scheduler platform.

**Progress, 2026-09-13:** [Core operations](../src/docspec/application/core_execution.py)
provide fresh attempts, explicit empty/null success, capture origins, adoption,
actual usage/generation/derivation and authoritative failure/interruption records.
Preparation permits direct value exchange before publication. Publishing an
operation includes its prepared prerequisites, journals the completed outputs,
then calls the shared publisher for one atomic unit. Recovery reconciles the
journal without rerunning a producer; already published results come directly
from the ledger. Opaque inputs have a closable streaming API.

The existing [local worker owner](../src/docspec/adapters/execution.py) now supplies
the same bounded completion-order loop to store tasks and direct Core batches.
Both paths close cancelled input streams. Processor ordering now uses the shared
`graphlib` [operation ordering](../src/docspec/domain/graphs.py); invalidation
follows that order once instead of repeatedly scanning the graph. The ledger and
operation coordinator also share bounded control-unit collection and iterator
ownership. [Lifecycle checks](../tests/test_core_execution.py) cover fresh calls,
fused and streaming operations, exact adoption, lost commit responses, callback
failure, interruption, cancellation, bounded concurrency and large opaque input.
The focused Core/processor/worker/storage/package run passed **105 tests** in
25.43 seconds; Ruff and documentation link/task-graph checks passed.
Explicit suspension now retains a checkpoint and the actual completed prefix.
Continuation checks the original definition, retained data, and application
verification before atomically claiming the unfinished attempt. Repeated
suspension, reopening, concurrent resume attempts and missing checkpoint data
are covered by [continuation checks](../tests/test_core_continuation.py).
Document caller cutover remains C19.

**Review correction, 2026-09-14:** The same lifecycle now reserves control
metadata capacity before recording outputs or events, including restored
continuation events. Oversized attached payloads cannot prevent the failed
outcome from being recorded. Bounded diagnostics and journal-creation failure
checks cover those earlier gaps; the final execution/continuation run passed
**28 tests**. Aggregate publications that exceed a unit's limit can be split
without rerunning completed producers.

**Start from:** `src/docspec/application/execution.py`, `src/docspec/application/processor_runtime.py`, `src/docspec/application/execution_evidence.py`, `src/docspec/domain/processors.py`, `src/docspec/runtime/execution.py`.

## C. Implement revisions and selected values

### C12 · Implement immutable partial-value transformations

**Depends on:** C11. **Status:** complete (2026-09-13).

Apply ordered JSON Patch operations to named occurrence values, preserving null versus missing operation fields, path semantics, array shifts, and preconditions. Derive a complete new occurrence value and record the actual transformation through C11. Batch independent records while preserving dependencies within each edit sequence.

**Done when:** Valid patches match the independent model, including ignored unrecognized operation members required by RFC 6902. Invalid paths, failed tests, missing required values, and out-of-domain results refuse without publishing partial success. Earlier occurrences remain unchanged and recoverable.

**Simplification:** Replace application-specific field-update loops for this behavior with the common transformation path; reuse existing JSON mechanisms where they satisfy C03.

**Implemented, 2026-09-13:** [Value edits](../src/docspec/application/core_edits.py)
prepare immutable occurrence replacements through the common operation lifecycle,
pinning patch instructions in the definition and recording qualified usage,
generation and derivation. They can fuse with prepared prerequisites or run
independently through the existing bounded worker. The
[JSON value owner](../src/docspec/domain/json_values.py) shares pointer syntax
with record admission and uses the shared codec for type-sensitive tests and
input snapshots. Only changed values are evaluated. Removing the document root
must be followed by a root add so the final occurrence has a complete value.
[Checks](../tests/test_core_value_edits.py) cover RFC examples, an independent
generated array-edit model, null/missing/type distinctions, invalid intermediate
edits, unchanged originals, batched edits and authoritative failure records.
The combined lifecycle, ledger, publisher, admission and package run passed
175 tests; the additional continuation/value-edit run passed 45 tests.

**Bulk tuning, 2026-09-14:** `prepare_value_edits` groups up to 256 independent
replacements in one bounded activity. The singular helper delegates to it.
Each replacement retains distinct usage, generation and qualified derivation;
patch failures preserve authoritative failure history without partial success.
Revision checks reuse bounded input windows and each grouped result. A
[profiled comparison](history/probes/2026-09-14-core-batch-edit-comparison.json)
records the actual reduction in reads and transactions; full contention remains C25.

**Start from:** [src/docspec/application](../src/docspec/application), [src/docspec/domain/content.py](../src/docspec/domain/content.py), [src/docspec/adapters/storage/records.py](../src/docspec/adapters/storage/records.py).

### C13 · Implement membership revision resolution

**Depends on:** C12. **Status:** complete (2026-09-13).

Resolve puts and removals over retained bases using DuckDB and the plan's member addressing rules. Validate sequential preconditions before reducing to the final change per key. Reject ambiguous conflicts and implicit branch merges.

**Done when:** Full resolution, affected-partition resolution, and the reference model agree on repeated keys, replacements, removals, duplicates, and invalid intermediate edits. Old and new states remain recoverable. Explicit composition produces complete membership and actual provenance.

**Simplification:** Replace the document-specific assumptions used to treat partition replacement or record-ID sorting as all state revision semantics. Reuse partition files and indexes.

**Implemented, 2026-09-13:** The
[state storage owner](../src/docspec/adapters/storage/core_states.py) validates
sequential removals before reducing edits with DuckDB. Full and affected-partition
resolution share one query; unchanged canonical rows and partition files are
reused. Inline bases use the existing writer to acquire a bulk representation
without changing their logical identity. Retained edit arrays use the same
resolver. [Revision preparation](../src/docspec/application/core_edits.py)
validates value-edit results and actual provenance, then records state generation
and derivation through the common lifecycle. Inserted occurrences bind through
a second state, keeping publication metadata bounded for bulk edits. The root
writer, revision writer and inline materialization share one manifest builder.

**Review correction, 2026-09-14:** Revision construction now shares every base
occurrence payload file. Native identity joins remove repeated occurrences from
the incoming layer, and the existing storage owner composes disjoint file
references through its shared manifest writer. The former payload-partition
overlay was removed. A regression covers new IDs in all 64 buckets plus a
repeated existing ID; base payload files remain unchanged. The storage,
revision, checkpoint, ledger and maintenance gate passed **61 tests**.

[Revision checks](../tests/test_core_revisions.py) cover an independent generated
history model, invalid intermediate edits, partition reuse, reopened old/new
states, explicit branches and chains, no-op revisions, mismatched value-edit
evidence, and 2,049 inserted occurrences through two state inputs. C15 owns
compaction and alternative physical representations; C19 owns document cutover.
The combined revision, state, admission, lifecycle, ledger, publisher, storage
and package run passed **244 tests** in 24.83 seconds. Ruff and the task/link
checks passed; the Core spec is unchanged.

**Start from:** [src/docspec/adapters/storage/records.py](../src/docspec/adapters/storage/records.py), `src/docspec/application/planner.py`, `src/docspec/application/maintenance.py`.

### C14 · Implement typed selected-value evaluation

**Depends on:** C13. **Status:** complete (2026-09-14).

Implement `whole`, `json_fields`, and `state_members`. Apply the existing per-member whole/field selector through DuckDB to all or named members. Preserve types, absent/present/null, material keys/identities, multiplicity, and array addressing. Compare a multiset by default; preserve the consumed sequence when the selection references a retained sorting rule in material operation configuration. Group by definition/codec, extract needed fields together, and use C05 encodings. Support direct retention or exact recovery from retained parents.

**Done when:** Both retention routes produce identical values and fingerprints. Invalid pointer escapes and duplicate requested keys refuse. Number/string, null/absence, and missing member versus missing field remain distinct. A bulk URL dependency still corresponds after title-only changes; material URL, membership, duplicate-count, key, or consumed-order changes affect the relevant comparison. Generated keys enter correspondence when declared material. Actual bound values and sorting definitions remain recoverable, not just hashes.

**Simplification:** Replace hard-coded dependency-field projections with retained definitions evaluated by one bulk path. Origin remains separate from equivalence unless material.

**Implemented, 2026-09-14:** [Selection storage](../src/docspec/adapters/storage/core_selections.py)
retains exact selected values and member origins independently of their parents,
or recovers them from protected immutable parents without writing another copy.
Both routes use the [native extraction owner](../src/docspec/adapters/storage/selection.py)
now shared with the capacity experiment. Whole states use per-member whole
selection with material keys. Field and sorting addresses are extracted together;
JSON types and presence remain explicit before the shared canonical encoder.
Opaque whole values retain their codec and support closable byte streams.

The version 1 sorting rule uses an `OperationDefinition` with implementation
`docspec.sort.canonical-json`, version `1`, and configuration
`{"pointers": ["/field"], "descending": false}`. It compares canonical JSON
encodings of tagged selected sort fields, then member keys to resolve ties. This
is an explicit byte-order rule, not numeric or locale ordering. The rule remains
retained with direct and recovered selections.

[Checks](../tests/test_core_selections.py) cover independent expected values,
reopening, parent-storage loss, bulk inputs beyond the metadata unit, types,
absence/null, unchanged URL comparisons after title edits, changed URLs, keys,
identities, multiplicity, array shifts, retained ordering, opaque streams,
external row admission and the canonical control-character escape. The broader
Core/storage/package run passed **280 tests** in 28.37 seconds. The shared
publisher now owns readiness and JSON-content checks for selections and
checkpoint recovery. Document caller cutover remains C19.

**Start from:** `src/docspec/application/processor_rules.py`, [src/docspec/domain/content.py](../src/docspec/domain/content.py), [src/docspec/adapters/catalog_artifact/digests.py](../src/docspec/adapters/catalog_artifact/digests.py), [src/docspec/adapters/storage/records.py](../src/docspec/adapters/storage/records.py).

### C15 · Implement checkpoints and compaction without new logical states

**Depends on:** C14. **Status:** complete (2026-09-14).

Bound edit replay through retained checkpoints, partition indexes, and physical compaction. Materialize full membership and values; preserve original revision/provenance information and all retained recovery paths. Replace physical references through the metadata owner only after equivalence is established.

**Done when:** Checkpointed, replayed, and compacted states agree and retain the same logical IDs. Reopen after interruption recovers a valid representation. Small lookups avoid replaying the full history; deletion of shared storage cannot break another retained state or selected value.

**Simplification:** Retire the requirement to create a new logical release solely for storage compaction, while retaining the existing integrity and recovery checks.

**Implemented, 2026-09-14:** Every revision publishes a complete membership
checkpoint; the maximum pending replay depth is zero. Small reads use the
checkpoint's partition index. The unused deferred-membership record variant was
removed. [State checkpointing](../src/docspec/adapters/storage/core_states.py)
repacks membership and active occurrence values, verifies unchanged membership,
and uses immutable occurrence admission to guard physical value relocation.
The shared publisher atomically changes the preferred representation through
the ledger. The state ID, dataset head, revision record and actual provenance
remain unchanged. Earlier representations and their files remain retained.

The [record storage owner](../src/docspec/adapters/storage/records.py) supplies
one native compaction operation with exact row-equivalence checking. The existing
document maintenance caller now delegates to that same operation rather than
decoding and rewriting each row through its own loop.
[Checks](../tests/test_core_checkpoints.py) cover fewer physical files, pruning
unused values from the new representation, duplicate membership, exact replay,
unchanged logical identities/events, selected-value recovery, scoped reads
without replay, wrong-copy refusal, and interruption before and after the
publication commit. The final checkpoint, record, reference, publisher, ledger,
maintenance and package run passed **187 tests** in 28.62 seconds. C18 owns authorized deletion and C19 owns
the document workflow's remaining cutover.

**Start from:** `src/docspec/application/maintenance.py`, `src/docspec/domain/release.py`, `src/docspec/adapters/storage/catalog.py`, `tests/test_maintenance.py`.

## D. Complete dependencies, reuse, and retention policy

### C16 · Implement dependency adequacy and affected-result queries

**Depends on:** C14, C08. **Status:** complete (2026-09-14).

Persist dependency descriptions and evidence, including material resources and identity/version uncertainty. Produce correspondence keys through C05. Query potentially affected results with scoped recursive SQL. Record discovered omissions and supplemental evidence without rewriting the original execution or fabricating historical values.

**Done when:** An omission blocks the justification that ignored it; adequate corrections can establish later correspondence. Unknown and established resource identities remain distinct; matching unknown descriptions alone cannot establish adequacy, and policy cannot waive it. Relevant declared-dependency traversal and local/global cases match fixtures; cycles in conservative declarations remain distinct from invalid generation dependencies checked by C09.

**Simplification:** Consolidate duplicate dependency/invalidation rules; operation-graph ordering and data-dependent reuse remain distinct responsibilities using shared evidence.

**Implemented:** The [dependency owner](../src/docspec/application/core_dependencies.py)
evaluates supplied declarations through the existing selected-value evaluator
and comparison encoder. It blocks unresolved omissions and uncertain material
resources. Typed, retained historical observations support corrections without
replacing original inputs or rewriting executions; unrelated corrections do
not clear other omissions. Evidence and corrected candidate keys commit together
under the result's checked evidence version.

The ledger's scoped recursive query follows declared dependencies and result
outputs, including operation-definition changes. Conservative cycles terminate
without creating provenance events. Inputs and returned result IDs are batched.
[Dependency checks](../tests/test_core_dependencies.py) cover metadata-only
changes, material changes, supported corrections, unknown history, invalid
supplements, unavailable inputs, reopening and concurrent evidence updates.
The combined dependency/selection/encoding/ledger/record/import gate passed
**151 tests**; the final dependency-only gate passed **14 tests** after adding
historical-input and definition-change cases. Ruff passed. C17 integrates this
assessment into reuse and preserves evaluated evidence for later comparisons.

**Start from:** `src/docspec/domain/processors.py`, `src/docspec/application/processor_rules.py`, `src/docspec/application/planner.py`, `src/docspec/application/failure_frontier.py`.

### C17 · Implement multi-result correspondence and exact reuse selection

**Depends on:** C16, C11. **Status:** complete (2026-09-14; strict regression and installed-package checks).

Connect batch candidate lookup to correspondence, current availability, and separate policy decisions. Allow several retained results for the same key, including nondeterministic alternatives when policy permits. Record the exact selected result and new request bindings using C09; integrate this before execution in C11.

**Done when:** A cache hit records the association and required request data without inventing execution. Multiple alternatives and explicitly fresh work coexist. Reopen preserves the prior exact selection. Metadata-only changes reuse unaffected work while material changes are handled correctly.

Use C16's effective dependency assessment for both candidate checks and
publication; replace the publisher's preliminary raw-definition comparison.
Retain evaluated dependency evidence when indexing results so comparison does
not require historical whole inputs after their policy-authorized removal.
Corrected evidence adds the corrected candidate key atomically; old index rows
remain hints subject to reassessment.

**Simplification:** Replace the one-winner cache and duplicated stage eligibility rules with one correspondence/selection owner. A derived cache may accelerate lookup but cannot determine historical meaning.

**Implementation evidence (2026-09-14):** One `CoreReuse` owner connects
bounded multi-candidate lookup to the shared dependency assessment and the
publisher's final evidence/availability guards. `CoreOperations.resolve` uses
the existing producer and continuation path; a successful new result and its
exact selection publish together. The 67-test lifecycle/reuse gate covers
metadata-only correspondence, alternatives, explicit fresh work, exact reopen,
input removal with retained comparison evidence, omission/policy races, and
suspension. Legacy cache caller retirement remains C19/C23.

**Integrated verification:** [C24](#c24--complete-conformance-regression-and-installed-package-checks)
owns the current regression and installed-package evidence. Capacity remains C25.

**Start from:** `src/docspec/adapters/processor_cache.py`, `src/docspec/application/processor_runtime.py`, `src/docspec/application/base_reprocessing.py`, `tests/test_processor_cache.py`, `tests/test_experiment_retention.py`.

### C18 · Implement current selection and policy-authorized cleanup

**Depends on:** C15, C17. **Status:** complete (2026-09-14; strict regression and installed-package checks).

Implement guarded current pointers and remove_under_policy with recorded authorization and outcomes. Retain historical retention status while tracking availability separately. Use C08's durable intent records for bounded, resumable deletion. Coordinate reachability, shared content, in-flight publication, and interrupted cleanup; reclaim crash leftovers only under an explicit policy.

Resolve physical reachability through the existing state/selected-value
manifests and ledger entity-location references as well as logical retention
links. Bulk membership must not expand into a second per-member SQLite graph.

**Done when:** Stale expected-current updates refuse; switching back preserves both states. Cleanup refuses without an applicable policy or when retention commitments outside its authorized scope still require the content. Durable intent, availability changes, partial deletion outcomes, and reopening reconcile without erasing historical execution evidence. Shared content and selected-value recovery are checked.

**Simplification:** Reuse the current selection and reachability behavior under the ledger owner. Replace separate ad hoc cleanup and pointer-write decisions.

**Implementation evidence (2026-09-14):** `CoreMaintenance` and the ledger
now own guarded current selection, exact policy scope, durable deletion intents,
per-file outcomes, and reopening/resumption. A 66-test gate covers physical
bulk-file reclamation, shared content, direct and parent-dependent selected
values, interruption, and exact restoration. Retrospective review then found that
recoverable progress journals also need physical protection. The shared staged
publisher now protects checkpoint/journal bytes, prepared outputs, adopted
outputs and declared inputs; the corrected 54-test cleanup/recovery gate passes.

**Integrated verification:** [C24](#c24--complete-conformance-regression-and-installed-package-checks)
owns the current regression and installed-package evidence. Capacity remains C25.

**Start from:** `src/docspec/adapters/storage/catalog.py`, `src/docspec/application/maintenance.py`, `src/docspec/adapters/blob_inventory.py`, `src/docspec/domain/policies.py`, `src/docspec/runtime/maintenance.py`.

## E. Move existing consumers onto the shared implementation

### C19 · Move the document pipeline onto Core and bulk data flow

**Depends on:** C13, C14, C17. **Status:** complete (2026-09-14; document integration, durable budgets, retained ordering).

Map source catalogs, captures, extraction, segmentation, processor graphs, evidence coordinates, and result layers to Core operations and bindings. Preserve source-specific interpretation in its application adapters. Route catalog joins, change detection, planning, dependency checks, and output preparation through bounded DuckDB/Arrow batches. Connect new workspace creation, publication, and lookup to the ledger as the sole authority.

**Done when:** Capture-only, later-processing, failure-repair, metadata-only-change, and alternative-result examples use the common lifecycle on newly created workspaces. Publication and reopen use the ledger without falling back to old control records. Meaningful document positions and consumed-order rules survive. Relational stages use bounded Arrow/DuckDB work; admission/encoding costs are measured without materializing a dataset-scale Python collection. Unchanged payloads and verified references are reused within their integrity scope.

**Simplification:** Remove corresponding bespoke prefix-reuse/planning paths as each caller moves. Domain extraction code remains; a second retention/reuse lifecycle does not.

**Implementation evidence (2026-09-14):** `DocumentPipeline` runs capture,
extraction, segmentation, and processor graphs through Core. The common keyed
import consumes one-shot streams; only compact membership addresses spool,
and shared row reads bound both Arrow rows and decoded bytes. The 20-test
document/algorithm gate includes 2,050 segments, coordinates, later processing,
metadata-only reuse, alternatives, repair, and bulk statistics. Bounded groups now share candidate lookup and publication across each document
stage; completed siblings survive a later producer failure. The 73-test
execution/reuse/provider/catalog gate passes. Source-byte and new-row limits
charge actual new work, and zero limits permit reuse. A controlling Core operation
now retains cumulative counters across reopening and handled failures; seven
fresh-process/concurrency checks cover resumption, changed-input refusal, and
concurrent starts. An uncheckpointed hard kill requires a new run ID and budget.
Installed source and document checks exercise the same owners. Provider and
statistics definitions retain the same segment-order description used by their
reader. An order-sensitive callback verifies invocation order, result order,
zero-call reuse, and the saved definition after reopening. The 16-test
document/provider gate and subsequent 1,192-test strict regression run pass.
Full-path capacity remains C25 acceptance evidence.

**Start from:** `src/docspec/application/planner.py`, `src/docspec/application/base_reprocessing.py`, `src/docspec/application/delivery.py`, [src/docspec/adapters/catalog_artifact](../src/docspec/adapters/catalog_artifact), [src/docspec/processing](../src/docspec/processing), `src/docspec/runtime/composition.py`.

### C20 · Consolidate Python API, CLI, and inspection

**Depends on:** C18, C19. **Status:** complete (2026-09-14; strict regression and installed-package checks).

Finish public create/revise/execute/reuse/retain/select/inspect operations and CLI commands around the shared lifecycle. Show requested versus executed work, exact selections, original provenance, failure, and current availability. Keep row conveniences and inspection results bounded; update current callers directly.

**Done when:** The same example works through Python and CLI, closes/reopens, compares both states, and explains why results were reused or rerun. No duplicate parsing, configuration, or execution logic lives in the CLI. New Core operations work without document-stage prerequisites.

**Simplification:** Remove obsolete aliases, redundant configuration and wrapper layers as callers move. Expose useful document workflows through the new API without old-signature compatibility shims.

**Implementation evidence (2026-09-14):** `CoreWorkspace` is the public
runtime. General state creation, revision, retention, comparison, inspection,
current selection and cleanup delegate to shared owners. The CLI now invokes
this runtime directly and preserves source-catalog commands; ten obsolete
command modules and old command registrations were removed. Inspection can
read output availability without loading bulk payloads. Nine focused API/CLI,
import-direction, and dependency-boundary checks pass. The migrated offline and
representation examples add a 17-test gate proving unchanged upstream work,
exact source/quote bytes, resource/configuration invalidation and reopening. Full source-caller and
installed-package qualification remains with C19/C21/C24.

**Incremental imports (2026-09-14):** `CoreWorkspace.upsert` and `docspec state
upsert` add and replace keyed values through the shared revision resolver,
publication journal and current-pointer checks. Stable batch IDs bind the exact
ordered input, base and optional dataset; retries recover the retained result.
The [usage guide](python-runs.md#append-new-records-and-replace-updated-records)
and [regressions](../tests/test_core_ingestion.py) cover streaming input,
interruption recovery and concurrent changes without a separate ingestion store.

**Integrated verification:** [C24](#c24--complete-conformance-regression-and-installed-package-checks)
owns the current regression and installed-package evidence. Capacity remains C25.

**Start from:** [src/docspec/runtime/__init__.py](../src/docspec/runtime/__init__.py), `src/docspec/runtime/inspection.py`, `src/docspec/application/inspection.py`, [src/docspec/cli](../src/docspec/cli), [src/docspec/cli_io.py](../src/docspec/cli_io.py).

### C21 · Preserve scheduler, remote storage, and independent exports

**Depends on:** C20. **Status:** complete (2026-09-14; strict regression and installed-package checks).

Adapt the existing Dagster execution, local workers, S3 storage, result sinks, and independent result exports to the common records and batch operations. Keep scheduler-owned retries/cancellation with the scheduler, while DocSpec owns semantic recovery and retention. Preserve pinned source-provider integrations and optional imports.

**Done when:** Adapter contract tests and installed-package examples preserve exact bytes, selected populations, failure records, stream closure, and original references. Direct and scheduled execution agree on retained meaning. The local Core package runs without scheduler or remote services.

**Simplification:** Reuse existing adapters; remove alternate lifecycle implementations inside them. This task preserves current capabilities and does not add a new remote-storage platform.

**Integrated verification:** [C24](#c24--complete-conformance-regression-and-installed-package-checks)
owns the current regression and installed-package evidence. Capacity remains C25.

**Start from:** [src/docspec/adapters/dagster.py](../src/docspec/adapters/dagster.py), [src/docspec/adapters/execution.py](../src/docspec/adapters/execution.py), [src/docspec/adapters/s3_blob.py](../src/docspec/adapters/s3_blob.py), [src/docspec/adapters/result_export](../src/docspec/adapters/result_export), `src/docspec/adapters/sinks.py`, [tests/test_dagster_adapter.py](../tests/test_dagster_adapter.py), [tests/test_s3_blob_adapter.py](../tests/test_s3_blob_adapter.py).

### C22 · Dropped

PROV export through the `prov` library is not needed: Core requires the
interpretation to be recoverable, and C24 checks it against the ledger. The
Keyed-State insertion/removal completeness checks formerly here move to C24. The
identifier is retained so earlier records still resolve.

## F. Finish consolidation and qualify the result

### C23 · Retire superseded code, schemas, configuration, and dependencies

**Depends on:** C20, C21. **Status:** complete (2026-09-14; retirement audit and strict regression).

Close the C01 retirement map: remove unused old parsers, cache/publication owners, graph algorithms, duplicate blob handling, stage-specific duplicate rules, obsolete formats, and dead wrappers after their callers have moved. Preserve the selected blob store's required mechanics. Update registrations, imports, examples, and dependency metadata together.

**Done when:** Each production lifecycle rule has one owner; all intended consumers use it. The retirement map records retained code and reasons as well as removed paths. Import-direction and package-boundary checks pass, and dependency changes are reflected in the lockfile.

**Simplification:** Cleanup is part of every preceding task; this is the final completeness audit. Judge simplification by fewer duplicate rules, data conversions, and supported paths, not arbitrary line-count targets.

**Implementation evidence:** The [retirement map](core-model-implementation-map.md#live-retirement-notes--2026-09-14)
records removed modules and formats, their replacement checks, and the distinct
source/catalog and processing capabilities retained with current callers. The
final audit found no old lifecycle callers in production, examples or test
support. [C24](#c24--complete-conformance-regression-and-installed-package-checks)
owns current import/package, dependency-lock and regression evidence.

**Start from:** [src/docspec](../src/docspec), [pyproject.toml](../pyproject.toml), [uv.lock](../uv.lock), [tests/conformance/test_import_directions.py](../tests/conformance/test_import_directions.py), [tests/test_package_boundary.py](../tests/test_package_boundary.py), [CONTRIBUTING.md](../CONTRIBUTING.md).

### C24 · Complete conformance, regression, and installed-package checks

**Depends on:** C23. **Status:** complete (2026-09-14; strict regression and exact installed-wheel acceptance).

Complete the Core requirement-to-test map and Hypothesis sequences begun in C02, including the PROV interpretation and the Keyed-State insertion/removal completeness checks against the ledger. Exercise revisions, selections, omissions, failures, deletion, concurrency, schema-version checks, and encoding across the installed package. Update tests to the new interfaces while preserving behavioral assertions, remove tests solely for retired formats/APIs, and run the repository's strict regression workflow. Ship the full two-field revision/reuse/reopen example.

**Done when:** Required tests and parameter cases pass without skips or fabricated coverage; independent fixtures agree with production. The wheel installs with declared dependencies, including the supplied shared artifact wheel, and works without optional services. Evidence names the tested revision and package.

**Simplification:** Use the existing pytest/JUnit and CI workflow. Do not add another test runner or a report format that merely restates pass/fail.

**Original completion evidence (2026-09-14, before the performance simplification):** The
[strict JUnit run](history/probes/2026-09-14-core-bounded-writer-regression.xml.gz)
passes 1,283 tests, including every required parameter case. One live-service
test outside the required map is deselected. Current Core, document, export and
scheduler behavior defines acceptance; retired formats do not. Installed CFR/FEC
example cases are now explicitly required by the map.

The [original installed-wheel acceptance](history/probes/2026-09-14-core-installed-bounded-writer.json)
pins wheel `bc73eb47c51b4fd359b43af9a9412b39058998307e1948aa1b31aaeae64200bc`,
Python 3.12.9 and the locked dependencies. Its
[native JUnit report](history/probes/2026-09-14-core-installed-bounded-writer.xml.gz)
records 393 passing checks with zero failures, errors or skips. Offline runtime,
export, corruption rejection, CLI help and the revision/reuse/reopen example pass
without optional-service packages. All 128 installed package files match the
frozen source; the 172 copied acceptance files stayed unchanged. This includes
the SQLite URI correction found by the [preceding failed package](history/probes/2026-09-14-core-installed-layout8.json).
The full regression gate also passes after layout adoption, that correction,
identity-bounds pruning, and the bounded writer/hash improvements. Bounds checks cover truthful admission, exact
Unicode/empty identities and complete union membership.

C25 records measured capacity and explicitly unqualified claims under the
[acceptance amendment](core-model-implementation-map.md#implementation-acceptance--2026-09-14).
Subsequent runtime changes require affected behavior checks and a new package pin.

**Performance simplification follow-up:** The
[architectural review](history/2026-09-14-core-architectural-performance-review.md)
owns the new verification and package pin for single-scan selection, typed
selected bytes, publication coalescing, named comparison pruning and shared
selected-content retention checks. Those format changes supersede the original
package for current implementation claims; the earlier receipts remain historical.

**Start from:** [conformance/test-matrix.json](../conformance/test-matrix.json), [tests/conformance](../tests/conformance), [tests/support](../tests/support), [tests/conftest.py](../tests/conftest.py), [.github/workflows/ci.yml](../.github/workflows/ci.yml), [docs/qualification.md](qualification.md).

### C25 · Qualify full-path performance and finish implementation docs

**Depends on:** C24. **Status:** complete (2026-09-14; amended implementation acceptance, with capacity limits recorded).

**Accepted scope, 2026-09-14:** The user directed us to wrap testing up. Apply the
[implementation acceptance amendment](core-model-implementation-map.md#implementation-acceptance--2026-09-14):
retain meaningful correctness gates and completed measurements, finish the running
independent larger-than-memory check, and start no further large or repeated
trials. The implementation is not certified against the full original capacity
matrix. Functional requirements remain unchanged.

**Done when:** C24's current source and installed-package gates pass; completed
representative results and failed/unrun performance claims are recorded without
overstatement; the independent value check passes; current documentation
names the implemented owners, measured settings and remaining performance limits.

**Delivered implementation:** One DuckDB/Arrow path uses bounded batches, shared
canonical encoding and immutable admitted records. Shared publication protection
bounds admission reuse; explicit audits remain fresh. Revisions verify every put
and its provenance while sharing unchanged payload files. Verified per-file
identity bounds prune lookup and union inputs. The common writer and digest
framer handle existing batches without another native component or cache layer.
Selection now scans its parent once and stores canonical comparison bytes in
typed rows through that writer. Entity publication combines small read batches;
named change comparisons use the existing file bounds. The
[architectural review](history/2026-09-14-core-architectural-performance-review.md)
records these follow-up changes and their verification. The
[complexity audit](history/2026-09-14-core-batching-complexity-audit.md) and
[paired measurements](history/probes/2026-09-14-core-identity-bounds-capacity.json)
record the reasons, gains and rejected alternatives.

**Evidence:** [C24](#c24--complete-conformance-regression-and-installed-package-checks)
is the sole owner of current regression and installed-package acceptance. The
[pre-simplification performance receipt](history/probes/2026-09-14-core-bounded-writer-capacity.json)
pins package `bc73eb47c51b4fd359b43af9a9412b39058998307e1948aa1b31aaeae64200bc`,
Python/dependencies, input bytes, machine, settings, raw receipts and limitations.

| Pinned baseline check | Result |
| --- | --- |
| Million-member durable build | 281.73 s; 11.28 GiB process peak. |
| Metadata and named selection | Open 0.37 s with no member payload reads; named evaluation 6.12 s after 0.86 s admission. Conservative payload-column scan bound 30.4 MB. |
| Complete field and ordered-field selection | 66.98 s and 59.78 s evaluation; all 1,048,576 results and retained ordering independently checked. |
| Whole-value selection | 203.87 s evaluation plus 137.86 s independent verification; 8.70 GiB peak. |
| Parent recovery and cleanup/publication race | Exact recovered evidence matches; protected content survives; interrupted removal resumes. |
| 100 writer batches with four readers | Every one of 102,400 edit events across the same 1,024 addresses reconciles after reopen, including provenance and stale updates. Total 1,155.26 s; writer peak 2.73 GiB; conservative sum of individual peaks 9.34 GiB. These fail the original time and memory targets. Reader metadata p95 remains below 6 ms. |
| Larger-than-memory population | All 2,097,152 values independently checked; exact digest matches. Build 669.24 s/11.88 GiB; selection 435.12 s/8.24 GiB; verification 407.46 s/9.60 GiB. Original applicable limits pass. |

The table measures the earlier package, not the new storage and selection paths.
No new large capacity trial is required by this follow-up; its small checks
establish only their stated scope.

**Performance limits:** The current Iceberg writer shares original data files and
writes changed rows and positional deletes; touched-bucket rewrites and eager
membership hashes are removed. Fresh availability checks still walk retained
file metadata. Full scans, native joins and a changed complete-sequence comparison
can still grow with the population or selected bytes. The
[current storage assessment](history/2026-09-14-core-architectural-performance-review.md#iceberg-production-writer)
separates the Iceberg measurements from the
[historical performance assessment](history/2026-09-14-core-performance-assessment.md).
Neither establishes a framework throughput comparison. The full long-history,
current-package document-capacity, complete lifecycle repetitions and other unrun
matrix cells remain unqualified. Earlier
[document control](history/probes/2026-09-14-core-document-text512-diagnostic.json),
[read/edit baselines](history/probes/2026-09-14-core-certified-read-cases.json),
[entity-layout measurements](history/probes/2026-09-14-core-entity-bucket-layout.json)
and failed trials retain their original package pins and limits; they do not
become current-package performance claims.

**Simplification:** Current guides describe one implemented lifecycle. The task
list owns sequence/status, the map owns decisions and the retained target matrix,
and receipts own measurements. Avoid duplicate verification totals and avoid
adding implementation complexity merely to satisfy an unneeded benchmark target.

**Subsequent user-requested check:** The
[retained catalogue reimports](history/probes/2026-09-14-iceberg-catalog-reimport.md)
exercise the current Iceberg writer with the two preserved source catalogues.
That record owns their complete value comparisons, small-revision checks, measured
timings and execution limits.

The [import and comparison follow-up](history/probes/2026-09-14-core-import-compare.md)
removes full-value joins from state comparison and recursive copies from admitted
record handoffs. It records exact catalogue comparisons, isolated snapshot-read
measurements, and the limits of the whole-import timings.

The subsequent [full reimport check](history/probes/2026-09-14-core-catalogue-reimport-2173b92.md)
verifies both complete catalogues after those fixes. That record owns the results,
revision timings and overheating limits on import-time comparisons.

The [incremental update check](history/probes/2026-09-14-core-catalogue-upsert.md)
then added 1,024 synthetic records and replaced 16 records in each retained
catalogue, exercised a following batch, and reopened exact retries. Both passed;
initial batches took 1.39 s and 1.01 s, with no reimport. This record owns the
fixture limits and distinguishes those timings from source acquisition costs.

**Start from:** [docs/capacity-workloads.md](capacity-workloads.md), [docs/qualification.md](qualification.md), [docs/architecture.md](architecture.md), [docs/record-storage.md](record-storage.md), [docs/python-runs.md](python-runs.md), [README.md](../README.md).

## G. Follow-on capabilities

The Core program above is complete. Tasks here are later decisions.

### C26 · Derive a keyed state in one operation

**Depends on:** C25. **Status:** implemented (2026-09-22).

**Why:** SpicySearch's prepared metadata ran one operation per member through
`resolve_many`. On a 100-record sample that added about 26 KB of ledger per record
and took 25 ms per record, against 0.15 ms to compute each value. The Core model
does not require per-member operations ([§1](core-model.md#1-scope-and-conformance),
[§8](core-model.md#8-logical-and-physical-representation)), and `upsert` already
publishes a whole batch as one operation over states. The catalogs, published as
bulk states, cost about 0.7 KB of ledger per record.

**Change:** add `CoreWorkspace.derive(rows, *, batch_id, definition, inputs,
base_state_id=None, removals=(), dataset=None)` by generalizing `upsert`:

- One caller-supplied `OperationDefinition` (implementation ID, version and
  configuration such as lookup digests) and one request. Source states and the
  base derived state bind as `StateInput`s; supplied lookup values are retained
  once and bind as `WholeInput`s; each has a `Whole()` dependency.
- Without a base, stream rows into a new keyed state as `create` does. With a
  base, publish a `Revision` of `Put`/`Remove` edits that shares the base's files,
  within the existing 8 MiB edit bound; callers split larger changes.
- The same `batch_id` retries to the same state; changed rows or inputs under it
  refuse. `dataset=` advances a current pointer with the existing stale-base check.
- `upsert` calls `derive` and keeps its definition, request ID and occurrence
  identities. Its request binds a digest-scoped `rows` state instead of the
  earlier `puts` state, so a batch recorded before 0.9.0 refuses on retry; no
  stack caller retries one, and workspace migration is out of scope.
- Consumers read lineage and deltas from DocSpec instead of asserting them:
  `CoreWorkspace.generating_request(state_id)` returns the executed request of
  the operation that generated a state, and `CoreStateReader.changes(older)`
  streams the members that differ from another state.

**Delivered implementation:** `docspec.application.core_ingestion.derive` freezes
the ordered rows and removals into a digest-scoped rows state and one request that
binds the base, caller inputs and rows. `upsert` now freezes its framing digest,
names its unchanged definition, and delegates to `derive` under its own identity
scope, preserving its definition, request ID and occurrence identities. The
shared revision path composes `Put`/`Remove` edits through `compose_revision`,
checking the 8 MiB edit bound as rows arrive. A derive without a base has no
edit bound: it writes the rows once and presents the rows state's files as the
result, so no payload is written or receipted twice. `generating_request` finds
the generating result through the ledger's output links; `changes` reuses
certified revision keys and the native address join that selections use.
Producer failure records the attempt without retaining a rows state.
Each value is canonically encoded once: its bytes frame the retry digest and
complete the stored occurrence record (`inline_occurrence_payload`), and the
spool keeps that record, so the writer neither decodes nor re-encodes it. Rows
the writer has just stored, and rows read back from files re-hashed on open,
are validated by the typed native decode (`stored_record`) instead of proving
canonical form again; caller-supplied bytes keep full admission. On 5,000 real
prepared values this took derive from 1.36 to 0.54 ms per record and reads from
0.23 to 0.028, with byte-identical stored records
([probe](history/probes/2026-09-23-derive-encode-once.json)).

**Done when:** tests cover an initial derive, incremental puts and removals,
exact retry, refusal of changed input under one batch ID, a stale dataset base,
provenance readback from a derived state to its exact input pins, and unchanged
`upsert` identities. The gate passes, [python-runs](python-runs.md) documents
the API, and the release is 0.9.0. SpicySearch and Engine consume it through
their [prepared metadata](../../spicyengine/PLAN.md#pm01) task.

**Verified:** the focused
[derivation suite](../tests/test_core_derivation.py) and the retained
[ingestion suite](../tests/test_core_ingestion.py) pass together with the
broader Core suites under the project gate. The derivation suite covers an
initial derive and its provenance readback to the rows state and every lookup
pin, incremental puts and removals sharing base files, removals-only revisions,
dataset promotion and its stale-base refusal, retry recovery after interruption,
refusal of changed rows, base, inputs, removals and order under one batch ID,
and unchanged upsert definition, request ID and occurrence identities. It also
covers a derive without a base above the 8 MiB edit bound (18,500 rows with
300-character keys), a revision refusing at that bound before its stream ends,
a result sharing its rows state's files, `changes` over a certified revision
chain agreeing with a full comparison, and `generating_request` for derived and
imported states. Release
0.9.0 and consumer cutover remain with the Engine
[PM01](../../spicyengine/PLAN.md#pm01) gate.

### C28 · Register bulk state members per layer

**Depends on:** C10, C18, C26. **Status:** implemented (2026-09-23).

**Why:** `CoreStateStorage.create` published one ledger `records` row (no
payload, its layer, digest and size) and one `retention` row for every member
of a bulk state. On 10,000 real prepared values that was 0.22–0.23 ms of a
0.50–0.54 ms per-row derive, and 729 bytes of ledger per member. The retained
Federal Register and Regulations.gov catalog ledgers are 698 MB and 1.54 GB;
1,008,682 and 2,222,756 of their rows are these member rows, against 49 other
records each. This change stops new states adding such rows; it does not shrink
those existing ledgers, which keep their rows until they are removed or the
catalogs are re-imported. The [architecture](architecture.md) and
[extension guide](extensions.md) already said the ledger creates no per-member
SQLite graph, and the Core model requires no physical record per member
([§1](core-model.md#1-scope-and-conformance), [§3.2](core-model.md#32-complete-retained-states),
[§8](core-model.md#8-logical-and-physical-representation)).

**Change:** register a bulk state's members once per layer, through the manifest
that already names its entity and membership layers and protects their files.
Give a member a ledger row only when a publication references it by identity.
Existing workspaces are not migrated: their member rows keep working, and a row
found in the ledger always takes precedence over the layers.

**Delivered implementation:** `create` writes no member rows. It checks the blobs
of content-valued members with one native scan (`member_contents`); refuses a
member whose identity the ledger holds with other bytes, as a state, or as the
new state's own name (`_check_ledger_copies`, one bounded identity query that
hashes only matching members); and on a retry refuses an occurrence whose bytes
changed. `Publication.read_records` resolves an entity with no row through the
entity layers of the available retained states (`CoreStateStorage.find_members`).
The session keeps a view of each layer (reference, commit time and data files),
admitted once; one query reads the distinct files, refuses copies that differ,
and prefers the newest layer by commit time, so a pin never keeps a superseded
layer alive. Payloads stream in byte-bounded groups and pass full admission.
The publication check resolves referenced entity and data keys the same way,
plus the parent a new direct selection names, and hands them to the ledger as
`MetadataBatch.members`; `commit` inserts their rows and retention rows before
the expected-version check, refuses a digest or entity/state conflict, and
leaves them out of the unit receipt so a retried unit keeps its identity. A
pinned member removed with its state is restored when a later state holds the
same bytes. An incoming entity or state whose identity the caller chose is
checked against the layers; only identities the publishing session minted for
its own outputs (generated entities, execution-scoped states, derive rows,
revision inputs) skip the search, so derive, upsert and document stages stay
flat. Every commit whose checks read layers or identity rows carries the
ledger's identity mark, a sequence kept in the `units` table without a schema
change; `commit` refuses it with `IdentitiesChangedError` if another such
commit moved the mark, and the publisher checks again. Revision puts copy their
occurrences into the digest-scoped inputs state and pin nothing. Document runs
resolve sources through the source state's own layer; their identities digest
their values, so no other copy can differ. Cleanup reads member blobs from each
physical data file once and refuses to remove the only state holding a member
that a recoverable operation binds by identity, naming the execution; removing
that execution with it abandons the journal. Exports retain an unpinned member
root in their own ledger without writing to the source. `stored_snapshot` and
`_retain_entities` are removed.

Semantics that changed: a removal policy names states and representations; an
unpinned member is not a removal target, and after its state's removal it no
longer resolves by identity unless a later state holds it again. An identity
reused with other bytes is refused when written if the ledger holds it (a pin,
an explicit record, a 0.9.1 member row) or if a caller-chosen record or state
reuses a member's identity; two bulk copies without a row are refused on every
search-backed read or pin, but a read narrowed to one state does not compare
other layers, and a revision union keeps its base's bytes. Publishing a
caller-chosen entity or state, or reading an unpinned member by identity, costs
one query over the data files of every retained entity layer after each layer's
first admission in the session, about 2–3 ms per layer on the probe machine; a
pinned member reads from its row as before.

**Done when:** creating a state writes no entity rows; by-identity reads, whole
inputs, adopted and selected outputs, parents, selection origins and export
roots resolve unpinned members; the first reference pins once and later
references and retries add nothing; differing copies and colliding identities
refuse, including across concurrent sessions; a removed pinned member is
restored by an identical copy; cleanup protects member blobs and pending
operations' members and releases a removed state's layers; a 0.9.1 workspace
keeps reading and protecting through its member rows until they are removed;
derive output is byte-identical; the gate passes.

**Verified:** the [first probe](history/probes/2026-09-23-layer-ledger.json), the
[review probe](history/probes/2026-09-23-layer-ledger-review.json) and the
[re-review probe](history/probes/2026-09-24-layer-ledger-rereview.json) derive
the first 10,000 PM01 Federal Register prepared values in 0.266–0.286 ms per row
against 0.476–0.535 on the base, interleaved at load 5–21: `_retain_entities`'
0.22–0.23 ms is replaced by a 0.006 ms content scan and a 0.002 ms identity
check, ledger commits drop from 15 to 4, and no layer search runs.
`bench_derive` on 5,000 rows: 0.33 against 0.53 ms per row, 6.01 against 7.01
encodes per row, with the fingerprint and rows-state identity of the earlier
receipts. The bench ledger holds 8 records and 8 retention rows in 102,400
bytes, against 5,008 and 5,008 in 3,645,440. Reading one unpinned member with 42
entity layers costs 100 ms for the session's first read and about 11 ms for
each later one. The identity check costs 0.006 ms per matching member (0.21 s
for 32,768). Cleanup of an 8,000-member state after 20 upserts reads 8,040 rows
from 41 data files instead of 168,230 through per-layer scans. The session's
layer views hold about 60 KB after 80 revisions, though they list shared files
per layer. The probes time single and repeated reads, caller-named
publications, collisions against rows, one cleanup chain and one derive; they do
not time concurrent sessions, recovery or workspaces with thousands of layers.
The [state](../tests/test_core_states.py), [maintenance](../tests/test_core_maintenance.py),
[document](../tests/test_core_documents.py), [export](../tests/test_result_export.py)
and [0.9.1 workspace](../tests/test_core_older_workspace.py) suites cover each
behavior above, including both orders of a concurrent collision; the last opens
a workspace the 0.9.1 source wrote. Thirty-three of 34 single-line mutations of
the new checks fail a test; the survivor was a redundant statement, since
reverted. The full gate passes with one test deselected and the PDF extra
skipped: `test_source_catalog_workers.py::test_derivation_names_the_engine_that_produced_the_digests`
hangs in this environment under load on the base as well, passes alone, and
passed in an independent full run of the branch.

## Coverage against the spec and plan

| Required outcome | Owning tasks |
| --- | --- |
| Core §1: explicit scope and checked conformance claims | C01, C02, C24 |
| Core §2: recoverable PROV interpretation | C02, C11, C24 |
| Core §3: occurrences, complete states, revisions, multiplicity, and meaningful positions | C10, C12–C15, C19 |
| Core §4: definitions, executions, bindings, contextual roles, and actual provenance | C02, C04, C11, C19 |
| Core §5: retention, availability, streaming, fusion, history, and authorized deletion | C07–C11, C14, C15, C18 |
| Core §6: bulk member-field selections, consumed order, adequacy, omissions, resources, and nondeterminism | C05, C14, C16, C17 |
| Core §7: correspondence, policy, exact retained associations, and provenance preservation | C08, C09, C16, C17, C20 |
| Core §8: batching, physical sharing, and compaction | C05–C09, C15, C19, C25 |
| Keyed-State Profile §9 | C02, C10–C13, C24 |
| Plan: one bulk data path and measured capacity | C03, C05, C06, C13–C17, C19, C25 |
| Plan: one metadata authority and preserved current selection | C08, C09, C18, C19 |
| Simplification and preservation of the existing product | C01, C04, C06, C19–C25 |

## Scope boundaries

The initial implementation includes the general Core behavior in these tasks,
the planned keyed-state implementation and its separately checked profile claim,
and preservation of the existing document, S3, Dagster, and independent-export
capabilities. Existing optional adapters remain optional to install and run.

`state_members` and deterministic consumption order are included. States remain
unordered dictionaries: meaningful positions live in data and sorting rules in
operation definitions, with no separate authored state-order mechanism.

Not planned: PostgreSQL, packed or remote content services beyond the existing S3 adapter, PROV export, RDF storage, Delta integration, additional value codecs, new schedulers, new source connectors, new domain processors, a native component, and authored state ordering. Any of these is a new decision with its own record if it becomes needed; nothing is prebuilt or held behind a trigger.

Legacy API support, old-format readers, and migration of existing workspaces are
excluded. Update owned consumers and fixtures directly to the new interfaces.
Changes to external repositories require their own scoped work only if consuming
their existing packages is insufficient.

Implementation tasks are complete when their evidence and retirement work are recorded.
