# Final D32 cache and sink-profile cleanup: independent review

Date: 2026-09-12. Reviewer: `review_profile_docs`. **Static review only; no tests or builds run by this reviewer.**

**Decision: APPROVE the bounded production change.** It stops creating unused cache storage when every selected processor disables caching and removes sink-profile fields that no implementation reads. Existing result identity, reuse verification, work accounting, storage limits and delivery behavior remain in place. A proposed mixed-policy test strengthens the current public evidence without adding a test or runtime framework.

Scope is the seven-file final slice: `src/docspec/runtime/composition.py`; the durable, returned and hybrid result-delivery profile JSONs; `tests/test_processor_cache_composition.py`; `tests/conformance/test_profile_descriptions.py`; and `tests/test_machine_files.py`. All seven live files were read after the parent declared them stable. The mixed-policy extension was prepared in `/tmp` during the parent's full-suite freeze, then applied and independently reread after that run completed.

This is **not** an implementation certificate for earlier D32/D33 governance, cache-profile/state, verifier or declaration removals at `87b0e7f`, `3adc7fc`, `7188e34` or `231f155`. Prior architectural agreement does not establish their full implementation review. Those remain separately tracked by the D39 reviewer.

## 1. Change summary

`_compose_local_run` now creates `LocalSqliteProcessorResultCache` only when any verified plan processor uses `ProcessorCacheMode.EXACT_INPUTS` (`src/docspec/runtime/composition.py:259`). Previously any nonempty processor list created a database even when every processor disabled caching.

The three result-delivery profiles now have `limits: {}` (`src/docspec/storage_profiles/durable-dataset-results-v1.json:27`; `hybrid-results-v1.json:29`; `returned-results-v1.json:25`). Removed fields described one bounded store, the source of record limits and a one-record synchronous return loop. Sink constructors do not read those fields. The profile tests now require a JSON object rather than a nonempty object (`tests/conformance/test_profile_descriptions.py:62`; `tests/test_machine_files.py:59`).

## 2. Function and data-flow trace

| Function or value | Location | Inputs → output | Verified behavior |
| --- | --- | --- | --- |
| `_verified_processors` | `src/docspec/runtime/composition.py:114` | Pinned plan and supplied implementations → ordered implementation mapping | Checks selected IDs and each actual description, then exact `ProcessorSet` equality and plan policies. |
| `_compose_local_run` | `src/docspec/runtime/composition.py:174`, `:205`, `:259` | Verified configuration → executor and services | Processor verification precedes storage construction. The new `any(EXACT_INPUTS)` condition reads the already-verified plan descriptions. |
| `ProcessorCachePolicy.__post_init__` | `src/docspec/domain/processors.py:146` | Declared mode/key schema → normalized enum and validated policy | Disabled mode has no key schema; eligible mode requires one. |
| `ProcessorDescription.__post_init__` | `src/docspec/domain/processors.py:743`, `:759` | Immutable policy and determinism → validated description | Rejects a nondeterministic processor declaring exact-input caching. Thus the composition condition need not repeat the determinism check. |
| `LocalSqliteProcessorResultCache.__init__` | `src/docspec/adapters/processor_cache.py:16` | Cache path → initialized SQLite table | Creates its directory/table. Omitting construction avoids an actual file/storage side effect, not just an unused Python object. |
| `ProcessorRuntime._invoke_processor` | `src/docspec/application/processor_runtime.py:142` | Request, implementation, optional cache and budget → verified result/reference/disposition | Charges the invocation, enables lookup only for deterministic exact-input processors, verifies hits and falls back to execution for missing/invalid/unavailable entries. This logic is unchanged. |
| `ProfileRegistry.from_file` parsing | `src/docspec/profile_registry.py:139` | JSON limits → profile description | Already accepts an empty limits object; this patch does not broaden the parser or introduce another format. |
| `_profile_limit` and `_local_storage` | `src/docspec/runtime/storage.py:42`, `:65` | Selected physical profiles → positive numeric adapter bounds | Still explicitly supply record, blob, root, member, merge and store limits to storage adapters. No result-delivery limit is read here. |
| `_return_records` | `src/docspec/adapters/sinks.py:68` | Iterator and receiver → summary/returned reference | Calls synchronous `accept` before pulling the next record, then `finish`. One-at-a-time behavior comes from the implementation. |
| Durable/returned/hybrid sink constructors and delivery | `src/docspec/adapters/sinks.py:104`, `:140`, `:194`, `:218` | Explicit storage/receiver/partition/clock arguments → delivery receipt | Consume no configurable limits object. Durable delivery groups one bounded store; hybrid checks equality of the durable and returned streams. |

## 3. Invariants and consequences

**Eligibility comes from actual accepted implementations.** The selected mapping must match `plan.stages.processor_ids`, every description ID must match its key, and `ProcessorSet(descriptions)` must equal `plan.processors` (`composition.py:130–137`). An empty graph yields false; an all-disabled graph yields false; any exact-input processor yields true. Mixed graphs still rely on the unchanged per-processor eligibility check before lookup (`processor_runtime.py:156`). No second cache authority, state record or configuration model is introduced.

**Reuse remains evidence-based.** A database entry is only a candidate immutable-result reference. The existing runtime verifies it against the request, description, segment, prerequisites and data-use policy before returning a hit (`processor_runtime.py:164–188`). Disabled processors continue through ordinary execution. Cache admission and logical invocation accounting remain distinct: the budget is charged before the cache branch (`:153–160`).

**The changed-retention test schedules real work.** A retention-policy change remains in non-stage governing content (`src/docspec/application/planner.py:478–486`, `:528–535`), so the second run is not merely a no-task reuse of its entire predecessor. Its asserted cache hit and unchanged observed process-call count specifically exercise processor-result reuse.

**Removing descriptive sink fields does not remove a limit implementation.** Empty objects were already valid under `ProfileDescription` and the registry (`src/docspec/domain/profiles.py:47`, `:63`; `profile_registry.py:149`). Explicit positive physical storage bounds remain checked and wired (`runtime/storage.py:42–45`, `:84–97`, `:111`). Existing synchronous acknowledgement, record IDs, delivery counts, receipts and retained-layer verification remain unchanged.

**Profile pins intentionally change.** Full profile descriptions include `limits` in their identity (`src/docspec/domain/profiles.py:70`, `:97`; `profile_registry.py:173`). The three changed descriptions therefore have new full pins; `_local_profiles` refuses plans whose pins differ from current descriptions (`runtime/storage.py:49–59`). This is an intentional greenfield cleanup, not a promise that old saved plans silently resume. No compatibility parser or legacy alias is needed.

## 4. Behavioral evidence inspected

- `test_cache_disabled_processor_runs_retains_and_recovers_without_cache_storage` (`tests/test_processor_cache_composition.py:47`) replaces the constructor with a fail-if-called function, performs actual public processing and retention, inspects word/byte counts and one recorded attempt, reconstructs the saved handoff, and checks that no database/sidecar exists. It uses the new typed fixture directly.
- `test_cache_enabled_processor_reuses_a_real_result_across_plan_changes` (`:74`) observes actual calls for an enabled processor and an independent disabled processor, retains and opens both real results, verifies a database exists, changes the retention policy, asserts a different plan ID, compares both retained result values and requires exactly one cache hit. Enabled calls stay at 1 while disabled calls become 2.
- The profile tests retain configuration/full-description pin checks, required roles, real implementation imports, schemas/capabilities and closed-shape refusal; only mandatory nonemptiness of `limits` is removed (`tests/conformance/test_profile_descriptions.py:48`; `tests/test_machine_files.py:35`).
- The existing returned-sink test proves acknowledgement happens before pulling each next record and verifies stable replayed receipts (`tests/test_result_sinks_and_recovery.py:82–113`). The durable/hybrid test checks exact immutable layer replay, reopens those layers and compares returned/durable receipt state (`:195–244`). These behavior tests remain unchanged.

No new tests merely assert that the removed descriptive keys are absent. Their removal is supported by the actual constructor/data-flow trace and existing sink behavior.

## 5. Findings and qualification limits

**No material correctness finding remains in the production slice.** The condition matches the verified set, disabled execution keeps its semantics, and sink limits are removed only where they were descriptive data.

**Resolved coverage gap:** the initial tests exercised an all-disabled graph and a singleton enabled graph, which did not distinguish `any` from `all` for a mixed graph. The parent applied the test-only extension after the full-suite run. It verifies both retained outputs and enabled calls 1, disabled calls 2, exactly one hit after a genuine plan change. A small shared constructor avoids repeating disabled-description reidentification.

**Resolved fixture correction:** the first mixed-test run refused two descriptions with the same name before execution. `ProcessorSet` requires distinct names as well as IDs (`src/docspec/domain/processors.py:910–915`). Parent corrected the disabled description to `uncached-statistics` while rebuilding its identity (`tests/test_processor_cache_composition.py:31–44`). This reviewer reread the corrected live test and the set validator. The real processor accepts/emits its current description's ID and digest (`src/docspec/processing/processors.py:82–90`, `:119–132`), so the renamed fixture still exercises real pinned processing. No production change accompanied that correction. The corrected two-case gate subsequently passed, as recorded below.

The change avoids cache construction based on selected processor eligibility. It does not promise fully lazy creation after discovering whether a particular run has any remaining tasks; an eligible graph can still initialize an empty cache for a no-work run. It changes neither the existing cache implementation's storage policy nor its broader scale/lifetime qualification. No global memory or concurrency qualification is inferred from these fixtures.

## 6. Conclusion

**VERDICT: APPROVE.** Static coverage is adequate for this bounded change, and confidence is high. It removes an observable unnecessary database side effect and unused profile configuration without adding infrastructure or weakening the checked result/storage behavior.

The parent owns the focused cache/profile/sink gate and full regression, both reported separately from this static review. This reviewer ran no pytest, build, lint or broad runtime command for D32. The final mixed-policy test was executed by the parent after its fixture correction; that result is recorded separately below.


## Parent-executed regression evidence

The parent reports the full strict suite passed **1,078 tests**, with one live integration test deselected and no warnings, in **247.07 seconds**. Original native log and JUnit are `/tmp/docspec-final-simplification.log` and `/tmp/docspec-final-simplification.xml`. That run includes the final production cache condition and sink-profile changes but precedes the test-only mixed-policy extension. Production did not change afterward. After correcting the duplicate-name fixture, the strengthened two-case gate passed **2 tests in 1.70 seconds**; log `/tmp/docspec-mixed-cache-gate.log`. The parent also reports Ruff, lock verification and diff checks passed. These are parent-executed results, distinct from this reviewer's static analysis.
