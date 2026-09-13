# Public runtime extraction: independent static review

Reviewed the uncommitted runtime extraction over `c41b00b`, including the later fetcher binding, exact task membership, cleanup, deadline, operation-recovery, and storage-only CLI corrections. This is a static review under `semi-formal-code-review`; I ran no tests, builds, or benchmarks. Runtime results belong in the parent agent's separate validation record. Paths below are relative to `/Users/mikewolfd/Work/DocSpec`.

The production paths achieve the bounded intent. All material issues found during review have code and regression-test corrections, including the final explicit adapter-wiring expectation. No material findings remain open. No repository files were edited by this reviewer, and the unrelated untracked history file was not read.

## 1. Patch summary

Python callers can supply a typed plan, workspace, fetcher, processors, and explicit execution settings to `prepare_local_run` (`src/docspec/runtime/__init__.py:25`). CLI commands adapt their parsed requests into that same factory (`src/docspec/cli/requests.py:162`, `src/docspec/cli/runs.py:39`). The former CLI-local composition/execution files are removed. The prepared object exposes local execution and externally dispatched tasks while retaining the existing plan, planned-store ledger, execution profile, handoff, store revisions, and run receipt (`src/docspec/runtime/preparation.py:18`, `src/docspec/runtime/execution.py:25`).

This is useful separation: embedded callers no longer manufacture CLI files or depend on private CLI services. Default extractors and segmenters remain explicit restrictions; fetcher and processor objects are actually injected. The temporary membership database supplies an efficient check that the newly public task method previously lacked. It is derived scratch, not another saved run state.

Important callers inspected include CLI prepare/start/resume/task/reconcile and storage-only commands, the offline example (`examples/offline_demo.py:159`), the Dagster process fixture, migrated workspace/worker/active-view tests, and the installed-wheel probe (`tests/support/installed_runtime_probe.py:39`).

## 2. Function trace

| Function or method | File:line | Inputs and output | Verified behavior |
| --- | --- | --- | --- |
| `prepare_local_run` | `src/docspec/runtime/__init__.py:25` | Typed values → `PreparedLocalRun` | Rejects conflicting recovery/planning options, composes once, then prepares or loads the saved handoff. |
| `_local_run_arguments` | `src/docspec/cli/requests.py:162` | Parsed CLI request → factory arguments | Builds the same domain plan, workspace, limits, policies, producer acceptance, timestamp, partition and sink choices. |
| `_verify_plan_policies` | `src/docspec/runtime/composition.py:102` | Plan and policies → validation | Checks both policy digests and attempt limit without requiring executable processors. |
| `_verified_processors` | `src/docspec/runtime/composition.py:115` | Plan and optional processor mapping → selected mapping | Requires default extract/segment identifiers, exact processor keys and complete descriptions; preserves the explicit empty mapping distinction. |
| `_utc_instant` | `src/docspec/runtime/composition.py:143` | Timestamp → validated timestamp | Shares the existing UTC timestamp parsing between CLI and typed composition. |
| `_local_profiles`, `_profile_limit` | `src/docspec/runtime/storage.py:39`, `:32` | Workspace/profile pins → verified local profiles and positive limits | Matches machine descriptions and allowed local implementations before selecting storage limits. |
| `_local_storage` | `src/docspec/runtime/storage.py:55` | Roots/profiles/producer → existing adapters | Uses the established control, store, records, blobs and catalog repositories and their profile limits. |
| `_local_storage_for_run_request` | `src/docspec/cli/requests.py:186` | CLI request → storage-only services | Uses policy/profile validation without demanding an installed implementation of a custom processor. |
| `_compose_local_run` | `src/docspec/runtime/composition.py:155` | Validated choices → bound services | Checks execution bounds, timestamp, independent producer types and fetcher descriptors before constructing storage; injects processors and the bound fetcher into the existing execution service. |
| `_local_processor_cache_path`, `_worker_composition_value` | `src/docspec/runtime/composition.py:74`, `:78` | Effective workspace/services → cache path and saved worker description | Pins actual roots, plan/profiles, policies, current fetcher descriptor, fixed timestamp, both accepted producers, partition and sink. No parallel recovery description is maintained. |
| `_content_fetcher_identity` | `src/docspec/runtime/fetcher.py:11` | Fetcher → implementation/configuration identity | Requires nonempty downloader identity and a valid SHA-256 configuration digest. |
| `_BoundContentFetcher.__init__`, `fetch` | `src/docspec/runtime/fetcher.py:26`, `:32` | Selected fetcher/candidate/task/attempt → checked stream | Detects descriptor mutation before calling the fetcher; compares downloader/configuration/task/attempt/transport metadata before returning chunks; closes rejected streams. |
| `_prepare_local_run` | `src/docspec/runtime/preparation.py:18` | Composition/resume choice → prepared object | Reuses or creates the existing ledger, summarizes exact tasks, and persists the existing worker/profile/handoff references. |
| `_load_prepared_local_run` | `src/docspec/runtime/preparation.py:131` | Composition/saved ref → prepared object | Loads verified controls, checks semantic identities, supported operation, limits/deadline, current effective worker, ledger, sink and base. |
| `PreparedLocalRun.__post_init__` | `src/docspec/runtime/execution.py:42` | Prepared services → lazy membership checker | Creates no membership file until a task actually requests admission. |
| `__enter__`, `__exit__`, `close` | `src/docspec/runtime/execution.py:50`, `:53`, `:59` | Object lifetime → scratch cleanup | Releases the disposable index; subsequent use can rebuild it. Caller must stop active workers before closing. |
| `_require_handoff`, `task_source` | `src/docspec/runtime/execution.py:63`, `:67` | Exact handoff → task iterator | Refuses another handoff and forwards closure to the sealed-ledger iterator. |
| `execute_task` | `src/docspec/runtime/execution.py:73` | Bound handoff/task → task result | Checks deadline, plan/operation, full initial StoreRef membership, then deadline again before loading the latest revision; executes or reuses sealed work and delivers through the existing service. |
| `_TaskMembershipIndex.__init__`, `require_member` | `src/docspec/runtime/task_membership.py:28`, `:43` | Ledger/limits/ref → admission or refusal | Uses the full canonical StoreRef; rejects later revisions; serializes build/lookup/close and opens each lookup read-only. |
| `_build` | `src/docspec/runtime/task_membership.py:65` | Sealed ledger → private SQLite index | Fully verifies the ledger, bounds count/record bytes/total bytes/database pages, recomputes complete task count/digest, and publishes readiness only after completion. |
| `_discard`, `close` | `src/docspec/runtime/task_membership.py:115`, `:120` | Scratch lifetime → cleanup | Removes temporary files and clears readiness; failures discard partial construction. |
| `reconcile` | `src/docspec/runtime/execution.py:98` | Result stream → run receipt ref | Calls the existing reconciler with the prepared plan/profile/handoff/base and existing storage; introduces no result authority. |
| `run` | `src/docspec/runtime/execution.py:118` | Prepared run → run receipt ref | Uses the existing bounded local backend; closes result/task generators and releases membership scratch on success or failure. |
| CLI task/reconcile/run handlers | `src/docspec/cli/runs.py:69`, `:116`, `:135` | Request files → existing command outputs | Task and reconcile paths use the prepared context manager; local run uses `run()` cleanup. |

## 3. Data flow and invariants

1. **One implementation path.** Runtime imports adapters/application/domain/ports, with no runtime-to-CLI import. Core import rules still forbid importing outer layers (`tests/conformance/test_import_directions.py:26`, `:140`). The new outer package does not move vendor dependencies into core. CLI request conversion is outside runtime; storage-only operations share policy validation without reconstructing execution (`src/docspec/cli/requests.py:186`).
2. **Saved identity describes effective work.** Preparation saves `_worker_composition_value`; recovery recomputes that same value from the actual fetcher and supplied settings, compares both content and semantic ID, then verifies the plan/ledger/sink/base bindings (`src/docspec/runtime/preparation.py:151`). Processor descriptions are checked against the complete plan at composition and again by existing application execution (`src/docspec/application/execution.py:178`). A live dependency that lies about its implementation cannot be proven honest by an identity string; no stronger claim is made.
3. **Admission precedes document side effects.** `execute_task` checks exact full initial references before `load_latest_store`, executor or delivery (`src/docspec/runtime/execution.py:85`). The SQLite index verifies the saved ledger's ordered-reference digest through `verify_planned_store_ledger` (`src/docspec/adapters/storage/stores.py:605`), then reconstructs the complete handoff task digest (`src/docspec/runtime/task_membership.py:99`). A revision-zero or same-plan check alone would not establish membership.
4. **Temporary index has no authority outside its lifetime.** Readiness is assigned only after successful full construction (`src/docspec/runtime/task_membership.py:113`). SQLite connections and ledger generators close explicitly; page/count/byte bounds cap the index; mutex ownership protects initialization, lookup and close. Closing during active worker execution remains a caller-lifetime error, explicitly documented. This does not establish aggregate scratch accounting or performance qualification.
5. **Fetcher evidence precedes accepted bytes.** The original object remains in composition for recovery comparisons, while only the execution dependency is wrapped (`src/docspec/runtime/composition.py:186`, `:234`, `:259`). Metadata mismatch closes the stream before acquisition consumes chunks (`src/docspec/runtime/fetcher.py:41`). Exact transport-version equality, including `None`, matches the existing checkpoint rule (`src/docspec/application/execution_checkpoints.py:118`).
6. **Recovery is recovery, not another trial or selection.** Existing references and store revisions remain the saved state; `.run()` returns a RunReceipt reference. Selection/export is separate. The guide preserves these limits and independent producer acceptance (`docs/python-runs.md:64`, `:82`, `:121`).

## 4. Test behavior and concrete edge cases

These are inspected assertions and static expectations, not reviewer-executed results.

| Tests | Expected behavior and supporting trace |
| --- | --- |
| `test_typed_run_injects_pinned_processor_and_recovers_without_request_files`, `tests/test_runtime_api.py:53` | Executes the actual custom processor/fetcher once, deletes caller JSON prerequisites, reconstructs the saved handoff, reuses sealed work and the same reconciled run. Factory and recovery traces above support the assertions. |
| `test_invalid_runtime_choices_refuse_before_storage_or_planning`, `tests/test_runtime_api.py:89` | Refuses its named network/concurrency/time/default-stage/processor/producer mistakes before the six state roots exist. This test does not claim every possible source-admission failure precedes all deterministic control writes. |
| Recovery settings and operation tests, `tests/test_runtime_api.py:121`, `:131`, `:145` | Changed limits/deadline refuse; wrong handoff and expired task refuse; a newly saved, internally consistent alternate-operation handoff also refuses. The last test recomputes its task digest, so refusal proves operation semantics rather than incidental digest mismatch. |
| `test_task_recovery_executes_only_an_unfinished_real_store`, `tests/test_runtime_api.py:164` | Replaces the former synthetic CLI tests with real running and sealed revisions. Only unfinished work calls the executor, using the latest verified revision; delivery sees the appropriate saved revision, and the task result retains its initial planned input. |
| Worker identity cases, `tests/test_local_worker_identity.py:38`, `:57`, `:90`, `:99`, `:114` | Matching settings reuse work; changed roots/configuration/clock/producers/partition/sink refuse before fetching; missing descriptors refuse before control writes. |
| Fetch metadata/mutation cases, `tests/test_local_worker_identity.py:126`, `:162` | Five metadata mismatches produce zero chunk reads, exactly one stream close and no captured files; post-prepare mutation causes zero delegate fetches. Existing execution records the artifact-integrity failure. |
| Exact membership cases, `tests/test_runtime_task_membership.py:49` | Same-plan extra stores, later revisions, alternate locators and digests refuse before mocked recovery/execution/fetch/delivery; persisted store bytes remain unchanged. |
| Valid reuse/concurrency cases, `tests/test_runtime_task_membership.py:81`, `:97` | Initial planned reference resumes latest completed revision; 40 concurrent lookups stream-build once, and explicit close causes verified rebuilding. |
| Digest/interruption/limit cases, `tests/test_runtime_task_membership.py:118`, `:140`, `:161`, `:179` | Complete task-digest mismatch, stream exception, SQLite page exhaustion and too-small worker scratch allowance all refuse without a ready/partial retained index. Iterator closure is asserted. |
| Deadline/zero-work/cleanup cases, `tests/test_runtime_task_membership.py:193`, `:207`, `:227` | A deadline crossing during admission refuses before store recovery; a real retained-base successor has zero tasks and creates no lookup files; executor failure and context exit release scratch. The deadline test replaces only the runtime module's clock binding. |
| Custom processor retention, `tests/test_release_selection_cli.py:106` | A Python-injected custom processor produces a real run that CLI retention can retain and open without needing that processor in CLI composition. It does not select current. |
| Installed probe, `tests/support/installed_runtime_probe.py:39`, package invocation `tests/test_package_boundary.py:514` | Builds catalog and typed run outside the checkout with the installed wheel, proves capture/process once and saved recovery, inspects retained records and rejects changed fetcher/root settings. The package fixture installs only the documented wheel bundle and verifies optional dependencies absent (`tests/test_package_boundary.py:435`, `:471`). |
| Migrated callers and import gates | Existing CLI/workspace/active-view/Dagster tests now reach the public factory. The final explicit wiring set includes the new membership module (`tests/conformance/test_import_directions.py:166`), while the allowed-area rules preserve the outer runtime direction (`:53`). |

No uncovered material production branch remains in this bounded extraction after the added zero-task, recovery-operation and post-admission-deadline cases. This review does not qualify a deployed external scheduler, active task cancellation, aggregate scratch enforcement, or custom extractor/segmenter identities.

## 5. Findings and disposition

- **Resolved — correctness:** Storage-only CLI setup originally demanded built-in processor availability and prevented retaining a custom Python run. It now checks plan policies and profiles only (`src/docspec/cli/requests.py:186`); the real custom-run retention test covers the path.
- **Resolved — correctness:** Public `execute_task` originally admitted any same-plan store/operation. Full planned-reference membership now precedes recovery (`src/docspec/runtime/execution.py:85`), backed by complete ledger and handoff verification with bounded lookup cost.
- **Resolved — correctness:** Injected fetcher metadata could disagree with prepared identity or the requested acquisition before bytes were accepted. Runtime binding now checks the live descriptor and all five relevant metadata fields and closes refusals (`src/docspec/runtime/fetcher.py:32`).
- **Resolved — correctness:** Saved handoffs for another operation could be reconstructed and executed as execute-and-deliver. Recovery now explicitly restricts the operation (`src/docspec/runtime/preparation.py:142`), and the regression input is internally consistent.
- **Resolved — correctness/resource lifetime:** First membership construction can outlast the initial deadline check. The second check now precedes recovery (`src/docspec/runtime/execution.py:86`); explicit result/task closure, `.run()` cleanup, and CLI context managers cover the added scratch lifetime.
- **Resolved — test coverage/configuration:** The exact outer wiring set now includes `docspec.runtime.task_membership` (`tests/conformance/test_import_directions.py:166`), matching its concrete local-store import (`src/docspec/runtime/task_membership.py:11`). This import is appropriate to the local assembly boundary.
- **Observation — maintainability/performance:** The index adds justified complexity because one full admission pass plus bounded lookups avoids a full scan for each dispatched task. Its first construction still verifies and streams the complete ledger, and lookups share a lock (`src/docspec/runtime/task_membership.py:48`, `:71`, `:89`). No benchmark or production-throughput claim follows. The guide accurately distinguishes its page allowance from aggregate concurrent scratch accounting (`docs/python-runs.md:110`).

## 6. Conclusion

VERDICT: APPROVE.

The patch achieves one supported typed composition path, retains existing evidence, and gives public task execution the necessary admission and resource checks. Coverage of changed production paths is ADEQUATE on static inspection. Confidence is HIGH for the scoped behavior and documentation; execution, integration, package and benchmark outcomes remain separate evidence. This review does not establish completion of the broader D03/D04 lifecycle, D13 stage-identity work, D38 acceptance, or the entire refactor.

## Parent validation record

The parent agent ran the implementation checks separately from this static
review. The integrated suite selected 943 tests: 942 passed, one failed, and one
live integration test was deselected (108.53 seconds). The sole failure was the
machine-file check for two conformance-matrix selectors that still named tests
moved or renamed during this extraction. Updating those selectors changed no
implementation, test assertions, or conformance status.

After that correction, `tests/test_machine_files.py`,
`tests/conformance/test_import_directions.py`, and `tests/test_runtime_api.py`
passed together: 25 tests in 3.14 seconds. The full suite was not repeated after
the selector-only correction. The isolated installed-wheel check passed within
the integrated run and in its earlier 10-test package gate. Ruff over `src`,
`tests`, and `examples`, plus `git diff --check`, passed. All Python commands used
the frozen lockfile with the Dagster extra enabled.

These are local regression and installed-package results. No CI, deployed
scheduler, scale qualification, publication, or complete D38 experiment-loop
claim follows from them.
