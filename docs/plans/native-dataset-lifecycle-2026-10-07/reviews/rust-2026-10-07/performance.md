# Independent adversarial review: execution, storage, and scaling

**Verdict: APPROVE the proposed architecture as a qualification program. No blocker or material contradiction was established.** This verdict does not select Rust components, establish capacity, or authorize a broad rewrite. The package explicitly reserves those decisions for pinned component probes and full-path measurements. The most important risks are outside a query engine's normal memory accounting, in native-table concurrency and maintenance, and in metadata/evidence growth rather than transformation throughput. Versioned external APIs below are evidence examples, not a recommended or qualified dependency tuple.

Reviewed statically at DocSpec commit `40e891ccece79495f7ab850d441ed8633be73449`. No benchmarks, repository edits, external messages, or implementation tests were performed. I did not read other reviewers' reports or receive author discussion. Initial hypotheses were recorded in [performance-hypotheses.md](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/reviews/rust-2026-10-07/performance-hypotheses.md) before reading historical `REVIEW.md` conclusions.

## Findings first

### F1 — Make the resource probe exercise retained buffers and allocation outside DataFusion

**Severity: OBSERVATION. Category: named-seam / maintained-component fit. Recommendation: KEEP the shared resource owner; sharpen its qualification case.** This is an existing gate to satisfy, not a missing decision that blocks the proposal.

**Evidence.** [README.md:35](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/README.md:35) proposes Arrow at reader/transform/validator/writer boundaries; [README.md:41](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/README.md:41) favors shared buffers; [README.md:82](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/README.md:82) requires byte-bounded queues and whole-process observation. DataFusion execution **55.1.0** documents that its memory pool excludes ordinary flowing batches and `DataSourceExec` internal allocations; it also documents operators that refuse rather than spill. Sharing its pool is useful, but cannot alone enforce the proposed process budget. [Versioned MemoryPool API](https://docs.rs/datafusion-execution/55.1.0/datafusion_execution/memory_pool/trait.MemoryPool.html).

Arrow array **60.0.0** documents that batch memory-size reporting can overcount shared buffers. Logical selected bytes, retained backing allocations, and process resident memory therefore need distinct interpretations. This is an accounting limitation, not evidence that sharing buffers is wrong. [Versioned RecordBatch API](https://docs.rs/arrow-array/60.0.0/arrow_array/struct.RecordBatch.html#method.get_array_memory_size).

**Counterexample.** A narrow classification branch retains slices from a wide text batch while a slow receipt writer lags behind. Decoding, compression, upload buffers, queued batches, and the relational engine all consume memory concurrently. A queue limited by logical selected text size can still keep larger backing allocations alive. A single admitted text/evidence value can also exceed the intended batch allowance before splitting by row count helps. This is a falsifiable workload, not an observed failure of the proposed implementation.

**What the plans already cover.** R1 preflight explicitly distinguishes engine and process limits ([R1:59](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/01-durable-storage.md:59)); R7 requires maintained admission/backpressure and uncertainty ([R7:21](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/07-operations-qualification.md:21)); wide text, nesting, memory, spill, and failures are already qualification inputs ([R7:81-85](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/07-operations-qualification.md:81)). The proposal does not assert that a DataFusion pool is a process memory limit.

**Smallest refinement.** In R7-T01/T06's probe specification, name ownership and charging for retained backing buffers, decoder/writer caches, fan-out, and oversized individual values. Select maintained controls first. Use an explicit supported-size/refusal rule or a bounded external-object path where a value cannot safely materialize; do not create a custom allocator or scheduler by default.

**Falsifiable acceptance.** Under preregistered process/scratch limits, run concurrent scan, validation, slow writing/upload, and cancellation with skewed values and an individual value beyond the normal batch budget. Either complete within the declared supported budget or refuse/backpressure before uncontrolled growth; retain completed phases and return the exact reason. Record engine reservations, retained queue allocations where measurable, and whole-process high water separately. An engine pool staying below its limit while the process exceeds its accepted limit fails this gate.

### F2 — Lance column backfill is a candidate capability, not the entire independent-field lifecycle

**Severity: OBSERVATION. Category: maintained-component fit / scope. Recommendation: KEEP Lance as a first candidate and keep the baseline; qualify the exact API/feature combination.** No unsupported implementation claim is made in the README.

**Evidence.** [README.md:37-45](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/README.md:37) correctly separates format capability, pinned implementation selection, and DocSpec's complete-result authority. The [Lance table specification](https://lance.org/format/table/) describes separate column files within fragments. The **12.0.0** Rust `Dataset` documentation exposes column addition, exact-version checkout, detached commits, and native table-provider integration. Its schema-evolution section also states that schema operations conflict with most concurrent operations and should run without other writes. These documented APIs establish useful maintained building blocks, not proof that an unselected release satisfies DocSpec's concurrency or independent group semantics. [Versioned Dataset API](https://docs.rs/lance/12.0.0/lance/dataset/struct.Dataset.html#schema-evolution).

**Counterexample.** While comments continue to arrive, two independent enrichments backfill different field groups, a source correction changes one enrichment's material input, and compaction changes physical row placement. Cheap column attachment alone does not demonstrate independent progress, exact occurrence correspondence, guarded group selection, or retained old readers. Physical row alignment must not become the source-occurrence authority.

**What the plans already cover.** R3 requires explicit source bindings, correspondence, combined-view cost, bounded backfill, and strict cutover ([R3:33-47](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/03-schema-backfill.md:33)); R7 already schedules early evolution/composition probes before component selection ([R7:17](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/07-operations-qualification.md:17)); native retention requirements remain binding ([README.md:94](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/README.md:94)). Full R3 delivery can follow the first fixed-schema path without postponing the component-fit probe.

**Smallest refinement.** Attach a pinned capability matrix to the storage probe: Rust/Lance/DataFusion/Arrow versions, enabled table features, exact public APIs, conflict behavior, object-retention controls, and supported readers. Compare independently selected field-group tables with native same-table column attachment and an equivalent wide-table baseline. Do not assume these layouts have identical update or read costs. Reuse maintained table/query integration before implementing a second query adapter or manifest system.

**Falsifiable acceptance.** Exercise the counterexample with exact reconstruction of old/new values, coverage, duplicate occurrences, and evidence. Count conflict retries, bytes rewritten, join/materialization reads, compaction work, and candidate retention. A layout fails if independent preparation must repeatedly restart expensive work, source correction attaches stale enrichment, or compatible readers cannot read the selected features within product targets. Reject the particular layout/component if it fails; the architecture permits that result.

### F3 — Qualify fragment count, evidence depth, and retained history as separate dimensions

**Severity: OBSERVATION. Category: algorithmic scope / metadata amplification. Recommendation: KEEP the small catalog and shared objects; make the scaling axes explicit in probe receipts.** The package already rejects a payload-only performance win.

**Evidence.** [README.md:22-29](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/README.md:22) puts evidence relationships in columnar storage and shared definitions outside per-row catalog transactions. [README.md:92](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/README.md:92) shares historical objects. Neither property by itself bounds manifest traversal, current-result lookup, complete explanation traversal, or deletion planning.

The current Lance **format documentation**, not a selected runtime, says flat manifests carry the complete fragment list and both opening and committing process it. Its fragment-tree specification offers a different representation with feature flags and reader requirements. That specification is not proof that the eventual pinned implementation enables it or that every promised reader supports it. [Lance fragment metadata specification](https://lance.org/format/table/fragment_metadata/).

**Counterexample.** Keep total payload bytes and one-row update size fixed but distribute those bytes across progressively more tiny fragments. Separately, hold the selected values fixed while generating many evidence-only phase results and retained versions. In the flat-manifest case, a complete F-fragment inventory retained across H versions can accumulate work/storage proportional to F × H even if payload deltas are tiny. This is an inference from the representation, not a measurement of Lance or DocSpec. Shared objects do not erase the cost of repeatedly enumerating their references. A long chain of compact evidence references can similarly make one explanation expensive even without duplicate payload storage.

**What the plans already cover.** R2 permits shared/partitioned manifests and explicitly measures traversal ([R2:15](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/02-incremental-publication.md:15)); R4 admits scans and manifest work despite delta writes ([R4:42](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/04-change-tracking.md:42)); R6 measures file/history growth and protected intermediate storage ([R6:53-55](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/06-maintenance-retention.md:53)); R7 requires a reshape if fixed changes become unaffordable ([R7:87](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/07-operations-qualification.md:87)). These are substantive protections, so missing numeric limits are an explicitly scheduled product decision.

**Smallest refinement.** Cross fragment count, retained-version count, completed-phase count, and explanation depth independently; distinguish selected-state opening, phase lookup, commit, evidence reconstruction, retention preview, and actual reclamation. Keep maintained format metadata/index/compaction facilities as the first alternatives. Do not preemptively build a custom persistent tree or a second reachability database.

**Falsifiable acceptance.** With fixed logical rows and fixed update size, increase each axis independently and record metadata bytes/requests decoded, catalog query work, latency, memory, and retained bytes. Read a pinned old state and one complete explanation; run retention preview and supported phase retirement. Adoption fails if required operations exceed preregistered product targets or need full-history replay without a qualified checkpoint/representation strategy. If protected roots prevent reclamation, report that cost rather than calling compaction a storage saving.

### F4 — The lifecycle experiment can establish architectural value, but not isolate a Rust-language speedup

**Severity: OPTIONAL. Category: evidence attribution. Recommendation: RESHAPE one claim in the rationale.** This does not invalidate the current README's deliberately limited claims.

**Evidence.** [RUST-REWRITE.md:132-134](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/RUST-REWRITE.md:132) says the first path would show whether Rust adds further execution gains. That path simultaneously changes layout, evidence factoring, retained phases, readers, and query engine. [README.md:7-11](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/README.md:7) and [RESEARCH.md:9](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/RESEARCH.md:9) correctly avoid inferring a language speedup from practitioner incidents.

**Counterexample.** A new run becomes faster solely because it skips a repeated receipt transfer; its Rust compute loop could be unchanged in cost or slower. The end-to-end result would still be useful, but would not isolate a language effect.

**Already covered.** R7 requires equivalent inputs/guarantees, stage attribution, failed runs, and full-path cost ([R7:81-87](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/07-operations-qualification.md:81)). It does not require causal language attribution.

**Smallest reshape and acceptance.** Change the rationale to say the experiment qualifies the assembled design. If a separate claim about Rust is desired, preregister a controlled arm with equivalent layout, algorithms, I/O, validation, resource limits, and work performed. Otherwise a result attributing gains to reduced passes/bytes satisfies the architecture's objective, and any claimed Rust-specific speed ratio fails attribution review.

## Artifact, lineage, and relationships

**Artifact:** [README.md](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/README.md), entire proposal at the pinned commit. It proposes an embedded Rust engine, Arrow execution, a DataFusion candidate, Lance-first storage qualification against Iceberg/Parquet, a maintained transactional catalog, and one set of lifecycle rules. **Category:** product architecture and operations, with proof/qualification work. The intended beneficiary is the producer/operator correcting and enriching retained datasets, plus consumers opening exact releases. The intended result is less repeated work and recoverable progress, not a promised numerical speedup.

| Prior artifact | Relationship | Evidence |
| --- | --- | --- |
| `RUST-REWRITE.md` | Performance rationale; reduces work before attributing language gains | Lines 5-7, 20-39, 55-59, 77-105, 132-134 |
| `RESEARCH.md` and practitioner `FINDINGS.md` / `followups.json` | Historical incidents and limits; motivate phase preservation, shared admission, visible resource use | Research lines 5-18, 26-39; findings lines 5-16, 20-29 |
| Core model | Semantic reference, not a mandated API/storage layout | `docs/core-model.md:20-24`, 60-82, 150-171, 251-307 |
| Commit `815b8b6` | Prior greenfield lifecycle package; current commit changes README's implementation proposal | `git show 815b8b6:docs/plans/native-dataset-lifecycle-2026-10-07/README.md` |
| Historical `REVIEW.md` | Records prior dispositions; was read after independent hypotheses and R1-R7 | Lines 26-41, 69-81; its approval supplied no approval authority here |
| Current architecture and larger-than-memory recipe | Informative implementation/comparison material only | `docs/architecture.md:24-59`; `docs/core-larger-than-memory.md:3-6`, 52-57; historical numbers are not rerun evidence |

| Seam | Owner and direction | Evidence / disposition |
| --- | --- | --- |
| Values, occurrence identity, field meaning, adequate reuse dependencies | Model → all lifecycle services | README 27-29, 115; R3 13-19; explicit semantics remain R3-T01's prerequisite |
| Exact selected inputs and qualified phase reuse | R1 shared bounded context → processing, comparison, refresh, publication | R1 27-42; R4 17; R5 15; one rule/reader owner, no global decoded cache |
| Candidate snapshots → selected complete result | Storage prepares immutable references; catalog guards atomic selection | README 45, 76-78; R1 17; table head cannot compete with serving authority |
| Columnar execution and resource controls | Engine connects maintained readers, operators, writers | README 35-41, 82-84, 118; F1 is the principal integration qualification |
| Changes → affected-output discovery | R4 certifies changes; R5 uses declared relationships | R4 13-27; R5 17-23; bounded equality-join/group scope, full-rebuild/refusal fallback |
| Selected result → independent release reader | R2 binds disclosure, coverage, exact objects and public readback | R2 13-29; consumer formats remain open before freeze |
| All retained roots → maintenance | R6 common inventory and guarded removal; native format retention also applies | R6 25-35; README 94; no second retention authority |

## Invariants and commitments

| Invariant | Status | Source and failure mode |
| --- | --- | --- |
| Equal bytes do not collapse distinct observations; changed values do not mutate old occurrences | PRESERVED | Core 60-70; README 27-29. Violation loses multiplicity/source meaning or attaches stale enrichment. |
| Retained state recovers exact membership, values, types, meaningful order, and evidence | PRESERVED | Core 68-82; README 145. Row counts or readable footers cannot establish this. |
| Data-input bindings determine retention obligations; provenance ancestry alone does not force full ancestor retention | RELIED-UPON | Core 150-171; R6 23-27. Conflating these increases retention without user benefit; omitting required bindings loses reproducibility. |
| Reuse preserves original generation and requires adequate material dependencies | PRESERVED | Core 221-237, 251-287; R3 17-19, 33. Equal rows cannot conceal receipt/policy changes. |
| One complete-result selection authority | NEWLY INTRODUCED in this proposal | README 45, 76; R1 17. Competing heads expose mixed data/evidence/coverage. |
| Acknowledgement follows durable required objects and catalog result; phase completion survives parent failure | NEWLY INTRODUCED | R1 17-21, 29-40. Violation repeats expensive work or loses acknowledged updates. |
| Optional change history accelerates comparison; its absence does not erase state readability | PRESERVED | R4 23-27; README 72. Corrupt evidence must refuse, rather than impersonate a valid empty change set. |
| Exact content and evidence outrank apparent performance improvement | PRESERVED | R7 81-87. Dropping readback, evidence, ordering, or failure history would make arms incomparable. |

The architectural commitments are stated without a required per-row catalog graph, custom consensus layer, general incremental SQL compiler, universal index, or workflow system. That restriction is valuable and internally consistent ([README.md:47,121,155](/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/README.md:121); R5:17; R1:15,29).

## User value and choices to keep

The first user-visible gain is a supported retry that starts at the first incomplete or invalidated phase and explains why. It directly addresses retained incident evidence without asserting that the historical defect still exists everywhere. Independent field groups then create a plausible route to cheaper enrichment changes, provided their combined-read and publication costs repay avoided writes. This is a meaningful benefit, not proof of its magnitude.

Keep the following choices:

- Keep Arrow visible at real bulk boundaries, immutable original evidence separately, and maintained codecs/decoders. Do not reintroduce per-row JSON as the universal execution representation.
- Keep original evidence and compact queryable relationships distinct, sharing processor/input metadata where meaning is shared. Measure complete explanation cost alongside storage savings.
- Keep phase completion, validation completion, local selection, public write, and public readback distinct. Structured validation surviving report-formatting failure is particularly well targeted.
- Keep full reconstruction as an oracle/fallback, with limited equality-join and affected-group rules. Many-to-many fanout and hot groups can legitimately require large work; expose scope instead of promising delta-proportional execution.
- Keep one catalog selection and retention owner, explicit pinned table references, and native format retention checks. Share semantic rules without forcing discovery, source policy, and backfill into one generic framework.
- Keep independent consumer admission and disclosure-materialization cost inside acceptance. A small write is not a small verified release by definition.

Conceptual debt added is real: value/group identities, correspondence, coverage, source succession, phases, public intent, and retention roots. Their purpose is traceable to correct reuse, recovery, and exact reading. Debt paid down includes duplicated receipt history, repeated admissions, per-function checkpoint frameworks, and competing mutable progress authorities. Neither delta is measured implementation evidence yet.

## Counterfactuals and kill criteria

**Counter-decision:** a maintained columnar engine over a wide immutable table with full recomputation and reusable durable phases is simpler. It can deliver the first recovery benefit. The proposal should select it for a workload if independent groups, indexes, or selective refresh fail to repay complete read/write/verification/maintenance costs; R7's gates already permit this. A Rust executable does not require choosing every proposed optimization.

**Removal probe:** remove R4 acceleration and full comparison remains; remove independent field grouping and a wide representation remains possible; remove durable phase results and the first promised recovery outcome breaks immediately. Remove the single catalog selection or required evidence and correctness breaks before performance becomes relevant. This identifies the first indispensable work correctly.

**Sibling subsumption:** maintained table storage, query execution, transaction databases, and codecs should supply mechanics. They do not by themselves decide DocSpec occurrence identity, adequate reuse, complete input coverage, disclosure, or which phase roots remain protected. Existing code is informative, not a migration constraint. No extra engine, format, allocator, or scheduler is justified by this static review.

**Kill criteria:** reject a selected mechanism if it cannot preserve exact values/evidence; cannot fit/refuse resource demand under declared limits; repeatedly loses qualified phases after late failure; cannot make captured progress under moving inputs; or makes fixed changes unaffordable as the independently varied population/fragment/history dimensions grow. Reject a format for a required consumer if supporting it forces unqualified reinterpretation or unacceptable recurring full materialization. Product thresholds must be set before results; this review invents none.

**Six-month critic:** the likely mistake is to report unchanged payload reuse while metadata, evidence traversal, cold readback, or wide-table export dominates total cost; the other is to label a query-engine memory allowance a process bound. The plans already demand the measurements that would expose both. Enforce those gates rather than adding speculative infrastructure today.

## Verdict rationale and confidence

**APPROVE.** Intent and shape match at the proposal's stated maturity. The user-value claim is supported as a testable architectural hypothesis. Core semantic commitments are honored; implementation choices remain replaceable. Conceptual debt is justified if the first recoverable path and full-path targets pass, and is not yet evidence of an implemented simplification. Maintained components partially subsume mechanics; DocSpec-specific lifecycle meaning still needs focused ownership.

Confidence is **high** that the linked plans address the major obvious performance traps and do not claim qualified runtime behavior; **medium** about eventual component fit, because no component tuple, type profile, consumer matrix, physical layout, resource target, or executed probe exists in this reviewed package. Those omissions are explicitly assigned decisions, not newly discovered blockers. F1-F3 make their falsification concrete; F4 corrects an attribution overreach in the rationale. No runtime speed, memory, or storage improvement is claimed by this review.
