# D16 failed-item repair — independent static review

VERDICT: APPROVE. The active profile declaration in F1 is corrected, and no material static finding remains in the integrated repair implementation and scoped tests. Final executed regression acceptance remains the parent's responsibility.

Repository: `/Users/mikewolfd/Work/DocSpec`; D16 changes over `c99874a`. Scope: `domain/dispositions.py`, the disposition and failure-linkage changes in `domain/delivery.py`, `application/planner.py`, `failure_frontier.py`, `base_reprocessing.py`, shared receipt verification and its checkpoint/inspection callers, the associated planner/prefix/disposition/frontier/lifecycle tests, `tests/support/experiments.py`, and `docs/repairing-failures.md`. Concurrent D04/D06 catalog additions and D11 transport changes have separate owners/reviews. In shared files, D11's transport-version edits are excluded from this D16 approval.

The review follows `/Users/mikewolfd/.agents/skills/semi-formal-code-review/SKILL.md`. It is static: source and assertions were read; no tests or builds were executed, no repository files were edited, and unrelated untracked history was not read. Parent execution results are separate evidence.

## 1. Patch summary

A successor can repair an accepted failed item without repeating its complete, compatible earlier stages. Retry intent uses the existing selection object: `none` by default, `transient` for temporary final failures, or `selected` for accepted failures within the selected population. Unchanged permanent failures therefore remain visible without automatic repetition.

The existing disposition gains `terminalFailure`, populated from the final entry failure only when the final disposition is failed. A bounded planner helper derives completion from retained outputs and receipts, records the existing execution mode and processor subset, and leaves actual byte/plugin checks to the established execution admission. No failed-stage field, alternate executor, failure ledger, or persistent completion index is introduced.

## 2. Function trace

| Function / method | File:line | Inputs → output | Verified behavior |
| --- | --- | --- | --- |
| `disposition_payload` | `src/docspec/domain/dispositions.py:19` | Final entry → disposition payload | Uses the entry's last failure for failed outcomes; keeps terminal failure null after success even when historical failures exist. |
| `parse_disposition_payload` | `src/docspec/domain/dispositions.py:34` | Persisted payload → stages and optional final failure | Closed shape, registered values, warnings, required identity, and terminal/outcome consistency. |
| `iter_delivery_records` | `src/docspec/domain/delivery.py:161` | Entry/store → ordinary delivery rows | Publishes disposition schema 3.0 and existing historical failure rows. |
| `_index_release_layer`, `_verify_release_relationships` | `src/docspec/domain/delivery.py:410`, `:651` | Streamed layer rows → logical admission | Indexes final failure digest and requires equal source, entry, and failure evidence. Unordered rows need not reconstruct final failure order. |
| `_CompiledSelection.compile` | `src/docspec/application/planner.py:148` | Selection → validated filters and retry mode | Rejects unknown or ambiguous retry values; existing population filters retain their role. |
| `RunPlanner.plan_run` | `src/docspec/application/planner.py:298` | Pinned catalog/base/plan → sealed planned ledger | Uses the existing workspace; spools dispositions and, when failures exist, retained evidence before saving planned work. |
| `_plan_store_references` | `src/docspec/application/planner.py:334` | Current/prior item join → planned entries | Applies population selection before retry admission. Unchanged failed items need explicit retry or a relevant change. Writes the chosen mode/subset into the ordinary entry. |
| `spool_failed_evidence` | `src/docspec/application/failure_frontier.py:32` | Admitted base layers → failed-source scratch rows | Scans each relevant layer once, keeps only failed sources, rejects invalid live rows, and explicitly closes reader iterators. |
| `failed_item_frontier` | `src/docspec/application/failure_frontier.py:96` | Source, saved stage policy, entry ID, bounded evidence → completion | Candidate/file association, representation/segment association, receipt coverage, unrequested-output refusal, and count bounds; missing promised evidence refuses. |
| `_completed_processors` | `src/docspec/application/failure_frontier.py:168` | Retained processor receipts and output → complete processor IDs | Recovers the original request-owning plan, verifies it matches the item's retained policy, and requires every segment result for a complete node. Empty segmentation is valid only with its stage receipt. |
| `FailedItemFrontier.changed_inputs` | `src/docspec/application/failure_frontier.py:70` | Previous/current stage choices and completion → retry eligibility | A change must affect or remove unfinished work; unrelated B does not retry failed A. All-complete late failure has no invented unfinished node. |
| `_failed_impact` | `src/docspec/application/planner.py:512` | Verified completion and new plan → existing mode/subset | Chooses full work, captures, representations, or segments; invalidates incomplete/changed processors and dependents. |
| `verify_processor_attempt_receipt` | `src/docspec/application/execution_evidence.py:142` | One saved attempt and known bounds/IDs → parsed fields | Shared closed shape, identity, elapsed time, outcome/failure checks. Full verification must supply the attempt ceiling; no-owning-plan observation explicitly supplies `None`. |
| `verify_processor_receipts` | `src/docspec/application/execution_evidence.py:184` | Explicit persisted fields, plan, receipts → verified results/invocations | Existing dependency, request/result, derived-output, retry-sequence, and settlement rules; takes persisted fields instead of constructing a fictional job. |
| `_verify_unfinished_attempts` | `src/docspec/application/failure_frontier.py:212` | Attempts without a result/owning-plan ref → observation | Requires failed, unique, contiguous attempts. Produces no reusable processor result. |
| `prepare_base_reprocessing` | `src/docspec/application/base_reprocessing.py:47` | Planned prefix and admitted base → verified seeded entry | Accepts captured or accepted-failure items, requires exact source and compatible prefix, verifies bytes/stage identities and unaffected results before new reuse receipts. |

The shared receipt refactor is mechanical for checkpoint and inspection callers: they pass the same actual entry fields and their existing explicit retry ceiling. Completion evidence and its verification stay in application code; disposition shape stays in domain code. The planner adds a bounded observation step rather than a second execution path.

## 3. Data flow and invariants

1. **Final classification is explicit and supported.** Delivery does not guess the last failure from sorted row order or candidate-local attempt numbers. Logical release admission requires the declared terminal failure to have matching saved source/entry/failure evidence (`dispositions.py:19`; `delivery.py:651`). This proves linkage, not independently reconstructed temporal order; the producer records the final ordered entry failure.
2. **Selected population and retry intent remain separate.** Existing selection filters run before failed-item admission. Healthy unchanged items do not rerun merely because retry intent changes. Source and non-stage governing changes retain conservative full planning (`planner.py:354`, `:475`, `:528`).
3. **Inherited item settings remain visible.** The planner uses each disposition's stage policy, not the latest release's stage policy. When independent B changes while A remains an unchanged permanent failure, the complete old item is inherited. A later selected repair finds the original processor-owning plan from saved requests (`failure_frontier.py:183`).
4. **Only complete compatible prefixes are inherited.** Missing stage output can mean incomplete work; an output without its promised receipt is contradictory and refuses. A complete processor requires all required segment invocations. A partially completed node is scheduled again; existing verified exact-input cache hits remain permitted (`failure_frontier.py:146`, `:207`; `processor_runtime.py:156`).
5. **No unfinished-stage fiction.** A final failure may occur after all requested outputs were produced. The frontier can therefore have all stages complete and no unfinished processor. Explicitly admitted repair reuses those outputs without unnecessary calls (`failure_frontier.py:70`, `:160`; `tests/test_failed_item_repair.py:136`).
6. **Fresh accounting is distinct from retained history.** Base preparation retains complete content and emits current `reused-base` receipts for unaffected results; it does not copy old attempt or failure rows. Recovery verifies the saved prefix and returns current progress without recreating those receipts (`base_reprocessing.py:202`, `:269`). Existing `WorkBudget` restoration charges current executed invocations once.
7. **Evidence observation is bounded.** The planner uses existing disk-backed scratch and scans each relevant layer once. Per-item files, representations, segments, receipts, and derived results have explicit population bounds. Full retained-catalog admission still incurs its existing verification cost; this change is not corpus-scale performance qualification.

Hypotheses confirmed: final disposition plus existing receipts can drive explicit retries; complete-prefix reuse fits existing modes; mixed retained policies need the original request-owning plan; shared receipt parsing avoids divergent validation. Hypotheses rejected during review: every failure implies an unfinished stage; every scheduled processor invocation must call its implementation despite a valid exact-input cache hit.

## 4. Test behavior and edge cases

These are reviewed assertions, not reviewer-executed results.

| Test group | File:line | Evidence inspected |
| --- | --- | --- |
| Final classification and linkage | `tests/test_dispositions.py:43`, `:53`, `:67`, `:82` | Opposite failure orders, reversed layer order, success with historical failures, missing/invalid class or retryability, and missing/wrong source/entry/failure evidence. |
| Explicit retry/default skip | `tests/test_planner.py:495`, `:512` | Failed capture without a complete prefix schedules full work only when explicitly selected; unchanged permanent failure schedules nothing. |
| Partial processor repair | `tests/test_failed_item_repair.py:20` | Complete upstream preserved; failed node scheduled; successful prior segment may cache-hit; no refetch/re-extraction/re-segmentation; old failure still visible and new failure absent. Repaired output digest/schema pairs equal a fresh-workspace build. |
| Transient versus deterministic | `tests/test_failed_item_repair.py:61` | Temporary failure retries under transient mode; deterministic failure remains inherited. |
| Removed/changed work | `tests/test_failed_item_repair.py:72`, `:105` | Removing failed processor yields successful segment prefix; independent B change skips whole failed item, replacing A admits repair. |
| Extraction and segmentation failure | `tests/test_failed_item_repair.py:87` | Correct predecessor mode chosen with exactly one original capture/extraction/segmentation across repair. |
| Late failure | `tests/test_failed_item_repair.py:136` | Failure after graph completion, with ordinary or empty segmentation, repairs without processor calls and clears only the new terminal failure. |
| Interrupted repair/accounting | `tests/test_failed_item_repair.py:161` | Real durable whole-processor checkpoint interruption; same handoff, mode, subset and no repeat calls on recovery; current budget seeded twice remains equal, charges two processor invocations and no inherited source/pages/segments. |
| Mixed retained plan | `tests/test_failure_frontier.py:81` | An inherited failed item survives an unrelated successor policy and later repairs with original receipt ownership and current desired graph. |
| One scan per layer | `tests/test_failure_frontier.py:105` | Wrapper refuses repeated scans and per-source lookups; actual public repair completes through the single-pass planner. |
| Missing promised receipt | `tests/test_failure_frontier.py:137` | Removing an extraction receipt while leaving its representation refuses rather than falling back to full work. |
| Completion and bounds | `tests/test_failure_frontier.py:157`, `:180`, `:190` | Empty segmentation and nonempty complete output have no unfinished processor; smaller segment allowance refuses observation. |
| Resource lifetime | `tests/test_failure_frontier.py:45`, `:64` | Retained generator references prove actual close on malformed reader rows and scratch-bound refusal. |
| Existing conformance migration | `tests/conformance/test_incremental_equivalence.py:412` | Default unchanged failure now explicitly stays unplanned; selected retry uses saved segments, calls only the failed processor, preserves healthy items, and compares complete active document state and terminal dispositions with a clean build. |
| Existing sink migration | `tests/test_result_sinks_and_recovery.py:172` | Accepted/rejected fixtures now include a real terminal FailureRecord and assert that exact failure reaches the delivered disposition; final-verdict checks remain. |

Healthy-prefix tamper and recovery cases remain relevant shared-verifier coverage, but do not substitute for the new failed-base tests. The repair interruption test covers a real whole-processor durable frontier, not arbitrary per-segment persistence, process-wide cancellation, or measurement of lost uncheckpointed work.

## 5. Findings

### F1 — Resolved: active profile declared the superseded disposition schema

`src/docspec/storage_profiles/local-jsonl-records-v1.json:17` initially advertised `docspec-disposition-record/2.0`, while `domain/dispositions.py:12` and delivery publish/accept only 3.0. The current declaration now names 3.0, verified after the root's correction. No legacy reader was added. Final executed profile/package checks must use the resulting current profile pin.

### F2 — Resolved: a processor-call assertion disallowed valid cache reuse

The first partial-repair fixture expected four implementation attempts although its descriptor enables deterministic exact-input caching. The earlier successful segment may legitimately hit that cache when the incomplete processor is scheduled again. The final assertion expects three attempts and explicitly distinguishes new invocation scheduling from inherited completed-node reuse (`tests/test_failed_item_repair.py:40`). No production cache behavior was weakened.

### F3 — Resolved: coverage initially omitted difficult failed-base paths

The final tests add independent processor changes, replacement of the failure, mixed retained policies, a late failure with every output complete, interruption/recovery with fresh budget restoration, and clean output comparison. These cover the material branches identified during the design and first test review.

### F4 — Resolved: duplicate processor-attempt validation

The frontier's no-owning-plan branch initially copied attempt shape/identity parsing from full evidence verification. `verify_processor_attempt_receipt` now shares those mechanics. Full verification still supplies the explicit ceiling and checks settlement; frontier observation still requires only failed contiguous attempts and contributes no reusable output. This is a narrow shared rule rather than a new evidence model.

### F5 — Resolved: retry choices needed ordinary user documentation

`docs/repairing-failures.md` now explains all three choices, qualified source IDs, population selection, relevant changes, complete-stage/cache semantics, late failure, retained history, and current-only accounting. The history decision alone is no longer the only usage guidance.

## 6. Conclusion and acceptance

The D16 implementation satisfies the requested behavior: a failed processor can repair without fetching valid content again; unchanged permanent failures do not automatically loop; old retained failures remain visible; explicit/relevant changes and complete-prefix evidence control retry work. No unmet D16 behavioral acceptance remains in the inspected source/tests. D20 cancellation and broader interrupted-work accounting remain separate.

Static confidence is high for the scoped control/data flow and assertions. The verdict is APPROVE; no tests or builds were executed by this reviewer. Parent-reported focused gates at review time were 98 planner/frontier/public repair/schema/prefix/router tests and 77 shared checkpoint/inspection/runtime tests. A subsequent combined suite reported 1,152 passes and six failures: an import-root allowlist, an old implicit failed-item retry expectation, a saved-worker recovery mismatch, the changed transport-version expectation, and two terminal-failure fixture omissions. The reviewer read those failure summaries but did not execute or resolve the gate. The parent is investigating and will supply final integrated/post-correction results separately; this report does not claim a green full suite.

Final D16 follow-up: the reviewer inspected the corrected conformance and sink fixtures described above. They strengthen explicit retry and terminal-evidence assertions rather than deleting refusal coverage. The parent reports their final D16 gate passed 86 tests in 27.37 seconds, covering conformance incremental behavior, sinks/recovery, dispositions, failed-item repair/frontier, planner and prefixes. D16 is ready to commit under this static approval. The separate D11 transport-line changes remain outside it.

## Parent execution evidence

The final focused gate passed **86 tests in 27.37 seconds** with
`uv run --frozen --extra dagster pytest -q` over incremental equivalence, result
sinks/recovery, disposition evidence, public failed-item repair, failure-frontier
verification, planning, and prefix reuse. The conformance test now explicitly
requests repair, proves no repeated capture/extraction/segmentation calls, and
compares the repaired active output with a clean build. Sink verdict fixtures
include the required final failure and assert its retained evidence. These
correct the three D16-related failures in the earlier combined regression gate.

A separate 74-test gate passed current-profile installed-wheel, release-integrity,
worker-identity, and local recovery checks. Full regression of the combined final
tree follows the separately reviewed transport edits. No CI, live provider
collection, push, or release result is inferred from these local runs.

Final combined regression: **1,184 passed, one live integration test deselected,
one known example-import warning in 154.41 seconds**. This run used the fixed
catalog and repair commits with the final transport changes, resolving the
earlier combined-suite failures described above.
