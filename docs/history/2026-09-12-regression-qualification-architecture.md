# D37: use pytest for regression and separate qualification claims

Read-only architecture review, September 12, 2026. No tests or source changes.

## Recommendation

**Approve retiring the runtime conformance runner, report verifier and CLI.** Run the normal pytest suite once in CI, retain its native JUnit output and logs, and maintain a current requirement-to-test map. Add one small repository-only pytest hook to ensure mapped tests were actually selected and completed successfully. Keep dataset capacity and package publication as separate, explicitly unqualified claims until their own evidence exists.

This removes duplicate execution and a custom evidence format while preserving the useful omission/skip checks. It does not establish conformance by changing declarations.

## Evidence and lineage

| Artifact | Role and observation |
| --- | --- |
| `docs/dataset-experiments-todo.md`, D37 | Current scope explicitly separates ordinary checks from representative capacity qualification; missing evidence must remain visible. |
| Standalone spec §15.1 | Earlier design requires a sealed report, exact clean Git source and failure for absent/skipped/xfailed tests. The last requirement remains useful; the custom report is an implementation choice. |
| `conformance/specification.json:9–11` | Current declaration still names SpicyRegs, DocumentRelease 2.0 and the 100k/1m/5m sequence, which no longer describes the clarified product. |
| `src/docspec/conformance/runner.py:237–284` | Runs pytest separately for each requirement, captures stdout/stderr in memory, parses a temporary JUnit file, then removes that directory. |
| `runner.py:289–302` | A hand-edited `implemented` label controls the verdict even if every selected test passed. |
| `runner.py:462–483` | Emitted dataset artifacts are empty, plan ID is null, store/byte counts are zero, peak memory is null. Wall time is test subprocess elapsed time. These are not measured dataset-capacity results. |
| `runner.py:490–645` | Reader verifies canonical form, self-contained hashes, shapes and count arithmetic. It does not retain/reopen original test output or bind the required test list to a supplied normative specification. Self-consistency is not independent proof that execution occurred. |
| `.github/workflows/ci.yml:44–59` | CI runs full pytest, then repeats mapped tests through the runner with `continue-on-error`. The workflow does not upload the generated report. |
| `tests/test_machine_files.py:36–104` | Static matrix tests compare duplicated requirement/status declarations and find literal function source text; this is weaker than actual pytest collection. |
| `src/docspec/domain/scale.py:1440–1545` | Existing scale-result admission binds the declared profile/input pins and checks reported values against limits. It does not itself execute or independently measure a dataset. |

The old report's useful assurances should move to the native layer that owns them. Pytest owns test execution, outcomes, selection and JUnit. CI owns exact checkout/run identity, logs and artifact retention. DocSpec owns actual dataset identity, retained-byte verification and the meaning of a capacity claim.

## Smallest remaining repository check

Use a single `conftest.py` hook enabled only by an explicit `--require-regression-map` option in the complete CI command. Ordinary focused pytest invocations remain usable without it. No external plugin, runtime package import, subprocess runner or saved report model is needed.

Strict mode must refuse positional node selectors containing `::`: selecting one parameter case by its full node ID happens before `pytest_itemcollected`, so its unselected siblings would otherwise be invisible. Whole test files/directories are sufficient for the complete gate. Ordinary focused invocations remain unrestricted.

1. Read the one maintained map of current requirement IDs to nonempty, distinct exact pytest function selectors. Remove `status`, `plannedTestModule`, the duplicated specification list and static pass claims. Keep current meaningful requirement text in the map or its directly linked guide.
2. `pytest_itemcollected` records original node IDs before deselection. `pytest_collection_finish` resolves a selector by exact equality or the same function's `[` parameter suffix. Each selector must match at least one original node, and every matching node must remain in final `session.items`. This catches missing/renamed tests, import-time optional dependency skips, and partial parameter omission through `-k` or `-m`.
3. A `pytest_runtest_makereport` wrapper observes pytest's existing reports without replacing them. A required node is complete only after successful setup, call and teardown reports, with no skipped or expected-failure outcome. Do not count an unexpected pass carrying `wasxfail` as clean required evidence without removing the stale expected-failure declaration.
4. At `pytest_sessionfinish`, preserve any existing nonzero exit code; if pytest would otherwise succeed while required nodes are incomplete, return `TESTS_FAILED` and print the missing/unsuccessful selectors. `--collect-only`, `--setup-only`, early successful exit and interrupted/missing execution cannot become complete proof.

Keep only sets/maps for test node IDs and their observed phases. This is test-run bookkeeping, not a second artifact or task ledger. The documented pytest hooks supply original collection and setup/call/teardown reports directly. [Pytest hook reference](https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_collection_finish)

An enabled gate with no admitted required population must also fail at session finish; an early collection exit is not an empty successful workload. The hook proves completed native sessions. A hook that exits during pytest session startup can bypass session-finish hooks entirely; CI must require the native JUnit artifact to exist, and this helper does not wrap or certify the whole Python process.

A post-run JUnit parser is a reasonable alternative but would need to reconstruct exact node IDs and distinguish deselected parameter cases that never appear in JUnit. The native hook has fewer ambiguous joins and does not replay tests.

## Native regression evidence

CI installs the required `dagster` and `s3` extras and runs pytest once with the map check, strict existing configuration and `--junitxml`. Missing extras must fail required mapped evidence even if ordinary local runs may skip them. Actual live-service tests remain outside this regression claim unless a separately configured job executes them; `pyproject.toml:61` currently excludes the integration marker.

Retain JUnit, logs and built wheels using CI artifacts, including on failure; tie them to the exact workflow run and checkout SHA. A command piped to `tee` must preserve pytest's nonzero status. Treat a failed or interrupted job as incomplete, regardless of how much JUnit was written. The pytest native format already records test outcomes and durations. [Pytest JUnit documentation](https://docs.pytest.org/en/stable/how-to/output.html#creating-junitxml-format-files)

GitHub artifacts provide immutable artifact IDs and a SHA256 digest. Download verification currently warns on mismatch; a consumer requiring fail-closed integrity should explicitly check the expected digest or checksum file. Do not advertise a warning as a rejection. Artifact hashes bind retained bytes, while trusted CI provenance establishes who ran the job; neither proves capacity. No custom DocSpec report seal is required. [GitHub artifact validation](https://docs.github.com/en/actions/tutorials/store-and-share-data#validating-artifacts)

## Preserve qualification without a broad success label

Map all nine former partial requirements in `docs/qualification.md`, stating what is supported and which claims remain unqualified:

| Former partial requirement | Current disposition |
| --- | --- |
| BOUNDARY-IMPORT | Keep actual current import-direction and optional-dependency tests; recognize the deliberate shared canonical JSON dependency. |
| BOUNDARY-CODE | Keep current package/source ownership checks; retire requirements whose only purpose was reproducing predecessor implementation. |
| SOURCE-CATALOG-CONTRACT | Keep current public catalog/provider admission and outcome evidence. Separate representative catalog capacity and optional physical formats not qualified. |
| RELEASE-MANIFEST | Keep current application release and portable-result artifact admission; explicitly retire the removed DocumentRelease 2.0 model. |
| COMPLETE-SEARCH-CORPUS | Replace with retained-result evidence; generic datasets do not claim downstream search completeness. |
| SCHEDULER-PORTABILITY | Keep actual local/native Dagster and real failure/recovery evidence. Record unsupported deployments or multi-node scale as unqualified. |
| DOCUMENT-RELEASE-INTEGRITY | Keep current application release, lineage, exact bytes and malformed-reference checks. Do not equate renamed requirements with newly executed checks. |
| SCALE | Rename the regression row to scale-format validation. Actual dataset capacity remains unqualified. |
| PACKAGE-RELEASE | Rename the regression row to installed-package validation. Publication remains a separate release fact. |

For an actual retained capacity claim, choose a representative workload that tests the supported operation, rather than requiring every contribution to run the entire historical ladder. Pin the real input or deterministic generator, plan/stage/resource/profile settings, installed package bytes, source revision, machine limits, cache conditions, operation and recovery scenario. Retain actual results plus wall-time, memory and storage/scratch evidence and a clean-result or invariant comparison. Verify the referenced outputs through existing catalog/result admission, and state the measured population/limitations. In particular, D37 already identifies retained-result verification memory growth as unmeasured; a fixture passing does not settle it.

Keep existing `ScaleProfile`/`ScaleResult` format checks and schemas during this bounded runner retirement. Their review as a capacity-reporting shape can be separate if a real campaign demonstrates needless fields. They cannot substitute for observed measurements and reopened evidence. Package publication similarly requires the actual published version/wheel digest and installation evidence, not only a local build test.

## Scope and acceptance

Remove `src/docspec/conformance/`, `src/docspec/cli/conformance.py`, parser registration and runner/report-only tests. Update the CLI surface test and import-direction registration. Retire `conformance/specification.json`; simplify `conformance/test-matrix.json`; preserve the scale schemas and real behavioral test modules. Replace static selector-source-text checks with actual collection/outcome checks. Update CI, contributor/operations guidance and standalone spec §15 plus the obsolete unconditional campaign prerequisite. Keep historical reports immutable as historical evidence.

Use pytest's built-in `pytester` fixture for focused hook qualification: valid run including all parameter cases; renamed/missing selector; deleted optional module through import-time skip; runtime skip; xfail; failed setup/teardown; parameter deselection; collect-only; and an early exit leaving required work unrun. An ordinary focused run without the option must still work. Verify JUnit is generated natively and the CLI no longer advertises the retired commands. Parent owns execution of these tests.

## Alternatives, risks and verdict

Keeping and repairing the 668-line runner preserves duplicate test execution and status/report maintenance without a demonstrated consumer for a custom runtime evidence API. Removing every check beyond vanilla pytest is smaller but loses required optional-integration and omission protection. The one native hook is the narrow middle choice.

The principal risk is making the smaller map appear to certify retired or unexecuted requirements. Explicit nine-row disposition, no static pass statuses, separate qualification evidence and a failure-preserving CI command prevent that claim. A real need for externally verifiable standalone test evidence with no CI authority would reopen the report-format decision; none was found in current callers.

**Verdict: APPROVE the reshape.** It serves contributors with one clear regression command and assessors with retained original evidence. It pays down duplicate execution, serialization and status declarations while preserving real correctness checks. Capacity and publication remain distinct and unqualified until demonstrated. Confidence is high for the runner/CI seam; no runtime success was inferred or tested here.
