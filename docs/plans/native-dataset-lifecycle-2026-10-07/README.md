# Proposal: a greenfield Rust dataset engine

Date: October 7, 2026. Status: proposed architecture for a new repository; implementation, component selection, and performance qualification remain open.

Build DocSpec as an incremental columnar engine whose basic units are reusable data fragments, independently updated field groups, and durable processing results. Users should be able to add data, correct records, add columns, refresh dependent results, and publish reproducible releases while reusing qualified work. An interrupted operation should explain what completed and resume from retained results.

The proposed Rust implementation starts with an embedded engine and thin command-line and HTTP interfaces. Rust makes memory ownership and concurrency explicit. The main performance objective is to reduce the data read, decoded, rewritten, validated, and transferred for each operation.

This proposal develops the direction in [RUST-REWRITE.md](RUST-REWRITE.md). Existing DocSpec code, APIs, storage choices, identity spellings, and module boundaries are reference material. The new repository has no migration or backward-compatibility requirement. Preserve evidence and lifecycle guarantees because users need them; make any changes to specification meaning explicit before claiming conformance.

The [research requirements and acceptance map](RESEARCH.md) supplies the observed problems and qualification cases. The interviews support prioritizing reuse, recoverable phases, and visible resource use. They do not establish a speedup from replacing the current stack. The [earlier architecture review](REVIEW.md) records lifecycle decisions that this proposal carries forward; it does not qualify the proposed Rust components.

## Proposed physical model

A dataset version selects an exact, consistent combination of data, schema, evidence, and completed input coverage. Store those parts at boundaries that match how they change.

| Stored part | Proposed representation | User value and performance purpose |
| --- | --- | --- |
| Source data | Immutable fragments containing source fields, with retained original bytes where required | New records can share existing fragments; corrections preserve earlier observations. |
| Derived fields | Independently versioned groups of columns that share processing and update behavior | Add or recompute an enrichment without rewriting unrelated columns. |
| Original evidence | Immutable source objects, receipts, diagnostics, and failed-attempt records | Preserve the exact material needed to explain or reproduce a result. |
| Evidence relationships | Compact columnar records connecting values and processing results to retained evidence | Query lineage without repeatedly embedding or decoding the entire processing history. |
| Catalog metadata | Version selections, processing definitions, input coverage, phase completions, release intents, and retention roots | Resolve reusable work and commit consistent selections without per-row catalog transactions. |

For Comments, source fields and original text, normalized fields, classifications, and evidence relationships can have different update schedules. Group fields by shared processing behavior; avoid a table or metadata record for every attribute or cell. Combine groups through exact version bindings and validated correspondence between their members.

Keep business keys, source observations, immutable values, field IDs, schemas, processing definitions, and dataset versions distinct. A rename or unrelated column addition must not remint existing values. Changed values receive new identities, while older values remain available under their declared retention policy. A storage engine's row ID is an implementation aid, not a replacement for source-occurrence or evidence identity.

When many rows share an input selection, processor, or validation result, retain that shared information once. Preserve individual evidence relationships where they differ. Identical bytes can share storage while separate observations retain their meaning, order, and multiplicity. An enrichment adds its own outcome and references to prior evidence; readers must still be able to reconstruct the complete explanation.

## Proposed components and selection gates

| Component | Starting proposal | Qualification before adoption |
| --- | --- | --- |
| In-memory data | Apache Arrow batches across readers, transformations, validators, and writers | Verify required types and value distinctions, bounded batch behavior, and conversion costs. |
| Relational execution | DataFusion, extended only where DocSpec requires additional behavior | Measure representative scans, joins, aggregates, spilling, and cancellation through the complete lifecycle. |
| Native table storage | Evaluate a pinned Lance release first against an Iceberg/Parquet baseline | Compare column backfill, partial updates, delete semantics, pinned reads, history, compaction, recovery, and actual consumer compatibility. |
| Catalog | A maintained transactional database appropriate to the deployment | Prove atomic selection guards, durable acknowledgement, indexed result lookup, and recovery across the required failure domain. |
| External storage and consumers | Maintained clients and decoders behind narrow adapters; retained exports where needed | Verify exact object identity, transfer recovery, conditional publication, disclosure, and independent reading. |

[DataFusion](https://datafusion.apache.org/) provides a Rust query engine built around Arrow. Its [streaming execution interface](https://datafusion.apache.org/library-user-guide/using-the-dataframe-api.html) allows consumers to pull batches incrementally. Keep Arrow visible at the internal execution boundary so abstraction layers do not force row-object or JSON conversions. Parsing, decompression, and some transformations still allocate; measure those costs explicitly.

[Lance's table format](https://lance.org/format/table/) supports versioned fragments containing separate column files, including column backfill without rewriting existing columns. That makes it a relevant candidate for this workload. Pin and qualify the implementation and enabled format features before selection. Preserve an explicit outcome for fields awaiting backfill; storage-level nulls alone cannot distinguish incomplete processing from a legitimate null result.

The catalog owns the selection of a complete DocSpec result. A native table format may commit its own snapshots, but a table snapshot alone does not select a complete dataset and its evidence. Prepare and protect those immutable snapshots first, then atomically select their exact combination through the catalog. Reconcile prepared but unselected outputs after failure.

Use maintained components for storage, transactions, decoding, and relational execution. Keep project-owned code focused on evidence meaning, dependency-aware reuse, lifecycle decisions, and user-facing operations.

## Execution and reuse

Before opening large payloads, resolve exact input versions, material dependencies, affected rows and fields, available completed work, and the checks still required. A trusted, previously admitted immutable input can support a metadata-only reuse decision. Unknown or mutable inputs still require admission.

```mermaid
flowchart LR
    A[Resolve pinned inputs and reusable results] --> B[Plan remaining rows fields and checks]
    B --> C[Stream required column batches]
    C --> D[Write data evidence and changes]
    D --> E[Verify phase outputs]
    E --> F[Commit completed phase]
    F --> G[Continue from retained results]
    G --> H[Publish and independently verify]
```

Apply this sequence at retained phase boundaries. Transfer completion, logical validation, publication, and independent readback have different completion criteria. A completed transfer can be reused while validation remains pending.

Bind one exact input selection and one bounded read/admission context per operation. Share necessary decoding, maintained evidence-member resolution, and equivalent verification within that context. Reuse verification only when immutable data and evidence, scope, schema, validator, policy, and trust domain qualify. Preserve required independent checks across trust boundaries.

Within a phase, execute compatible operations together to reduce intermediate files and repeated scans. Retain outputs at expensive recovery boundaries. Record exact input and evidence identities, scope and order, material processor/configuration dependencies, immutable output identities, verification outcomes, and resume preconditions. Keep complete runtime provenance separately so unrelated package changes do not automatically invalidate all work.

Writers produce change information from the updates they actually apply: affected keys and fields, before/after references, and schema or evidence-only changes. Refresh planning combines those changes with old lineage and old/new relationship keys to discover affected outputs, including newly possible join matches. Recompute complete affected groups where required. A global transformation may require a full scan; the plan must expose that scope and its reason.

Use optional indexes and retained change descriptions where they reduce total cost. Include their initial construction, admission, maintenance, and recovery in the measurement. Complete states remain independently readable. Missing optional change history can require a full comparison; corrupt retained change evidence must fail verification rather than silently establish correctness.

## Commits, recovery, and resource limits

Parallel workers prepare immutable candidates. Referenced objects and required metadata must be durable before the catalog acknowledges a completed result. Short catalog transactions validate the expected output version, active processing definition, complete input coverage, and the candidate's verification outcome. Serialize conflicting selections while allowing independent preparation to continue. Backup frequency is separate from operation boundaries.

A result binds its output and covered inputs atomically. Routine refresh may publish a complete captured input selection while live sources advance, provided coverage does not regress along accepted source history. Timestamps or equal row values do not prove that ordering. Branches or incomparable input histories require explicit reconciliation. Strict schema cutover additionally establishes an exact source boundary and prevents workers using the old definition from committing afterward.

Completed restoration, transfer, processing, and validation results use the same catalog and retention rules as other results. File existence, row counts, a valid footer, or a stale running marker alone cannot establish completion. Persist structured validation outcomes before formatting optional reports. The supported inspect/resume operation reports actual consumed identities, reusable phases, remaining work, and reasons for reruns.

Share resource accounting across processing, decoding, writing, and remote requests. Bound queues by bytes, including large text and receipt batches. Coordinate concurrency with available memory and scratch space, and spill eligible operations when required. Observe whole-process memory separately from engine limits. Preflight known sizes and uncertain estimates; unknown requirements do not imply unlimited capacity. Cancellation and resource refusal preserve qualified completed phases.

Collect phase progress, rows, bytes, elapsed time, requests, memory observations, and temporary-storage high-water marks during necessary work. Keep CPU and I/O capacity shared across concurrent jobs rather than allowing each library to assume it owns the whole machine.

## Publication, history, and maintenance

An immutable release binds exact data, schema, evidence, input coverage, and disclosure policy. Reuse already available eligible objects and transfer missing ones. Retain consumer-specific exports as derived results so the same export can be reused. Determine actual consumer requirements before freezing the public format.

Record release intent and protect its objects before changing an external pointer. Use conditional updates and enforce publisher fencing at the storage boundary; reconcile publication outcomes after interruption. Resume independent readback without rewriting an already published release. Public objects must contain only bytes authorized for disclosure, including physically retained deleted or unselected rows.

History consists of pinned snapshots sharing unchanged objects. Compaction may improve layout while preserving exact logical results, source meaning, and evidence. One root inventory protects selected versions, retained inputs, completed phases, active readers, releases, and supported restore points. Distinguish application history, recovery, public-release, and optional change-history retention windows.

Expiration alone does not delete objects. Record removal intent and unavailability before deletion, prevent new references to objects being removed, and reconcile exact deletion outcomes. Restore points continue to protect their required bytes until explicitly retired. Maintenance must preserve the native format's own snapshot and object-retention requirements as well as DocSpec's roots.

## Expected lifecycle behavior

| User action | Intended work | How to check it |
| --- | --- | --- |
| Add comments | Process new inputs and affected dependencies; share existing fragments | Compare the complete result with independent reconstruction and measure unchanged bytes reused. |
| Correct comments | Update affected data, evidence, joins, and summaries | Check removals, new matches, group results, and preserved earlier versions. |
| Add an enrichment column | Read required inputs and write the new field group | Verify coverage, existing-column reuse, and the distinction between pending work and null values. |
| Change evidence only | Update evidence and affected qualifications; reuse eligible values | Verify new evidence bindings and explain which computation or checks rerun. |
| Retry after transfer | Resume from the durable transfer result | Inject failure after transfer and verify completed transfer does not repeat. |
| Read an older version | Open its pinned snapshot directly | Independently reconstruct its values, schema, membership, and evidence. |
| Publish a small update | Transfer missing eligible objects and verify the release | Measure transfer and readback work; test interruption around pointer publication. |
| Compact or expire history | Improve physical cost while honoring every retained root | Verify equivalent reads and refusal to delete required bytes. |

## Repository boundaries and requirement ownership

Start with an embedded engine and thin application entry points. Keep processing stages in one process where practical. Use these responsibility boundaries to guide modules and crates; split crates only when independent ownership or dependency control warrants it.

| Boundary | Responsibility |
| --- | --- |
| Model | Identities, schemas, evidence meaning, and reuse rules |
| Catalog | Atomic selections, completed phases, coverage, and retention roots |
| Storage | Native table format, immutable objects, and pinned readers |
| Engine | Planning, batch execution, and shared resource accounting |
| Application | Command-line and HTTP interfaces, progress, inspect, resume, and publication |

Give genuinely shared rules one implementation: typed encoding, identity admission, completed-result resolution, promotion checks, object-reference verification, and reachability. Keep source policy, backfill planning, affected-output discovery, and consumer distributions focused. Assess a maintained executor if deployment requirements justify scheduling beyond the embedded engine.

The linked lifecycle plans retain their requirement and task IDs. Their product guarantees and acceptance criteria apply to the Rust proposal; references to current implementations provide evidence rather than required module paths.

| Requirement | Detailed plan | Rust proposal responsibility and user outcome |
| --- | --- | --- |
| R1 Durable data and commits | [Durability](01-durable-storage.md) | Catalog and storage retain acknowledged updates and completed phases through worker loss. |
| R2 Incremental publication | [Publication](02-incremental-publication.md) | Storage and application services publish consistent releases with qualified object reuse. |
| R3 Schema evolution and backfill | [Schema evolution](03-schema-backfill.md) | Model and engine evolve field groups without rebuilding unrelated values or interrupting readers. |
| R4 Verified changes | [Change tracking](04-change-tracking.md) | Writers and the catalog retain exact changes without repeated full comparisons. |
| R5 Dependent refresh | [Refresh](05-dependent-refresh.md) | The planner updates affected joins and summaries while inputs continue to advance. |
| R6 Maintenance and retention | [Maintenance](06-maintenance-retention.md) | Storage and the catalog control physical cost while honoring history and recovery promises. |
| R7 Operations and qualification | [Qualification](07-operations-qualification.md) | Engine and application services expose resource use, explain reuse, and support recovery. |

## Delivery and acceptance

Start with the retained Comments fixture and a complete path through preparation, validation, publication, and independent readback. Define identity, field groups, phase completion, reuse dependencies, selection guards, and retention semantics before implementation. Select pinned components through bounded probes of this path; settle consumer requirements before format freeze.

1. **Qualify the recoverable Comments path.** Deliver the relevant R1 and R2 work, minimum R6 protection, and R7 progress, inspect/resume, fault injection, and measurement. Prove reuse after expensive phases and on unchanged input. Full schema-backfill delivery is not a prerequisite for this fixed-schema path.
2. **Reduce measured repeated work and qualify incremental updates.** Use stage counters to prioritize R4 and R5. Measure affected-row discovery, repeated decoding and validation, partial writes, evidence storage, and publication reuse. Compare ordered streaming, selective access, and indexes on eligible workloads.
3. **Expand schema evolution and maintenance.** Deliver broader R3 backfill and cutover, enrichment composition, required consumer distributions, and measured R6 compaction and retention improvements. Preserve the first path's recovery guarantees.

Assign an integrating owner for catalog transactions, phase-result and reuse rules, identity and schema semantics, release roots, and retention. Deliver applicable slices of R1-T01–T05, R2-T01–T05, R6-T01/T04–T05, and R7-T01–T06 for the first path, together with foundational R3-T01 identity work. Each slice must satisfy its applicable correctness and failure checks.

Correctness requires exact agreement with independent reconstruction for values, keys, types, null/missing/empty distinctions, multiplicity, applicable ordering, schema meaning, and evidence. Preserve original values, selected generations, ordered duplicates, receipt history, explicit missing/refused outcomes, and failed attempts. Verify that accepted updates survive, freshness claims match coverage, stale definitions cannot be promoted, and required bytes remain retained.

Required recovery cases include failure after qualified receipt transfer, failure while formatting a report after validation, a trusted unchanged-input rerun, a receipt-only change, and interruption after public write but before readback completes. Completed eligible phases must be reused; incomplete or invalidated checks must run.

The [adversarial qualification cases](07-operations-qualification.md#adversarial-qualification-cases) define concrete failure sequences, passing results, and owner tasks. Run the applicable memory, concurrency, and cleanup probes before component/layout selection; qualify the complete owned behavior before claiming support. The [independent review](ADVERSARIAL-REVIEW.md) records the rationale.

Hold a small change set fixed while increasing dataset size and retained history. Measure selection, decoding, and validation passes; bytes scanned, written, and transferred; requests; cold admission and index construction; catalog commit cost; reader verification; whole-process and engine memory; scratch-space high-water marks; recovery work; and total time to a verified release. Include cold, warm, unchanged, and resumed operations. Retain failed runs and explain unavoidable full-scope work. Proposed improvements remain hypotheses until measured on equivalent inputs and guarantees.

## Decisions still required

Choose the storage format, catalog deployment, partition and field-group layout, and dependency/index granularity from the qualification results. Establish product targets for consumers, acknowledgement delay, freshness, maximum backfill lag, recovery time and loss tolerance, resource and cost limits, historical-byte disclosure, and separate retention windows. Worker-loss durability is required; qualify broader failure domains against the agreed targets.

General incremental SQL, automatic failover, universal indexes, and automatic orphan sweeping require a demonstrated need before expanding scope. New-repository development remains free to replace prior implementation choices while preserving the accepted evidence and lifecycle guarantees.
