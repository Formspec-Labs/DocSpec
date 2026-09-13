# Superseded portable route removal review

## 1. Summary and scope

**Static verdict: APPROVE the D18/D27/D30 simplification, subject to removing or explicitly resolving the six-file orphan noted below.** The new result export is the supported consumer route. The old campaign-specific builder, portable verifier, retention-floor system and their exclusive support are removed. Current retained state, processors, source evidence, failure accounting, catalog verification and the shared Rulespec integration remain.

Reviewed the deletion inventory in `/tmp/docspec-portable-removal-files.txt`, remaining code/test/tool edits against `ff5fe91`, concrete extraction-quality tests, surviving current validation paths, import/caller searches and maintained documentation changes. The reviewer ran no tests/builds and changed no source. Parent owns the full regression and commit evidence.

## 2. Deletion and surviving call traces

The reviewed inventory contains 1,087 deleted files: 14 runtime modules, nine packaged schemas, 1,047 old portable fixture files, ten exclusive test/support modules, five campaign/calibration/restamping tools and two campaign pin/calibration files. The large file count is predominantly generated fixture trees, not a deletion of 1,087 separate product capabilities.

Removed runtime code is isolated to `adapters/document_release/`, `document_release_support.py` and `processing/retention_floors.py`. Before deletion, its executable callers were the fixed campaign builder, calibration/restamper chain and their old-format tests. Searches of remaining source, tests, tools and conformance data found no executable import or configuration reference to the removed modules and tools.

The active retained model in `domain/release.py:21–24` keeps the same current format version and shared derivation identity. Its only change removes a stale comment coupling that model to the now-deleted portable namespace. `adapters/storage/catalog.py` still uses `adapters/platform_artifact.py`, which already delegates structural identity and membership to Rulespec. This current retained-state integration is deliberately preserved.

`application/commit.py:172` retains `DocumentReleaseVerifier`; `:217` calls the existing logical-layer verifier. `domain/delivery.py:250` retains cross-layer and exact identity checks, and `:588` parses current disposition/terminal-failure evidence. `application/execution_checkpoints.py:123` and `:187` continue to use shared stage and processor receipt verification. The new export consumes those established semantics instead of transplanting the old campaign's document/comment/attachment taxonomy.

The attachment research tool remains. `tools/select_attachment_sample.py:126` removes only its `selected-pdf` mode, whose sole caller was the deleted floor-population tool. Its ordinary unavailable-document selection still feeds the same counts and sample rows, and its only current worker caller at `:207` agrees with the simplified three-field tuple. The research tests retain ordinary selection, distinct population counts, provenance and other tool entry-point checks.

## 3. Quality evidence and simplification judgment

`tests/test_extraction_quality.py` replaces an unsupported implication of quality with concrete observations and explicit limits:

- At `:29`, the known complete visible output is less than 1% of source markup because script bytes are intentionally suppressed. A retention floor would reject useful complete visible text.
- At `:42`, dropping a short critical block still retains over 90% of markup and passes verification of the mappings that were actually supplied. A high ratio cannot establish source completeness.
- At `:58`, a named visible-block resolver refuses invented duplicated content. At `:75`, empty HTML/XML visible output remains an explicit input failure.
- At `:80`, deterministic PDF provider pages expose an empty page and refuse a false truncated-page mapping. This qualifies DocSpec's evidence behavior, not a real PDF parser or all PDF extraction quality.
- At `:95`, exact source pins detect changed/truncated supplied bytes; a newly captured shorter unpinned upstream response cannot reveal an absent passage by itself.

The export guide records these distinctions and retains `semanticCompleteness=not-established`. `nonempty-text` is an explicit consumer requirement; failures can still be retained as experiment results. No replacement ratio, semantic score, new failure taxonomy or parser replay framework was added. The old calibration implementation is unnecessary once its unsupported policy is retired.

Removing exclusive tests and generated fixtures is justified because the old format itself is intentionally unsupported. Current invariants still have direct tests. Historical-byte reproduction and legacy readers are expressly not user requirements; provenance remains available in Git and dated decision records.

## 4. Surviving verification and test evidence

Current retained admission still has `tests/conformance/test_document_release_integrity.py:106` covering every retained authoritative object and `:134` refusing missing source bytes. These were not deleted.

The export suites retain independent workspace-free reading, unchanged stage-call counts, complete active populations, explicit failure/capture-only policy choices, wrong producer/pin/reference refusals, bounded output, exact segment bytes, interruption/no-replace publication, late tampering, mixed owning plans and same-plan/different-result identity. The installed probe copies exact capture and processing evidence and reads it after source/workspace removal.

The failed-item repair/frontier suites remain, including final failure classification, relevant versus unrelated changes, complete prefix reuse, missing promised receipt refusal, empty completed segmentation and interrupted repair accounting. Canonical JSON, source catalog, storage, parser and processor tests remain independent of the removed portable format.

The reviewer additionally checked the installed shared artifact implementation for a jointly understated root/manifest bound: `_iter_manifest_members` sums actual descriptor sizes and checks its reference totals at `_artifact.py:2869–2886`; `_admit` fully exhausts that iterator at `:3026–3062` before opening payload members at `:3106–3113`. This preserves the export's pre-read bound without a second local manifest parser.

**Execution:** no reviewer execution. Parent is running the final regression over the stable removal candidate and will retain its outcome separately. Previous export qualification is recorded in the export review; no new full-suite result is inferred here.

## 5. Findings and remaining limits

No material surviving-code defect was found. Import directions narrowed with the deleted module; no compatibility alias or hidden fallback remains. Contributor/schema/tool guides point to the current runtime and result-export suites. Dated decisions preserve their original reasoning with an explicit retirement notice.

**F1 — small orphan fixture input.** `tests/fixtures/source_catalog_release_v1/valid/` still contained six files at the review snapshot. It was the deleted portable restamper's source input, and no remaining source/test/tool/conformance reference was found. Remove those files after confirming the caller search, or identify a concrete current use. This is a bounded cleanup item in the same removal scope; do not retain it solely for historical reproduction.

Current retained-state verification cost and the export's bounded multi-pass admission are unchanged by deleting old code. This review does not claim broad large-corpus qualification, exhaustive parser quality, semantic completeness or removal of every unused declaration across the repository.

## 6. Verdict and checklist acceptance

**APPROVE D18** as concrete quality characterization and removal of unjustified retention floors, with the recorded limits above. The acceptance wording must not imply a universal detector of missing meaningful text.

**APPROVE D27** because the supported result export uses the shared generic container, the replaced portable structural implementation is removed, and current retained-state mapping has a distinct continuing role through the same shared package.

**APPROVE D30** for this inventoried retirement once the six-file orphan is resolved. The surviving tools have distinct current research/schema/reporting purposes; the old mint, calibration and restamping route no longer imposes a second product path. This is an evidence-backed removal, not a compatibility migration.

## Parent follow-up

The six source-catalog fixture input files were confirmed to have no remaining
caller and removed. The final deletion count is **1,093 files**; the six extra
files were test input, not retained user data. No compatibility route remains.

The first complete regression passed 1,068 tests and failed three assertions
that still expected the old encoder's UnicodeEncodeError. Their earlier
source-refusal and lazy-identity assertions passed. The shared encoder instead
raises its public ValueError subclass with the exact lone-surrogate context;
the test now expects that behavior. All 67 focused policy/encoding checks passed.
An unused test import left by the earlier encoder removal was also deleted.
The final complete regression passed **1,071 tests**, with one live integration
case deselected, in **273.45 seconds**. It emitted two existing Python warnings
from explicit `os.fork()` crash tests running after threaded tests; both crash
checks passed. The run used the frozen Dagster and S3 extras and includes the
actual installed provider, runtime, export and native Dagster probes. Ruff and
diff checks passed, and 429 maintained-document local links resolved. These are
local checks; no new remote CI, merge, release or capacity claim follows.
