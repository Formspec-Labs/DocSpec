# Source collection outcomes review

## 1. Patch and scope

**Static verdict: APPROVE.** D08/D10 consume SpicyDocs' existing collection report and evidence methods, preserve them in catalog receipts, and require a separate explicit choice for partial versus total record rejection. The implementation does not add collection execution, provider validation rules, or a second failure ledger.

This review covered `domain/source_outcomes.py`, source descriptions/summaries and schemas, the SpicyDocs adapter, catalog builder/admission, runtime and CLI composition, preview/inspection, the public facade, focused tests, the installed provider fixture, and the maintained guides/decision note. Native Dagster/profile changes in the same worktree were reviewed separately. The reviewer read sources and tests, performed no test/build execution, and changed no repository files except this retained review report at the root's request. The unrelated historical findings file was not read.

The agreed user benefit is precise: a contributor can see what the provider published, what it rejected, and why, then choose whether that input is acceptable for a dataset experiment. Reported source collection counts remain distinct from catalog selections, fetched documents and processor work.

## 2. Function and evidence traces

| Concern | Inspected path and result |
| --- | --- |
| Provider boundary | `src/docspec/adapters/spicy_docs_source_native.py:90` requires the installed public outcome/evidence capabilities after ordinary independent provider admission. It snapshots the report in a `SourceNativeDescription`; `:130` returns that snapshot. `record_evidence`, `iter_failures` and `read_evidence` delegate to the provider's existing membership/byte checks. No upstream failure ledger is copied. |
| Local acceptance and immutability | `domain/source_outcomes.py:12` defines the four existing provider outcome names and the default empty/no-rejection set. `:26` checks the local decision fields and source scope, freezes exact JSON and enforces its byte cap. `ports/source_catalog.py:35` validates the description, bounds its complete serialized size, and exposes a detached JSON copy. `:498` repeats those same shared checks for admitted summaries. |
| Before-write composition | `adapters/catalog_artifact/builder.py:106` uses one private described-source delegate, not a new source model. `_snapshot_sources` at `:122` reads each original description once, checks acceptance and aggregate description bytes. `runtime/catalogs.py:45` applies it before scratch/storage construction; `cli/source_catalog.py:639` applies it before publication. The underlying builder at `:296` uses the exact captured descriptions. |
| Persistence and resume | Builder `:373` binds complete descriptions and sorted accepted outcomes into the existing resume identity. `adapters/catalog_artifact/inputs.py:184` compares that identity canonically. Builder `:480` retains complete descriptions in receipt format `2.0`; `:575` refuses an oversized receipt instead of omitting evidence. Catalog item/policy shapes remain unchanged. |
| Catalog admission | `adapters/catalog_artifact/verification.py:109` reads bounded exact small members and checks the current schema. `:136` parses retained descriptions, requires canonical accepted-outcome order, checks each decision and exact root source pins, and recomputes the existing source-system/schema-set digests. Ordinary reopening needs no SpicyDocs import and does not repeat upstream collection admission. |
| Public views and command verification | `application/catalog_preview.py:27` exposes descriptions and acceptance. `application/inspection.py:158` exposes them only with independently admitted source summary and names the provider-reported scope. `cli/source_catalog.py:214` shares description parsing with command receipts; `:535` compares complete descriptions by canonical bytes, independent of source ordering. The CLI uses command-receipt format `2.0` and a repeatable explicit accepted-outcome option. |

The pinned provider's reader and admission implementation were checked independently during architecture review. It owns outcome classification, count equations, stable traversal and original evidence membership. The catalog's accepted producer declares the retained report under an exact source pin; DocSpec is not claiming to prove that upstream report again.

## 3. Invariants and design judgment

- **Partial and total rejection remain different choices.** Defaults accept only `empty` and `no-record-rejections`. Accepting partial rejection does not authorize an all-rejected source with zero published records. CLI options replace the whole accepted set when supplied; the guide shows how to match the Python defaults-plus-partial example.
- **Unreported is not successful.** Ordinary supplied records retain `collectionOutcome: null`. They do not invent a provider observation. The accepted-outcome policy applies to reported provider outcomes.
- **Incomplete collection does not become an accepted partial dataset.** The current SpicyDocs provider refuses unresolved traversal and transient/unclassified collection failures before publishing an admissible release. Its accepted partial result represents deterministic record rejection. DocSpec does not introduce a new unresolved-outcome state or infer permanent source absence.
- **Exact JSON evidence stays exact.** Full descriptions are deeply frozen, canonically compared for resume and command verification, and retained without truncation. JSON numeric and Boolean values remain distinct.
- **Acceptance does not alter dataset population semantics.** A successor catalog is still the full chosen universe. Omitted prior items enter planning as deletions subject to run selection; `observed-crawl` and acceptance of partial/total rejection do not request incremental upserts.
- **Original evidence has one owner.** The catalog contains the report and source pin. Reading raw provider evidence later still requires the original provider artifact/blobs and independent verifier acceptance. Returned failure limits bound returned rows, not necessarily the provider's total ledger scan.

The small parser, one description snapshot and existing receipt fields are justified by the actual caller decision and recovery invariant. A separate provider semantic verifier, generic acceptance class hierarchy or extra failure ledger would duplicate existing ownership without serving this slice.

## 4. Tests and documents inspected

`tests/test_source_collection_outcomes.py:37` refuses disallowed partial/total inputs before iteration or workspace creation. `:51` distinguishes partial-only admission from total rejection. `:61` reopens null, empty, rejected and no-rejection catalogs while keeping collection rejection out of catalog failure counts. `:75` proves description snapshotting and exactly one original `describe()` call. `:98` interrupts a real persistent catalog build and refuses changed acceptance or nested JSON description on resume, then compares the restored build with clean output. `:129` and `:136` cover metadata/receipt overflow without truncation. `:146` executes a retained partial successor: the missing item becomes deleted, the kept capture is reused, and the provider outcome remains inspectable.

`tests/test_source_catalog_installed_wheel.py:593` extends the existing isolated wheel proof with actual provider-published empty, partial and total-rejection releases. It reads original rejected-record bytes, checks zero/one failure sampling, refuses an undersized evidence read and unknown evidence pin, and distinguishes provider placeholder failures from published-record evidence. Public catalog building refuses unapproved rejection before creating output; preview and prepared inspection preserve the exact report. The actual provider also refuses two unstable traversals before release publication. These tests use the installed pinned provider rather than a substitute DocSpec classifier.

The final installed fixture correction preserves the distinction under test. Its partial-result accepted record now supplies a valid agency (`:598`), so the assertion of zero downstream catalog failures remains meaningful. The original normal fixture still has empty agency lists, and new assertions at `:587` demonstrate zero provider rejections alongside three DocSpec required-metadata failures. This corrects test input rather than weakening interpretation admission or conflating the two failure counts.

`tests/test_spicy_docs_source_native.py:122` refuses an installed reader missing the required public capability before publication. `tests/test_source_catalog_cli_verify.py:117` reseals a command receipt after changing only a nested number to a Boolean and requires disagreement with the admitted catalog. Existing root/input/digest/schema and package admission tests remain applicable.

The guide in `docs/catalog-inputs.md:109`, `docs/catalog-evidence.md:17`, `docs/inspection.md:53` and the source-outcomes decision note preserve the boundaries above. They distinguish reported evidence from independent upstream verification, separate count units, explain partial-successor omission and retain the original evidence requirement.

**Runtime evidence:** execution is root-owned. An initial gate reported 118 passes plus a stale `FakeReader` fixture failure. After correction, the combined gate reported 121 passes and one installed fixture failure in 18.26 seconds: the new partial input's empty agency list correctly triggered catalog required-metadata refusal. After the bounded fixture correction described above, the installed follow-up passed both tests in 6.25 seconds (`/tmp/docspec-source-outcomes-installed-corrected.log`). The reviewer inspected that correction and read the final log, but did not execute any gate. These are overlapping focused/follow-up runs, not a claimed single 123-test run or a full current-worktree regression.

## 5. Findings and resolutions

1. **A Boolean acceptance switch would have conflated partial and total rejection — resolved during architecture review.** The final accepted-outcome set preserves the real user choice without another policy class.
2. **Proposed duplicated provider arithmetic/traversal verification — removed from scope.** The final parser checks the local decision and bounded provenance fields; provider semantic admission remains upstream.
3. **CLI exact-evidence comparison used Python JSON equality — resolved.** A nested `1` changed to `true` could have passed the new mapping comparison after recomputing the command receipt identity. Canonical full-description bytes and the resealed tamper regression now enforce exact agreement.
4. **The immutability test itself used the same weak `== 1` assertion — resolved.** The final assertion checks both exact integer type and value, so leaked `True` cannot pass.

No remaining material static finding was identified in this slice.

## 6. Verdict and acceptance limits

**APPROVE, high confidence in the bounded source and inspected acceptance coverage.** With the separately reported corrected focused/installed execution, D08/D10 meet the reviewed acceptance. This establishes the supported installed consumption boundary, not live provider-wide completeness, a second upstream verifier, general failed-acquisition publication, a new append mode or full physical retry-cost accounting. D23 and broader D38/D45/D46 qualification retain their separate scope.
