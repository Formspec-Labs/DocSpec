# Retained result export and independent reader review

## 1. Summary and scope

**Static verdict: APPROVE the bounded D26/D29 implementation.** The public export reads an admitted retained result, copies its active rows and typed byte/evidence dependencies, and publishes a distinct shared artifact. The public reader admits that artifact without the original workspace or processing implementations. It preserves accounted failures and exposes a separate, explicit text requirement. No new planner, scheduler, processing run, quality score, or historical workspace format is introduced.

Reviewed the new `src/docspec/adapters/result_export/` modules, `src/docspec/result_export.py`, `src/docspec/runtime/exports.py`, shared helper extractions in execution evidence, failure frontier, controls and processing artifacts, import boundaries, export tests, the isolated installed runtime probe, and `docs/result-exports.md`. Concurrent shared blob-writer work is excluded. This review was static: the reviewer ran no tests or builds and changed no source files. Parent execution is reported separately below.

This approval does not complete D18's extraction-quality characterization or D27/D30's removal of the superseded portable route. The user's no-legacy instruction means historical-byte reproduction is not a reason to retain that route.

## 2. Function and data traces

- `runtime/exports.py:38–49` validates the chosen admission and byte allowance, resolves the existing pinned storage profiles, opens storage with `create=False`, admits the exact retained release, and checks its processing-plan identity. It constructs no fetcher, extractor, segmenter, processor, or execution backend.
- `adapters/result_export/writer.py:38–48` validates the output producer before creating an owned sibling temporary directory. `:62–97` copies each distinct typed dependency once and refuses conflicting bytes/roles for one object key. Control files retain their exact bytes and are checked through the existing control repository; blobs use the existing verified, closed streaming reader.
- `writer.py:99–121` streams every active layer unchanged, including disposition and failure evidence; row counts must still match the retained layer. `references.py:14–36` follows only declared capture/representation/segment blobs and receipt references; invocation receipts additionally embed their owning plan, result and prerequisite results. Arbitrary processor JSON, source catalogs, base releases, provider resources and historical store/run trees are not recursively followed.
- `writer.py:123–168` writes the small index, delegates manifest/root identity to the installed Rulespec artifact package, admits the completed export, and delegates no-replace directory publication. A repeated exact export admits the existing destination and returns its exact pin; a different or invalid destination refuses. The owned temporary directory is cleaned in `finally` at `:169–171`.
- `writer.py:144–151` includes the source retained artifact's physical digest in the product specification and also supplies the source as an `ArtifactInput`. This is intentional: the shared logical derivation uses input logical identity, while different exact results may share one plan-derived logical release ID. The explicit source physical pin prevents these result exports from sharing an export logical identity accidentally.
- `reader.py:239–263` bounds the root and both root/manifest-declared payload totals before shared admission. Shared Rulespec admission owns membership, sizes, hashes, root identity and the external expected pin. `:157–212` then verifies the closed DocSpec product/index/member shape and required core layers.
- `reader.py:213–226` passes streamed original rows into existing `verify_logical_release_layers`, admits only their typed evidence closure, checks per-item completion and byte evidence, and refuses unreferenced extra members. `:90–105` records each full typed reference; a later request for a different reference, including a different media type, is refused even if its bytes happen to share a digest.
- `admission.py:145–180` checks every item's retained stage ordering, receipt links and requested-stage completion. Successful items cannot omit requested work. `:79–108` uses each item's saved invocation plan, rather than assuming the newest release plan owns inherited processor evidence. Attempts without an owning invocation use the existing explicit unfinished-attempt verifier and cannot invent successful results.
- `admission.py:53–76` verifies segment bytes and evidence against the exact representation slice through `verify_segment_representation`. It checks every declared identity mapping against captured bytes through the small extracted `verify_representation_mapping` helper. Named derived transforms remain recorded evidence and are counted as not replayed.
- `reader.py:107–139` rechecks consumed members before exposing seekable streams, bounded bytes, control values or rows. Explicit iterator closure releases its file; closing the view releases disposable SQLite state. `io.py:47–76` accurately distinguishes ordinary completion's mutation check from abandoned reads, for which only closure is promised.

The shared extractions retain one validation implementation: `storage/controls.py:63–80` now serves workspace and export control loading; `application/execution_evidence.py:142` and `:187` hold the existing retained-stage and unfinished-attempt mechanics, and `failure_frontier.py:126` and `:159` delegate. `processing/artifacts.py:203–220` extracts one mapping comparison from its original full verifier, which still checks source identity before delegation. No fake entry, synthetic plan or second evidence model was introduced.

## 3. Invariants and simplification judgment

The export is a consumer view of the complete active result. Its identity, exact bytes, source coordinates, per-item requests, dispositions and typed processing evidence travel together. It is not an executable workspace backup. The reader does not fetch external provenance or instantiate plugins to reproduce derived transformations. Accepted publisher identity and exact bytes do not establish that every meaningful source passage was extracted.

The two versioned choices have direct user value. `retained-evidence` admits valid captured or processed state, including accounted failures. `nonempty-text` additionally refuses selected failed items, selected items without recognized non-whitespace text, and invalid UTF-8 text. Both keep all rows; neither silently drops documents to pass a threshold. Reasons are counted completely with at most ten sampled source-qualified reasons.

The shared artifact package owns structural identity and publication. Existing DocSpec logical-row, stage-receipt, processor-receipt and byte-evidence helpers own document meaning. The new code is limited to typed closure, export layout, a storage-only public composition and explicit consumer requirements. Its temporary SQLite index provides bounded joining and exact-reference membership; it is disposable and adds no persisted authority or index registry.

The required total byte limit includes root, manifest and payload. Separate finite limits cover metadata/index roots, rows/controls, per-item metadata/control dependencies and representation/segment/identity-source evidence. The first reader uses the existing in-memory evidence primitives with a 64 MiB ceiling per needed evidence blob; raw capture-only blobs may stream within the artifact allowance. The SQLite setting bounds spooled input, not exact database-file size or total process heap. Multiple verification passes and inherited full retained-release admission have real cost; no large-corpus performance or memory qualification is established here.

The no-legacy instruction supports deleting the old portable verifier/builder route after this slice's acceptance. `platform_artifact.py` remains necessary for current retained catalog state and already uses Rulespec. It should not be deleted merely because the old portable subtree is removed. Existing visible-text parsers and evidence behavior also remain necessary for current processing.

## 4. Inspected test evidence

- `tests/test_result_export.py:23` creates a real processed retained result, compares all exact exported active rows, repeats export with the same pin, moves original dataset roots away, reads embedded blobs and invocation dependencies, and traps workspace reopening. Fetch/extraction/segmentation/processor counts remain unchanged.
- `:67` distinguishes capture-only and accepted-failure retention from `nonempty-text` refusal, preserves disposition accounting and checks bounded refusal reasons. `:88` exports a zero-task successor's full inherited population and old owning processor evidence.
- `:102`, `:124`, `:147` and `:173` cover wrong pin/producer/allowance/reference, explicit early iterator closure, interrupted staging/no replacement, and changed/missing/extra members.
- `tests/test_result_export_admission.py:55` reseals an understated root and asserts refusal before any payload member opens. This tests the pre-read bound rather than merely testing a stale root digest.
- `:82` rewrites a segment and its receipt links, reseals all shared descriptors/root, first proves the independent shared container admits, and then requires DocSpec to refuse the false exact slice. The check reaches product semantics rather than stopping at container hashing.
- `:130` creates a real processor result with a large warning body and lowers the named per-item limit for the fixture, proving separately referenced results are charged. `:148` preserves a held failed item's original processor plan after an unrelated processor changes. `:166` uses two real selected runs to create one retained population whose processor invocations come from two distinct owning plans; the independent export reader resolves both and counts both selected items. `:209` compares a failed and successful result of the same plan and requires distinct export logical identities.
- `tests/support/installed_runtime_probe.py:223–262` runs export through the installed public API, asserts no additional stage calls, closes the prepared services, renames both the original workspace and source input, and independently opens exact exported captures, representations and invocation plan/results. A fresh reader refuses later blob tampering with the original inputs still unavailable.
- Import-direction changes allow only the new outer facade/composition; the reader does not create a core-to-adapter dependency or import CLI execution.

**Parent execution reported at review time:** the actual installed wheel export probe passed **1 test in 9.14 seconds**. The reviewer inspected its assertions but did not execute it. Parent owns focused/adversarial and whole-worktree outcomes and may append them distinctly. No summed or inferred full-suite count is claimed here.

## 5. Findings and remaining scope

**Resolved F1 — pre-admission byte-limit bypass.** Root totals alone could understate the manifest's payload bytes and let shared admission read beyond the requested allowance before detecting the inconsistent counts. `reader.py:250–259` now prechecks the maximum of root and manifest payload totals, plus manifest/root bytes. The resealed-root test checks that payload files never open.

**Resolved F2 — uncharged processor evidence.** Per-item accounting originally omitted separately referenced invocation plans/results/prerequisites before the shared processor verifier retained their parsed values. `admission.py:157–165` now charges each distinct typed dependency before that verifier runs. The real oversized-result test exercises the additional charge.

**Resolved F3 — exact segment bytes were not proved by logical rows alone.** Structural IDs and coordinates could be self-consistent while segment bytes differed from the declared representation slice. The reader now reuses the established byte/evidence helpers, with explicit finite read ceilings, and the resealed semantic-negative test reaches this refusal.

**Resolved F4 — abandoned-reader overclaim.** Closing an abandoned generator releases resources but does not execute the shared member source's post-yield mutation comparison. The helper and guide now make the narrower accurate guarantee.

No material production finding remains. The initially missing multi-owning-plan test is now present: two real selected runs produce one export containing invocations from both distinct plans, and the reader resolves both. A separate real same-plan/different-result test covers the physical source pin in export identity. The reviewer inspected these final additions; their execution remains parent-owned.

D18 remains a separate acceptance gap: nonempty text alone cannot detect a dropped important passage, repeated boilerplate, partial PDF extraction or an unpinned upstream truncation. The guide says so. Concrete supported-format examples must characterize those cases and explain why discarded retention ratios did not establish completeness. This calls for bounded evidence and an honest recorded limit, not a new semantic scoring framework.

D27/D30 remain removal work until the old portable verifier, identity helpers, exclusive schemas, fixed-campaign builder/calibration/restamper chain and their exclusive tests/fixtures are deleted or a current non-legacy purpose is demonstrated. The historical reproduction language in the tool/export guides must follow the user's explicit instruction. Current retained state, shared container integration and reusable processing evidence must stay.

## 6. Verdict and acceptance

**APPROVE for D26/D29 within the stated bounds.** Static traces and inspected tests support optional export without reprocessing, exact source/evidence closure, complete active population accounting, explicit consumer refusal, deterministic repeat/no-replace behavior, and independent installed reading with tamper refusal. Runtime qualification remains the parent's separately attributed evidence.

Do not mark D18, D27 or D30 complete merely because this API exists. Finish the concrete quality characterization and delete the superseded route without a compatibility layer. This review does not establish semantic completeness, replay of named derived transformations, executable historical recovery, or large-corpus qualification.

## Executed acceptance follow-up

The parent ran the final focused gate: **79 tests passed in 27.99 seconds**.
It included both export suites, extraction-quality characterization, failure
frontier/repair, processing/visible-text evidence, import directions and the
package ownership check. The completed tests additionally cover two owning
processor plans in one export and same-plan results with different physical
output pins, closing the review's narrow coverage gap. The earlier actual
isolated installed-wheel probe passed **1 test in 9.14 seconds**.

These checks qualify the bounded local workflows. The PDF quality case injects
known page text into the existing optional-parser seam; it does not establish
real PDF extraction completeness. No current full-suite, remote CI, package
publication, or corpus-capacity result is claimed here.
