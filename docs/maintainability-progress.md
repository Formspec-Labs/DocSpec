# Maintainability implementation evidence

This records local work against the [to-do list](maintainability-todo.md), begun
from `b1736e9` on 2026-09-11. Git history records the logical local commits;
this evidence does not establish remote CI, a merge, publication, or full
conformance. The pre-existing catalogue-cleaning findings
under `docs/history/` remain separate work.

## Completed outcomes

| Items | Outcome and evidence |
| --- | --- |
| A1 | Attachment reporting accepts explicit receipt, selection, and output paths. Replaying the existing September 5 sample produced the same 4,207-byte report as the original implementation: SHA-256 `df30817f951086294b3b8849ebcd57276d87ff1023d4bd6c414ccfdcd631fa42`. Help and the repository portability check pass. The historical report calculations remain unchanged. |
| A2, A3, A5, A6 | Added `CONTRIBUTING.md`, a task-to-code/test map, reproducible local/CI commands, optional-dependency guidance, and packaging instructions. Repaired all 14 wiki overview links and checked maintained-page relative links. CI now preserves the Dagster extra explicitly on `uv run` commands and supplies the vendored Rulespec wheel to its empty-environment installation check. |
| A4 | [Offline walkthrough](offline-walkthrough.md) runs the real catalog policy/builder, local application lifecycle, release commit, and catalog verification on one synthetic local HTML input. `run_local` provides the same closed-request execution path to CLI and embedded callers. The documented module entry point passes with sockets forbidden; repeated output paths refuse before changing the first result. Walkthrough, CLI, input-bound, active-run, and dependency checks: 33 passed. |
| B1–B4 | Added maintained architecture, decision-status, documentation-ownership, and schema/fixture guides. They distinguish application release state from portable bundles, accepted rules from implementation evidence, and generated snapshots from maintained guidance. |
| C1, C3, C4 | Removed the four confirmed unused private definitions; corrected the universe-staging rationale; reused UTF-8 offset calculation; shared SDK-free S3 error interpretation. The full default suite passed after these changes. |
| C2, C7, C8 | [Cleanup decisions](cleanup-decisions.md) record the retired application wrapper and unused public helpers, shared member descriptors, and retained policy serializers/caches. `preserved_captures` now uses the existing ordered run-root helper. Current callers govern retention; legacy compatibility is not required. |
| C5, D3 | CLI commands now live in focused `cli/` modules; the largest is 354 lines after the move. `docspec`, `python -m docspec.cli`, `main`, and `build_parser` remain available. Shared JSON I/O preserves error types and output handling. The intentional input correction bounds the actual read, including growth after `stat`; two focused tests cover that race. Failure-receipt hashing uses the same bounded reader. |
| C6 | Serial and parallel catalog derivation share fixed-count digest headers, deferred detail-count headers, and final result assembly. Every scheduling, streaming, and payload loop retains its original syntax tree. Catalog, succession, fallback, and installed-wheel checks: 95 passed. [Raw comparison evidence](maintainability-catalog-comparison.json) preserves all samples and exact derived values for 4,096 rows across 64 partitions. |
| D5 | Split local storage into blobs, controls, document stores, records, catalog, and narrowly shared file operations. Existing public class imports and profile strings remain valid. All 26 moved definitions retained identical syntax trees. Storage, catalog, bounded-partition, and boundary checks: 117 passed. |
| D1 | Separated release format/identity rules, coverage calculations, member/schema reading, body indexing, and semantic validation. Verification now lives in `adapters.document_release.verify`; builders import rule owners directly, and the old import facade is removed. All 109 moved definitions retained identical syntax trees before comment cleanup. Release, builder, schema, encoding, and package checks: 298 passed. The fixture restamper still matches a clean rebuild. |
| D2 | Split the source-catalog artifact into building, reading, verification, source inputs/recovery, bounded rows, accounting, schema rules, and digest derivation. The largest module was 499 lines at the move boundary. All 97 definitions moved unchanged; three calls then became owner-qualified so fault injection still reaches the shared engine or reader. Catalog, succession, spawned workers, recovery, installed-wheel, offline, and boundary checks: 126 passed. Six further runs over the existing 4,096-row catalog preserve every digest, count, diagnostic, and actual serial/parallel engine identity. |
| D4 | Source conversion now names joins, normalization, selection, and provenance. The common policy module records the six interpretation forms and stopping decisions; source policies still choose their own rules. Document/comment conversion shrank from 403/227 to 103/100 lines; Federal Register conversion shrank from 245 to 82. [Comparison evidence](maintainability-policy-comparison.json) records byte-identical results for 246 calls, including two refusals. Independent review led to a real retained-filing conversion test and three strict comment-version refusal checks. Policy, catalog, wheel, offline, and import checks: 142 passed. |
| D7 | The command-receipt reader is 17 lines, catalog `build` is 67, and root-binding validation is 60, reduced from 300, 230, and 266. Named phases preserve validation order, lazy digest computation, resource lifetimes, byte accounting, and gate-before-commit publication. Helper expansion reproduces the original statement trees. A durable-workspace test proves publication retry without recomputing policy, and all 29 complete portable verdicts remain byte-identical after the binding split. The longer digest-plan helper retains the existing nine-entry declaration and its identity rationale. |
| D8 | Split the 1,697-line source-catalog store into pinned filesystem operations, staging, immutable lookup/publication, and current-pointer advancement. All 21 definitions and seven constants retain identical statement trees; fault injection now targets each real owner. Independent review confirms descriptor lifetimes, safe cleanup, blob-before-root publication, and pointer admission order. Storage/catalog/wheel/import checks: 110 passed; 16 public-boundary checks passed again after removing an incidental export. [Size decisions](cleanup-decisions.md#large-modules-reviewed-by-responsibility) retain the coherent catalog schema, scale family, and bounded segmentation algorithm with their rationale. |
| E1 | Shared setup lives in `tests/support/` and `tests/helpers.py`, including formerly dynamic cross-test imports. The extraction preserved all 638 original named test functions and their assertions; later predecessor retirement deliberately removes obsolete acceptance tests. Conformance test selectors stay at their existing paths. Full default suite after extraction: 875 passed, 3 skipped, 1 deselected. |
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
- All **40 command help pages** compare byte for byte with the baseline parser.
  An empty environment successfully installs the built wheel together with its
  pinned vendored Rulespec dependency, imports the public APIs, and runs the
  installed command. The first installation attempt exposed the unavailable
  public-index dependency; CI and the guide now supply its wheel explicitly.
- Ruff and `uv lock --check` pass. Sealed fixture files and packaged schema bytes
  have not been regenerated or changed.

The catalog comparison preserved every digest, count, diagnostic field, and
reported engine. Separate serial batches varied (median 1.784 → 2.094 seconds).
Alternating the old and new implementations in the same process did not reproduce
that slowdown (five samples each, median 1.851 → 1.761 seconds). The two-worker
medians were 1.072 → 1.063 seconds. These are small local checks for a regression,
not evidence of a speedup or large-corpus qualification.

The full suite needs another run after the remaining refactors. Continue to use
focused checks during each move, then run the final full suite and clean-wheel
checks. These checkpoints describe the work verified at each stage, not a claim
that the entire to-do list is complete.

## Still open

Execution/checkpoint responsibilities, test-file organization, and the unfamiliar
contributor exercise remain open on the to-do list. The last exercise requires
actual participant evidence; line counts and this agent's familiarity cannot
substitute for it.

D6's first extraction separates read-only checkpoint verification into
`application/execution_checkpoints.py` and shared request/result rules into
`application/processor_rules.py`. The coordinating service still owns every
store save and the one cumulative work budget. All 28 original substantive
methods preserve their statement trees after explicit owner/name substitutions;
the constructor passes the same dependency objects to the verifier. Focused
checkpoint, processor-only recovery, retry/cache, budget, policy-security,
pipeline, result-sink, and recovery-conformance checks: **65 passed**. Processor
runtime and base-reuse preparation were the remaining D6 boundaries at that
checkpoint. The exact staged snapshot also passed four import-direction checks
for **69 passed** in total.

The subsequent runtime extraction gives `ProcessorRuntime` the single copied
registry, retry/cache behavior, and original clock dependencies. It receives the
caller's mutable records, results, receipts, and work budget; failed-attempt
evidence remains available after exceptions. `execution_evidence.py` shares
receipt persistence and sanitized failure classification, while acceptance stays
with the service. All 19 substantive methods preserve their statement trees
after explicit owner substitutions. The same **69 focused checks passed**.
Base-reuse preparation remains before D6 closes.

Independent static reviews approved the storage/CLI split, portable verifier,
catalog artifact split and import retirement, and source-policy conversion.
The reviews led to corrected fault-injection imports, failure-receipt coverage,
and stronger cross-filing and strict comment-version tests. Architecture review
also approved retiring predecessor portable-format handling while retaining
both current 2.0 workflows. The runtime now uses current portable rules only;
frozen predecessor data remains provenance and, for the old source-catalog
fixture, a required current restamping input.
