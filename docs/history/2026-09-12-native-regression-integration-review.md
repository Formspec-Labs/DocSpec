# DocSpec native regression integration review

**VERDICT: APPROVE. No actionable defect found in the reviewed D37 integration.** Native pytest now supplies the test execution and reports, while a small optional hook prevents incomplete required work from producing a successful strict result. The map retains the existing behavioral checks and explicitly separates regression evidence from capacity and publication claims. Profile flattening preserves the existing normalized description identity.

## 1. Scope and patch summary

Independent semi-formal static review on September 12, 2026, using `/Users/mikewolfd/.agents/skills/semi-formal-code-review/SKILL.md`. Reviewed the current uncommitted D37 changes against committed HEAD `1d73408b12ef5fa40de47dc30b68f970c8702005`: the regression map and native hooks/tests, deleted custom runner/CLI/specification, profile field flattening and callers, CI, and current qualification/contributor/operations documentation.

The session-scoped wheel fixture and corresponding installed-test argument changes are a separate D35 slice. I inspected them where they affect the new map and installation evidence, but this certificate does not claim a complete review of every D35 boundary-test simplification. Earlier D39 implementation slices and unrelated history remain outside scope. The protected untracked catalogue-cleaning history file was not read or modified.

The separate hook review in `/tmp/docspec-regression-hook-review.md` approves the two-file hook boundary. This review independently traces the wider integration; it does not treat that earlier approval as evidence for CI, profiles or requirement scope decisions.

**No pytest, build, installation, live request or source edit was performed by this reviewer.** Read-only JSON/AST comparisons and `git diff --check` passed. The coordinating parent owns focused/full strict-suite execution and its attribution. This approval establishes reviewed behavior, not an unreported test result or remote CI success.

The removed runner repeated pytest once per requirement group and serialized a separate DocSpec report, including empty dataset artifact lists, zero byte counters and unmeasured peak memory (`HEAD:src/docspec/conformance/runner.py:426–487`). Its only executable repository consumers were the removed CLI and runner tests. The new code consumes native collection and phase reports without executing selectors itself or adding another persisted result format (`tests/conftest.py:56–136`).

## 2. Function and integration trace

| Function / boundary | Location | Input → output | Verified behavior |
| --- | --- | --- | --- |
| `pytest_addoption` | `tests/conftest.py:39` | native parser → optional strict flag | Ordinary focused pytest is unchanged unless strict admission is explicitly requested. |
| `_unique_object`, `pytest_configure` | `tests/conftest.py:47`, `:56` | repository map + native arguments → config stash | Reject duplicate/empty declarations and parameter-specific map selectors; strict positional arguments must select files/directories, preventing uncollected parameter siblings from disappearing. No shell execution or second collection pass. |
| `pytest_itemcollected` | `tests/conftest.py:87` | native item → original node ID set | Records the collected population before normal deselection. |
| `pytest_collection_finish` | `tests/conftest.py:92` | map + original/selected node IDs → required phase sets | Each function must collect at least one exact node or parameter case. Every collected matching case must remain selected. Missing or deselected work raises a native usage error. |
| `pytest_runtest_makereport` | `tests/conftest.py:109` | native setup/call/teardown report → phase set or failed sentinel | Any non-pass or expected-failure report permanently disqualifies the node. Returns the original report, rather than maintaining a second test-result representation. |
| `pytest_sessionfinish` | `tests/conftest.py:121` | required states + native exit status → final native status/diagnostic | Requires exactly successful setup, call and teardown. Empty required bookkeeping under strict mode refuses. Existing nonzero exits remain; otherwise incomplete work changes success to test failure. |
| `_project` and pytester regressions | `tests/test_regression_gate.py:16–126` | actual hook source + tiny fixture project → native subprocess outcomes/JUnit | Tests the real hook under native collection, reporting, skips and early exits, without a simulated report implementation. |
| `docspec_wheel` fixture | `tests/conftest.py:17` | session temporary directory → one built wheel path | Native session scope reuses one build; consumers still create distinct installed environments. It introduces no runtime or package service. Reviewed as an integration seam for separate D35 work. |
| `_description_identity` | `src/docspec/profile_registry.py:45` | machine profile → normalized pinned fields | The former implementation already normalized `verifier.testId` to top-level `verifierTestId` for hashing. The new direct field produces the same identity input. The removed status was never included. |
| `ProfileRegistry.from_file` | `src/docspec/profile_registry.py:112` | closed machine JSON → registered profile | Requires the current closed shape and a nonempty verifier test ID. Configuration digest, schema/media declarations, capabilities, limits and compatibility validation remain. No predecessor field fallback. |
| `ProfileRegistry.select` | `src/docspec/profile_registry.py:183` | explicit profile IDs → profile pins | Still rejects unimplemented selections and missing dependencies, and pins the complete normalized description. Test status never authorizes implementation use. |
| `_local_profiles` | `src/docspec/runtime/storage.py:49` | plan pins + workspace descriptions → actual registered profiles | Still requires exact selected-pin equality and the supported local implementation modules before storage composition. Flattening does not weaken execution/recovery configuration admission. |
| `_cmd_profile_list`, `_cmd_profile_verify` | `src/docspec/cli/profiles.py:52`, `:30` | profiles → description output | List output exposes the requirement ID without a hand-maintained pass status. Verification still reports both physical file digest and semantic description digest; it does not execute tests. |
| `_required_test_ids`, machine-profile test | `tests/test_machine_files.py:31`, `:35` | one map + all packaged profiles → consistency assertions | Verifier IDs must name actual map groups. All implementation modules, roles, dependencies, schemas and limits remain checked. |
| CLI/import removal | `src/docspec/cli/parser.py:9`, `:251`; `tests/conformance/test_import_directions.py:17` | production entry points/import map → current surface | Removes conformance command registration and the dead package dependency direction. The native pytest helper remains test-only; production gains no pytest dependency. |
| CI regression step | `.github/workflows/ci.yml:33–49` | locked environment + checkout → native pytest exit/JUnit/log | Installs Dagster and S3 extras, clears external `PYTEST_ADDOPTS`, runs strict pytest once, preserves pipeline failures and requires a nonempty JUnit file. |
| CI evidence retention | `.github/workflows/ci.yml:51–73` | current build/report files → workflow artifacts | Retains report/log/lock/wheels even on failure, under the workflow commit. Separate wheel installation still checks core imports and help. No custom conformance report is published. |

## 3. Coverage fidelity and identity invariants

**Requirement scope is revised openly, not converted from partial to passed.** `docs/qualification.md:38–55` accounts for all nine formerly partial groups. The historical code-absence group is retired; search completeness becomes retained-result evidence, scale becomes scale-format validation, and package publication becomes installed-package behavior. The guide explicitly leaves actual capacity, source reliability, semantic quality and publication unestablished (`:57–87`). Removing the obsolete mandatory size ladder does not claim those measurements happened.

I compared the old and new JSON map values independently:

- Old map: **24 groups, 252 selector references, 206 unique functions**.
- New map: **23 groups, 256 selector references, 211 unique functions**.
- The five old unique selectors no longer named are the deleted runner test, three historical symbol/archive absence tests, and the renamed installed-wheel test. The latter is retained under `test_installed_wheel_preserves_public_runtime_and_packaged_resources`.
- Ten unique functions are newly mapped: the renamed installed-wheel test; installed provider, bill and native Dagster probes; two real SDK retry tests; and four retained export/admission checks. No existing current runtime behavior selector was lost.
- All **211** current unique selectors resolve to actual function declarations in the named files by static AST inspection. This is not a claim that pytest collection or all their parameter cases were executed by me; strict native collection remains the authoritative check.

The map intentionally repeats functions under multiple requirement groups. The hook merges them into node IDs and observes the same native execution once (`tests/conftest.py:98–106`). It does not repeat a large installed test for every group that cites it. The explicitly unrequested live integration case is not mapped; strict mode requires all mapped parameter cases, not every optional test in the repository.

**Failure and omission cannot become a required pass through phase bookkeeping.** Native collection captures parameter cases before `-k`/marker deselection. Missing or omitted cases refuse before execution. Phase state is monotonic: empty set → successful phases, or permanent `None`; later success cannot erase a previous skip/failure (`tests/conftest.py:87–117`). Session finish also refuses collect-only, setup-only and early successful exit without completed required work (`:121–136`). Native pytest's expected-failure wrapper sets `wasxfail` on XFAIL and non-strict XPASS reports (`.venv/lib/python3.12/site-packages/_pytest/skipping.py:276–312`); the hook consumes those reports and the native subprocess tests exercise that behavior.

**CI supplies the outer process boundary.** A pytest exit before session startup may bypass session-finish hooks. CI independently requires the native JUnit file after a successful pipeline (`.github/workflows/ci.yml:46–49`). Bash `pipefail` preserves a failing pytest command through `tee`; a report is not enough on its own. Native JUnit and the console log are complementary: the hook may reject an otherwise successful/XPASS run at session finish, so consumers must use the command/workflow outcome and retained log rather than count only JUnit pass entries. The qualification guide directs reviewers to the exact workflow run and separates local output from another commit's CI evidence (`docs/qualification.md:25–31`). This is not protection against arbitrary hostile plugins rewriting pytest or fabricating reports, nor is such a new process framework warranted here.

**Profile bytes change, while their effective pinned values remain identical.** For all ten packaged profile JSON files, removing `verifier.status` and moving `verifier.testId` to `verifierTestId` produces exactly the current parsed JSON value; no configuration, limit, capability, implementation, version or compatibility value changed. The identity builder used that same flattened test-ID key before this patch (`HEAD:src/docspec/profile_registry.py:45–67`; current `:45–67`). Therefore the normalized description pin and selected profile set remain unchanged, although the physical file/directory digests correctly change. The current loader rejects the old closed shape; this is a deliberate greenfield descriptor change, not a compatibility claim.

The test reference remains identity-bearing exactly as before. It names a requirement group, not an implementation capability or a pass claim. Runtime selection still uses implementation status, dependencies and exact profile pins; it does not read the regression map or verifier status (`src/docspec/profile_registry.py:183–207`; `src/docspec/runtime/storage.py:49–59`).

## 4. Test and edge-case evidence

Inspected tests support these expected outcomes; none were executed by this reviewer:

| Concrete case | Evidence | Expected outcome |
| --- | --- | --- |
| Every parameter case passes once and native JUnit names both | `tests/test_regression_gate.py:29` | Successful strict run, two native cases, one execution each. |
| Ordinary focused invocation with an irrelevant/missing map selector | `tests/test_regression_gate.py:38` | Passes without strict mode, preserving normal contributor workflow. |
| Renamed function or module-level optional-import skip | `tests/test_regression_gate.py:45–56` | Missing required selector causes usage error instead of treating an empty population as success. |
| `-k` removes one parameter; positional selection hides siblings | `tests/test_regression_gate.py:59–74` | Both refuse, through original-versus-selected population checking and the positional-node guard respectively. |
| Call skip, XFAIL, XPASS, setup skip/error, teardown error | `tests/test_regression_gate.py:77–93` | Required node remains incomplete and strict execution fails. |
| Collect-only/setup-only and successful early exits | `tests/test_regression_gate.py:96–117` | Missing phases or an uninitialized required population prevent success. |
| Empty or duplicate declarations | `tests/test_regression_gate.py:120–126` | Native usage error with the map diagnostic. |
| Profile value drift and mismatched execution pins | `tests/test_profile_registry.py:37–50`; `tests/conformance/test_profile_descriptions.py:48–140` | Complete description changes remain pinned; malformed/unpinned machine values and changed plan pins refuse before work. Removing the test that toggled retired verifier status is appropriate. |
| Every packaged profile references a current requirement | `tests/test_machine_files.py:35–62` | All ten descriptions still load, name implemented modules and map verifier IDs to existing regression groups. |
| Real SDK requests versus document attempts | `tests/test_s3_native_retries.py:64–110` | The newly mapped parameterized tests count actual SDK HEAD/GET requests and inspect separately attributed document failures and replay. They are not constructor-only retry assertions. |
| Installed native Dagster recovery | `tests/test_dagster_experiment.py:15–45` | Uses the shared built wheel in a fresh environment, runs the existing actual installed probe, and asserts no repeated captures/completed sibling work. A missing Dagster dependency cannot silently qualify this mapped test. |
| Retained results versus new-task counts | `tests/test_result_export.py:23–99` | Newly mapped checks independently open results after removing original roots, preserve exact layer data, avoid processing repeats, account for capture/failure gaps, and export the complete population of a zero-task successor. |
| Structurally valid but false segment evidence | `tests/test_result_export_admission.py:82` | The newly mapped adversarial test reseals the shared container and still requires semantic refusal of wrong representation-slice bytes. |
| Installed public API and packaged descriptions | `tests/test_package_boundary.py:217–350` | The renamed test retains exact packaged profile/schema checks, clean installation, core imports, CLI and the installed public runtime probe. The shared session fixture changes build ownership, not isolated installation behavior. |

The parent will provide full strict-suite/JUnit execution evidence after integrating the final candidate. This certificate does not reuse the earlier 1,071-pass suite as proof of the new strict gate.

## 5. Findings and hypotheses

**No actionable findings in this integration.**

- **H1 — deleting the custom runner silently drops behavioral coverage:** refuted by the map comparison. Removed checks belong to retired machinery/absence assertions; the renamed package test and additional native/export/provider tests preserve or improve current evidence.
- **H2 — skipped/deselected/partly executed work can count as required success:** refuted for the native lifecycle and supported strict command by the collection/phase traces and real pytester edge cases. CI addresses the separate pre-session report boundary without adding a process wrapper.
- **H3 — flattening profile verification metadata changes runtime identity or removes admission:** refuted by all-ten-profile JSON equivalence after the explicit field transformation and the unchanged normalized identity/selection path.
- **H4 — native test success is being presented as capacity or publication evidence:** refuted by the renamed map groups and explicit qualification limits. Those claims still require actual measurements or destination evidence.
- **H5 — the replacement recreates the removed runner under a different name:** refuted. It adds one map and bounded in-memory native report bookkeeping, no scheduler, report format, execution loop or production dependency. The optional session wheel fixture uses normal pytest dependency injection.

## 6. Conclusion

**VERDICT: APPROVE.** Coverage of the changed integration: **ADEQUATE**. Confidence: **HIGH** on the inspected coverage, phase, profile-identity and composition boundaries. The coordinating parent retains responsibility for executing the final strict suite, committing the reviewed candidate and qualifying remote CI. D37's representative capacity work remains open, as documented.
