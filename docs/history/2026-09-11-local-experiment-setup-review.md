# D03 local experiment setup — independent static review

VERDICT: APPROVE. The processor policy preflight finding is resolved, and the final tests address the dependency-order coverage gap. No material finding remains in this bounded setup change.

Repository: `/Users/mikewolfd/Work/DocSpec`; reviewed D03 changes over `a19c3f0`, alongside the separately reviewed D17 representation adapter. This review follows `/Users/mikewolfd/.agents/skills/semi-formal-code-review/SKILL.md`. The reviewer read source, callers, tests, and maintained documentation but executed no tests or builds and edited no repository files. D16 disposition and failed-item repair changes are excluded; their integration was shelved during this gate. Unrelated untracked history was not read.

## 1. Patch summary

`prepare_local_experiment` accepts the user's source catalog, workspace, explicit work bounds and acceptance choices, and selected implementation objects. It derives the existing `ProcessingPlan` and delegates the existing `prepare_local_run`. Users no longer repeat stage digests, processor descriptions, the processor ID mapping, installed profile descriptions, or ordinary local execution defaults (`src/docspec/runtime/experiments.py:65`).

`PreparedLocalRun.plan` exposes the exact composed plan. `local_execution_limits` becomes the shared source of defaults for Python and CLI requests. The public lower-level plan API remains available for callers that already have a plan. There is no new experiment identity, serialized configuration, registry, worker type, or ledger.

The additional wrapper has concrete user value: it removes duplicated facts and canonicalizes a supplied processor graph while retaining the same runtime and evidence. Shared policy validation fixes a real early-refusal gap without introducing another policy implementation.

## 2. Function trace

| Function / method | File:line | Inputs → output | Verified behavior |
| --- | --- | --- | --- |
| `prepare_local_experiment` | `src/docspec/runtime/experiments.py:65` | Catalog, workspace, explicit authorities/bounds, implementation objects → `PreparedLocalRun` | Snapshots descriptions, validates the existing processor graph, normalizes execution order, builds the ordinary plan, delegates the existing runtime. |
| `_configured_stages` | `src/docspec/runtime/experiments.py:35` | Stop point and selected objects → policy plus exact objects | Refuses unknown stop points and processors beyond the stop; constructs only requested defaults. |
| `stage_policy` | `src/docspec/runtime/experiments.py:53` | Selected stage settings → existing `StagePolicy` | Shares stage selection and pin construction with the convenience path. |
| `ProcessorSet.__post_init__`, `execution_order` | `src/docspec/domain/processors.py:905`, `:942` | Descriptions → validated dependency order | Existing duplicate ID/name, missing dependency, and cycle refusal; deterministic topological order. |
| `local_execution_limits` | `src/docspec/runtime/defaults.py:6` | Optional local bounds → existing `ExecutionLimits` | Preserves CLI defaults; omitted in-flight count follows worker count; supplied allowances are not enlarged. |
| `_local_run_arguments` | `src/docspec/cli/requests.py:165` | Validated request → typed runtime arguments | Uses shared defaults and named execution-bound arguments; request closed-shape parsing remains the owner of CLI validation. |
| `prepare_local_run` | `src/docspec/runtime/__init__.py:37` | Existing plan and settings → prepared or recovered run | Same composition/preparation path; rejects incompatible resume options and delegates saved-handoff admission. |
| `_stage_implementations` | `src/docspec/runtime/composition.py:163` | Requested flags and implementations → selected objects | Refuses unrequested supplied objects, preserves selected object identity, and creates defaults only when requested and absent. |
| `_verified_processors` | `src/docspec/runtime/composition.py:120` | Plan, policies, implementations → ordered mapping | Exact IDs and descriptions agree with plan; shared retry/data-use/external-execution checks now precede storage construction. |
| `verify_processor_policies` | `src/docspec/application/processor_rules.py:21` | Plan and actual descriptions → validation | Shared pure check for data-use digest, allowed execution scope, and retry digest. |
| `_compose_local_run` | `src/docspec/runtime/composition.py:180` | Effective settings → existing services | Stage/profile/processor/deadline/network/concurrency/time/producer/sink/partition/fetcher checks run before `_local_storage` at line 224 and plan write at line 229. |
| `StoreExecutionService.verify_configuration` | `src/docspec/application/execution.py:184` | Saved plan plus live implementations → checked plan | Retains exact stage/processor and policy checks before execution or saved-work reuse; shared policy helper does not remove the runtime guard. |
| `_load_prepared_local_run` | `src/docspec/runtime/preparation.py:131` | Existing handoff and reconstructed services → prepared run | Exact operation, execution limits/deadline, worker configuration, plan, ledger, sink, and base checks remain unchanged. |
| `PreparedLocalRun.plan` | `src/docspec/runtime/execution.py:53` | Prepared run → `ProcessingPlan` | Returns the actual composition plan; no competing configuration is reconstructed. |

The import direction remains outer runtime → application/domain/processing/ports. CLI requests consume runtime defaults. The small delayed import of `prepare_local_run` avoids a package-initializer cycle and delegates the existing public function; it does not create another implementation.

## 3. Data flow and invariants

1. **One saved plan.** Supplied processor descriptions feed the existing `ProcessorSet`; its canonical dependency order feeds the plan and requested processor IDs. The mapping retains each description's original implementation object (`experiments.py:110`, `:132`). The exact default stage objects that supplied the pins also execute (`experiments.py:114`, `:131`).
2. **Output-affecting identity remains checked.** Plan construction pins stages, policies, profiles, processor graph, source and base. Existing composition checks compare live objects with that plan before saving work, and execution checks again before execution/reuse (`composition.py:204`, `:209`; `application/execution.py:184`). An object that changes its description is not silently accepted through the initial snapshot.
3. **Authority and attempt choices remain explicit.** Both accepted producers, work limits, evidence time, and deadline are required. A base is an explicit optional argument; the helper never chooses the catalog head or derives acceptance from the supplied artifact. It does not create a new wall-clock identity (`experiments.py:69`, `:79`; `docs/python-runs.md:87`).
4. **Defaults are useful and finite.** One partition, installed local profiles, retain-all, local-content policy, no accepted failures, and retry attempts matching explicit work limits are derived defaults. Empty processors means no processor output. Requested stage defaults are constructed once; capture constructs neither extractor nor segmenter. Execution allowances are shared with the CLI and remain upper bounds (`experiments.py:108`, `:117`; `defaults.py:6`).
5. **Invalid configuration refuses before dataset creation.** Selection, graph, requested-stage, processor-policy, network, and global retry incompatibilities reach pure/domain checks before `_local_storage`. A wrong source producer still uses the established source-admission path; this review does not claim every possible artifact admission failure occurs before all existing control writes (`composition.py:208`–`:229`).
6. **Recovery uses existing authority.** Same normalized settings produce the same plan and handoff. Handoff reconstruction still checks full saved worker settings and references; changed deadline, time, stage, fetcher, or storage root does not override saved work (`preparation.py:140`–`:168`; installed probe lines 160–190).
7. **No lifecycle duplication.** Capture, processing a retained base, retain, inspection, and execution all use existing services. This patch adds setup convenience, not export, cancellation, new scheduling behavior, or arbitrary format support.

Hypotheses confirmed: an ordinary configuration can derive the already required plan without a second schema; processor order can be normalized without losing object association; stage defaults need be constructed only once; one shared policy helper can provide both early refusal and the existing runtime mutation guard. The initial hypothesis that description equality alone established policy compatibility was refuted and corrected.

## 4. Test behavior and edge cases

These are assertions inspected statically, not reviewer-executed results.

| Test / probe | File:line | Expected behavior and evidence |
| --- | --- | --- |
| Capture then processing and recovery | `tests/test_local_experiments.py:36` | Real retained capture, processing with a fetcher that would fail if called, same handoff recovery, exposed plan and inspected reuse/output. |
| Derived versus hand-built plan | `tests/test_local_experiments.py:66` | Exact plan, handoff, execution profile, and run equality with a configured actual processor. |
| Default object construction | `tests/test_local_experiments.py:91` | Default factories called once; the exact objects reside in the executor and execute without reconstruction. |
| Reversed dependency graph | `tests/test_local_experiments.py:116` | Reversed and dependency-order tuples produce equal plan/handoff/run; derived dependent output records the correct processor and input association; both real objects run once. |
| Processor policy mismatch | `tests/test_local_experiments.py:138` | Different retry, data-use policy, or external execution under local-only policy refuses before the workspace path set changes. Fixtures construct otherwise valid processor descriptions. |
| Other invalid setup | `tests/test_local_experiments.py:163` | Invalid stop, duplicate graph, unrequested stage, unknown selection, too-small network allowance, and inconsistent global retry refuse without new dataset paths. |
| Explicit overrides | `tests/test_local_experiments.py:185` | Retention, selected population, profiles, partitions, and execution bounds enter the actual plan/profile. |
| Changed saved-handoff settings | `tests/test_local_experiments.py:201` | Time, deadline, and stage changes refuse recovery. |
| Independent source acceptance | `tests/test_local_experiments.py:215` | Missing required producer is a call error; an unaccepted producer refuses source admission. |
| Shared CLI defaults | `tests/test_local_experiments.py:225` | Public defaults match CLI values, worker count determines omitted in-flight count, and invalid zero workers refuse. |
| Installed public lifecycle | `tests/support/installed_runtime_probe.py:35` | Builds a real catalog with independently selected producer values; new helper captures/retains, then visible-text processes/retains with injected stages and processor. Exactly one fetch/extraction/segmentation and three processor calls remain unchanged on recovery. |
| Installed evidence and refusal | `tests/support/installed_runtime_probe.py:120` | Inspects source, reuse, results, and changed configuration; reads original and derived bytes; verifies declared representation/segment coordinates and child pins; changed stages/fetcher/root refuse; no caller plan/request files exist. |
| Isolated execution boundary | `tests/test_package_boundary.py:514` | Copies fixture and probe outside checkout, invokes installed interpreter with `-I`, checks exit status and success output. |

The probe's final configuration uses the D17 visible-text extractor and whole-block segmenter, not a hidden test helper or an application parser copy. Its three expected processor calls correspond to three retained segments. The probe validates declared evidence mappings and does not assert independent semantic completeness or corpus-scale qualification.

## 5. Findings

### F1 — Resolved: processor policy disagreement was discovered only after preparation

The initial helper derived a plan from processor descriptions, but `_verified_processors` checked only exact description equality. A normal `WorkLimits.max_attempts=1` plus default `ContentStatisticsProcessor` retry policy of three could prepare and persist work, then refuse its first execution. Data-use mismatch had the same gap. The promised configuration preflight therefore had a concrete exception.

`verify_processor_policies` now owns the existing policy checks (`application/processor_rules.py:21`). Composition calls it at `runtime/composition.py:145`, before storage construction at line 224. Execution calls the same helper at `application/execution.py:200`; its earlier check still establishes that the live retry policy equals the saved plan. The three focused negative cases at `tests/test_local_experiments.py:138` cover the actual mismatch paths and unchanged workspace paths.

### F2 — Resolved: normalization initially lacked a multi-processor behavioral assertion

The first equivalence test used one processor and could not establish association after graph ordering. The real two-node dependency test at `tests/test_local_experiments.py:116` now supplies both tuple orders, checks identical plan/handoff/run, inspects dependent output, and checks each implementation ran once. No new graph validation implementation was needed.

### Remaining findings

None in the reviewed D03 change. Existing full-retained-admission cost, arbitrary plugin behavior, cancellation, export convenience, and D16 failure-repair semantics are outside this setup approval. The new helper does not promise those capabilities.

## 6. Conclusion and acceptance

**APPROVE, high confidence for this bounded static review.** The change meets D03's implementation shape: one workspace, installed profile/default selection, explicit bounds and independent verifier acceptance, selected implementations, early configuration refusal, and existing saved effective configuration. The isolated installed probe expresses the required small-configuration capture → retain → process → retain → recover path. Parent-executed validation is required to establish that runtime outcome and should be retained separately.

`docs/python-runs.md:3`–`:92` and `docs/history/2026-09-11-local-experiment-setup-architecture.md` accurately describe the simplification and its limits. This closes the ordinary plan-construction gap for D03 when its executed gate is accepted; it does not imply D04 export convenience or D16 repair is complete. D17 has its own static report. No source or test changes remain requested by this review.

Execution evidence: none generated by this reviewer. The parent reported the final focused and installed-package gate passed 131 tests in 14.94 seconds with Ruff and diff checks passing, and that the full non-D16 suite was running. Those are parent-reported results, not this reviewer's independent execution or a completed full-suite assertion.


## Root-executed validation

After the final source changes, the parent agent ran 131 focused and isolated
installed-package checks: all passed. The regression suite passed 1,089 tests in
146.29 seconds, excluding one live integration test and the two unfinished D16
test files. It reported one non-failing `runpy` warning from importing the shared
offline example before executing that module. Ruff and diff checks passed.
These are local results; no remote CI, publication, or corpus-scale qualification
is claimed.
