# Core implementation tasks

Proposed 2026-09-13. These **24 tasks** (C22 is dropped; its identifier is retained so earlier records resolve) implement the [Core spec](core-model.md)
and [implementation plan](core-model-implementation-plan.md), informed by the
[current-code comparison](history/2026-09-13-core-model-implementation-comparison.md).
All tasks are proposed; none is marked implemented by this document. Task IDs are
local planning labels, not newly created jobs or tracker issues.

The [recursive validation](history/2026-09-13-core-plan-swarm-validation.md)
records the review findings and corrections. C10 admits roots before C11 executes
operations; task IDs below reflect that corrected dependency order.

The outcome is one simpler shared lifecycle, with the existing document features
using it, and a bounded bulk data path through DuckDB and Arrow. Python coordinates
operations and batches. The SQLite ledger owns logical publication; the existing content-addressed blob store owns bytes.
Nothing is deferred behind a trigger; a capability that turns out to be needed is a new decision. Tests accompany each task; the final validation tasks
complete the coverage and qualify the assembled implementation.

Legacy support is out of scope. Adopt the new APIs and storage formats directly;
build no old-format importer, compatibility layer, migration framework, or dual-write
path. Preserve useful product behavior through the new implementation. Retention
and recovery requirements apply to data admitted by that implementation.

## Completion rules shared by every task

- Deliver the named observable behavior, its meaningful checks, and its caller changes together.
- Keep dataset-scale data in bounded native batches wherever the operation permits;
  preserve required sequential edit semantics and account for unavoidable Python work.
- Reuse unchanged payloads and verified references within their integrity scope.
  A small local edit should not force decoding or encoding every unaffected record.
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

### C01 · Map ownership, preservation, and retirement

**Depends on:** none. **Status:** proposed.

Map each required Core behavior to its current owner, target owner, useful user behavior, and code to retire. Reconcile overlapping items in the existing dataset and maintainability checklists so each change has one task owner. D48/D54's shared lifecycle and cleanup work belongs to C11, C19, C21, and C23; their earlier Dagster-only ownership is superseded. External search-recipe qualification remains separate. Record the direct replacement of old APIs and storage formats and define full-workload capacity targets.

**Done when:** A coverage and retirement map names the shared owners, document workflows to preserve, old entry points and formats to remove, and measurable time/memory/scan limits. Targets and test inputs are recorded before tuning. Legacy compatibility and data migration are excluded.

**Simplification:** Keep the existing runtime → application → ports/domain organization. No new framework, service, or repository for every concept in the spec.

**Start from:** [docs/dataset-experiments-todo.md](dataset-experiments-todo.md), [docs/maintainability-todo.md](maintainability-todo.md), [CONTRIBUTING.md](../CONTRIBUTING.md).

### C02 · Specify records, identity rules, and independent fixtures

**Depends on:** C01. **Status:** proposed.

Specify the versioned occurrence, state, revision, operation definition, execution, input/result binding, selected-value, resource, and selection records. Separate member keys, occurrence/entity IDs, content digests, request IDs, and attempt IDs. Define contextual raw/derived roles, new/adopted entities, uncertainty, empty/null/failure outcomes, and physical versus logical state identity.

**Done when:** Known-answer fixtures cover every distinction and invalid combination, including generation cycles/self-dependence, duplicate generation, and usage timing for streams. A small independent state/selection reference model and Hypothesis generators exist for later implementation checks. Record the actual PROV interpretation from the beginning; C24 checks it against the ledger.

**Simplification:** Several concepts may share one record or manifest. The reference model is test code; production has one implementation of each rule.

**Start from:** [docs/core-model.md](core-model.md), [docs/core-model-implementation-plan.md](core-model-implementation-plan.md), [src/docspec/domain/content.py](../src/docspec/domain/content.py), [src/docspec/domain/processors.py](../src/docspec/domain/processors.py).

### C03 · Qualify the required bulk operations

**Depends on:** C02. **Status:** proposed.

Exercise DuckDB/Arrow on the actual selected-value encodings, nested JSON edits, schema validation, and hashing requirements. Extend probes to absent/null/type/composite cases, duplicate-key and pointer-syntax admission, canonical escaping of extracted strings/objects, and bounded execution over input larger than the allowed working memory. Measure parsing, Python conversions/callbacks, scratch use, and output streaming.

**Done when:** The canonical-bytes path for extracted values passes its fixtures across the admitted domain, and reproducible baselines exist for each required operation without treating the existing membership probe as a complete resolver or production benchmark.

**Simplification:** One engine, no native component.

**Start from:** [docs/history/probes/2026-09-13-engine-resolver-probe.py](history/probes/2026-09-13-engine-resolver-probe.py), [docs/capacity-workloads.md](capacity-workloads.md), [src/docspec/adapters/storage/records.py](../src/docspec/adapters/storage/records.py).

## B. Build the shared data and operation path

### C04 · Implement typed records and one admission path

**Depends on:** C02. **Status:** proposed.

Implement the fixed records with msgspec; use explicit typed decoding and strict conversion at public boundaries. Reject duplicate JSON keys before decoding loses them. Generate fixed-record schemas from the same types. Compile and reuse jsonschema-rs validators for supplied payload schemas with pinned draft, references, formats, numeric behavior, and structured errors. Keep JSON Patch's ignored extra operation members as its defined exception to unknown-field rejection.

**Done when:** Valid values round-trip; duplicate keys, unknown fixed-record fields, malformed references, ambiguous presence, unsupported numeric values, and invalid outcomes refuse consistently. Supplied payload schemas remain authoritative for payloads. Fixed schemas and decoding agree on the acceptance fixtures.

**Simplification:** Retire corresponding handwritten shape parsers and duplicate structural validators as their callers move. Keep cross-record semantic checks with their owners.

**Start from:** [src/docspec/domain](../src/docspec/domain), [src/docspec/adapters/catalog_artifact/schemas.py](../src/docspec/adapters/catalog_artifact/schemas.py), [src/docspec/cli_io.py](../src/docspec/cli_io.py), [pyproject.toml](../pyproject.toml).

### C05 · Implement canonical value and correspondence encoding

**Depends on:** C03, C04. **Status:** proposed.

Reuse the existing canonical JSON codec and SHA-256. Implement purpose-tagged, versioned operation/dependency encodings, explicit absent/present values, labeled composites, and material identities/resources. Preserve exact opaque binary bytes and distinct logical identities.

**Done when:** Every encoding has known-answer bytes and digests. Number/string, null/absence, Unicode/control-character escaping, object ordering, and malformed numeric cases match across the chosen execution paths. Extracted JSON is canonically re-encoded or passes a proven byte-equivalent path before hashing. Encoding version changes cannot silently reinterpret retained identities.

**Simplification:** One canonical rule set per codec and one hash family. Ordinary JSON serialization and engine-internal hashes do not become alternate identity encoders.

**Start from:** [src/docspec/domain/identity.py](../src/docspec/domain/identity.py), [src/docspec/adapters/framing.py](../src/docspec/adapters/framing.py), [src/docspec/adapters/catalog_artifact/digests.py](../src/docspec/adapters/catalog_artifact/digests.py), [docs/canonical-json.md](canonical-json.md).

### C06 · Make record storage and interchange batch-native

**Depends on:** C03, C04, C05. **Status:** proposed.

Expose bounded Arrow record-batch readers/writers and DuckDB relations for internal work. Carry queryable keys, types, presence, references, and digests alongside encoded payloads. Stream writes and reads; preserve partition selection, physical admission, producer exceptions, cancellation, and iterator closure. Decode Python objects at actual consumer boundaries.

**Done when:** Production bulk paths no longer round-trip entire batches through per-row dictionaries. Read/write results match the new record definitions and independent fixtures; early cancellation, oversized values, and producer failures close resources correctly. Row and byte ceilings are enforced.

**Simplification:** Replace internal fetch-row/parse/encode handoffs and per-item workspace spooling where relational batches express the same work. Keep a small row convenience API over the shared path.

**Start from:** [src/docspec/ports/record_storage.py](../src/docspec/ports/record_storage.py), [src/docspec/adapters/storage/records.py](../src/docspec/adapters/storage/records.py), [src/docspec/ports/record_workspace.py](../src/docspec/ports/record_workspace.py), [src/docspec/adapters/reconciliation.py](../src/docspec/adapters/reconciliation.py), [tests/test_parquet_arrow_stream.py](../tests/test_parquet_arrow_stream.py).

### C07 · Connect the existing blob store to publication and cleanup

**Depends on:** C04, C05. **Status:** proposed.

Keep the existing content-addressed blob store and its S3 adapter. Preserve logical artifact IDs independently of storage addresses. Define the content readiness and protection hooks used by publication and cleanup, and route every removal through `remove_under_policy`.

**Done when:** Exact bytes survive write and reopen; equal bytes can back distinct entities. Integrity, concurrent creation, stream closure, interruption, and adapter durability checks pass. Nothing deletes content except the policy operation.

**Simplification:** No new storage library. Preserve DocSpec retention rules and the provider-neutral content boundary.

**Start from:** [src/docspec/ports/blob_store.py](../src/docspec/ports/blob_store.py), [src/docspec/adapters/storage/blobs.py](../src/docspec/adapters/storage/blobs.py), [src/docspec/runtime/storage.py](../src/docspec/runtime/storage.py), [tests/test_storage_adapters.py](../tests/test_storage_adapters.py).

### C08 · Implement the authoritative SQLite metadata backend

**Depends on:** C04, C05. **Status:** proposed.

Implement versioned metadata tables, initialization, constraints, indexes, bounded parameter/result batches, and explicit connection ownership. Use sqlite3 with WAL, FULL durability, foreign-key checks, short transactions, and bounded busy/retry handling. Implement the shared storage primitives for all six operations in plan §4, plus bounded internal writes for attempt progress/failure, dependency evidence, immutable policies, and imported entities/states. Define stable update identities and retry semantics. Reject unsupported schema versions explicitly.

**Done when:** Initialization, write/close/reopen, unsupported-version refusal, rollback, consistent batch lookup, multiple candidate rows per key, and concurrent writer checks pass. Failure/progress and added evidence survive reopening without implying successful retention or overwriting original records. Large requests use set-based joins or bounded request tables rather than one query per item.

**Simplification:** The old processor cache stays a derived cache during transition; it never becomes a competing authority. DuckDB reads metadata through controlled snapshots/batches; ledger writes remain owned here.

**Start from:** [src/docspec/adapters/processor_cache.py](../src/docspec/adapters/processor_cache.py), [src/docspec/adapters/storage/controls.py](../src/docspec/adapters/storage/controls.py), [src/docspec/adapters/storage/catalog.py](../src/docspec/adapters/storage/catalog.py), [src/docspec/runtime/storage.py](../src/docspec/runtime/storage.py).

### C09 · Implement durable publication and recovery

**Depends on:** C06, C07, C08. **Status:** proposed.

Implement successful-retention checks and publication units: retain required bytes/descriptions, establish recoverability, then atomically publish results and associations. Use the same admission checks for imported entities and complete states without fabricating a producing execution. Validate actual generation/usage/derivation relationships across incoming and retained records. Protect referenced content from cleanup through publication. Separate completion, historical retention, current availability, and reuse eligibility. Reconcile uncertain commits using stable identities.

**Done when:** Fault injection before/after content retention and metadata commit yields complete published units or recoverable unpublished work. Publication retry does not duplicate logical results. Generation self-dependence, cycles, conflicting generation, and invalid stream usage timing refuse at admission. Whole-value, state, and selected-value bindings enforce their distinct retention duties. Reuse selections use the same publisher and recheck evidence versions and availability against concurrent omissions or cleanup.

**Simplification:** Consolidate release and result publication rules into one owner. No per-record transaction requirement or mandatory storage reread is introduced.

**Start from:** [src/docspec/application/commit.py](../src/docspec/application/commit.py), [src/docspec/application/execution_checkpoints.py](../src/docspec/application/execution_checkpoints.py), [src/docspec/adapters/storage/catalog.py](../src/docspec/adapters/storage/catalog.py), [tests/test_stage_checkpoint_recovery.py](../tests/test_stage_checkpoint_recovery.py), [tests/test_checkpoint_blob_admission.py](../tests/test_checkpoint_blob_admission.py).

### C10 · Implement general root states and occurrence membership

**Depends on:** C09. **Status:** proposed.

Create and read root states with scalar or structured admitted values and opaque value references. Keep source/member keys separate from immutable occurrence IDs. Store full membership, multiplicity, and values in retained batch representations.

**Done when:** Equal values remain distinct occurrences; full roots recover after reopen. A source metadata change can keep its member key while receiving a new occurrence identity. Imported roots do not invent construction activities. Single-member and batch APIs use the same implementation.

**Simplification:** General roots no longer require a document candidate, extraction, or segmentation. Existing document records become application payloads instead of defining every dataset's shape.

**Start from:** [src/docspec/domain/content.py](../src/docspec/domain/content.py), [src/docspec/domain/source_catalog.py](../src/docspec/domain/source_catalog.py), [src/docspec/domain/storage.py](../src/docspec/domain/storage.py), [src/docspec/adapters/catalog_artifact/reader.py](../src/docspec/adapters/catalog_artifact/reader.py).

### C11 · Implement the common operation lifecycle

**Depends on:** C10. **Status:** proposed.

Expose direct Python operation entry points for captures, transformations, and adoption of existing artifacts. Record particular attempts, input/result bindings, actual usage/generation/derivation, resources, outcomes, and retry/recovery behavior. Support explicit fresh execution independently of recovery. Use graphlib for the bounded operation graph and shared bounded work scheduling.

**Done when:** Fresh identical requests produce distinct attempts. Publication retries retain stable identities; verified continuation may retain an unfinished attempt, while invoking the producer again records a new attempt and preserves the earlier one. Empty/null success differs from failure/interruption. Adopted artifacts preserve provenance. Streaming and fused execution preserve declared operation boundaries without a persistence barrier between every step.

**Simplification:** Use one lifecycle for direct calls and adapters. C17 adds reuse to this same lifecycle; it does not introduce a second executor or scheduler platform.

**Start from:** [src/docspec/application/execution.py](../src/docspec/application/execution.py), [src/docspec/application/processor_runtime.py](../src/docspec/application/processor_runtime.py), [src/docspec/application/execution_evidence.py](../src/docspec/application/execution_evidence.py), [src/docspec/domain/processors.py](../src/docspec/domain/processors.py), [src/docspec/runtime/execution.py](../src/docspec/runtime/execution.py).

## C. Implement revisions and selected values

### C12 · Implement immutable partial-value transformations

**Depends on:** C11. **Status:** proposed.

Apply ordered JSON Patch operations to named occurrence values, preserving null versus missing operation fields, path semantics, array shifts, and preconditions. Derive a complete new occurrence value and record the actual transformation through C11. Batch independent records while preserving dependencies within each edit sequence.

**Done when:** Valid patches match the independent model, including ignored unrecognized operation members required by RFC 6902. Invalid paths, failed tests, missing required values, and out-of-domain results refuse without publishing partial success. Earlier occurrences remain unchanged and recoverable.

**Simplification:** Replace application-specific field-update loops for this behavior with the common transformation path; reuse existing JSON mechanisms where they satisfy C03.

**Start from:** [src/docspec/application](../src/docspec/application), [src/docspec/domain/content.py](../src/docspec/domain/content.py), [src/docspec/adapters/storage/records.py](../src/docspec/adapters/storage/records.py).

### C13 · Implement membership revision resolution

**Depends on:** C12. **Status:** proposed.

Resolve puts and removals over retained bases using DuckDB and the plan's member addressing rules. Validate sequential preconditions before reducing to the final change per key. Reject ambiguous conflicts and implicit branch merges.

**Done when:** Full resolution, affected-partition resolution, and the reference model agree on repeated keys, replacements, removals, duplicates, and invalid intermediate edits. Old and new states remain recoverable. Explicit composition produces complete membership and actual provenance.

**Simplification:** Replace the document-specific assumptions used to treat partition replacement or record-ID sorting as all state revision semantics. Reuse partition files and indexes.

**Start from:** [src/docspec/adapters/storage/records.py](../src/docspec/adapters/storage/records.py), [src/docspec/application/planner.py](../src/docspec/application/planner.py), [src/docspec/application/maintenance.py](../src/docspec/application/maintenance.py).

### C14 · Implement typed selected-value evaluation

**Depends on:** C13. **Status:** proposed.

Implement whole values and labeled JSON Pointer composites. Preserve types, absent/present/null, keys, multiplicity, and array addressing. Group by definition/codec, extract needed fields together, then use C05 encodings. Support direct retention or exact recovery from retained parents.

**Done when:** Both retention routes produce identical values and fingerprints. Invalid pointer escapes refuse before engine evaluation. Number/string and null/absence never collide semantically. Unrelated parent metadata and state IDs do not invalidate value-only dependencies; relevant membership changes do. Generated keys enter correspondence when declared material. Actual bound values remain recoverable, not just their hashes.

**Simplification:** Replace hard-coded dependency-field projections with retained definitions evaluated by one bulk path. Origin remains separate from equivalence unless material.

**Start from:** [src/docspec/application/processor_rules.py](../src/docspec/application/processor_rules.py), [src/docspec/domain/content.py](../src/docspec/domain/content.py), [src/docspec/adapters/catalog_artifact/digests.py](../src/docspec/adapters/catalog_artifact/digests.py), [src/docspec/adapters/storage/records.py](../src/docspec/adapters/storage/records.py).

### C15 · Implement checkpoints and compaction without new logical states

**Depends on:** C14. **Status:** proposed.

Bound edit replay through retained checkpoints, partition indexes, and physical compaction. Materialize full membership and values; preserve original revision/provenance information and all retained recovery paths. Replace physical references through the metadata owner only after equivalence is established.

**Done when:** Checkpointed, replayed, and compacted states agree and retain the same logical IDs. Reopen after interruption recovers a valid representation. Small lookups avoid replaying the full history; deletion of shared storage cannot break another retained state or selected value.

**Simplification:** Retire the requirement to create a new logical release solely for storage compaction, while retaining the existing integrity and recovery checks.

**Start from:** [src/docspec/application/maintenance.py](../src/docspec/application/maintenance.py), [src/docspec/domain/release.py](../src/docspec/domain/release.py), [src/docspec/adapters/storage/catalog.py](../src/docspec/adapters/storage/catalog.py), [tests/test_maintenance.py](../tests/test_maintenance.py).

## D. Complete dependencies, reuse, and retention policy

### C16 · Implement dependency adequacy and affected-result queries

**Depends on:** C14, C08. **Status:** proposed.

Persist dependency descriptions and evidence, including material resources and identity/version uncertainty. Produce correspondence keys through C05. Query potentially affected results with scoped recursive SQL. Record discovered omissions and supplemental evidence without rewriting the original execution or fabricating historical values.

**Done when:** An omission blocks the justification that ignored it; adequate corrections can establish later correspondence. Unknown and established resource identities remain distinct; matching unknown descriptions alone cannot establish adequacy, and policy cannot waive it. Relevant declared-dependency traversal and local/global cases match fixtures; cycles in conservative declarations remain distinct from invalid generation dependencies checked by C09.

**Simplification:** Consolidate duplicate dependency/invalidation rules; operation-graph ordering and data-dependent reuse remain distinct responsibilities using shared evidence.

**Start from:** [src/docspec/domain/processors.py](../src/docspec/domain/processors.py), [src/docspec/application/processor_rules.py](../src/docspec/application/processor_rules.py), [src/docspec/application/planner.py](../src/docspec/application/planner.py), [src/docspec/application/failure_frontier.py](../src/docspec/application/failure_frontier.py).

### C17 · Implement multi-result correspondence and exact reuse selection

**Depends on:** C16, C11. **Status:** proposed.

Connect batch candidate lookup to correspondence, current availability, and separate policy decisions. Allow several retained results for the same key, including nondeterministic alternatives when policy permits. Record the exact selected result and new request bindings using C09; integrate this before execution in C11.

**Done when:** A cache hit records the association and required request data without inventing execution. Multiple alternatives and explicitly fresh work coexist. Reopen preserves the prior exact selection. Metadata-only changes reuse unaffected work while material changes are handled correctly.

**Simplification:** Replace the one-winner cache and duplicated stage eligibility rules with one correspondence/selection owner. A derived cache may accelerate lookup but cannot determine historical meaning.

**Start from:** [src/docspec/adapters/processor_cache.py](../src/docspec/adapters/processor_cache.py), [src/docspec/application/processor_runtime.py](../src/docspec/application/processor_runtime.py), [src/docspec/application/base_reprocessing.py](../src/docspec/application/base_reprocessing.py), [tests/test_processor_cache.py](../tests/test_processor_cache.py), [tests/test_experiment_retention.py](../tests/test_experiment_retention.py).

### C18 · Implement current selection and policy-authorized cleanup

**Depends on:** C15, C17. **Status:** proposed.

Implement guarded current pointers and remove_under_policy with recorded authorization and outcomes. Retain historical retention status while tracking availability separately. Use C08's durable intent records for bounded, resumable deletion. Coordinate reachability, shared content, in-flight publication, and interrupted cleanup; reclaim crash leftovers only under an explicit policy.

**Done when:** Stale expected-current updates refuse; switching back preserves both states. Cleanup refuses without an applicable policy or when retention commitments outside its authorized scope still require the content. Durable intent, availability changes, partial deletion outcomes, and reopening reconcile without erasing historical execution evidence. Shared content and selected-value recovery are checked.

**Simplification:** Reuse the current selection and reachability behavior under the ledger owner. Replace separate ad hoc cleanup and pointer-write decisions.

**Start from:** [src/docspec/adapters/storage/catalog.py](../src/docspec/adapters/storage/catalog.py), [src/docspec/application/maintenance.py](../src/docspec/application/maintenance.py), [src/docspec/adapters/blob_inventory.py](../src/docspec/adapters/blob_inventory.py), [src/docspec/domain/policies.py](../src/docspec/domain/policies.py), [src/docspec/runtime/maintenance.py](../src/docspec/runtime/maintenance.py).

## E. Move existing consumers onto the shared implementation

### C19 · Move the document pipeline onto Core and bulk data flow

**Depends on:** C13, C14, C17. **Status:** proposed.

Map source catalogs, captures, extraction, segmentation, processor graphs, evidence coordinates, and result layers to Core operations and bindings. Preserve source-specific interpretation in its application adapters. Route catalog joins, change detection, planning, dependency checks, and output preparation through bounded DuckDB/Arrow batches. Connect new workspace creation, publication, and lookup to the ledger as the sole authority.

**Done when:** Capture-only, later-processing, failure-repair, metadata-only-change, and alternative-result examples use the common lifecycle on newly created workspaces. Publication and reopen use the ledger without falling back to old control records. No bulk internal stage converts the entire population to Python records. Unchanged payloads and verified references are reused within their integrity scope.

**Simplification:** Remove corresponding bespoke prefix-reuse/planning paths as each caller moves. Domain extraction code remains; a second retention/reuse lifecycle does not.

**Start from:** [src/docspec/application/planner.py](../src/docspec/application/planner.py), [src/docspec/application/base_reprocessing.py](../src/docspec/application/base_reprocessing.py), [src/docspec/application/delivery.py](../src/docspec/application/delivery.py), [src/docspec/adapters/catalog_artifact](../src/docspec/adapters/catalog_artifact), [src/docspec/processing](../src/docspec/processing), [src/docspec/runtime/composition.py](../src/docspec/runtime/composition.py).

### C20 · Consolidate Python API, CLI, and inspection

**Depends on:** C18, C19. **Status:** proposed.

Finish public create/revise/execute/reuse/retain/select/inspect operations and CLI commands around the shared lifecycle. Show requested versus executed work, exact selections, original provenance, failure, and current availability. Keep row conveniences and inspection results bounded; update current callers directly.

**Done when:** The same example works through Python and CLI, closes/reopens, compares both states, and explains why results were reused or rerun. No duplicate parsing, configuration, or execution logic lives in the CLI. New Core operations work without document-stage prerequisites.

**Simplification:** Remove obsolete aliases, redundant configuration and wrapper layers as callers move. Expose useful document workflows through the new API without old-signature compatibility shims.

**Start from:** [src/docspec/runtime/__init__.py](../src/docspec/runtime/__init__.py), [src/docspec/runtime/inspection.py](../src/docspec/runtime/inspection.py), [src/docspec/application/inspection.py](../src/docspec/application/inspection.py), [src/docspec/cli](../src/docspec/cli), [src/docspec/cli_io.py](../src/docspec/cli_io.py).

### C21 · Preserve scheduler, remote storage, and independent exports

**Depends on:** C20. **Status:** proposed.

Adapt the existing Dagster execution, local workers, S3 storage, result sinks, and independent result exports to the common records and batch operations. Keep scheduler-owned retries/cancellation with the scheduler, while DocSpec owns semantic recovery and retention. Preserve pinned source-provider integrations and optional imports.

**Done when:** Adapter contract tests and installed-package examples preserve exact bytes, selected populations, failure records, stream closure, and original references. Direct and scheduled execution agree on retained meaning. The local Core package runs without scheduler or remote services.

**Simplification:** Reuse existing adapters; remove alternate lifecycle implementations inside them. This task preserves current capabilities and does not add a new remote-storage platform.

**Start from:** [src/docspec/adapters/dagster.py](../src/docspec/adapters/dagster.py), [src/docspec/adapters/execution.py](../src/docspec/adapters/execution.py), [src/docspec/adapters/s3_blob.py](../src/docspec/adapters/s3_blob.py), [src/docspec/adapters/result_export](../src/docspec/adapters/result_export), [src/docspec/adapters/sinks.py](../src/docspec/adapters/sinks.py), [tests/test_dagster_adapter.py](../tests/test_dagster_adapter.py), [tests/test_s3_blob_adapter.py](../tests/test_s3_blob_adapter.py).

### C22 · Dropped

PROV export through the `prov` library is not needed: Core requires the
interpretation to be recoverable, and C24 checks it against the ledger. The
Keyed-State insertion/removal completeness checks formerly here move to C24. The
identifier is retained so earlier records still resolve.

## F. Finish consolidation and qualify the result

### C23 · Retire superseded code, schemas, configuration, and dependencies

**Depends on:** C20, C21. **Status:** proposed.

Close the C01 retirement map: remove unused old parsers, cache/publication owners, graph algorithms, blob mechanics, stage-specific duplicate rules, obsolete formats, and dead wrappers after their callers have moved. Update registrations, imports, examples, and dependency metadata together.

**Done when:** Each production lifecycle rule has one owner; all intended consumers use it. The retirement map records retained code and reasons as well as removed paths. Import-direction and package-boundary checks pass, and dependency changes are reflected in the lockfile.

**Simplification:** Cleanup is part of every preceding task; this is the final completeness audit. Judge simplification by fewer duplicate rules, data conversions, and supported paths, not arbitrary line-count targets.

**Start from:** [src/docspec](../src/docspec), [pyproject.toml](../pyproject.toml), [uv.lock](../uv.lock), [tests/conformance/test_import_directions.py](../tests/conformance/test_import_directions.py), [tests/test_package_boundary.py](../tests/test_package_boundary.py), [CONTRIBUTING.md](../CONTRIBUTING.md).

### C24 · Complete conformance, regression, and installed-package checks

**Depends on:** C23. **Status:** proposed.

Complete the Core requirement-to-test map and Hypothesis sequences begun in C02, including the PROV interpretation and the Keyed-State insertion/removal completeness checks against the ledger. Exercise revisions, selections, omissions, failures, deletion, concurrency, schema-version checks, and encoding across the installed package. Update tests to the new interfaces while preserving behavioral assertions, remove tests solely for retired formats/APIs, and run the repository's strict regression workflow. Ship the full two-field revision/reuse/reopen example.

**Done when:** Required tests and parameter cases pass without skips or fabricated coverage; independent fixtures agree with production. The wheel installs with declared dependencies, including the supplied shared artifact wheel, and works without optional services. Evidence names the tested revision and package.

**Simplification:** Use the existing pytest/JUnit and CI workflow. Do not add another test runner or a report format that merely restates pass/fail.

**Start from:** [conformance/test-matrix.json](../conformance/test-matrix.json), [tests/conformance](../tests/conformance), [tests/support](../tests/support), [tests/conftest.py](../tests/conftest.py), [.github/workflows/ci.yml](../.github/workflows/ci.yml), [docs/qualification.md](qualification.md).

### C25 · Qualify full-path performance and finish implementation docs

**Depends on:** C24. **Status:** proposed.

Measure the production request-to-durable-publication path: full construction, metadata-only reuse, small edits, long histories, larger-than-memory states, checkpoints, and concurrent readers/writer load. Record whole-process memory, scratch, scanned/encoded bytes, Python conversions, SQL/transaction counts, and executed/reused work. Tune the chosen implementation and complete current-behavior documentation.

**Done when:** The C01 targets pass for pinned representative inputs, packages, machine, cache, batch, and durability settings. Equivalent workloads show the intended reduction in conversions and work per change. Recheck behavior affected by tuning. Documentation distinguishes implemented, measured, and optional capabilities and links reproducible evidence.

**Simplification:** Retire superseded architecture and workflow instructions. Document the qualified implementation and its reproducible build and validation steps.

**Start from:** [docs/capacity-workloads.md](capacity-workloads.md), [docs/qualification.md](qualification.md), [docs/architecture.md](architecture.md), [docs/record-storage.md](record-storage.md), [docs/python-runs.md](python-runs.md), [README.md](../README.md).

## Coverage against the spec and plan

| Required outcome | Owning tasks |
| --- | --- |
| Core §1: explicit scope and checked conformance claims | C01, C02, C24 |
| Core §2: recoverable PROV interpretation | C02, C11, C24 |
| Core §3: occurrences, complete states, revisions, and multiplicity | C10, C12–C15 |
| Core §4: definitions, executions, bindings, contextual roles, and actual provenance | C02, C04, C11, C19 |
| Core §5: retention, availability, streaming, fusion, history, and authorized deletion | C07–C11, C14, C15, C18 |
| Core §6: selected values, adequacy, omissions, resources, and nondeterminism | C05, C14, C16, C17 |
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

Not planned: PostgreSQL, packed or remote content services beyond the existing S3 adapter, PROV export, RDF storage, Delta integration, additional value codecs, new schedulers, new source connectors, new domain processors, a native component, and authored state ordering. Any of these is a new decision with its own record if it becomes needed; nothing is prebuilt or held behind a trigger.

Legacy API support, old-format readers, and migration of existing workspaces are
excluded. Update owned consumers and fixtures directly to the new interfaces.
Changes to external repositories require their own scoped work only if consuming
their existing packages is insufficient.

Implementation tasks are complete when their evidence and retirement work are recorded.
