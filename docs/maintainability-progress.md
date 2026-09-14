# Maintainability implementation evidence

This dated pre-Core record preserves its original findings. Current implementation
owners and retirement evidence are in the [Core ownership map](core-model-implementation-map.md);
use the [architecture guide](architecture.md) for current APIs.

This records local work against the [to-do list](maintainability-todo.md), begun
from `b1736e9` on 2026-09-11. Git history records the logical local commits;
this evidence does not establish remote CI, a merge, publication, or full
conformance. The pre-existing catalogue-cleaning findings
under `docs/history/` remain separate work.

The later dataset refactor retires the old portable implementation and its
fixture/calibration chain. The checks below describe their recorded revisions;
current contributor commands live in the maintained guides.
The [qualification guide](qualification.md) supersedes the historical custom
conformance runner below and separates native regression evidence from capacity
and publication claims.

## Completed outcomes

| Items | Outcome and evidence |
| --- | --- |
| A1 | Attachment reporting accepts explicit receipt, selection, and output paths. Replaying the existing September 5 sample produced the same 4,207-byte report as the original implementation: SHA-256 `df30817f951086294b3b8849ebcd57276d87ff1023d4bd6c414ccfdcd631fa42`. Help and the repository portability check pass. The historical report calculations remain unchanged. |
| A2, A3, A5, A6 | Added `CONTRIBUTING.md`, a task-to-code/test map, reproducible local/CI commands, optional-dependency guidance, and packaging instructions. Repaired all 14 wiki overview links and checked maintained-page relative links. CI now preserves the Dagster extra explicitly on `uv run` commands and supplies the vendored Rulespec wheel to its empty-environment installation check. |
| A4 | [Offline walkthrough](offline-walkthrough.md) runs the real catalog policy/builder, local application lifecycle, release commit, and catalog verification on one synthetic local HTML input. The original `run_local` supplied the closed-request execution path; current callers use the shared [Python runtime](python-runs.md). The documented module entry point passes with sockets forbidden; repeated output paths refuse before changing the first result. Walkthrough, CLI, input-bound, active-run, and dependency checks: 33 passed. |
| B1–B4 | Added maintained architecture, decision-status, documentation-ownership, and schema/fixture guides. They distinguish application release state from portable bundles, accepted rules from implementation evidence, and generated snapshots from maintained guidance. |
| Documentation consolidation | [Merged useful wiki material](history/2026-09-11-wiki-consolidation.md) into maintained catalog, extension, and operations guides. Retired 28 generated summaries, duplicate navigation trees, and the extracted dependency dump. Exact generation metadata and a per-file hash inventory retain provenance; the original snapshot remains in Git. Current documentation routes through one maintained index. |
| C1, C3, C4 | Removed the four confirmed unused private definitions; corrected the universe-staging rationale; reused UTF-8 offset calculation; shared SDK-free S3 error interpretation. The full default suite passed after these changes. |
| C2, C7, C8 | [Cleanup decisions](cleanup-decisions.md) record the retired application wrapper and unused public helpers, shared member descriptors, and retained policy serializers/caches. `preserved_captures` now uses the existing ordered run-root helper. Current callers govern retention; legacy compatibility is not required. |
| C5, D3 | CLI commands now live in focused `cli/` modules; the largest is 354 lines after the move. `docspec`, `python -m docspec.cli`, `main`, and `build_parser` remain available. Shared JSON I/O preserves error types and output handling. The intentional input correction bounds the actual read, including growth after `stat`; two focused tests cover that race. Failure-receipt hashing uses the same bounded reader. |
| C6 | Serial and parallel catalog derivation share fixed-count digest headers, deferred detail-count headers, and final result assembly. Every scheduling, streaming, and payload loop retains its original syntax tree. Catalog, succession, fallback, and installed-wheel checks: 95 passed. [Raw comparison evidence](maintainability-catalog-comparison.json) preserves all samples and exact derived values for 4,096 rows across 64 partitions. |
| D5 | Split local storage into blobs, controls, document stores, records, catalog, and narrowly shared file operations. Existing public class imports and profile strings remain valid. All 26 moved definitions retained identical syntax trees. Storage, catalog, bounded-partition, and boundary checks: 117 passed. |
| D6 | Reduced `StoreExecutionService` from 1,867 to 705 lines by separating read-only checkpoints (541), processor runtime (339), shared processor rules (167), stage evidence (36), and base preparation (305). Store saves, one cumulative budget, materialization, and accepted-failure decisions stay in the service. Prepared base content transfers the same mutable records/results/receipts, preserving partial failed work. Independent reviews approved each slice and reproduced the original statements after explicit owner/field substitutions. Final focused recovery, cache, budget, policy, pipeline, import, and incremental-equivalence checks: 80 passed. |
| D1 | Separated release format/identity rules, coverage calculations, member/schema reading, body indexing, and semantic validation. Verification now lives in `adapters.document_release.verify`; builders import rule owners directly, and the old import facade is removed. All 109 moved definitions retained identical syntax trees before comment cleanup. Release, builder, schema, encoding, and package checks: 298 passed. The fixture restamper still matches a clean rebuild. |
| D2 | Split the source-catalog artifact into building, reading, verification, source inputs/recovery, bounded rows, accounting, schema rules, and digest derivation. The largest module was 499 lines at the move boundary. All 97 definitions moved unchanged; three calls then became owner-qualified so fault injection still reaches the shared engine or reader. Catalog, succession, spawned workers, recovery, installed-wheel, offline, and boundary checks: 126 passed. Six further runs over the existing 4,096-row catalog preserve every digest, count, diagnostic, and actual serial/parallel engine identity. |
| D4 | Source conversion now names joins, normalization, selection, and provenance. The common policy module records the six interpretation forms and stopping decisions; source policies still choose their own rules. Document/comment conversion shrank from 403/227 to 103/100 lines; Federal Register conversion shrank from 245 to 82. [Comparison evidence](maintainability-policy-comparison.json) records byte-identical results for 246 calls, including two refusals. Independent review led to a real retained-filing conversion test and three strict comment-version refusal checks. Policy, catalog, wheel, offline, and import checks: 142 passed. |
| D7 | The command-receipt reader is 17 lines, catalog `build` is 67, and root-binding validation is 60, reduced from 300, 230, and 266. Named phases preserve validation order, lazy digest computation, resource lifetimes, byte accounting, and gate-before-commit publication. Helper expansion reproduces the original statement trees. A durable-workspace test proves publication retry without recomputing policy, and all 29 complete portable verdicts remain byte-identical after the binding split. The longer digest-plan helper retains the existing nine-entry declaration and its identity rationale. |
| D8 | Split the 1,697-line source-catalog store into pinned filesystem operations, staging, immutable lookup/publication, and current-pointer advancement. All 21 definitions and seven constants retain identical statement trees; fault injection now targets each real owner. Independent review confirms descriptor lifetimes, safe cleanup, blob-before-root publication, and pointer admission order. Storage/catalog/wheel/import checks: 110 passed; 16 public-boundary checks passed again after removing an incidental export. [Size decisions](cleanup-decisions.md#large-modules-reviewed-by-responsibility) retain the coherent catalog schema, scale family, and bounded segmentation algorithm with their rationale. |
| E1 | Shared setup lives in `tests/support/` and `tests/helpers.py`, including formerly dynamic cross-test imports. The extraction preserved all 638 original named test functions and their assertions; later predecessor retirement deliberately removes obsolete acceptance tests. Conformance test selectors stay at their existing paths. Full default suite after extraction: 875 passed, 3 skipped, 1 deselected. |
| E2 | Organized the three large test files into eight catalog suites, five portable-verifier suites, and three Regulations.gov suites, with explicit family setup in `tests/support/`. All 183 test functions, 230 collected cases, and original test/helper statements remain; parameter IDs and order within each function are unchanged. Focused runs passed all 230 cases. Independent review caught three moved conformance selectors; corrected paths preserve names, order, and declared status. All 206 configured selectors collect successfully (248 cases), and the actual conformance runner passes all 11 source-catalog checks. The contributor and schema guides now route to the focused suites. |
| E3, E4 | Contributor guidance defines dependency direction, helper ownership, size review prompts and exceptions, public interfaces, and reviewer responsibilities. The PR template asks for behavior, validation, format/API impact, and open follow-up work. |

## Verification checkpoints

- Default suite after initial cleanup and test-support extraction: **875 passed,
  3 skipped, 1 deselected** in 100.69 seconds.
- Full suite after storage and CLI changes, with the Dagster extra: **890 passed,
  1 deselected**, no skips, in 100.95 seconds.
- Release refactor and shared member descriptor: **298 focused checks passed**;
  `restamp_document_release_fixtures.py --check` reports a matching clean rebuild.
- Full suite after the release refactor and offline example, with Dagster:
  **891 passed, 1 deselected**, no skips, in 114.79 seconds.
- Full suite after the catalog split, source-policy refactor, and review fixes:
  **896 passed, 1 deselected**, no skips, in 111.32 seconds.
- After removing the two old import facades, catalog, release, installed-wheel,
  offline, and boundary checks: **343 passed**. After removing the unused
  profile and CourtListener helpers: **21 passed, 1 deselected**. The policy
  writer also completes a real canonical round trip and refuses overwriting
  its existing output after switching to the shared CLI input reader.
- Logical commits were checked in isolated staged snapshots: storage **22**,
  portable module split **284** plus a matching fixture rebuild, catalog split
  **148**, CLI split **64**, policy conversion **138**, and offline example **1**.
  Test-support extraction passed **886** suite checks plus **2** installed-wheel
  checks after the temporary validation checkout was given Git tracking and a
  separate runtime directory. The receipt-validator and catalog-build phase
  refactors then passed **123** and **96** focused staged-snapshot checks.
- Predecessor portable-reader retirement preserves all **29 complete current
  verifier results**, including diagnostic messages and ordering (comparison
  SHA-256 `6be952d9035c014a095db9cf991206fdd99a7d4995b6244147335f001f84ad27`),
  and all **1,067 fixture/schema file hashes**. Independent review caught and
  closed a malformed-schema-set refusal gap using complete, correctly restamped
  bundles. The corrected release/schema/builder/encoding checks: **203 passed**;
  the fixture restamper still matches a clean rebuild.
- The combined staged tree through predecessor retirement passed the full suite
  with Dagster: **816 passed, 1 deselected**, no skips, in 122.57 seconds. The
  lower count reflects withdrawal of obsolete predecessor acceptance tests.
  The subsequent root-binding phase extraction passed **203** focused checks
  and preserved every byte of the 29-result current-corpus replay above.
- Final execution and test organization were checked in isolated staged snapshots:
  checkpoint verification **69**, processor runtime **69** plus **8** dedicated
  cache checks, catalog storage **110**, and organized tests/conformance **239**.
  Base preparation then passed **80** focused checks, including incremental
  equivalence. Independent reviews approved each change. Test execution exposed
  missing fixture registration; independent review caught three stale conformance
  selectors. Both were corrected before the combined checks.
- The complete implementation at `d38dd9c` passed the full default suite with
  Dagster: **816 passed, 1 deselected**, no skips, in **114.74 seconds**. All
  **1,067** sealed fixture/schema files and **29** complete current verifier
  results remain unchanged; the fixture restamper matches a clean rebuild.
- A clean local checkout of `d38dd9c` builds both distributions. Its wheel installs
  with the pinned vendored Rulespec wheel in an empty environment; the public
  APIs, extracted execution/storage owners, and installed CLI work outside the
  source checkout without Dagster, boto3, or httpx installed.
- The [sealed conformance report](history/2026-09-11-maintainability-conformance.json)
  records a clean checkout of `d38dd9c`: **294 test executions passed**, with no
  failures, errors, or skips. **15 of 24 requirements pass**; nine remain declared
  partial, so the overall verdict is `fail` with
  `DOCSPEC-CONFORMANCE-TEST-NOT-IMPLEMENTED`. This is incomplete qualification,
  not a failing test or a conformance claim. The local source checkout initially
  refused because it contained unrelated untracked work; the clean copy pins the
  tested commit without changing that work. The CI comment now describes partial
  evidence without its obsolete implementation counts.
- All **40 command help pages** compare byte for byte with the baseline parser.
  An empty environment successfully installs the built wheel together with its
  pinned vendored Rulespec dependency, imports the public APIs, and runs the
  installed command. The first installation attempt exposed the unavailable
  public-index dependency; CI and the guide now supply its wheel explicitly.
- Ruff and `uv lock --check` pass. Sealed fixture files and packaged schema bytes
  have not been regenerated or changed. The final documentation check resolves
  **84 relative file links** across 11 maintained entry pages.

The catalog comparison preserved every digest, count, diagnostic field, and
reported engine. Separate serial batches varied (median 1.784 → 2.094 seconds).
Alternating the old and new implementations in the same process did not reproduce
that slowdown (five samples each, median 1.851 → 1.761 seconds). The two-worker
medians were 1.072 → 1.063 seconds. These are small local checks for a regression,
not evidence of a speedup or large-corpus qualification.

Independent static reviews approved the storage/CLI split, portable verifier,
catalog artifact split and import retirement, and source-policy conversion.
The reviews led to corrected fault-injection imports, failure-receipt coverage,
and stronger cross-filing and strict comment-version tests. Architecture review
also approved retiring predecessor portable-format handling while retaining
both current 2.0 workflows. The runtime now uses current portable rules only;
frozen predecessor data remains provenance and, for the old source-catalog
fixture, a required current restamping input.


## Follow-up: documentation, tool placement, and remaining code owners

The follow-up through `3b5f289` implements the September 11 request for another
organization and superseded-code pass. Three subagents covered documentation,
tool architecture, implementation, and independent review. All scoped reviews
approved the corrected result; this does not replace the human exercise below.

| Area | Completed change and evidence |
| --- | --- |
| Documentation | Consolidated useful wiki explanations into three maintained guides for catalog/processing, extensions, and operations. Retired 28 generated pages and two duplicate navigation trees; moved exact generation metadata and retained all 31 tracked-file hashes. The [consolidation record](history/2026-09-11-wiki-consolidation.md) distinguishes Git provenance from the removed ignored dependency cache. |
| Tool placement | Added a [tool inventory](../tools/README.md). Integrated policy creation into `docspec source-catalog write-policy` and moved source-catalog command handling under `cli/`. Architecture review retained the fixed-corpus portable mint as a historical recipe and the research, proof, schema, and fixture tools in their present roles. [Cleanup decisions](cleanup-decisions.md#place-tools-according-to-their-current-purpose) explain the current consumers and limits. |
| Transport owners | Split the 832-line fetcher into local-file, HTTPS, S3, and routing modules; the largest is 335 lines. The same 11 public exports remain, and all 17 moved definitions have identical syntax trees. |
| Regulations.gov owners | Split the 2,131-line module into six implementation owners; the largest is 530 lines. Explicit dependencies preserve selection/resume state, field provenance, and lazy identity calculation. The corrected extraction preserves all 174 complete outputs/refusals from 74 existing tests, byte for byte: SHA-256 `c5414b8c0b72cd834b107a952fd553400aa2b378976eecdd3d66f0fa7561b833`. Six additional cases pass against both old and new classes and preserve refusal order without filling the identity cache. |
| Shared research reading | Seven tools reuse one manifest/JSONL reader. Their complete before/after receipts remain byte-identical on the same mixed plain/gzip inputs with a deterministic publisher substitute. Actual worker and direct-script checks run separately. Outside-home catalog paths now produce a receipt instead of failing. Removed an unused constant and the superseded three-argument sample-worker input. |
| Current reader and error handling | Removed the `spicy_regs` installed-package fallback and renamed the adapter for its current `spicy_docs` owner, preserving accepted artifact producer labels. Expected missing-reader errors now reach the CLI's structured error response. Malformed policy URL templates now receive the existing policy validation error, with real-command tests for null, number, object, list, and missing-placeholder inputs. |
| Interrupted tool receipts | Resume discards only an incomplete final record, preserves valid final JSON without a newline, and refuses corrupt completed lines. Dry runs remain read-only. Focused tests cover truncated JSON/UTF-8, normal and reprobe continuation, and complete-line corruption. |

Final local checks at `3b5f289`:

- Full suite with Dagster: **850 passed, 1 integration test deselected**, no
  skips, in **117.18 seconds**. Ruff, dependency-lock validation, and whitespace
  checks passed.
- A clean clone built the source distribution and wheel. Its installed command
  created a canonical policy member outside the checkout in an empty environment
  with vendored Rulespec and without Dagster, boto3, httpx, or SpicyDocs installed.
  Imports of the new transport, policy, and reader owners also passed there.
- The fixture restamper still reports a matching clean rebuild. All tracked
  files under `fixtures/`, `tests/fixtures/`, and `src/docspec/schemas/` are
  unchanged from `b0878fd`.
- The consolidated documentation check resolved **208 local links and 17
  anchors** across 13 pages and verified the 31 tracked wiki hashes and exact
  generation metadata. The final evidence-only additions also pass: **213 local
  links and 19 anchors** across 14 pages.

The 174-record comparison covers the mechanical policy extraction at `8fd8600`;
the subsequent malformed-template correction in `c27bd34` deliberately changes
invalid-input handling. Likewise, the earlier 40-page help comparison predates
the new policy command and current-reader help text. These checks establish
local behavior and packaging; the earlier sealed conformance report remains
pinned to its recorded revision and does not qualify the new tree automatically.

## Still open

**30 of 31 items are complete.** E5 requires an unfamiliar person's contribution
exercise and feedback; line counts and this agent's familiarity cannot substitute
for that evidence. The [walkthrough exercise](offline-walkthrough.md#unfamiliar-contributor-exercise)
now lists the steps and evidence to return: commit, change, governing rule,
commands/results, elapsed time, files consulted, and friction. No participant
result has been supplied, so E5 remains unchecked.
