# D05/D14 reference experiment: independent static review

**Verdict: APPROVE.** The reference experiment and example-scoped phrase processor meet the bounded D05/D14 acceptance. No unresolved material production finding remains. They demonstrate the existing public APIs and processor seam without adding a resource framework, scheduler or state ledger.

The requested semi-formal code-review skill was applied. This reviewer read source, test assertions, documentation and package-test setup; no tests/builds, repository edits or commits were performed. Root separately reports that the focused gate passed all 33 cases, including the actual copied installed example. That execution result is not this reviewer's own runtime evidence.

## 1. Scope and practical change

The old one-document synthetic source/fetcher and manual CLI/plan assembly are replaced with four supplied documents and real local-file acquisition. One item is excluded from the run, one selected file is initially missing, explicit retry repairs that retained failure, and later processing compares configuration and vocabulary alternatives. Each result remains independently inspectable.

The new `examples/phrase_match_processor.py` uses the existing processor types and verified reference-data identity. It produces literal quotes useful to a reviewer, with exact segment byte offsets and unchanged enclosing source evidence. It remains outside the installed library's supported processor inventory.

Reviewed paths are relative to `/Users/mikewolfd/Work/DocSpec`: the processor and rewritten `examples/offline_demo.py`; four text/two vocabulary fixtures; removal of `examples/offline/source.json`; representation-example and installed-probe caller migrations; phrase/offline/visible-text/package tests; the offline and phrase guides, architecture note and maintained links. No core implementation was added by this slice.

## 2. Function and evidence traces

| Path | Traced behavior |
| --- | --- |
| `examples/phrase_match_processor.py:27` | Parse a closed vocabulary, enforce byte/term/phrase bounds and unique term IDs/exact within-term phrases, then freeze selected strings and compiled literal patterns. |
| `:66` | Require `REFERENCE_DATA`, immutable bytes and matching SHA-256 before parsing; validate the Boolean case setting. Resource identity, configuration digest, item limits and retry/data-use policies enter the existing processor description. |
| `:101` | Require content/evidence and exact request description, input, fields, limits and absent prerequisites. Decode original UTF-8; escaped patterns retain overlapping starts, check adjacent Unicode alphanumeric/underscore boundaries and map original character positions to byte offsets. |
| `:140` | Sort matches deterministically; retain segment ID/digest, resource identity and enclosing evidence. Refuse count/output/duration overflow instead of truncating. Construct the existing DerivedRecord/ProcessorResult, provider receipt digest and measured local byte/time use. An empty match list remains a successful record. |
| `examples/offline_demo.py:62` | Refuse an existing output directory first. Record example/processor file digests and DocSpec version without claiming a complete installed-runtime hash. Build the bounded supplied catalog through the public builder and distinguish its selected rows from the run exclusion. |
| `:119`, `:129`, `:135` | Execute and retain through public prepared APIs; inspect the initial two captures and one actual missing-file failure; materialize the exact expected bytes; explicitly retry transient failures and verify the original failure still exists in the prior retained result. |
| `:142`, `:149`, `:157` | Process retained captures, reconstruct the same saved handoff, then create two alternatives from the same explicit base. Confirm upstream payload identities and zero new upstream work for both alternatives. |
| `:39`, `:168` | Read admitted saved bytes to check quotes, segment digest and exact plain-text source slices. Compare updated-resource output values against an independent clean workspace. |
| `tests/test_package_boundary.py:514` | Copy the actual example, matcher and fixtures outside the checkout; run the detailed typed probe and then the actual example with the isolated installed interpreter. Only the copied example directory is explicitly added to its path. |

## 3. Invariants and ownership

The processor neither reads a resource path during processing nor contacts an external service. Its resource identity participates in ProcessorDescription identity and is returned in the result for the ordinary runtime resource-equality check. The existing request/reuse-key and result admission remain responsible for safe reuse; the example introduces no competing cache or result validator.

Offsets address original segment bytes. Enclosing source evidence is copied unchanged; the matcher does not infer narrower raw-file offsets for transformed HTML/PDF text. The walkthrough's stronger exact-source-slice assertion is valid for its declared plain-text inputs only.

All example collection is restricted to the small checked-in fixture and explicit existing limits. It is not a new large-dataset reader, semantic classifier, live-resource integration or publication mechanism. `retain` preserves alternatives without changing current selection. Execution management remains with Dagster when used; the script uses the existing direct local convenience and adds no control system.

## 4. Tests inspected and remaining qualification

`tests/test_phrase_match_processor.py` inspects non-ASCII original quote slices, case changes, Unicode adjacency, escaped punctuation, overlapping literal ordering, successful empty matches, changed resource identity, wrong resource pins/types, frozen settings, invalid/duplicate vocabulary data, byte/count/duration refusal and wrong invocation. Its result helper calls the real `validate_processor_result`, so the tests check admission rather than only formatter output.

`tests/test_offline_example.py:17` forbids socket connections and observes real fetch, extraction, segmentation and processor calls through the complete script. It asserts seven fetch attempts across initial failure/repair and independent clean comparison, six extractions/segmentations, 24 processor calls, four/three/five alternative match counts, distinct plans/releases, no new segments in resource reprocessing, clear exclusion and comparisons, and refusal to overwrite the previous example directory.

The installed package test executes this same script and checks its failure/repair, match, recovery and clean-comparison outcomes. The separate detailed runtime probe remains; it now uses supplied records and a real local fetcher instead of importing the old synthetic example classes. Its recovery refusal changes actual live fetcher chunk configuration rather than assigning a metadata property.

Root reports 33 focused/installed cases passed. This review does not establish a full regression result, live RefSpec/provider behavior, published wheel status, native Dagster interruption or the complete D38 workflow.

## 5. Findings and resolutions

1. **Resolved — readable configuration wording.** The initial phrase guide claimed matching/resource bounds appeared directly in ProcessorDescription. They actually contribute to `configuration_digest`, while ordinary item limits and the resource identity are readable fields. The guide now makes that distinction; no new configuration framework was added for the correction.
2. **Resolved — unnecessary optional dependency in the command.** The offline guide originally requested the Dagster extra despite using only the direct local path. It now uses `uv run --frozen python ...`; native Dagster qualification remains separate.

No remaining material correctness, evidence, scope or ownership finding was found. Future production consumers may justify promoting shared functionality, but this useful example alone does not justify a supported phrase-processing service.

## 6. Acceptance and confidence

**D05: accepted for the requested small installed reference experiment.** It demonstrates several documents, a run exclusion, a recoverable real local failure, initial retained capture, later processing and understandable saved output.

**D14: accepted for a meaningful optional processor.** Users can inspect literal results and source evidence, change a real configuration or pinned resource and run a new retained alternative. The local vocabulary is explicitly synthetic reference data; no semantic or authoritative resource claim is made.

Confidence is high for this bounded static conclusion. D38's growth/interruption/complete-loop proof and D20/D21 native execution qualification remain separate acceptance work.
