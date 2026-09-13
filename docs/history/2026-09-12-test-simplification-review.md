# D35 test simplification review

Date: 2026-09-12. Reviewer: independent `review_profile_docs` agent.
Initial snapshot: `1d73408`, before the bounded package-test cleanup authorized during this assessment. References below identify that reviewed snapshot; an implementation addendum will identify changes separately.

**Decision: simplify test setup and structural assertions, while retaining the current behavioral cases.** The useful next changes are a shared built-wheel fixture, removing historical-name and spelling guards, and constructing typed runtime fixtures directly. No evidence supports deleting the recovery, refusal, cost, resource-lifetime, or installed-interface scenarios merely because several suites exercise the same runtime.

This is a bounded application of the requested semi-formal-code-review skill. Initial assessment was static: no tests, builds, Vulture scans, or sibling-repository operations were run. The unrelated untracked historical findings file was not read. Parent owns D37 runner retirement and requirement-selector changes; those are excluded from the implementation recommendations here.

## 1. Scope and intended change

D35 asks for meaningful behavior checks without making internal moves expensive (`docs/dataset-experiments-todo.md:1127`). Existing progress already reuses the same offline behavioral test in its installed environment, preserves crash/publication checks, and replaces obsolete catalog CLI shape assertions (`docs/dataset-experiments-todo.md:1135`, `:1144`).

The assessment examined the package/import boundaries; four installed-wheel entry tests; their environment setup; the shared offline behavioral test; typed runtime, local experiment, workspace, worker-identity and stage fixture setup; and their existing support helpers. It is not an exhaustive classification of every test in the repository.

No production behavior change is proposed. Parent subsequently authorized implementation of finding F1 in `tests/test_package_boundary.py` only; F2 and F3 remain recommendations. No replacement test framework, general fixture object model, persistent environment cache, or benchmark is warranted.

## 2. Function and fixture traces

| Function or fixture | Snapshot location | Inputs and output | Verified role |
| --- | --- | --- | --- |
| `_working_directory_expression` | `tests/test_package_boundary.py:122` | AST expression → root identifier/string | Strips attributes, indexing and path arithmetic. It checks a variable's spelling, not its actual destination. |
| `test_no_repository_code_names_a_sibling_checkout_or_an_outside_working_directory` | `tests/test_package_boundary.py:134` | Repository Python source → violations | Combines literal local-path checks, literal module-name checks, actual AST imports and an exact set of `cwd` root names. These are separable assertions. |
| `test_superseded_source_formats_are_absent_from_repository_code` | `tests/test_package_boundary.py:223` | Files, exports and arbitrary decoded text → absence checks | Encodes retired filenames and symbols, including concatenated strings to avoid matching its own scan. It does not exercise the supported catalog reader. |
| `test_production_imports_stay_inside_the_standalone_boundary` | `tests/test_package_boundary.py:285` | Absolute AST imports → allowed dependency edges | Preserves the actual third-party/inner-layer boundary and the one shared canonical gateway. The archived-name branch is independently removable. |
| `test_non_docspec_product_areas_are_absent_from_production` | `tests/test_package_boundary.py:320` | Directory names and raw source → violations | Checks retired directory names and words in source, including documentation, with file-specific exceptions. |
| Native import checks | `tests/conformance/test_import_directions.py:120`, `:136`, `:194` | Production modules and an independent interpreter → import-edge/loaded-module checks | Relative and absolute imports are checked; the complete core may load only the shared encoder's established dependency baseline. These protect behavior that textual name bans do not. |
| Installed package test | `tests/test_package_boundary.py:396` | Fresh build → isolated venv → copied examples and probes | Checks package resources, console entry, dependency absence and real public runtime operation outside the checkout. |
| Installed provider test | `tests/test_source_catalog_installed_wheel.py:40` | Another fresh build → distinct provider/core/consumer environments | Separates reader-only dependencies from provider execution and proves independent admission. |
| Installed native Dagster test | `tests/test_dagster_experiment.py:15` | Another fresh build → native multiprocess probe | Copies actual examples and verifies native interruption/reexecution and saved document checkpoint reuse. |
| Installed bill test | `tests/test_govinfo_bill_installed_wheel.py:16` | Another fresh build → provider acquisition + DocSpec environment | Copies the actual example and checks its provider pin, captured XML, offline later processing and refusal meaning. |
| `_seeded_local_run` | `tests/support/profiles.py:25` | Temporary root and profile pins → CLI request path and roots | Creates real source bytes/catalog and a typed plan, then writes that plan and CLI JSON just to make it available to callers. |
| `_local_run_request` → `_local_run_arguments` | `src/docspec/cli/requests.py:102`, `:154` | CLI JSON and saved plan → typed runtime kwargs | Parses closed CLI shapes, producer records, policies, clock, roots and execution settings; then rereads the plan. |
| Typed runtime fixture | `tests/test_runtime_api.py:25` | `_seeded_local_run` → private CLI parser → kwargs | Deletes request/plan files only after parsing; execution is file-independent, but fixture setup still depends on the CLI representation. |
| High-level experiment and shared repair fixtures | `tests/test_local_experiments.py:23`, `tests/support/experiments.py:37` | Same CLI round trip → typed configuration | Only need the typed catalog/workspace/settings for most cases. CLI parity is an explicit separate test at `tests/test_local_experiments.py:223`. |

## 3. Data flow and invariants to preserve

1. **The installed artifact and installation isolation are different controls.** Every installed suite currently builds DocSpec independently (`tests/test_package_boundary.py:400`; `tests/test_source_catalog_installed_wheel.py:48`; `tests/test_dagster_experiment.py:25`; `tests/test_govinfo_bill_installed_wheel.py:31`). Reusing those wheel bytes within one test session removes repeated build work. It must not reuse mutable environments: the core-only/provider reader checks intentionally observe absent optional dependencies, and native Dagster/acquisition examples intentionally install different extras.
2. **Test input configuration should belong to the interface under test.** The seed helper already has the typed `ProcessingPlan` before serialization (`tests/support/profiles.py:55`). Python tests need that object, a `LocalWorkspace`, producers and settings. CLI cases still need the real request writer and parser. Splitting the existing helper preserves one source/catalog setup and avoids manufacturing a second public runtime API in tests.
3. **Import direction is the boundary, not a historical filename or a word in a comment.** Preserve actual AST import checks and the one explicit shared canonical dependency (`tests/test_package_boundary.py:285`; `tests/conformance/test_import_directions.py:120`). Preserve complete-core dynamic import checks (`tests/conformance/test_import_directions.py:194`) and fresh-environment dependency absence (`tests/test_package_boundary.py:473`). Removing text/name inventories does not authorize importing sibling source trees.
4. **Resource and reuse observations are behavior.** Worker metadata mismatch tests assert zero chunk reads, exactly one close, no capture and an artifact-integrity failure (`tests/test_local_worker_identity.py:126`). Saved-worker changes assert refusal before fetch (`:57`); live post-prepare mutation is a separate boundary (`:162`). Those tests should remain even though they share setup.
5. **Local and installed execution of the same assertion body is deliberate.** `tests/test_package_boundary.py:560` copies `tests/test_offline_example.py` and runs it against the installed wheel. The latter observes actual per-phase calls and clean/reused output agreement (`tests/test_offline_example.py:18`, `:68`, `:71`). These are two environments, one behavioral definition. Do not collapse them into summary-only smoke checks.

## 4. Test behavior and concrete edge cases

| Case | Existing evidence inspected | Assessment |
| --- | --- | --- |
| Rename a safe `cwd` variable without changing its value | `_working_directory_expression` and exact allowlist at `tests/test_package_boundary.py:122`, `:161`, `:172` | Current test fails solely on spelling. Remove this pseudo path analysis; do not replace it with a custom path evaluator. Keep concrete local-checkout literal/import checks and actual isolated execution. |
| Mention Rulespec in a truthful docstring outside a named exception | Raw regex at `tests/test_package_boundary.py:329` | Current test fails despite no import. Remove raw-word ban; shared dependency remains constrained by actual import edges. |
| Split a runtime composition module | Exact module set at `tests/conformance/test_import_directions.py:158` | Current test requires an unrelated allowlist edit. A future bounded change could assert allowed areas plus the fixed inner→outer prohibition. This is outside the single-file cleanup now authorized and should be coordinated with D37's owner. |
| Introduce an optional import into the core | `tests/conformance/test_import_directions.py:194`; installed absence checks at `tests/test_package_boundary.py:496` | Keep both the runtime loaded-module baseline and genuine clean installation. The current-interpreter `-I` smoke at `tests/test_package_boundary.py:353` is weaker: `-I` does not uninstall extras. It may be retired later because the stronger installed test executes import and CLI help. |
| Change CLI request format while preserving Python API | Python fixture at `tests/test_runtime_api.py:25`; parser at `src/docspec/cli/requests.py:104` | Many Python behavior tests fail during unrelated CLI setup. Return typed seed values directly, while retaining CLI parser cases and explicit default parity. |
| Alter worker configuration after prepare vs during reconstruction | `tests/test_local_worker_identity.py:90`, `:162` | Keep both. They exercise different times at which identity must be enforced. |
| Construct an unrequested stage, or use another selected implementation | `tests/test_runtime_api.py:90`; `tests/test_local_experiments.py:92` | Construction traps are justified: they protect optional dependency loading and ensure the selected implementation executes. Do not remove them as mere private-name references. The latter's private executor identity assertions could eventually become direct call observations, without deleting the case. |
| Derive the same plan from small and explicit settings | `tests/test_local_experiments.py:67` | Keep. It compares independent supported entrypoints, pinned handoff/profile and actual run identity, rather than restating one implementation's intermediate dictionary. |
| Duplicate artifact tests vs duplicate behavior implementations | Four wheel builds above; copied offline test at `tests/test_package_boundary.py:560` | Share build output only. The separate installed scenarios and same copied behavior are useful qualification, not redundant assertions to delete. |

No claim is made that all existing tests will pass after an unimplemented fixture change. The proposed boundaries are supported by static traces; parent-owned focused execution must establish the final patch result.

## 5. Findings and small changes

### F1 — WARNING: historical and spelling guards make harmless refactors fail

**Category:** maintainability. **Locations:** `tests/test_package_boundary.py:73`, `:122`, `:223`, `:320`, `:344`, `:378`.

The suite keeps lists of obsolete product areas, filenames and public names after their implementation has deliberately been removed. It also scans raw words and permits `cwd` expressions by root-variable spelling. These assertions do not exercise today's supported interfaces and require extra exception updates for truthful documentation or internal moves.

**Small change:** remove the three historical-only test functions, their private scan/constants, archived-name assertions within other tests, and raw sibling-word/`cwd` spelling checks. Retain actual AST sibling/dependency imports, concrete developer-checkout path checks, packaged resource bytes, current metadata/entrypoints, real isolated imports and optional dependency absence. Preserve the lazy Dagster import behavior while dropping removed-class and scheduler-directory absence assertions. Root authorized only this change during this review.

### F2 — WARNING: four independent builds duplicate package setup

**Category:** maintainability and repeated work. **Locations:** `tests/test_package_boundary.py:400`; `tests/test_source_catalog_installed_wheel.py:48`; `tests/test_dagster_experiment.py:25`; `tests/test_govinfo_bill_installed_wheel.py:31`.

All four commands build the same checkout with `uv build --wheel` during one test session. Each then creates a genuinely different consumer environment.

**Small change:** one session-scoped pytest fixture returns a built wheel from a session temporary directory. Tests may copy that wheel into their private wheelhouse. Keep virtual environments, copied examples, provider pin verification, interpreter isolation and dependency-absence assertions per test. A shared subprocess/venv class hierarchy is unnecessary. Coordinate the fixture registration with the agent currently editing `tests/conftest.py` for D37. This eliminates three build invocations when all four suites run together; no runtime speedup has been measured.

### F3 — WARNING: Python behavior fixtures depend on private CLI serialization

**Category:** maintainability and interface independence. **Locations:** `tests/support/profiles.py:25`; `tests/test_runtime_api.py:25`; `tests/test_local_experiments.py:23`; `tests/test_local_worker_identity.py:33`; `tests/test_runtime_stages.py:61`; `tests/support/experiments.py:37`.

These callers create a typed plan, serialize it and a CLI request, then use private CLI readers to reconstruct typed kwargs. A harmless CLI-only change therefore affects tests for Python injection, recovery, failure repair and stage reuse.

**Small change:** separate the existing real source/catalog/typed configuration setup from its CLI file-writing wrapper. Use direct typed values in Python-focused fixtures. Keep `_write_local_run_request` and request parsing in CLI/workspace cases; keep `test_shared_execution_defaults_preserve_explicit_cli_behavior` as a dedicated cross-interface parity check. Do not introduce another configurable test-runner abstraction, migrate unrelated low-level fixtures, or change any behavior assertion as part of this extraction.

### F4 — OBSERVATION: the remaining focused support and qualification checks have distinct jobs

**Locations:** `tests/test_offline_example.py:18`; `tests/test_local_worker_identity.py:57`, `:126`, `:162`; `tests/test_local_experiments.py:67`, `:116`; `tests/conformance/test_import_directions.py:136`, `:194`.

Real call observations, exact captures, dependent processor ordering, early refusal, explicit close, saved handoff identity and independent installed consumers are useful defenses against different failures. The separate core import prohibition also protects against accidentally widening the general allowed-area map. No blanket test deletion or helper-file split is justified by file length, similarly named cases, or a static unused-name list. Callback exports, fault-injection controls and dataclass equality remain explicitly excluded from speculative removals.

The D37 wrapper retirement and its duplicate subprocess execution are parent-owned. This review does not propose another runner, another result format, or a second implementation of its selector qualification.

## 6. Conclusion

**VERDICT: APPROVE the bounded simplification direction.** F1 is authorized for a single-file implementation. F2 and F3 are concrete remaining work, not claims of already completed cleanup.

- The proposed changes reduce incidental setup and spelling dependencies while preserving the inspected behavior and installed boundaries.
- Static coverage is adequate for these bounded recommendations; this is not an exhaustive test deletion audit or evidence of full-suite execution.
- Confidence is high in F1–F3's concrete traces and medium in broader suite-ownership completeness.
- D35 should close only after the selected simplifications land and focused gates pass, or after explicitly recording why a remaining setup cost is retained. No new tests are needed merely to justify removing historical-name assertions.

## Authorized F1 implementation addendum

Parent authorized the bounded edit to `tests/test_package_boundary.py` after the assessment. The file now removes 194 lines and adds five, with no changes by this reviewer to production, other tests, configuration or selectors. Static parsing and the scoped `git diff --check` pass. The independent package execution is pending the parent's shared runtime slot.

Deleted historical-only test functions:

- `test_superseded_source_formats_are_absent_from_repository_code`
- `test_non_docspec_product_areas_are_absent_from_production`
- `test_git_is_the_only_predecessor_code_record`

Renamed surviving tests to describe their remaining checks:

- `test_no_repository_code_names_a_sibling_checkout_or_an_outside_working_directory` → `test_repository_code_avoids_sibling_imports_and_personal_checkout_paths`
- `test_dagster_adapter_is_lazy_and_has_no_parallel_runtime_or_deployment_schema` → `test_dagster_adapter_import_is_lazy`
- `test_docspec_metadata_wheel_has_no_legacy_document_dependency` → `test_installed_wheel_preserves_public_runtime_and_packaged_resources`

Current traces: literal personal/sibling checkout paths and actual sibling imports at line 73; current dependency/extras/entrypoint metadata at line 100; generated schema bytes at line 138; actual core dependency imports and exact shared canonical gateway at line 146; current interpreter import/help smoke at line 177; lazy Dagster import at line 198; isolated wheel resources, absent dependencies and actual copied runtime/example tests at line 217. Parent owns selector updates for these names.

The deleted checks include raw sibling module-string/word scans, the `cwd` variable-name evaluator/allowlist, lists of archived package areas, removed API/class names and scheduler/archive directory absence assertions. Concrete path literals, actual imports, wheel ownership and bytecode exclusion, packaged schema/profile contents, current public entrypoint, isolated environments and missing optional dependency checks remain. No replacement tests were added merely to validate deletion.

### Executed F1 validation

After the parent granted the shared runtime slot, this reviewer ran `uv run --frozen --extra dagster --extra s3 pytest -q tests/test_package_boundary.py`: **7 passed in 11.97 seconds**, log `/tmp/docspec-d35-package-gate.log`. This includes the actual isolated wheel/runtime/example checks, not only static package assertions. Scoped Ruff and `git diff --check` also pass. No broad suite was run. F1 is complete locally; parent owns final review, selector updates and commit. The wheel-fixture and typed-fixture recommendations remain unimplemented at this point.

## Follow-on setup simplifications prepared under parent authorization

**Shared wheel:** the four installed consumer files now accept the parent's `docspec_wheel` session fixture and no longer run their own build commands. Private virtual environments, optional dependency absence, provider hashes, copied examples/probes and behavioral assertions remain. The provider and bill suites still copy the shared artifact into private wheelhouses. The two local subprocess helpers no longer accept a checkout-working-directory override that only their removed build call needed. Parent owns the fixture in `tests/conftest.py` and final combined execution. This reviewer made no changes to that file.

**Typed fixtures:** while the parent froze the repository for its full regression, this reviewer prepared only temporary source copies and a unified patch at `/tmp/docspec-d35-typed-fixtures-k8k8gy_j/typed-fixtures.patch`. It changes exactly 14 files, with 66 additions and 72 deletions, and passes static AST parsing plus read-only `git apply --check`. It has not been applied or executed at this point.

The patch extracts `_seeded_local_run_arguments` in existing `tests/support/profiles.py`, returning the original plan, workspace, policies, producers, local execution defaults, fixed clock/deadline, partition policy and sink ID. `_seeded_local_run` becomes its CLI serialization wrapper. Thirteen Python-focused caller files use the typed helper: `tests/support/experiments.py` and the runtime API, local experiments, worker identity, runtime stages, inspection, read-only storage, capture lifecycle, prefix reuse, task membership, inspection evidence, prefix budget and export admission suites. Existing CLI, workspace, profile-conformance and release-selection consumers keep the file writer. The real CLI-default parity case still calls the actual private CLI adapter, so it does not become a comparison of a helper with itself. Existing Python file-independence is explicit: the typed runtime fixture executes with neither request nor plan input JSON present.

No production API, new fixture class, generic test runner, persistent cache or test-environment model is introduced. No existing behavior case is removed by either follow-on patch. Final application, combined regression evidence and commits remain parent-owned.

Parent review corrected one parity dependency before application: the CLI wrapper must keep `_write_local_run_request`'s independent explicit 1/1 worker defaults. The temporary patch now omits the new derived execution local and max-workers/max-in-flight overrides from that wrapper. Consequently, a changed Python default can fail the existing comparison against the independently specified CLI fixture. The corrected patch still passes AST parsing and `git apply --check`; no live edit or runtime occurred.

## Parent integration evidence

The native regression replacement and shared wheel build passed 1,076 tests with one live integration deselected in 249.91 seconds before commit `1643526`. The subsequent typed fixture patch preserves the CLI writer's independent 1/1 defaults; 169 affected runtime, recovery, worker, inspection, prefix, export, CLI and profile cases passed in 48.98 seconds. Ruff and diff checks passed. These are local execution results, not remote CI.
