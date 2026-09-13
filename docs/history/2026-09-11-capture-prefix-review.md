# Capture-first and prefix-reuse review

Static implementation verdict: **APPROVE**. All material findings and the specific acceptance-evidence gap raised during review are resolved. **D15 and D24's stated acceptance criteria are supported** by the inspected code and final test assertions. No material finding remains open.

Reviewed repository: `/Users/mikewolfd/Work/DocSpec`; uncommitted changes against `8495301`, including the later mixed-result, selection, empty-layer, and receipt-accounting corrections. This review used `/Users/mikewolfd/.agents/skills/semi-formal-code-review/SKILL.md`. It is independent **static analysis only**: no tests, builds, or repository edits were performed by this reviewer. The parent records executed validation separately. The unrelated untracked catalogue-cleaning history was not read.

## 1. Patch summary

Runs may stop after capture, extraction, segmentation, or their selected processors. The plan stores nullable identity/digest pairs for omitted stages. Each entry retains the full requested policy, while a separate ordered processor subset describes work that must run. A later plan reuses the verified prefix of a pinned base and uses the ordinary executor for remaining work; the separate processor-only execution loop is removed.

Planning now reads each retained item's own stage policy, rather than assigning the newest release plan to inherited items. Changing selection chooses the population without invalidating otherwise identical successful work. Other governing changes and previously failed items remain conservative full repairs. Shortening selected items replaces their descendants and preserves other items, including older processor layers across multiple generations.

The public `PreparedLocalRun.retain()` delegates to existing immutable retention, independently of current selection. Plans/stores and store-entry members move to version 3; disposition records move to version 2. CLI active-view version 2 calls its byte total `capturedBytes`, correctly including reused captures.

Principal callers are `prepare_local_run` (`src/docspec/runtime/__init__.py:57`), `PreparedLocalRun.execute_task` and `run` (`src/docspec/runtime/execution.py:75`, `:130`), and the existing planner, checkpoint, delivery, reconciliation, and release services. The installed probe exercises capture, retention, later processing, and recovery through the same public API (`tests/support/installed_runtime_probe.py:127`).

## 2. Function trace

| Function or group | File:line | Input → output | Verified behavior |
| --- | --- | --- | --- |
| `StagePolicy.__post_init__`, requested-stage properties, serialization | `src/docspec/domain/plans.py:76` | Optional pin pairs → closed policy | Each pair is complete or absent; segmentation requires extraction; processors require segmentation; processor IDs are distinct. |
| `ProcessingPlan` identity and wire methods | `src/docspec/domain/plans.py:144`, `:211`, `:243` | Source/base, policy, graph → plan ID/version 3 | Stage order must equal the processor graph; all output settings remain identity-bearing. |
| `DocumentEntry` construction and serialization | `src/docspec/domain/jobs.py:121`, `:152`, `:191`, `:210` | Full policy, mode, rerun subset → entry ID | Subset is ordered, distinct, and contained in the full graph; only `FROM_SEGMENTS` permits a proper subset. |
| `DocumentStore` checkpoint and wire methods | `src/docspec/domain/jobs.py:262`, `:304`, `:332`, `:349` | Stable entry population → immutable revisions | Store identity binds entry IDs; a checkpoint cannot change entry population/order; version 3 is explicit. |
| Storage entry-member read/write | `src/docspec/adapters/storage/stores.py:176`, `:252` | Entry bytes ↔ saved store | Both sides use entry schema 3, matching the packaged storage profile. |
| `stage_policy`, `_stage_implementations` | `src/docspec/runtime/__init__.py:31`; `src/docspec/runtime/composition.py:157` | Stopping point/objects → pinned requested stages | Rejects objects beyond the stopping point; constructs defaults only for requested stages. |
| `_verified_processors`, `_compose_local_run` | `src/docspec/runtime/composition.py:119`, `:174` | Plan and injected objects → bound services | Validates requested implementations before storage construction; an empty graph creates neither a default processor nor a SQLite processor cache. |
| `configured_stage_policy`, stage/output checks | `src/docspec/application/stage_identity.py:14`, `:33`, `:43`, `:54` | Actual implementation descriptors/output → agreement/refusal | Absent stages remain absent; emitted and checkpoint child identities must match the selected implementation. |
| `estimate_item`, `_entry_estimate` | `src/docspec/application/planner.py:222`, `:408` | Entry mode and metadata → bounded estimate | Excludes retained capture/extraction/segmentation and counts only processor reruns; memory and duration limits still apply. |
| `plan_run`, `_plan_store_references` | `src/docspec/application/planner.py:290`, `:325` | Verified catalog/base, selection → sealed store ledger | Source comparison and per-item stage impact determine entry mode; failed items cannot take the successful-prefix shortcut. |
| `_plan_impact`, `_stage_impact`, `_non_stage_governing_content` | `src/docspec/application/planner.py:457`, `:470`, `:494` | Prior/current settings → invalidation | Selection remains in plan identity but not rebuild invalidation. Extraction changes start from captures; segmentation changes start from representations; processor changes start from segments. |
| `_spool_item_states`, `_spooled_item_states`, `_previous_source_items`, merge/classify | `src/docspec/application/planner.py:563`, `:599`, `:538`, `:615`, `:652` | Retained dispositions/source rows → ordered item state | Reuses the bounded workspace and merge join; requires exact source/disposition populations; historical retry failures do not imply terminal failure. |
| `_base_payloads`, `prepare_base_reprocessing` | `src/docspec/application/base_reprocessing.py:29`, `:47` | Pinned reader and current checkpoint → verified seed/current progress | Validates source, successful disposition, complete candidates, relevant pins, blobs, receipt coverage, and unaffected processor results before new wrapper receipts. Restores semantic order from layer order. |
| `execute_store`, `_reprocessing_reader`, `_execute_entry` | `src/docspec/application/execution.py:115`, `:173`, `:215` | Planned/latest revision → processed revision | Verifies checkpoints and restores counters before starting another attempt; seeds once; preserves already-completed new work; executes only missing requested stages. |
| Capture, payload reload and persistence helpers | `src/docspec/application/execution.py:444`, `:510`, `:529`, `:555`, `:604`, `:619` | Source/retained bytes → checked objects | Fetch context closes; blobs and reversible segment relationships are checked; retained input materialization remains memory-bounded. |
| `verify_entry`, `verify_processor_receipts` | `src/docspec/application/execution_checkpoints.py:100`, `:372` | Entry, plan, exact receipts → verified frontier | Full policy equality; ordered candidate/representation coverage; rejects unrequested output; requires requested-stage completion, including empty segmentation receipts and exact processor-result coverage. |
| `extraction_observation`, `seed_verified_entries` | `src/docspec/application/work_budget.py:156`, `:173` | Verified receipt observations/frontier → cumulative counters | Live and resumed extraction read the same metadata count; missing observations refuse; reused work is not recharged; stable unit identities make repeated seeding idempotent. |
| `iter_delivery_records`, integrity index and relationship checks | `src/docspec/domain/delivery.py:160`, `:378`, `:584`, `:706` | Terminal entries/layers → disposition and checked ownership | Full policy is retained per item. Every derived row must name a processor requested by that source item. |
| `_assemble_layers`, `_retired_derived_layers` | `src/docspec/application/reconcile.py:480`, `:548` | Affected items, fragments, inherited layers → complete new state | Creates required empty current processor layers; preserves unaffected rows; considers all inherited retired processor layers, not only the immediately preceding plan. |
| `_verify_expected_layers` | `src/docspec/application/commit.py:345` | Complete retained state → admission/refusal | Requires core/current processor layers; inherited nonempty derived layers must pass the shared per-item verifier; unplanned empty extras refuse. |
| `PreparedLocalRun.retain`, `retain_release` | `src/docspec/runtime/execution.py:120`; `src/docspec/application/commit.py:429` | Bound run receipt → retained result | Checks plan/base identity, rejects stateless or rejected runs, and uses existing verified retention without changing current. |
| Existing catalog retention and selection | `src/docspec/adapters/storage/catalog.py:394`, `:445` | Verified staged result/expected current → retained or selected pin | Exclusive write lock; exact immutable store set; digest-qualified publication; selection checks expected current independently from immutable lineage. |
| `_cmd_run_active` output | `src/docspec/cli/runs.py:210`, `:292` | Saved work → status view | Version 2 makes the acquisition total's meaning accurate for reused captures. |

## 3. Data flow and invariants

- **Requested output and pending work are separate.** The full policy starts in the plan, is copied to each entry and disposition, and remains identity-bearing. `processor_ids_to_run` affects entry identity without narrowing what a successful result must contain (`domain/jobs.py:128`, `:141`; `execution_checkpoints.py:107`, `:323`). Processor IDs themselves hash the complete descriptions, including configuration, resources, dependencies, item limits, and applicable policy, so comparing IDs does not discard those dimensions (`domain/processors.py:761`, `:770`).
- **Reused content stays bound to exact inputs.** The source item must match the pinned base; captured files must cover its ordered candidates; relevant stage pins and selected child identities are checked. Missing or contradictory promised evidence refuses reuse, rather than invoking the fetcher as a repair fallback (`base_reprocessing.py:84`, `:110`, `:196`; `execution_checkpoints.py:119`, `:145`).
- **Recovery preserves new progress.** After reconstructing and checking the retained prefix, the seed helper compares it with a recovered entry and returns the current entry, preserving new representations, segments, results, and receipts. Only a fresh entry receives the newly constructed seed (`base_reprocessing.py:204`, `:259`, `:271`). The ordinary executor checkpoints the seed only if it changes the entry (`execution.py:238`).
- **Completion is specific to the requested prefix.** Capture requires all candidates; requested extraction requires every representation/receipt; requested segmentation requires every receipt even when it yields zero segments; processors require their full graph over the actual segments. Absent stages cannot carry output (`execution_checkpoints.py:164`, `:185`, `:243`, `:323`).
- **Mixed results have per-item ownership.** Reconciliation replaces affected rows across all relevant layers, retains unaffected rows, and removes retired empty layers. The logical verifier joins each derived row to that item's requested processors. Required empty current layers represent a valid empty result, rather than silently missing output (`reconcile.py:490`, `:511`, `:520`; `domain/delivery.py:593`, `:706`).
- **Recorded cost survives restarts.** Exact extraction receipts yield page/frame observations before budget seeding. Execution and recovery use the same parser; no inference from output boundaries remains. Full work charges new captures; extraction from captures charges new extraction; segmentation from representations charges new segments; segment reuse charges requested processor invocations only (`execution.py:130`; `execution_checkpoints.py:166`, `:344`; `work_budget.py:173`).
- **No second lifecycle authority is introduced.** The existing plans, immutable stores, receipts, layers, and retained release pins remain authoritative. New requested-processor and observation values are derived verification state, not retained cursors or alternate ledgers. The new runtime retention method is a small delegation to the existing service.

## 4. Test behavior and edge cases

The assertions below were read; their expected behavior follows the traces above. This table is not an execution report.

| Test evidence | Asserted behavior and static outcome |
| --- | --- |
| `tests/test_stage_selection.py:23`, `:35`, `:40`, `:52`, `:68`, `:86`, `:99`, `:108` | All stopping points round-trip; partial pins and impossible order refuse; rerun subsets retain the full requested policy; missing wire fields refuse. Expected to pass. |
| `tests/test_runtime_api.py:90`, `:108` | Constructor traps include default stages, processor, and SQLite cache; unrequested objects refuse before output roots exist. Expected to pass. |
| `tests/test_capture_lifecycle.py:74` | Two candidates retain exact acquisition evidence; later extraction/segmentation make no fetch and agree with clean output; repeated budget seeding does not charge source bytes. Expected to pass. |
| `tests/test_capture_lifecycle.py:124` and `:251` | Hard stops after durable capture and after new extraction/segmentation of reused input; recovered workers preserve progress and do not repeat completed work. Expected to pass. |
| `tests/test_capture_lifecycle.py:154`, `:213` | Shorter capture/extraction results remove descendants; two selected items are shortened across generations without refetching, preserving then retiring inherited processor rows. Expected to pass with selection-only invalidation removed. |
| `tests/test_capture_lifecycle.py:178`, `:187`, `:198` | Missing candidates, unrequested representation, and missing requested extraction output refuse completion. Expected to pass. |
| `tests/test_runtime_stages.py:84` | Changed extractor and segmenter take different retained prefixes; calls show no unnecessary upstream work; each resulting active document state equals its clean rebuild. Empty segmentation is included. Expected to pass. |
| `tests/test_prefix_reuse.py:33`, `:93` | Mixed per-item policies choose the appropriate prefix; missing/repeated files, wrong source, and wrong extraction policy refuse before new receipt writes. Expected to pass. |
| `tests/test_planner.py:365` | Changing only source-partition/bucket selection schedules no work for identical completed items. Expected to pass. |
| `tests/test_prefix_budget.py:126`, `:167`, `:218` | Custom extraction reports seven pages/frames while retaining three boundaries; resumed count remains seven and rejects more work. FULL/FROM_CAPTURES/FROM_REPRESENTATIONS/FROM_SEGMENTS cases charge only new stages, reject missing observations, and retain exact-once call counts. Expected to pass. |
| `tests/test_processor_only_checkpoint_recovery.py:62` | Changed processor plus dependent resume from durable layer boundaries, keeping unaffected results and refusing tampered receipts before more work. Expected to pass through the unified executor. |
| `tests/test_processor_reprocessing.py:115`, `:300` | Changed processor/dependents, removal, addition, and rename preserve the valid input prefix and retire superseded output. Addition asserts call counts/rerun IDs, then compares its retained state with a fresh platform, identical source content, and the same added processor description. Expected to pass. |
| `tests/conformance/test_incremental_equivalence.py:151` | Changed-processor incremental and targeted/compacted results compare to clean active state. Together with the explicit addition comparison, this covers both processor routes. Expected to pass. |
| `tests/test_release_integrity.py:89`, `:177` | Missing per-item stage policy and derived output absent from its own item's processor request refuse. Expected to pass. |
| `tests/test_experiment_retention.py:35`; `tests/test_release_selection_cli.py:46`; `tests/test_runtime_api.py:53` | Retained alternatives preserve their base, stale current selection refuses, and repeat Python retention is idempotent without selecting current. Existing assertions remain applicable. |
| `tests/support/installed_runtime_probe.py:127` | Installed public runtime retains captures, later runs injected stages/processor without fetching again, retains/inspects output, recovers exactly, and refuses changed settings. Package execution belongs to the parent's validation. |

Other changed tests migrate the wire version, full-policy/subset distinction, status field name, or receipt-based budget input. The final conformance selector now names the inspected selection-reuse test (`conformance/test-matrix.json:354`), and the package assertion matches the installed probe's capture-first success message while retaining its process-exit check (`tests/test_package_boundary.py:526`). Existing FULL checkpoint tests and work-budget tests remain relevant alongside the new prefix-specific regressions. No assertion in this review establishes general cancellation, live provider behavior, arbitrary custom-code correctness, or scale/performance qualification.

## 5. Findings

**F1 — Resolved correctness finding: selective shortening could not retain inherited processor output.** The original patch preserved an unselected item's old processor rows but rejected their layer because the newest plan omitted that processor. The final correction verifies per-item requested-processor ownership and retires all inherited old layers across generations (`domain/delivery.py:593`, `:706`; `commit.py:345`; `reconcile.py:548`). The two-generation lifecycle regression and negative ownership test cover the failure shape.

**F2 — Resolved maintainability/correctness finding: an unrequested processor cache was still constructed.** Capture-only work unnecessarily opened/created SQLite cache state. Composition now passes no cache for an empty processor graph; the existing null-cache behavior applies, and the constructor-trap test includes the SQLite cache (`runtime/composition.py:271`; `tests/test_runtime_api.py:99`).

**F3 — Resolved accounting finding, initially reported by the implementing agent and independently traced here.** Execution counted receipt metadata while resume inferred pages from representation shape. The final shared observation parser and required verified mapping preserve the exact recorded count for custom extractors (`work_budget.py:156`, `:173`; `execution_checkpoints.py:166`). The seven-versus-three page/frame regressions demonstrate the distinct failure case in both fresh and retained-capture work.

**F4 — Resolved test-coverage finding: processor addition needed a direct clean comparison.** D15 explicitly requires comparing each reuse route with a clean rebuild (`docs/dataset-experiments-todo.md:341`). The final assertion now builds a fresh platform with identical source content and the exact added processor description, then compares it to the previously retained addition result (`tests/test_processor_reprocessing.py:300`, `:311`). The shared comparator includes source items, files, representations, segments, and derived output values while removing execution-specific receipt/attempt fields (`tests/support/incremental.py:82`). This closes the identified gap without a production change.

The selection-only invalidation change and creation of required empty current processor layers were reviewed as deliberate bounded corrections. They reuse established identity, layer, and verification mechanisms. The maintained guides and architecture decision now describe requested prefixes, original acquisition evidence, conservative other-governing changes, exact-result retention, and the remaining lifecycle limitations. The stale documentation-index claim was corrected.

## 6. Conclusion and acceptance assessment

**D15:** **The stated done-when is supported.** The implementation and assertions cover changed extraction, changed segmentation, processor changes/dependents, addition, valid-prefix verification, and downstream invalidation. Clean comparisons cover extraction, segmentation, capture-to-processing, processor replacement, and processor addition. Source/processor identities, resources, prerequisites, and applicable policies remain pinned. Selection no longer triggers unnecessary refetches. This is a bounded contiguous-prefix result: previously failed items may still require full repair, aggregate registry changes may invalidate multiple children, and arbitrary custom-code correctness is not established.

**D24:** **The stated done-when is supported.** A usable capture or processing run is retained without portable export; incomplete requested work is distinguishable and refuses successful completion; immutable retention and guarded current selection prevent silent replacement of another result. The new API delegates to those same transitions. The dependency label D20 does not negate this evidence: cancellation remains separate unfinished D20 work. D04's broader inspection and export convenience are also not claimed complete.

**VERDICT: APPROVE** the implementation and the bounded D15/D24 acceptance assessment. No material finding remains open.

- The patch achieves its capture-first and valid-prefix-reuse intent through existing services and evidence, with no second retained state system.
- Coverage of changed production paths: **ADEQUATE** for this static implementation verdict and the stated D15/D24 acceptance criteria.
- Confidence: **HIGH** in the inspected code/dataflow and test assertions. Runtime pass counts, package qualification outcomes, and full-suite status must be attached separately by the parent.


## Root-run validation

The reviewer above inspected code and assertions without executing tests. Root
and the implementing agents ran these checks against the final production changes:

- Focused capture, prefix, checkpoint, work-budget, and stage-reuse gate: **62
  passed**, including custom extractors reporting seven pages/frames while
  retaining three PDF page boundaries.
- `uv run --frozen --extra dagster pytest -q`: **1,025 passed, two failed, one
  live integration test deselected** in 133.55 seconds. Both failures were stale
  test metadata: the conformance matrix named the former selection test, and
  the package test expected the installed probe's former success message. The
  installed probe itself completed successfully. Neither failure required a
  production-code change.
- After correcting both metadata references and adding the explicit
  processor-addition clean comparison, `uv run --frozen --extra dagster pytest
  -q tests/test_machine_files.py tests/test_package_boundary.py
  tests/test_processor_reprocessing.py`: **15 passed** in 12.43 seconds. This
  rebuilt and installed the wheel, exercised capture/retain/later-process/retain
  outside the checkout, and verified the new clean-comparison assertion.
- `uv run --frozen --extra dagster ruff check src tests examples` and
  `git diff --check`: **passed**.

These are local execution and static-review results. They do not establish live
source-provider behavior, complete cancellation, general scale qualification,
CI, package publication, or deployment. D04, D16, D19, D20, D38, and the broader
D39 acceptance remain open.
