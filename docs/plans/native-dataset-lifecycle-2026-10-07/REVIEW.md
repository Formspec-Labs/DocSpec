# Review of the greenfield dataset lifecycle design

**Approve the revised planning direction.** The plans apply the performance, efficiency/reuse, flexibility, and practitioner-research recommendations while retaining the requested greenfield freedom. The first delivery now proves phase reuse, recovery, and operator visibility through one complete Comments path. Runtime behavior and measured performance remain unqualified.

## Findings and dispositions

| Finding | Disposition and acceptance |
| --- | --- |
| Final operation durability alone does not protect expensive completed intermediate work. | **Reshaped in R1/R2/R5/R6.** Ordinary retained phase results survive parent/report failure and support exact resume. Transfer completion is distinct from validation/publication; tests require no repeated qualified transfer or matching work after late failure. |
| Repeated selection, decoding, and equivalent admission can dominate a small change. | **Reshaped in R1/R4/R5.** Share one bounded exact-input read/admission context per operation and check completed work before staging. Required independent checks remain; count actual passes and distinguish scope/trust changes. |
| Dataset status can leave phase progress, resource pressure, and retry work unexplained. | **Reshaped in R7.** Emit phase rows/bytes, timing, engine/process memory and scratch during necessary work; preflight shared capacity; show qualified reuse, next action, and rerun reasons without another scan. |
| Broad implementation identity can invalidate unrelated work or hide receipt-only changes. | **Reshaped in R1/R3/R4/R5.** Explicit material dependencies control reuse; full runtime provenance stays available. Receipt-only updates preserve changed evidence and rerun affected checks without unnecessary value recomputation. |
| Bespoke format assumptions and report formatting can cause avoidable recovery work. | **Reshaped in R1/R2/R7.** Maintained decoders/member helpers are shared by production commands. Persist structured validation and protect bytes before optional rendering; independent test expectations remain separate. |
| Research includes fixed cases and unverified gaps, so it cannot justify replacing every existing mechanism. | **Recorded in RESEARCH.** Use historical successes/failures as acceptance examples. Preserve the scorecard correction and term-resume uncertainty; existing code remains optional and no runtime result is inferred. |
| Full-ledger backup coupled to every operation can dominate small updates. | **Reshaped in R1.** A maintained durable catalog commits operation completion; recovery backups have an independent policy. Optional bounded group commit must justify itself with measured acknowledgement cost. No acknowledgement precedes durable objects and metadata. |
| Requiring every input to remain latest can prevent a busy dataset from publishing. | **Reshaped in R5.** Routine promotion accepts complete consistent captured versions with nonregressing coverage and visible lag. Strict cutover/latest policies retain live-head checks. Continuous-ingestion qualification tests progress and later convergence. |
| Global schema/definition identity can force unrelated value churn. | **Reshaped in R3.** Stable field IDs and separate observation/value/schema/definition identities permit explicit metadata evolution and dependency-based reuse. Changed values never overwrite old occurrences; computed coverage remains explicit. |
| Adding an independent enrichment to a wide base table can rebuild unrelated data. | **Reshaped in R3/R5.** Group fields by processing/update behavior; version enrichments independently and compare pinned join/materialization cost with avoided rewriting. Exact source bindings or qualified correspondence prevent stale matches. |
| Native-only or Python-only publication can miss actual consumer needs. | **Reshaped in R2.** Consumer requirements precede format freeze. Canonical immutable releases support thin readers or derived distributions; no competing mutable data authority. |
| Physical reuse can disclose rows excluded from a logical view. | **Kept and strengthened in R2/R6.** Authorize all bytes, revalidate policy changes, compare disclosure layouts, and explicitly materialize when required. A later release cannot revoke bytes already disclosed. |
| An isolated change-discovery win can hide scans, commits, or publication costs. | **Reshaped in R4/R7.** Early probes and a complete update-to-independent-reader path drive decisions. Fixed-size changes are measured over growing population and history. No inherited relative percentage establishes lifecycle success. |
| Reusing current owners can become an accidental restriction on greenfield design. | **Replaced throughout.** Share semantic rules and select maintained components because they fit requirements. Existing APIs, adapters, identity formats, scheduler, and global single-writer topology are not mandatory. |
| Retention timers can conflict with required inputs or supported restore points. | **Kept and strengthened in R6.** Separate policy windows and one complete root inventory. Nominal expiry does not authorize deletion; catalog recovery and object availability have a common support boundary. |
| Additional generic infrastructure can increase complexity without user value. | **Deferred.** General incremental SQL, universal indexes, automatic failover, and orphan sweeping need measured requirements. Use focused services and maintained infrastructure first. |

## Artifact summary and lineage

The artifacts are [README](README.md), the linked R1–R7 plans, and the [research requirements and acceptance map](RESEARCH.md). They cover native source capture through durable phases/commits, schema evolution, dependent processing, public reading, and maintenance. They are product and operations plans supported by qualification tasks.

The accepted review called for bounded durability cost, consistent captured refreshes, independent enrichments, shared semantics, consumer-led publication, and early end-to-end evidence. The subsequent greenfield instruction supersedes implementation-preserving defaults in the earlier plans. Task IDs remain planning references; no migration work or legacy compatibility layer is introduced.

The practitioner research changes priority and acceptance: first qualify exact selected-input reuse, durable expensive phases, supported resume, authoritative decoding, and phase/resource visibility on a complete fixed-schema Comments path. Then remove measured duplicate work and qualify incremental refresh; broader evolution and maintenance follow. Foundational identity/schema and minimum retention remain prerequisites. The interview timings are historical incident evidence, not controlled baselines or measured gains for this design.

| Prior material | Relationship to this design |
| --- | --- |
| [Practitioner findings](/Users/mikewolfd/Work/corpora/session-reports-2026-10-07/data-processing-interviews/FINDINGS.md) and [follow-up corrections](/Users/mikewolfd/Work/corpora/session-reports-2026-10-07/data-processing-interviews/followups.json) | Supply concrete phase loss, repeated work, observability, identity, and verifier cases; distinguish fixed examples from unverified capability gaps. |
| [Core model scope](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/core-model.md:24) | Leaves storage, transport, scheduling, and correspondence mechanisms open; does not require the current local assembly. |
| [Complete states and immutable occurrences](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/core-model.md:60) | Product guarantees retained: recover exact meaning, distinguish observations from equal values, and never mutate an old occurrence. |
| [Retention obligations](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/core-model.md:150) | Required inputs and outputs remain available while their retention claim stands. A schema composition or narrow input binding must describe its true scope. |
| [Dependency-based correspondence](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/core-model.md:251) | Supports reuse when material dependencies correspond; a global table/schema change alone must not dictate all recomputation. |
| [Current architecture](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/architecture.md) and source revision `4ce53d5a7aace2b8da52345bf799a22198559162` | Informative comparison only. Current SQLite backup strategy, API shapes, table identity rules, and scheduler ownership do not constrain the greenfield implementation. |

## Relationships and responsibilities

| Responsibility | Authority | Consumers |
| --- | --- | --- |
| Durable versions, phase/results, coverage, guarded selections | One transactional catalog and commit service | Processing workers, backfill, refresh, release preparation, operations |
| Exact selected-input reuse and production interpretation | Shared bounded read/admission context and maintained decoding/member helpers | Processing, change comparison, validation, resume, and status; independent test oracles remain separate |
| Immutable source/value/table bytes | Selected maintained storage/table components | Pinned readers, computation, publication, maintenance |
| Schema and material-dependency meaning | Versioned metadata and focused domain rules in the same catalog | Evolution, change detection, reuse, readers |
| Public distribution and pointer | Immutable release root with durable catalog intent and external pointer reconciliation | Actual application/browser/analysis/download consumers |
| Physical protection and removal | Shared root inventory and deletion service | Every retained result, release, recovery point, reader, and in-flight operation |
| Scheduling | Selected executor, if needed | Bounded workers; it cannot override committed dataset truth |

No component may introduce a second authoritative current head for the same dataset. Table-engine roots are pinned by the committed version. External public pointers remain separate observable effects, with explicit reconciliation rather than a fictitious cross-service transaction.

## Invariants and user value

**Preserved:** complete recoverable states; exact source evidence; identity distinct from value equality; correct required-input retention; independently readable pinned releases; no premature acknowledgement; no unsupported freshness or reuse claim.

**Newly specified:** stable field IDs; explicit value-group and material-dependency identity; independently retained phase completions; qualified selected-input/validation reuse; durable commits independent of backup frequency; accepted-source nonregression; policy-specific promotion; atomic definition cutover; restore/object support alignment; observable phase progress and usable resume.

**Deliberately replaced:** mandatory reuse of current modules, whole-schema identity invalidation, fixed local-ledger recovery machinery, Python-first consumer assumptions, and a global single-writer requirement. These were implementation choices rather than necessary user outcomes.

Operators gain recoverable, inspectable phase progress and explicit next actions; consumers gain complete reproducible releases; producers can change fields without automatically rebuilding unrelated values. New conceptual cost is bounded to schema/value/dependency distinctions, ordinary phase/results and coverage/order evidence, and shared commit/release/retention rules. These distinctions prevent false reuse, repeated expensive work, stale publication, and lost history without another cache or workflow authority.

## Counterfactuals and qualification gates

A full-rebuild/full-export design is simpler and remains the correctness baseline; choose it for a workload if measurements show incremental complexity has no useful payoff. Removing optional change history should affect speed, not correctness. Removing required coverage or ordering evidence must prevent unsupported promotion rather than silently guess.

A single wide table may win when fields always change together and joins cost more than avoided work; independent field groups are a measured boundary, not mandatory per-column normalization. A local embedded catalog may fit a local product, but cannot inherit service durability without qualifying the required failure domains. Parallel workers do not remove atomic commit or fencing requirements.

The design fails if a late/reporting failure discards qualified work, an unchanged trusted operation unnecessarily restages its payload, operators cannot identify the next supported action, fixed-size changes become unaffordable as history grows, continuous ingestion prevents progress, schema metadata falsely implies computation, consumers cannot read the format, or recovery/deletion breaks retained content. These observations require reshaping the responsible mechanism, not adding a second authority. An index must repay cold construction/admission and recovery costs; a warm lookup win alone is insufficient.

Open gates are numeric product targets, actual consumers, maintained-component selection, identity/schema conformance, portable admission, bounded group evidence, provider behavior, disclosure, and retention durations. R7 starts evidence collection before expensive decisions are fixed and completes qualification on the assembled system.

## Verdict

**APPROVE as a greenfield planning package.** Intent and design align; required user guarantees are preserved while implementation constraints are removed. Reuse is justified by common meaning and maintained capabilities. Specific component selection and API signatures remain implementation decisions backed by early probes.

Confidence is high in cross-plan consistency and the stated tradeoffs. Performance magnitude, provider guarantees, identity/schema representation details, and operating limits require implementation and independent qualification. This review does not certify implementation, deployment, or public availability.
