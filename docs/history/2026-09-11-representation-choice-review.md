# D17 representation choices — independent static review

VERDICT: APPROVE. No material production finding remains. One incorrect negative-test fixture was found and corrected before this final assessment.

Repository: `/Users/mikewolfd/Work/DocSpec`, D17 changes over `a19c3f0`. Scope: `src/docspec/processing/visible_text_runtime.py`, `tests/test_visible_text_runtime.py`, `examples/representation_choices.py`, `docs/representations.md`, and the representation-guide addition to `docs/catalog-and-processing.md`. The example uses the concurrently introduced `prepare_local_experiment`; that D03 API has a separate review. This review follows `/Users/mikewolfd/.agents/skills/semi-formal-code-review/SKILL.md`. No tests or builds were executed by this reviewer. No repository files were edited, and unrelated untracked history was not read.

## 1. Patch summary

An ordinary local dataset run can explicitly choose HTML/XML visible text and whole-block segmentation. `VisibleTextExtractor` adapts the existing parsers to the existing extraction/result/identity interfaces; `VisibleTextBlockSegmenter` uses their declared block boundaries. Exact source captures remain separate. The representation example builds one real source catalog, runs either markup or visible-text processing, retains the result, and reads bytes and coordinates through the public inspection API (`examples/representation_choices.py:34`).

The implementation preserves the default source-markup routes. It adds no parser, plugin loader, intermediate persisted state, or alternate executor. The guide explains the concrete defaults and the limits of visible text, PDF, images, and bounded segmentation (`docs/representations.md:14`, `:68`, `:152`).

## 2. Function trace

| Function / method | File:line | Inputs → output | Verified behavior |
| --- | --- | --- | --- |
| `_headings` | `src/docspec/processing/visible_text_runtime.py:36` | Heading map → sorted immutable pairs | Text names and integer levels 1–6; caller dictionaries are copied. |
| `_parser_identity` | `src/docspec/processing/visible_text_runtime.py:44` | Existing parser → child ID/configuration digest | Binds parser identity/configuration, block mapping version, and UTF-8 input rule. |
| `VisibleTextExtractor.__init__`, `configuration_digest` | `src/docspec/processing/visible_text_runtime.py:66`, `:80` | Optional heading settings → configured extractor | Frozen setting snapshot; aggregate pins both actual routing children. |
| `_parser`, `selected_identity` | `src/docspec/processing/visible_text_runtime.py:86`, `:94` | Captured media type → parser/selected child pin | HTML and XML aliases route explicitly; unsupported types refuse. |
| `_read` | `src/docspec/processing/visible_text_runtime.py:97` | Captured reference and bytes → parsed visible text | Shares captured digest/size/media check, requires UTF-8, and converts only unparseable/no-text input errors to deterministic input errors. |
| `_verify_captured_bytes` | `src/docspec/processing/extraction.py:590` | Captured reference and bytes → verification | Actual bytes agree with the blob; captured and blob media types agree after normalization. |
| `extract` | `src/docspec/processing/visible_text_runtime.py:111` | Captured input → identified representation and receipt | Retains derived bytes separately; emits selected child ID/configuration and input/output pins with parser metadata. |
| `evidence_resolver`, nested `resolve` | `src/docspec/processing/visible_text_runtime.py:144`, `:156` | Source and declared block mapping → exact derived block bytes | Reuses the parser, requires the same source, and refuses altered block bounds/source span/transform. |
| `_block_mappings` | `src/docspec/processing/visible_text_runtime.py:167` | Parsed blocks → ordered evidence mappings | Uses the existing source-run resolver for one enclosing captured span per complete block. |
| `VisibleTextBlockSegmenter.selected_identity` | `src/docspec/processing/visible_text_runtime.py:195` | Representation → segmenter pin | Refuses a non-visible-text representation. |
| `VisibleTextBlockSegmenter.segment` | `src/docspec/processing/visible_text_runtime.py:200` | Visible representation → segment tuple | Requires named block transform and source byte coordinates; delegates exact slices/IDs/evidence to `build_segment`. |
| `VisibleText.rendition_range`, `TextRun.resolve` | `src/docspec/processing/visible_text.py:176`, `:144` | Derived interval → captured interval | Exact runs resolve byte offsets; transformed runs return an honest enclosing span without interpolating entity/whitespace changes. |
| Existing XML/HTML `extract` | `src/docspec/processing/visible_text.py:362`, `:406` | Source bytes → text, blocks, source runs | XML normalization and HTML suppression remain owned by existing parsers. |
| `build_segment` | `src/docspec/processing/artifacts.py:142` | Representation and complete boundary → segment | Slices retained bytes and resolves the representation's own evidence mapping. |
| `verify_representation_evidence`, `verify_segment_evidence` | `src/docspec/processing/artifacts.py:185`, `:237` | Declared mappings, bytes, optional resolver → verification | Tests can round-trip declared block evidence; generic verification does not establish exhaustive mapping coverage. |
| `run_example`, `main` | `examples/representation_choices.py:34`, `:95` | New output directory and choice → retained result/inspection JSON | Existing fixture source/fetcher, public experiment preparation, retention, and read-only inspection; CLI validates the choice. |

The processing module remains inside the existing domain/processing dependency direction. It neither imports runtime nor storage adapters. The example is the outer caller that chooses implementations.

## 3. Data flow and invariants

1. **Source and derived bytes remain distinct.** The adapter verifies the captured input, then uses a separate content reference for visible text. The real example compares original capture bytes/digest across both choices (`visible_text_runtime.py:97`, `:114`; `tests/test_visible_text_runtime.py:134`).
2. **Output-affecting settings are pinned.** Heading maps become immutable tuples. Aggregate configuration binds both children; retained representations/receipts use the selected HTML/XML child pair (`visible_text_runtime.py:72`, `:80`, `:113`). Updating the caller's original dictionary cannot mutate the configured extractor.
3. **Transformed source coordinates are honest.** Entities, headings, normalized spaces, and separators can change bytes. A block records an enclosing source span; it does not claim byte-for-byte source identity (`visible_text.py:144`, `:176`; `docs/representations.md:105`).
4. **Only whole declared blocks are segmented.** Derived mappings cannot be subdivided by inventing offsets; the domain resolver permits only their declared boundary. Block separators are excluded from segments, and a large block remains large (`domain/content.py:486`; `visible_text_runtime.py:200`; `docs/representations.md:109`).
5. **Input refusal is distinct from integrity refusal.** Malformed/no-visible-text/unsupported/invalid-UTF-8 input is deterministic; mismatched captured bytes or media remain integrity failures. Unexpected mapping failures are not blanket-converted (`visible_text_runtime.py:97`; `tests/test_visible_text_runtime.py:114`, `:120`, `:126`).
6. **Evidence verification has an explicit scope.** The resolver verifies supplied complete-block mappings against regenerated blocks. Generated separators are unmapped; the shared verifier does not require exhaustive mappings. This is not a claim to independently rederive every representation byte or prove complete semantic extraction. Documentation makes no such claim (`processing/artifacts.py:195`; `docs/representations.md:257`).
7. **Defaults and optional formats remain accurate.** HTML/XML defaults retain markup, PDF text uses page evidence and optional `pypdf`, images retain bytes without OCR or full decoding, and whole blocks do not imply token-window fit. The new choice does not widen parser resource guarantees (`docs/representations.md:20`, `:181`, `:186`, `:197`).

Hypotheses confirmed: existing parsers can serve the runtime without duplicated parsing machinery; immutable heading settings make identity checks stable; whole-block evidence avoids unsupported sub-block coordinate inference. Hypothesis refuted: changing a validated representation's kind with `dataclasses.replace` can test segmenter refusal without first recomputing identity; that fixture was corrected.

## 4. Test behavior and edge cases

These are static assertions reviewed, not executed results.

| Test | File:line | Expected behavior and evidence |
| --- | --- | --- |
| HTML/XML complete blocks | `tests/test_visible_text_runtime.py:36` | Entities, UTF-8 non-ASCII, XML normalization, and HTML `pre` blank lines yield expected whole blocks; source slices include the real entity and exclude inserted heading markers. Declared representation/segment evidence round-trips. |
| Immutable heading configuration | `tests/test_visible_text_runtime.py:59` | Mutating the original dictionary leaves output/policy unchanged; a newly configured extractor changes child and aggregate pins; frozen fields refuse assignment. |
| Foreign or altered mappings | `tests/test_visible_text_runtime.py:78` | Source-span/source-byte tampering and foreign transform refuse; correctly recreated foreign-kind identity reaches the segmenter's intended refusal. |
| Deterministic input refusals | `tests/test_visible_text_runtime.py:114` | Hidden-only HTML, malformed XML, unsupported PDF, and invalid UTF-8 classify as deterministic input. |
| Captured digest failure | `tests/test_visible_text_runtime.py:120` | Changed bytes retain artifact-integrity classification. |
| Captured media mismatch | `tests/test_visible_text_runtime.py:126` | Changes only blob media; existing capture semantic ID remains valid because it binds blob digest, while the adapter's shared media check specifically refuses. |
| Real public choices | `tests/test_visible_text_runtime.py:134` | Runs markup and visible text via public API, retains and inspects them, proves identical captures, different representation bytes, expected segments, and contained source coordinates. |
| Recovery and changed pins | `tests/test_visible_text_runtime.py:153` | Same settings recover without another fetch; changed headings refuse saved-handoff recovery. |

The tests exercise the new actual user path, rather than only adapter serialization. The existing parser tests separately cover run-to-source offsets. The final installed probe also selects the D17 extractor and segmenter, preserves exact source bytes, inspects three visible blocks, verifies declared representation/segment evidence and selected child pins, and checks that recovery adds no calls (`tests/support/installed_runtime_probe.py:94`, `:108`, `:120`, `:136`). Its source and document producer values are separately selected by the caller at lines 39–42. The wheel test invokes this probe outside the checkout with `-I` (`tests/test_package_boundary.py:514`). These assertions were read statically; the parent owns their execution evidence. Corpus-scale memory/CPU bounds, browser-rendered visibility, and semantic completeness are not established here.

## 5. Findings

**F1 — resolved, test correctness.** The original foreign-kind negative used `replace` while retaining `representation_id`, so `Representation.__post_init__` would raise `ValueError` before the segmenter ran (`domain/content.py:532`). It now uses `Representation.create` and exercises the intended `IntegrityError` branch (`tests/test_visible_text_runtime.py:101`).

**F2 — resolved by implementer review, input consistency.** The adapter initially checked only blob bytes/size. It now shares `_verify_captured_bytes`, which also checks captured/blob media agreement. The focused media-only negative reaches this condition (`visible_text_runtime.py:98`; `tests/test_visible_text_runtime.py:126`).

**Observation — scope of evidence.** The mapping resolver proves declared whole-block evidence. It does not add exhaustive representation rederivation or a new generic verifier. The guide preserves that distinction and explains why arbitrary subdivision is refused. No new framework is warranted for the delivered D17 value.

## 6. Conclusion

VERDICT: APPROVE

The patch makes a useful existing extraction capability available through the normal lifecycle, preserves original captures, and exposes verifiable source coordinates. Its small adapter and segmenter reuse current parsers, data models, checks, execution, and inspection. D17 acceptance is supported for the documented HTML/XML choices and current PDF/image limits, subject to parent-owned runtime gates.

Coverage of changed paths: ADEQUATE. Confidence: HIGH for the traced static behavior. Runtime results, installed-package qualification, large-corpus qualification, and publication are separate evidence. D03 convenience-API completion and D16 failed-item repair are outside this verdict.


## Root-executed validation

After the final source changes, the parent agent ran 131 focused and isolated
installed-package checks: all passed. The regression suite passed 1,089 tests in
146.29 seconds, excluding one live integration test and the two unfinished D16
test files. It reported one non-failing `runpy` warning from importing the shared
offline example before executing that module. Ruff and diff checks passed.
These are local results; no remote CI, publication, or corpus-scale qualification
is claimed.
