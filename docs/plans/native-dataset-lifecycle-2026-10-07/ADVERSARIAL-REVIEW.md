# Independent adversarial review of the Rust proposal

**Verdict: APPROVE the architecture as a proposal and qualification plan.** The independent reviewers found no material blocker or contradiction after tracing the README through the lifecycle plans and Core guarantees. Keep the design direction and make the qualification cases below explicit. This review does not establish implementation correctness, component compatibility, capacity, or production readiness.

Reviewed on October 7, 2026, against DocSpec commit `40e891ccece79495f7ab850d441ed8633be73449`. The target is [README.md](README.md). The independent reports assess that unchanged baseline. No benchmarks or runtime tests were executed.

**Integration follow-up, October 7, 2026:** The accepted scenarios now have sequences, passing criteria, owners, and decision gates in [R7's adversarial qualification cases](07-operations-qualification.md#adversarial-qualification-cases). Existing R1–R6 tasks link to those shared cases, and the README points to the acceptance work. The Rust-specific attribution sentence has been corrected in RUST-REWRITE.md and R7. This integrates requirements into the plan; it does not complete the tests or extend the reviewers' verdict to an implementation. The independent reports and their recorded hashes remain unchanged.

## Findings and disposition

The strongest residual risks are already assigned to R1–R7. They remain requirements to prove before adopting components or making operational claims. A missing implementation or an explicitly deferred product target is not, by itself, an architectural defect.

| Review | Independent verdict | Main conclusion |
| --- | --- | --- |
| [Consistency and recovery](reviews/rust-2026-10-07/consistency.md) — Martin Kleppmann–inspired conceptual lens | APPROVE | Atomic selection, honest reuse, captured-input progress, external fencing, and safe removal are coherent. Add precise cases for dependency omissions, related input versions, and delayed publishers. |
| [Execution, storage, and scaling](reviews/rust-2026-10-07/performance.md) — columnar database and Rust execution architect | APPROVE | The plan already measures the important performance costs. Exercise allocations outside the query engine, native API concurrency, and metadata/evidence growth. |
| [Reliability and integration](reviews/rust-2026-10-07/operations.md) — reliability and integration architect | APPROVE | The main crash, publication, retention, and consumer hazards are covered. Qualify their combination against the actual native storage and provider mechanisms. |

No reviewer recommended replacing the catalog, adding a transaction coordinator, building a scheduler, or reversing the field-group direction. No BLOCKER or CONCERN survived their source traces. The wording finding was optional; the other recommendations sharpened existing acceptance work. Both kinds of recommendation are now integrated into the plan.

### Integrated correction: attribute measured gains to the assembled design

At the reviewed commit, RUST-REWRITE.md:134 suggested the first lifecycle comparison could show Rust's additional execution gains. That experiment changes storage layout, query engine, evidence representation, reuse, and recovery together. A faster result can establish the value of the assembled design, but cannot isolate the programming language's contribution.

The corrected [RUST-REWRITE.md:134](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/RUST-REWRITE.md:134) attributes the first experiment to the assembled design and requires equivalent work for a separate Rust-specific comparison. [R7 performance acceptance](07-operations-qualification.md#performance-acceptance) now states the same attribution rule.

## Qualification cases accepted for integration

This table records the review recommendations. [R7](07-operations-qualification.md#adversarial-qualification-cases) owns their maintained acceptance criteria; the linked task plans own implementation.

| Case and existing owner | Adversarial sequence | Passing result |
| --- | --- | --- |
| **Shared memory accounting** — R7-T01/T04/T06, R1 preflight | Run a narrow branch retaining slices of wide text batches while receipt decoding, validation, compression, and a slow writer run concurrently. Include an individual value larger than the normal batch allowance. | Complete within the declared supported limits or apply a supported refusal/backpressure path. Preserve completed phases. Distinguish engine reservations, retained buffers, and whole-process memory; a small engine reservation alone does not pass. |
| **Native field-group concurrency** — R1-T01, R3-T01/T07, R7 probes | Ingest new comments while separate enrichments backfill, a correction changes material inputs, and compaction moves physical rows. Compare same-table column attachment, separate field groups, and a wide-table baseline. | Values, occurrence correspondence, coverage, and evidence remain exact. Retried conflicts do not repeatedly discard qualified expensive work. Count rewrite, join, export, and maintenance costs before selecting the layout. |
| **Metadata and evidence scaling** — R2-T06, R4-T06, R6-T07, R7-T06 | Hold logical data and update size fixed while independently increasing fragment count, retained versions, phase results, and explanation depth. | Opening, reuse lookup, commit, explanation, retention preview, and retirement meet preregistered targets. Include metadata bytes and requests, not only changed payload. Explain protected storage that cannot yet be reclaimed. |
| **Native cleanup and retained roots** — R1-T01/T05, R6-T01/T04/T05/T07 | Keep an old reader, prepared unselected snapshot, completed failed-parent phase, release, and restore point active while exercising every supported native cleanup route. | Every promised root remains readable. Native deletion cannot bypass DocSpec protection. Removing the final applicable root permits exact reclamation with truthful availability. |
| **Discovered dependency omission** — R3-T01/T07, R7-T04 | Resolve reuse of a classification that declared only text as material. Discover that language was also material, record the omission, then resume the pending adoption. | Refuse reuse that ignores the omission. Preserve the original declaration and execution. Adequate supplemental evidence can qualify a new association; unknown historical input stays unknown and unaffected work remains eligible. |
| **Related input consistency** — R2-T01, R5-T01/T05 | A producer declares Comments C2 and Dockets D2 as one related generation. Try promoting C2/D1 from prior coverage C1/D1. Both components are nonregressing, but the combination violates that relationship. | Refuse C2/D1 under the coupled-source rule and accept C2/D2. Under explicitly independent-source rules, an admissible mixed vector can pass while reporting exact pins. Full reconstruction of an inadmissible vector cannot make it eligible. |
| **Delayed authority and restore** — R2-T04, R1-T04/T06, R7-T05 | Pause publisher A after intent under its authority epoch. Hand authority to B without changing the pointer, then resume A with its still-matching pointer token. Also restore an advertised catalog point while an old publisher/deleter remains alive. | A cannot write or relabel its old attempt with B's epoch. Recovery enforces current authority, reconciles exact external effects, and resumes readback after a confirmed write. Restored metadata never falsely promises deleted bytes. Qualify the actual provider. |
| **Complete consumer path** — R2-T01/T05/T06, R7-T06 | Keep a consumer on an old pin while publishing corrections, deletions, and evidence-only changes. Include any required flat export or search distribution. | Each read stays within one data/schema/evidence selection. Include export, transfer, and independent readback in latency/cost. A fast native update with an unaffordable distribution does not pass the consumer target. |

These cases make the existing criteria falsifiable; they do not add new infrastructure or require invented numeric targets. The first fixed-schema Comments fixture can proceed while later evolution cases are prepared. Relevant targets and bounded component probes must precede the decisions they support, as [R7's decision gates](07-operations-qualification.md#remaining-product-decisions) require.

## Component facts independently checked

The coordinator checked the reviewers' key external claims against primary documentation. The versions below are evidence examples, not a selected or mutually qualified dependency combination.

- DataFusion execution 55.1.0 documents that its memory pool does not track ordinary flowing batches or all source-operator allocations. The shared process budget therefore needs controls and observations beyond the engine's reported reservation. [MemoryPool API](https://docs.rs/datafusion-execution/55.1.0/datafusion_execution/memory_pool/trait.MemoryPool.html).
- Arrow array 60.0.0 documents that batch memory-size reporting can count shared buffers more than once. Distinguish retained backing allocations from logical selected bytes and observed process memory. [RecordBatch API](https://docs.rs/arrow-array/60.0.0/arrow_array/struct.RecordBatch.html#method.get_array_memory_size).
- Lance 12.0.0 documents schema-evolution operations that conflict with most concurrent operations. Separate column files establish a useful capability; the independent backfill lifecycle still needs a pinned API/layout test. [Dataset schema evolution](https://docs.rs/lance/12.0.0/lance/dataset/struct.Dataset.html#schema-evolution).
- Lance's maintained guide describes cleanup that removes old version metadata and unreferenced data, including an aggressive mode whose safety depends on concurrent work. That reinforces the existing requirement to qualify native maintenance against DocSpec's retained roots. [Lance read/write and cleanup guide](https://lance.org/guide/read_and_write/#cleanup-old-versions).

## Lineage and relationships

| Artifact or boundary | Role in the conclusion |
| --- | --- |
| [Rust rationale](RUST-REWRITE.md) and prior lifecycle README at `815b8b6` | Establish the direction and inherited lifecycle rules. Rust and storage choices remain hypotheses. |
| [Research requirements](RESEARCH.md) | Support phase reuse, reliable recovery, and operational visibility. Historical incident timings do not prove a rewrite speedup. |
| [Core model](../../core-model.md) | Defines occurrence identity, retained input scope, adequate dependencies, honest reuse associations, and logical/physical separation; physical technology remains open. |
| [R1](01-durable-storage.md) and [R3](03-schema-backfill.md) | Own completion, dependency meaning, identity, protection before object creation, and guarded selection. |
| [R4](04-change-tracking.md) and [R5](05-dependent-refresh.md) | Own exact changes, bounded affected-output discovery, related-input checks, and output/coverage selection without a second cursor. |
| [R2](02-incremental-publication.md) and [R6](06-maintenance-retention.md) | Own public effects and disclosure, native/consumer object lifetime, removal reservations, and restore support. |
| [R7](07-operations-qualification.md) | Owns shared acceptance, real-provider checks, product limits, and full-path cost evidence. |
| [Historical review](REVIEW.md) | Supplies decision lineage after independent hypotheses; its earlier approval is not evidence of Rust component readiness. |

Current Engine and SpicyRegs consumers were used as behavioral examples, not compatibility restrictions: exact source references matter for original-record lookup, and query/download readers must keep data and receipts under one captured generation. Those examples support the existing consumer matrix and independent-readback gates. See [Engine originals](/Users/mikewolfd/Work/spicy-stack/spicyengine/docs/prepared-search.md:236) and [SpicyRegs readers](/Users/mikewolfd/Work/spicy-stack/spicy-regs/docs/generation-publication.md:85).

## Invariants to retain

| Invariant | Review disposition and evidence |
| --- | --- |
| Equal content does not collapse distinct observations; changes do not overwrite earlier occurrences | PRESERVED — [Core:60](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/core-model.md:60), [R3:15](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/03-schema-backfill.md:15). |
| One catalog selects the complete result and its coverage; native table heads do not compete | PRESERVED — [R1:17](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/01-durable-storage.md:17), [R5:13](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/05-dependent-refresh.md:13). |
| Completion, current availability, reuse eligibility, and public status remain distinct | PRESERVED — [Core:175](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/core-model.md:175), [R1:29](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/01-durable-storage.md:29). |
| Required input bindings determine retention; narrower processing dependencies do not silently reduce it | RELIED-UPON — [Core:152](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/core-model.md:152), [R6:23](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/06-maintenance-retention.md:23). |
| Optional change history can expire without making a complete state unreadable | PRESERVED — [R4:23](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/04-change-tracking.md:23). |
| Public effects require durable intent and actual authority enforcement; deletion respects every retained root | PRESERVED — [R2:27](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/02-incremental-publication.md:27), [R6:25](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/06-maintenance-retention.md:25). |

No broken invariant was established. Exact representation and provider enforcement remain to be implemented and qualified.

## User value and counterfactuals

The strongest supported benefit is recoverable progress: a completed transfer, processing result, or validation survives a later failure, and the operator can identify the next action. Field groups, compact evidence relationships, and incremental updates offer further savings only if avoided writing outweighs composition, metadata, validation, maintenance, and distribution costs.

A wide table with full rebuilding and durable expensive phases remains a valid smaller comparison arm. Remove optional change acceleration and correctness should survive through full comparison. Remove retained phase results and the first recovery promise fails. Remove complete selection or required evidence and reproducibility fails. This distinction identifies the essential work without requiring every optimization.

Reject a selected component or mechanism if it cannot preserve required meaning, retained roots, or external authority. Reshape an optimization if fixed-size changes exceed product targets as data, fragments, evidence, or history grow. Reporting fewer rewritten bytes cannot rebut a slower or less reliable complete consumer path.

The review supports continuing the bounded qualification program. It does not support declaring the implementation bulletproof, freezing the component combination, or claiming a numerical speedup.

## Independence, records, and limits

Each reviewer started with a fresh context containing the target, scope, persona, and review method. No author conversation or peer findings were supplied. Reviewers recorded their hypotheses before reading historical review conclusions. They shared primary artifacts and could inspect the older review for lineage, so this was independent review with controlled context, not anonymization of the source material. The consistency reviewer used a conceptual lens inspired by Martin Kleppmann; this is not his participation or endorsement.

The coordinator read every report, checked the cited plan requirements, verified representative consumer evidence and the key external component claims, and accepted the unanimous verdict without elevating existing qualification work into new blockers. Reports were retained with only scratch-directory references adjusted; findings and verdicts were unchanged. [Review record and hashes](reviews/rust-2026-10-07/review-record.json) retain that distinction. Initial hypothesis logs are saved beside each report.

Confidence is high in the traced consistency of the written proposal and its linked requirements. Component feasibility, exact memory behavior, operating limits, consumer support, and performance remain unqualified. Static adversarial review supplies failure cases and checks; only implementation evidence can establish those runtime claims.
