# Prepared-service base-reader lifetime review

**VERDICT: APPROVE the bounded implementation.** The change moves whole-base admission from every store task to the first prefix-reusing task in one prepared execution service. It binds the reusable reader to the complete immutable base reference, serializes first admission, and drops the view on close. The checks on records, blobs, receipts, checkpoints and worker settings remain in the task path.

## Scope and evidence status

Independent semi-formal static review of the uncommitted changes to:

- `src/docspec/application/execution.py` — 21 changed lines introducing a service-local reader/reference pair, initialization lock, ownership comment and `close()`.
- `src/docspec/runtime/execution.py` — 10 changed lines connecting reader cleanup to the existing prepared-run lifecycle and documenting that lifecycle.
- `tests/test_runtime_base_reader.py` — real retained-input tests, including concurrent execution, fresh recovery and corruption after admission.
- `docs/python-runs.md` — the prepared lifetime and its limits.
- `conformance/test-matrix.json` — six native pytest selectors added under `DOCUMENT-RELEASE-INTEGRITY`.

Final snapshot: uncommitted changes over `ddc875f`, September 12, 2026. Production and tests were frozen during this final pass.

The parent owns the motivating capacity measurements and runtime gate. No tests, uv commands, builds or performance probes were run by this reviewer. `git diff --check` passed as a read-only check. No implementation edits were made. Unrelated D52 changes, planner/index work, global caching and the protected history file were excluded.

The parent reports the corrected focused file passed **12 tests in 9.08 seconds**, with Ruff passing. These are parent-run results, not this reviewer's execution. The earlier mixed gate exposed test-fixture defects; its failures are not a passing qualification. The complete strict suite was still running when this certificate was finalized.

This certificate approves the implementation and inspected test assertions. It does not establish a measured speedup, capacity ceiling, a full-suite pass or a new guarantee against arbitrary concurrent workspace mutation.

## Findings

**No unresolved production blocker was found.** Two test setup issues were reported by this reviewer and fixed: using `SourceItem.item_id` rather than nonexistent `source_item_id`, and constructing an identity-valid changed `ProcessingPlan` before testing cached-reference refusal. The parent also corrected the counter assertions to use public inspection work counts and the tamper path to use the actual workspace blob root. I re-read the final fixture, all twelve parametrized cases, and those corrections.

The admission boundary is deliberately one prepared-service use, not every task. A later task does not reread an otherwise unused base root/run/store artifact solely because that artifact changed after the initial successful admission. It still reads and verifies the records and evidence it uses. Fresh first base use after close or reconstruction goes through whole-base admission again. This is the intended immutable-reader lifetime, not a cache of globally trusted files. The existing public reader already describes one verified immutable view reusable for an application operation. [document_catalog.py:13](/Users/mikewolfd/Work/DocSpec/src/docspec/ports/document_catalog.py:13)

## Function trace

| Function / method | Inputs and outputs | Verified behavior |
| --- | --- | --- |
| `StoreExecutionService.__init__` — [execution.py:66](/Users/mikewolfd/Work/DocSpec/src/docspec/application/execution.py:66) | Existing pinned plan/services → executor | Creates exactly one optional `(DocumentReleaseRef, DocumentCatalogReader)` pair and one standard-library `Lock`. No process-global state, filesystem index, scheduler model or configuration knob is added. |
| `_reprocessing_reader` — [execution.py:176](/Users/mikewolfd/Work/DocSpec/src/docspec/application/execution.py:176) | Current store and verified plan → admitted base reader or `None` | FULL-only stores still return `None`; prefix work without a base still refuses. Under the lock, it calls the existing `open_reader` only when no successful admission is present. Assignment occurs after `open_reader` returns, so failures cannot leave admitted state. Every reuse compares the complete reference, including release ID, locator and digest. |
| `StoreExecutionService.close` — [execution.py:196](/Users/mikewolfd/Work/DocSpec/src/docspec/application/execution.py:196) | Existing executor → cleared optional pair | Takes the same lock and drops the view. Repeated close is harmless. The documented caller rule remains to stop active workers before close; this method does not become a cancellation service. |
| `PreparedLocalRun.close` — [runtime/execution.py:69](/Users/mikewolfd/Work/DocSpec/src/docspec/runtime/execution.py:69) | Prepared resource → released scratch and base view | Calls executor cleanup in `finally`, even if membership scratch cleanup raises. Existing context-manager exit invokes it. |
| `PreparedLocalRun.run` — [runtime/execution.py:146](/Users/mikewolfd/Work/DocSpec/src/docspec/runtime/execution.py:146) | Existing handoff/tasks → reconciled run | Still closes in its outer `finally`. The local executor's thread-pool context exits before this cleanup, matching the after-workers-stop rule. [adapters/execution.py:134](/Users/mikewolfd/Work/DocSpec/src/docspec/adapters/execution.py:134) |
| Recovery construction — [preparation.py:87](/Users/mikewolfd/Work/DocSpec/src/docspec/runtime/preparation.py:87) | Saved handoff + newly assembled services → new prepared run | Existing recovery verifies saved profile, worker composition, plan, ledger, sink and base reference. Composition constructs a new `StoreExecutionService`; no reader state is loaded from the handoff. [composition.py:246](/Users/mikewolfd/Work/DocSpec/src/docspec/runtime/composition.py:246) |
| Native resource lifetime — [dagster_experiment.py:121](/Users/mikewolfd/Work/DocSpec/examples/dagster_experiment.py:121) | Native dependencies → yielded prepared run | Existing native resource uses `with ... as prepared: yield prepared`, so teardown reaches the same cleanup. No Dagster lifecycle or process cache is introduced. A separately constructed resource/process starts without an admitted reader. |

## Data flow and preserved invariants

The cache starts empty. The first successful `open_reader(plan.base_release)` creates a view containing immutable release metadata and the record-storage dependency. The lock covers only initialization, exact-reference checking and reset; it does not serialize whole task execution. The local catalog reader does not own a persistent open row stream or database handle. Its `scan_source` opens record reads as needed. [catalog.py:73](/Users/mikewolfd/Work/DocSpec/src/docspec/adapters/storage/catalog.py:73)

The pair retains neither `DocumentReleaseVerifier`'s per-admission distinct-blob set nor decoded contents of every active record. That set remains local to each full verification. This patch reduces repeated full admissions; it does not itself remove that set or prove a new asymptotic memory bound for one admission. [commit.py:187](/Users/mikewolfd/Work/DocSpec/src/docspec/application/commit.py:187)

First admission still calls `LocalManifestDocumentCatalog.open_reader`, which calls the existing complete `open` gate. Nothing changes shared artifact admission, published-layer comparisons, plan/run/commit linkage, source lineage or full retained-byte verification. [catalog.py:175](/Users/mikewolfd/Work/DocSpec/src/docspec/adapters/storage/catalog.py:175)

Each task still verifies its exact handoff, current worker configuration, deadline, planned-task membership and recovered store identity. `StoreExecutionService.execute_store` still verifies current checkpoints before it requests the base reader. An admission failure therefore occurs before `store.start` and saving the next task attempt. [runtime/execution.py:76](/Users/mikewolfd/Work/DocSpec/src/docspec/runtime/execution.py:76), [application/execution.py:118](/Users/mikewolfd/Work/DocSpec/src/docspec/application/execution.py:118)

For prefix work, `prepare_base_reprocessing` still checks the source's acquisition inputs, requested stages, retained disposition, candidate coverage and configured prefix identity. It reloads stage controls/results, reconstructs the selected prefix, and invokes the existing checkpoint verifier. [base_reprocessing.py:48](/Users/mikewolfd/Work/DocSpec/src/docspec/application/base_reprocessing.py:48)

Record reads still verify the pinned root and selected member bytes, canonical rows and ordering. The reused reader has no bypass around those calls. [catalog.py:95](/Users/mikewolfd/Work/DocSpec/src/docspec/adapters/storage/catalog.py:95), [records.py:558](/Users/mikewolfd/Work/DocSpec/src/docspec/adapters/storage/records.py:558), [records.py:315](/Users/mikewolfd/Work/DocSpec/src/docspec/adapters/storage/records.py:315)

Checkpoint verification still reloads stage receipts, checks captured candidates and source identities, verifies captured/representation/segment blobs, validates stage receipt outputs and selected implementation identities, and validates processor receipts/results. Recovered current-plan progress must still match its retained prefix. [execution_checkpoints.py:69](/Users/mikewolfd/Work/DocSpec/src/docspec/application/execution_checkpoints.py:69), [base_reprocessing.py:194](/Users/mikewolfd/Work/DocSpec/src/docspec/application/base_reprocessing.py:194)

Sealed tasks that require no new prefix work need not open the base merely because a new prepared object exists; they retain their existing saved-output verification path. The fresh-admission guarantee applies when the new lifetime first consumes its base.

## Test behavior and edge cases

The new fixture creates a real three-document retained extraction result using existing test support and the public experiment factory. It then plans one-entry segmentation tasks, asserts every mode is `FROM_REPRESENTATIONS`, and makes any source refetch or repeat extractor invocation fail. This gives the tests actual persisted evidence and multiple task boundaries rather than synthetic readers alone. [test_runtime_base_reader.py:24](/Users/mikewolfd/Work/DocSpec/tests/test_runtime_base_reader.py:24)

- **Sequential and concurrent tasks:** the thread case brings all three tasks to the admission call with a barrier. Both branches count real `open_reader` calls, require exactly one, reconcile results and retain/read all three files/representations and six new segments. Public inspection work counts assert no new captures or representations. [test:82](/Users/mikewolfd/Work/DocSpec/tests/test_runtime_base_reader.py:82)
- **Failed first admission:** an injected first-open failure leaves the planned store reference unchanged; a retry succeeds through the real opener and the next task reuses that successful view. Two attempted opens are required. [test:115](/Users/mikewolfd/Work/DocSpec/tests/test_runtime_base_reader.py:115)
- **Close and saved-handoff reconstruction:** after one real task, repeated close is safe. Either later use of the same object or an explicitly reconstructed prepared object admits the base again before unfinished work. [test:138](/Users/mikewolfd/Work/DocSpec/tests/test_runtime_base_reader.py:138)
- **Complete reference binding:** each ID/locator/digest mutation is placed in a valid newly identified plan, and the service refuses it without a second open. This directly exercises the guard rather than failing earlier in plan construction. [test:160](/Users/mikewolfd/Work/DocSpec/tests/test_runtime_base_reader.py:160)
- **Corruption after initial admission:** a used blob, extraction receipt or actual files-layer record member is changed after the first task. The next task must raise `IntegrityError` while the open count remains one, demonstrating that all three retained read checks remain active. The member case mutates the actual selected member's bytes, not a mocked verifier. [test:176](/Users/mikewolfd/Work/DocSpec/tests/test_runtime_base_reader.py:176)
- **Failure during ordinary `run()`:** after one completed task, an injected later task failure propagates and the executor's cached reader is cleared by the existing outer cleanup. [test:206](/Users/mikewolfd/Work/DocSpec/tests/test_runtime_base_reader.py:206)

The lifecycle guide accurately explains the exact retained base, checks on used evidence, after-workers-stop cleanup and the absence of continuous checking of unrelated base objects while a run remains open. It does not imply that cached admission replaces final retention checks. [python-runs.md:252](/Users/mikewolfd/Work/DocSpec/docs/python-runs.md:252)

All six added map selectors match the actual test functions and therefore cover their twelve collected parametrized cases. The change adds only native pytest node selectors, not a stored pass/fail verdict or another runner. The existing hook expands matching parameters, refuses omissions/deselection, and requires successful setup/call/teardown reports. [test-matrix.json:231](/Users/mikewolfd/Work/DocSpec/conformance/test-matrix.json:231), [conftest.py:97](/Users/mikewolfd/Work/DocSpec/tests/conftest.py:97)

## Conclusion

**APPROVE. Changed-path coverage: adequate for the bounded lifecycle change. Static confidence: high.** The change uses the existing immutable reader at the existing dependency-injection lifetime, with standard-library synchronization and no alternate verification mechanism. It preserves per-entry tamper and lineage checks while avoiding whole-base admission for every task in that lifetime. It remains safe under the documented rule that callers stop workers before closing the prepared service.

The parent's focused tests and representative measurements must determine runtime correctness and the actual cost reduction. This static approval makes no throughput, full-corpus capacity or live-provider claim.

## Parent execution

The initial focused gate passed 59 cases and exposed six errors in the new test
setup: incorrect receipt counter names, an unavailable composition attribute,
and changed plans with stale identities. Corrections use public inspection,
the actual workspace blob root and valid newly identified plans; they change
no production behavior. The corrected file, including an additional used-record
corruption case, passed **12 tests in 9.08 seconds**.

The complete strict suite then passed **1,095 tests**, with one live integration
test deselected, in **274.61 seconds**, including native Dagster and isolated
installed packages. Native evidence is retained locally in
`/tmp/docspec-reader-full.log` and `/tmp/docspec-reader-full.xml`.
Ruff, lock consistency and maintained-document local file checks passed.
Performance remeasurement remains separate; this is local validation, not a
remote CI or publication result.
