# D52 retained public-comment example review

**VERDICT: APPROVE for the implemented D52 scope.** The example preserves admitted provider facts, nulls, diagnostics, pins and exact partition bytes; exposes document candidates without fetching them; and uses the existing public DocSpec build/read/preview APIs. The guide now accurately describes source-field locations as provider-declared. One pre-existing provider provenance defect needs a destination-owned follow-up, not a DocSpec parser workaround.

## Scope and evidence status

Independent semi-formal static review of the stable uncommitted example against DocSpec `e854c39`, September 12, 2026:

- `examples/spicyregs_comments.py` — new 199-line example.
- `docs/spicyregs-comments.md` — new guide, including the corrected provider-declared wording.
- `tests/test_spicyregs_comments_example.py` — five behavior tests.
- `tests/test_source_catalog_installed_wheel.py` — 16-line addition to the existing isolated provider-wheel test.

I read the implementations and relevant public API callers/callees, not just test names. I also inspected source files inside `vendor/spicy_docs-0.3.0-py3-none-any.whl`, pinned by the repository manifest to SHA-256 `bef15f967b0840ccc119c812edca92b38c63adb8943074be17655b86c96f83f1`, provider source revision `8e485fe052c794cf18b041debe970c813812c05b`. This review does not certify that entire provider implementation.

No tests, uv commands, package builds or implementation edits were performed by this reviewer. Runtime qualification belongs to the parent's gate and must be reported separately. `git diff --check` was read-only and returned successfully. Unrelated D37 capacity work and the protected untracked history file were excluded.

## Findings

### F1 — Existing provider can mislabel the field index of a valid attachment after an invalid format

**Severity:** warning; upstream correctness limitation, not introduced by this patch.

In the pinned wheel, `spicy_docs/sources/public_comments/native.py:535–554` removes malformed format entries from `usable`. `comment_rendition_rows` then enumerates that filtered sequence at line 571 and constructs `sourceField` from the new index at line 580. The original `attachments_json` remains unchanged, and the invalid entry receives a diagnostic, but the valid entry's declared path can point to the wrong original array position.

Minimal reproduction input: set an otherwise valid public-comment row's `attachments_json` to this JSON text:

```json
[{"formats":[{}, {"url":"https://example.test/comment.pdf","format":"pdf","size":123}]}]
```

The direct public provider calls are `field_diagnostics(record)` and `comment_rendition_rows(record)`. Static trace predicts:

- The invalid first entry receives `malformed-attachment-format` at `attachments_json[0].formats[0]`.
- The PDF candidate actually came from `attachments_json[0].formats[1]`.
- Its returned `sourceField` is incorrectly `attachments_json[0].formats[0]`, and its `renditionId` is `attachment-0000-0000` after the reindexing.

This reproduction was not executed. It follows directly from the inspected filtering and enumeration loops. The minimal provider correction is to preserve each original format index through filtering and use it when constructing the provider's rendition ID and source-field path, with a provider regression for invalid-before-valid formats.

The D52 example copies the provider's raw record and rendition rows without claiming to rederive these locations. Its current fixture has a valid first format, so its asserted `[0]` location is correct. The revised guide uses “provider-declared source-field locations.” This limitation therefore does **not** block D52 completion under the agreed scope: exact preservation of provider facts and retained bytes, not independent correction of upstream declarations. Record the fix in SpicyDocs; do not duplicate attachment parsing in DocSpec.

### F2 — Requested scope and uncertainty are represented honestly

**Severity:** observation; no change requested.

A candidate-bearing row is catalog-selected, while rows without candidates remain inspectable but unavailable for document capture. The exact docket filter is a separate, read-only metadata decision. The example does not construct execution services, fetch attachments, promote table-supplied text into captures/representations, or claim source spans. `documentProcessing` explicitly reports “not requested” and zero captures. [spicyregs_comments.py:109](/Users/mikewolfd/Work/DocSpec/examples/spicyregs_comments.py:109), [spicyregs_comments.py:155](/Users/mikewolfd/Work/DocSpec/examples/spicyregs_comments.py:155)

## Function trace

| Function or boundary | Inputs and outputs | Verified behavior |
| --- | --- | --- |
| `fixture_partition` — [example:42](/Users/mikewolfd/Work/DocSpec/examples/spicyregs_comments.py:42) | Three synthetic rows → Parquet bytes | Uses the provider's declared physical column list and Polars string types. Includes Unicode, nulls, table-supplied text, malformed attachment JSON and an optional missing required ID. No replacement source parser. |
| `run_example` — [example:129](/Users/mikewolfd/Work/DocSpec/examples/spicyregs_comments.py:129) | New absolute output path and exact docket choice → saved source report/catalog inspection | Refuses existing paths before writing. Records installed provider identity, retains original Parquet, injects one captured partition and one terminal missing-part observation into the provider's existing publish command. Admits the result through `SpicyDocsSourceNativeAdapter` using exact artifact pin and explicit accepted verifier. |
| Provider publication | Injected `PublicTableCapture` → source-native release or refusal | The wheel's CLI delegates to its existing registered acquisition and publisher. Its public-comment profile owns schema checks, exact-byte replay, Hive agency projection, attachment interpretation and observed-crawl acceptance. Wheel `cli/source_native.py:129–169`; `sources/public_comments/profile.py:10–43`; `native.py:373–491`. |
| `build_comment_catalog` — [example:68](/Users/mikewolfd/Work/DocSpec/examples/spicyregs_comments.py:68) | Admitted comment source + workspace → existing catalog result | Checks source family and accepts only a nonempty no-record-rejection outcome. Bounds candidate accumulation; carries every admitted raw row, source description, evidence and offered rendition into existing supplied-record input. Converts only provider-offered candidates, retaining expected size and unknown digest. |
| Supplied-record build | Bounded supplied rows → immutable public catalog | Existing `SuppliedRecordSource` checks count/bytes and duplicate identity, closes the generator on refusal, and snapshots canonical rows before storage. Existing policy verifies supplied identity/renditions and selects only records with candidates. [supplied_records.py:40](/Users/mikewolfd/Work/DocSpec/src/docspec/adapters/supplied_records.py:40), [supplied_records_catalog.py:101](/Users/mikewolfd/Work/DocSpec/src/docspec/application/supplied_records_catalog.py:101) |
| `inspect_comments` — [example:109](/Users/mikewolfd/Work/DocSpec/examples/spicyregs_comments.py:109) | Admitted catalog + docket text → bounded matches, null-field report and candidates | Uses exact case-sensitive field comparison; tests `value is None`, preserving empty strings as observed values. Closes the public row stream. |
| Installed test addition — [installed test:139](/Users/mikewolfd/Work/DocSpec/tests/test_source_catalog_installed_wheel.py:139) | Existing wheel environment + copied example/tests → subprocess result | Installs the same pinned provider's `public-table` extra only after core/reader absence checks. Copies the new files into the temporary runtime tree and executes those exact behavior tests with the installed interpreter under `-I`. No second harness or provider checkout import. |

## Data flow and invariants

The original `input.parquet` bytes are retained before provider publication. The provider's captured ZIP pins those same bytes; the example stores the exact admitted row and `record_evidence` result in catalog metadata. Candidate mapping keeps provider rendition IDs, media types, locator, expected size and nullable expected digest. Original rendition declarations remain alongside the candidate conversion. [example:90](/Users/mikewolfd/Work/DocSpec/examples/spicyregs_comments.py:90)

The supplied catalog uses a distinct example source namespace. Its required `sourceIssuedVersion` is a fingerprint of the admitted row, not an invented publisher date. The actual `modify_date`, including null, remains in the raw record. Empty titles become unavailable only in the convenience title field; their original values remain unchanged in metadata. The guide explains both choices. [guide:44](/Users/mikewolfd/Work/DocSpec/docs/spicyregs-comments.md:44)

The new supplied input does not fabricate a collection outcome. Its original provider description/outcome remains nested in metadata; the example preserves the original `observed-crawl` source scope. The provider's coverage rule assumes contiguous parts starting at zero and stops at the first missing part; it explicitly does not retain that missing-part response or establish all-agency membership/a publisher-wide instant. The guide distinguishes these observations from Regulations.gov API and Mirrulations inputs. [guide:52](/Users/mikewolfd/Work/DocSpec/docs/spicyregs-comments.md:52); wheel `native.py:744–768` and `profile.py:21–22`.

A malformed attachment JSON field remains a retained field with a diagnostic, whereas a missing required comment ID causes the provider to refuse the entire partition. The example preserves that refusal and the original input instead of fabricating a partial catalog or row-rejection ledger. Unrelated publication errors remain errors. [example:163](/Users/mikewolfd/Work/DocSpec/examples/spicyregs_comments.py:163)

Bounds are explicit: ten supplied records, forty accumulated candidates, 1 MiB canonical supplied bytes and 8 MiB catalog scratch. These are example limits, not corpus-capacity claims or a statement that provider admission uses only 1 MiB. Existing supplied-record validation happens before catalog storage is constructed. [example:78](/Users/mikewolfd/Work/DocSpec/examples/spicyregs_comments.py:78), [example:103](/Users/mikewolfd/Work/DocSpec/examples/spicyregs_comments.py:103)

## Test behavior and edge cases

All five new tests were read. Their assertions exercise these real paths:

1. **Facts/nulls/diagnostics/candidates** — compares catalog metadata to separately admitted provider records, descriptions and evidence; reads the retained evidence ZIP and compares its partition bytes with original Parquet. Checks all 16 fields, null versus empty text, preserved malformed JSON, candidate size/unknown digest and one selected/two unavailable catalog items. Checks observed-crawl outcome and absence of processing storage. [test:37](/Users/mikewolfd/Work/DocSpec/tests/test_spicyregs_comments_example.py:37)
2. **Metadata filter** — changes the exact docket query against the same catalog, checks case sensitivity and null reporting, and compares every saved file's bytes and modification time before/after. This establishes read-only reuse of the catalog, not execution reuse. [test:98](/Users/mikewolfd/Work/DocSpec/tests/test_spicyregs_comments_example.py:98)
3. **Whole-partition refusal** — missing comment ID produces no source release/catalog, reports the provider's acquisition failure, leaves the row-rejection ledger unreported and preserves the original input pin. [test:113](/Users/mikewolfd/Work/DocSpec/tests/test_spicyregs_comments_example.py:113)
4. **Record bound** — reducing the example allowance to two rejects three rows before dataset storage exists. [test:124](/Users/mikewolfd/Work/DocSpec/tests/test_spicyregs_comments_example.py:124)
5. **Existing output/unrelated failure** — preserves an existing directory and marker, and refuses an unrelated mocked publication error rather than reporting it as the expected invalid-row demonstration. [test:132](/Users/mikewolfd/Work/DocSpec/tests/test_spicyregs_comments_example.py:132)

An autouse socket guard rejects network connections for these tests. The installed gate uses the real pinned SpicyDocs wheel plus a newly built DocSpec wheel, copies only the example/support/tests needed outside the checkout, and retains its earlier core-only and reader-only optional-dependency checks. The new tests themselves do not skip. Their success remains a runtime result for the parent to establish; this certificate does not predict a measured pass.

## Conclusion

**APPROVE. Coverage of changed behavior: adequate. Static confidence: high.** The implementation satisfies D52's bounded catalog/read/report/candidate workflow without adding another publication pipeline, parser, runtime abstraction, artifact format or execution path. It preserves provider-declared provenance rather than silently correcting it. The identified attachment-index defect is a separate SpicyDocs follow-up and narrows what can be claimed about those declarations; it does not invalidate exact preservation or the current fixture's evidence.

This approval does not establish live upstream availability, all-comment coverage, large-table capacity, attachment acquisition/processing, or correctness of every upstream provider transformation. Those claims are absent from the corrected guide.

## Parent execution and disposition

The root agent ran the focused public-comment and installed-provider gate:
**7 tests passed in 13.58 seconds**. The installed subprocess repeats the five
comment tests using isolated wheels after verifying the absence of optional
dependencies from the core and reader environments. Native evidence is retained
locally at `/tmp/docspec-comments-gate.log` and `/tmp/docspec-comments-gate.xml`.
Ruff and `uv lock --check` passed. The development dependency now selects the
existing provider's `public-table` extra so ordinary contributor and CI commands
run these tests; installed core and reader dependencies are unchanged.

The provider provenance fix is recorded in [SpicyDocs P01](../../../spicy-docs/docs/simplification-todo.md#public-comment-attachment-provenance).
No provider implementation changed and no remote CI or publication is claimed.
