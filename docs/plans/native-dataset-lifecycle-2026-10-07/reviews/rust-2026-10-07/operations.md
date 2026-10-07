# Independent reliability and integration review

**VERDICT: APPROVE the planning direction.** I found no material contradiction requiring a reshape after tracing the Rust proposal through the detailed lifecycle plans, Core meaning, and representative consumers. This approval does not select Lance, establish a deployment durability profile, qualify an implementation, or approve production publication. The strongest unresolved risks are component-adoption gates already owned by the plan.

Review target: `/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/README.md`, DocSpec commit `40e891ccece79495f7ab850d441ed8633be73449`. The checkout matched that commit and was clean when reviewed. Static analysis only; no code changes, tests, execution benchmarks, external messages, or reviewer coordination. Review method: `/Users/mikewolfd/.codex/skills/semi-formal-architecture-review/SKILL.md`.

Citation convention: `README`, `RUST`, `RESEARCH`, `R1`–`R7`, and `REVIEW` below mean files in `/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/`: `README.md`, `RUST-REWRITE.md`, `RESEARCH.md`, `01-durable-storage.md`, `02-incremental-publication.md`, `03-schema-backfill.md`, `04-change-tracking.md`, `05-dependent-refresh.md`, `06-maintenance-retention.md`, `07-operations-qualification.md`, and `REVIEW.md`. Other paths are relative to `/Users/mikewolfd/Work/spicy-stack/`. All numbers are current file line numbers.

## Findings first

There are **no BLOCKER or CONCERN findings**. The observations below are concrete ways to falsify the accepted design at its existing gates. They are not claims that the proposal omits the named guarantee, or that a hypothetical implementation is broken.

### O1 — Keep native snapshot lifecycle subordinate to the complete DocSpec root inventory

- **Severity / category:** OBSERVATION / named-seam.
- **Location:** `README.md:37–45,92–94`; `01-durable-storage.md:17–19,48–53`; `06-maintenance-retention.md:13–17,25–35,43–49`.
- **Failure sequence to test:** A worker reserves and writes a native candidate snapshot. Before DocSpec selects it, native cleanup regards the snapshot as obsolete, or another native writer advances its mutable head. Cleanup deletes an object also needed by a completed phase, a pinned reader, or a supported catalog restore point. A later catalog selection/restore now names unavailable bytes. A native table's successful snapshot transaction alone cannot prevent this cross-owner race.
- **What the plans already cover:** R1 requires protection before retained object production, durable objects before catalog acknowledgement, and a pinned native snapshot rather than a competing serving head. R6 includes in-flight candidates, completed phases, readers, releases, and restore points in one inventory; it reserves removals atomically and bars new references. The Rust README explicitly adds preservation of the native format's own retention requirements. I therefore reject the hypothesis that native cleanup is simply absent from the architecture.
- **Recommendation / smallest change:** **KEEP.** At R1-T01/R6-T01 component selection, record the actual native APIs and privileges that enforce this inventory. If a candidate cannot expose or safely restrict its cleanup, reject that component or disable its autonomous deletion. No new generic coordination service is justified by this review.
- **Falsifiable acceptance:** With an old reader, a retained failed-parent phase, a prepared unselected snapshot, and an advertised restore point simultaneously active, run every supported native compaction/cleanup route and stop processes on each side of selection/removal. All promised roots still read exactly; removal rejects or narrows its candidates; no native maintenance credential bypasses the authoritative removal decision. Retiring the final root permits exact deletion and makes availability status agree with reality.

### O2 — Consumer distributions belong in the measured supported path

- **Severity / category:** OBSERVATION / user-value.
- **Location:** `README.md:37–39,88,137–149`; `02-incremental-publication.md:13–23,35–46`; `07-operations-qualification.md:15–17,76,81–91`.
- **Failure sequence to test:** The embedded engine produces an efficient native release. A chosen browser or SQL consumer needs a flat distribution, which materializes the entire population. A small update now pays a full rewrite and transfer; or a consumer combines the new base with an older enrichment because it resolves separate mutable locations. The native benchmark succeeds while the supported user path remains slow or inconsistent.
- **What the plans already cover:** R2 requires a named-consumer matrix before format freeze, derived distributions pinned to canonical releases, exact population/type equality, and compatible base/enrichment coverage. R7 includes disclosure materialization and independent reading in end-to-end cost. Current consumers demonstrate why those requirements matter: Engine retains exact source-state bindings for original-record lookup (`spicyengine/docs/prepared-search.md:223–243`); SpicyRegs query/download readers capture one generation and keep subject and receipt members together (`spicy-regs/docs/generation-publication.md:85–124`). Those are behavioral examples, not legacy API or Iceberg requirements imposed on the rewrite.
- **Recommendation / smallest change:** **KEEP.** Name the first promised consumer in R2-T01 and include its complete read path in the first qualification that claims consumer value. An offline fixture may precede that decision, as R2:46 expressly allows. Do not freeze the public format from native-engine results alone.
- **Falsifiable acceptance:** Hold an old consumer open while a release containing a correction, deletion, and evidence-only change is promoted. Every response resolves wholly to its captured release and exact source/evidence bindings. Include any required export generation and download in the measured small-update result. If the export dominates, change distribution/layout or the performance claim before adopting the candidate.

### O3 — Restore must recover external authority, not merely readable objects

- **Severity / category:** OBSERVATION / trust-posture.
- **Location:** `README.md:76–80,90–94,153`; `01-durable-storage.md:19–25,51–61,69`; `02-incremental-publication.md:27–29,38`; `06-maintenance-retention.md:31–35`; `07-operations-qualification.md:31–35,47,72–75`.
- **Failure sequence to test:** Publisher A obtains authority and writes a durable release intent. The public pointer changes, but A loses its response. Recovery starts from an older catalog image while A can still reach storage. The restored process either republishes an older selection or reuses A's authority; meanwhile a pending delete may already have succeeded. Merely restoring a mutually readable catalog/object set would not establish exclusive current authority.
- **What the plans already cover:** R2 binds attempts to authority epochs, denies adoption of replacement epochs, and demands fencing at the storage boundary rather than a catalog lease check. R1 requires manual recovery to enforce exclusive authority where needed and explicitly qualifies restoration against external intents. R6 forbids restored metadata from falsely promising deleted bytes. These requirements rule out the failure; choosing a provider capable of enforcing them remains unqualified.
- **Recommendation / smallest change:** **KEEP.** Make this compound restore/old-worker scenario an explicit instance of R7-T05, using the chosen provider. Preserve the current separation between required worker-loss durability and product-selected wider disaster recovery.
- **Falsifiable acceptance:** After restoring each advertised point, leave the old publisher/deleter alive and deliver delayed responses and writes. The old authority cannot advance the channel or delete protected objects. Unknown writes reconcile exact identity; a confirmed public write resumes readback without another promotion; status identifies any history outside the supported recovery promise. Do not count a test double as provider evidence.

### O4 — Byte-bounded streams must fail usefully when one unit exceeds capacity

- **Severity / category:** OBSERVATION / operations.
- **Location:** `README.md:35–41,66,82–84`; `01-durable-storage.md:38–42,59`; `07-operations-qualification.md:21,27–35,57–63,81–87`.
- **Failure sequence to test:** Receipt transfer is durably complete. Validation next encounters a single unusually large text/receipt value or a non-spillable operator. A bounded batch count and engine limit still permit decoder or allocator memory outside that limit. The process is killed, or an operator repeatedly resumes the same unaffordable phase without a supported way to distinguish its resource refusal from corrupt input.
- **What the plans already cover:** Shared accounting covers decoding, processing, writing and remote requests; queues are bounded in bytes, preflight treats uncertainty explicitly, whole-process memory is observed, and refusal/cancellation preserve completed phases. R7 requires supported resume with capacity preconditions and includes wide text, nested attachments and hot groups. The DataFusion streaming claim does not promise that every operator or decoder has bounded memory; the proposal correctly requires separate measurement.
- **Recommendation / smallest change:** **KEEP.** Include an individually oversized value and a non-spillable step in R7-T04/T06 fixtures. Report the phase's precise refusal and supported next action, without silently resetting budgets or discarding completed transfer.
- **Falsifiable acceptance:** The first failed attempt preserves its completed transfer and structured refusal/attempt evidence. Status reports unknown or exceeded capacity honestly. With an explicitly sufficient resource configuration, supported resume starts at validation, performs no new transfer, and yields the same exact result as an uninterrupted reference run. If the supported profile cannot handle the value, refusal remains an explicit product limit.

## Artifact summary

The proposal responds to repeated whole-data work and the loss of expensive intermediate results. It selects an embedded Rust engine as the starting implementation shape, Arrow as the internal batch boundary, DataFusion as the initial relational candidate, and a pinned Lance release as the first storage candidate against Iceberg/Parquet. It retains a separate transactional catalog for complete result selection and one inventory for retention. Component selection and performance remain open (`README.md:3–11,31–47,151–155`).

Its category is **product architecture with operations and proof work**. The beneficiaries are dataset producers making corrections or enrichments, operators recovering interrupted jobs, and consumers needing reproducible selections. What goes in is an exact input/evidence selection. Work proceeds through retained preparation, processing and validation phases. What comes out is a catalog-selected result and a separately promoted immutable release. Independent reconstruction, fault injection, actual consumer reading and whole-lifecycle cost establish acceptance (`README.md:49–107,135–149`; `R7:51–87`).

The proposal adds no migration obligation. Greenfield freedom is explicit; changes to Core meaning still need an explicit conformance decision (`README.md:9`). This distinction prevents current Python APIs, Iceberg layout, identity spellings, or Engine integration from becoming accidental restrictions.

## Lineage and relationships

| Prior artifact | Relation | Evidence |
| --- | --- | --- |
| `RUST-REWRITE.md` | Immediate design direction: reusable fragments/field groups, compact evidence, Arrow, qualified Lance/DataFusion, phase reuse | `RUST:5–39,55–105,119–134` |
| Practitioner interviews | Observed late-failure and repeated-work pain; do not prove a rewrite speedup | `/Users/mikewolfd/Work/corpora/session-reports-2026-10-07/data-processing-interviews/FINDINGS.md:5–16,20–29,35–41`; `RESEARCH:5–18` |
| Research requirements | First delivery must reuse qualified completed phases and explain remaining work | `RESEARCH:24–41,47–49` |
| DocSpec Core | Governs recoverable membership, immutable occurrences, data-input retention and honest reuse; leaves technology open | `DocSpec/docs/core-model.md:18–24,60–82,150–203,251–305` |
| Earlier lifecycle plan at `815b8b6` | Rust commit replaces its README framing, carries R1–R7 requirement/task IDs forward | `git diff 815b8b6 40e891ccece79495f7ab850d441ed8633be73449 -- docs/plans/native-dataset-lifecycle-2026-10-07/README.md`; current `README:123–143` |
| Historical `REVIEW.md` | Lineage after independent hypotheses; does not qualify Rust components | `README:11`; `REVIEW:26–41,75–81` |
| Current exports and architecture | Behavioral comparison: exact state/evidence closure, trusted pin, independent reading, explicit current selection | `DocSpec/docs/result-exports.md:3–8,24–31,46–48`; `DocSpec/docs/architecture.md:28–33,65–85` |
| Engine and SpicyRegs | Examples of actual consumer pin/evidence requirements; APIs/storage are replaceable | `spicyengine/docs/prepared-search.md:118–142,216–243`; `spicy-regs/docs/generation-publication.md:85–124`; `spicy-regs/docs/native-generation-lifecycle.md:3–28` |

| Component or seam | Owner | Depends on | Consumers | Named? |
| --- | --- | --- | --- | --- |
| Exact inputs, completion and reuse | R1 with R3 dependency meaning | Admitted identities, policy, scoped validation and retained bytes | Every operation, resume, R4/R5 | Yes — `R1:27–42`; `R3:15–19` |
| Complete dataset selection | Catalog | Durable pinned native snapshots, expected output, definition and coverage guards | Readers, refresh, backfill, release | Yes — `README:45,76–78`; `R1:17–25`; `R5:29–35` |
| Native snapshot/object lifetime | Storage plus common retention inventory | Native metadata and exact object/version references | Workers, compaction, readers, recovery | Yes — `README:92–94`; `R6:13–35` |
| Public release effect | Application/publication with catalog intent | Disclosure-safe immutable root, pointer condition, external fencing | Independent readers and distributions | Yes — `R2:15–29` |
| Resource refusal and continuation | Engine/application | Shared capacity, durable phases, precise attempt status | Operators through CLI/API | Yes — `README:82–84,113–121`; `R7:21–37` |
| Consumer distribution | R2 reader/export owner | Named consumer needs, selected canonical release and exact type/coverage rules | Browser, SQL, application, downloads | Yes — `R2:13–23,35–46`; format choice intentionally open |

There is an integrating-owner requirement for catalog transactions, reuse, identity/schema, releases and retention (`README:143`). Named people and products are not yet chosen; that is delivery work already required, not proof of diffuse semantic ownership.

## Invariants and commitment check

| Invariant | Status | Evidence and consequence if violated |
| --- | --- | --- |
| Equal bytes do not collapse distinct observations; changed values do not mutate old occurrences | PRESERVED | `Core:60–70,144`; `README:27–29,145`; `R3:15,33`. Otherwise prior evidence points at a different observation. |
| A retained state recovers its complete membership, relevant order and multiplicity | PRESERVED | `Core:68–82`; `README:145`; `R2:17,23`; `R6:13–17`. Otherwise compaction/export silently drops or resurrects members. |
| Input-binding scope determines retention; dependency declarations do not narrow whole-state retention | RELIED-UPON | `Core:152–171`; `R1:31–38`; `R6:23–27`. A phase's exact selection must retain the data it actually claims to bind. Future schema/identity design must instantiate this, not substitute a processor's narrower read set. |
| Historical success, present availability, and reuse eligibility are distinct | PRESERVED | `Core:175–179`; `R1:33–42`; `R6:27–35`; `R7:29–33`. A retained success marker cannot authorize reuse after bytes expire. |
| Reuse preserves original execution and result association | PRESERVED | `Core:269–287`; `R3:17–19,33`; `R5:25,33`. Adopting old values must not fabricate another producing execution. |
| No acknowledgement before durable required objects and the catalog commit | NEWLY INTRODUCED operating guarantee | `R1:17–25`; `README:76`; `R7:72`. Otherwise worker replacement loses accepted work. |
| A complete result atomically binds output and nonregressing accepted input coverage | NEWLY INTRODUCED | `README:78`; `R4:27`; `R5:29–39`. Otherwise retries can publish older or incomparable inputs as fresh. |
| One catalog selects complete data/schema/evidence; table heads cannot compete | PRESERVED/REFINED authority rule | `README:45`; `R1:17`; `R2:15–17`. Otherwise readers can combine different generations. |
| External effects require durable intent, provider-enforced authority and reconciliation | NEWLY INTRODUCED | `R1:25`; `R2:27–29`; `R6:31–33`. A process lease alone cannot fence a delayed write/delete. |
| Physical public bytes must all be permitted for disclosure | PRESERVED requirement | `R2:21–23`; `R7:76`. Logical delete filters cannot hide bytes in a downloadable object. |
| Current and recoverable roots win over nominal expiry | PRESERVED/EXTENDED | `Core:199–203`; `R6:23–35,46–49`. Restoring old metadata cannot make deleted data reappear. |
| Scope and measured qualification bound conformance/capacity claims | PRESERVED | `README:3,9,145–155`; `R7:3,17,87–91`. A prototype or package choice cannot imply reliable service operation. |

`Core` in this table means `DocSpec/docs/core-model.md`. No BROKEN invariant was established.

## Crash and race pressure tests

| Adversarial position | Required design outcome | Existing authority |
| --- | --- | --- |
| Before in-flight reservation | Only disposable scratch may exist; no retained candidate is unprotected | `R1:17` |
| After object write, before result commit | Exact object response reconciliation; candidate remains reserved until commit or abandonment | `R1:17,57` |
| After durable phase commit, before response | Stable identity finds the committed phase; no repeated qualified transfer/computation | `R1:9,34–42,51–57` |
| After validation, during report formatting | Structured validation and bytes remain; retry presentation alone | `R1:36,51`; `R7:35,58` |
| After a competing selection | Expected-output, definition and coverage checks refuse invalid promotion; independent preparation can survive | `R1:17–19`; `R5:29–39` |
| After schema cutover | Old-definition workers cannot commit; accepted later inputs remain available | `R3:43–47,57`; `R7:69` |
| After public write, before durable completion | Protected intent reconciles exact public identity; resume readback without another write | `R2:27–29,38–40`; `R7:62` |
| After publisher loses authority | Storage boundary denies stale writes, including replacement-epoch adoption | `R2:27,38` |
| Between removal plan and execution | New protections shrink the plan; an atomic removal reservation bars later references | `R6:29–31,47` |
| After delete, before response/metadata reconciliation | Exact absent/deleted/failed outcome; restored state cannot assert false availability | `R6:31–33`; `R1:25,61` |
| Compaction while old reader is active | Equivalent replacement is selectable; old physical references remain protected | `R6:13–17,45–49` |
| Anonymous reader outlives service window | No implicit lease; status distinguishes expired support | `R2:27,39`; `R6:35,57`; `R7:31` |

These are traced required outcomes, not executed test results.

## External technical claims checked

The cited current primary documentation supports the narrow candidate rationale: Lance documents immutable version manifests and fragments with separate column files for backfill; its table metadata also includes deletion and reader-feature semantics. This establishes a plausible candidate, not an adopted capability set. [Lance table format](https://lance.org/format/table/).

DataFusion documents `execute_stream` as incremental batch production and separately documents `collect` as buffering output. That supports the README's execution-interface claim; it does not establish whole-process bounds or lifecycle savings. [DataFusion DataFrame API](https://datafusion.apache.org/library-user-guide/using-the-dataframe-api.html).

The README already requires a pinned implementation and enabled-feature qualification (`README:37,43`). A current documentation page is not evidence about an unspecified future pin. No external provider durability, fencing, or cleanup claim was assumed.

## User-value and counterfactual checks

**Value:** A failed receipt validation or optional report no longer forces a completed transfer to repeat; operators can identify the next supported action. Small edits can share unchanged values and field groups, while readers receive one reproducible selection. The research supports these outcomes, including a demonstrated correction that existing scorecard recovery already worked (`RESEARCH:15–18,37–49`; practitioner `FINDINGS.md:23–29`). It does not establish that Rust itself or a new storage system supplies the improvement.

**Debt paid down:** Shared phase/result semantics replace ad hoc completion inference; one selected-input context replaces repeated same-scope selection/admission; retained exports prevent identical distribution work from being recreated. **Debt added:** field-group correspondence, native snapshot lifetime integration, authority fencing, and multiple retained-result roots. The plan contains these in named owners and defers general incremental SQL, universal indexing, automatic failover and orphan sweeping (`README:121,153–155`; `RESEARCH:33`). Actual maintainability or speed improvement remains unmeasured.

**Smaller-shape candidate:** A maintained full-rebuild/full-export implementation with durable expensive phase results may satisfy the first Comments recovery outcome. It is the appropriate counter-arm. Field groups, indexes and change acceleration must earn their complexity on complete consumer cost, not on a warm isolated query (`RUST:26,132–134`; `R4:42–48`; `R7:83–87`). This does not justify an implementation-preserving restriction on the greenfield proposal.

**Kill criteria:** Reject a chosen component if it cannot preserve a protected native snapshot, fence a stale publisher at the actual write boundary, preserve meaningful value distinctions, or supply the promised independent consumer path. Reshape an optimization if a fixed small update remains unaffordable as data/history grows once required export, readback, retention and recovery are included (`README:145–153`; `R7:81–91`).

**Counter-decision:** A single wide table plus whole-version publication reduces composition and selective refresh machinery. It also repeats unrelated writes and may lose recovery value unless retained phase semantics survive. Prefer it where measured total cost wins; the plans allow that result through component and grouping qualification (`R3:35,63–65`; `R7:17,87`).

**Removal probe:** Removing optional R4 changes should first make comparison slower; complete states remain readable and correct full comparison remains available (`R4:23–27`). Removing durable phase roots first breaks operator recovery after a late failure (`R1:36–42`; `R6:27`). Removing coverage or evidence bindings must cause refusal rather than a convenient stale success (`R5:29,37`; `R7:77`).

**Sibling subsumption:** The existing Core model supplies meaning but no native lifecycle implementation choice; R1–R7 already own the expanded behavior. The README is an integration proposal rather than another scheduler, journal, or mutable authority. Current export and consumer implementations demonstrate behaviors but do not subsume the new design under the stated greenfield scope.

**Six-month critic:** The most credible criticism would be that a fast native prototype was mistaken for a qualified product while consumer export, large receipts, native cleanup or restore costs remained unmeasured. Existing gates are correctly placed to prevent that. The next useful evidence is a complete pinned Comments path and the named adverse cases, not another abstract subsystem.

## Hypothesis log and limits

Initial hypotheses were written to `/Users/mikewolfd/Work/spicy-stack/DocSpec/docs/plans/native-dataset-lifecycle-2026-10-07/reviews/rust-2026-10-07/operations-hypotheses.md` before reading historical `REVIEW.md` conclusions. No other reviewer outputs were read.

| Hypothesis | Update after tracing |
| --- | --- |
| H1: native candidates plus catalog commit can recover if crash boundaries are protected | CONFIRMED as design requirement by `R1:17–25,51–61`; component realization unqualified |
| H2: publication needs real fencing and physical disclosure checks | CONFIRMED; both are explicit in `R2:21,27,36–40`, so absence-based findings are refuted |
| H3: restore, readers, phases and native cleanup may split retention authority | REFINED; `R6:25–35` and `README:94` require one complete inventory; O1/O3 identify falsifiable adoption checks |
| H4: completion scope/trust and usable resource recovery are required | CONFIRMED and covered by `R1:27–42,59`; `R7:21–37,55–63` |
| H5: open product targets should not automatically block fixture work | CONFIRMED by `R2:46`; `R7:89–93`; no invented numeric targets or consumer obligations imposed |
| H6: physical field groups must preserve Core outcome/occurrence/evidence meaning | CONFIRMED at stated invariant level by `README:27–29,145`; `R3:9–35`; representation remains a required upfront design task |

This review does not prove a selected provider's fencing semantics, Lance/DataFusion integration, schema/value encoding, process memory limits, consumer compatibility, or recovery time. Those are explicitly unselected or unimplemented. The independent oracle and component probes have not run. The filemap is a navigation aid, not proof of current file contents; target and referenced files were read directly.

## Verdict basis

**APPROVE.**

- **Intent versus shape: MATCHES.** The design makes expensive phases durable, gives complete-result selection one owner, and preserves independent consumer verification.
- **User-value claim: SUPPORTED as direction.** Research supports recovery and reduced repeated work; performance magnitude and Rust-specific benefit remain hypotheses.
- **Commitment status: HONORED at design level.** No established Core, publication, retention or authority requirement was found broken.
- **Conceptual debt delta: PAYS DOWN intended duplication while adding explicit lifecycle machinery.** Net maintenance/performance benefit must still be measured.
- **Sibling subsumption: PARTIAL semantic reuse.** Core meaning and the detailed lifecycle plans carry the guarantees; current implementation choices are not constraints.
- **Confidence: HIGH in the static operational consistency; MEDIUM in practical feasibility until the named native/provider/consumer gates run.**

No mandatory document reshape follows from this reviewer’s findings. Keep the scope bounded and enforce the existing gates with the concrete O1–O4 failure cases before adopting components or making operational claims.
