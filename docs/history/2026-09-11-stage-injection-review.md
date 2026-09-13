# D13 configured stages: independent static review

Scope: the uncommitted D13 changes over `99849b2`, including the required stage pins, public builder/injection, default registries and PDF behavior, planning, execution/checkpoint checks, wire/profile migration, caller fixtures, installed probe, and maintained guides. Review method: `semi-formal-code-review`. This reviewer ran no tests, builds, or benchmarks and edited no repository files. Parent-owned execution results should be recorded separately. Paths below are relative to `/Users/mikewolfd/Work/DocSpec`.

No material finding remains open. The counter-mutation and schema-description issues found during review are resolved. The identity-only and digest-only output refusal cases now have separate assertions. The final README navigation sentence and maintained guides accurately describe the implemented API; broader checklist completion remains the parent's decision against its acceptance criteria.

## 1. Patch summary

The existing `StagePolicy` now names one effective extractor plus its configuration digest and one effective segmenter plus its policy digest (`src/docspec/domain/plans.py:69`). A registry can be that effective stage. `docspec.runtime.stage_policy` derives the pins from actual objects, and `prepare_local_run` accepts those same objects (`src/docspec/runtime/__init__.py:30`, `:42`). Plans, stores and ordinary segmentation receipts move to required 2.0 shapes with no legacy reader.

This closes the practical gap where changed extraction or segmentation settings could retain the same plan identity and be skipped as unchanged work. Both pins now participate in plan identity and planning impact. Correctly changed settings use the existing full `REPAIR` path, including acquisition; selective stage reuse remains D15. Actual selected child identities stay on outputs and receipts rather than being replaced by registry identifiers.

Important callers reviewed include the CLI plan/run adapters, offline example, installed-wheel probe, shared pipeline/processor/profile/sink fixtures, planner and storage fixtures, existing processor-only/checkpoint tests, and new runtime stage tests. The review did not inspect the unrelated untracked catalogue-cleaning history file or independently audit sibling repositories.

## 2. Function trace

| Function or behavior | File:line | Input → output | Verified behavior |
| --- | --- | --- | --- |
| `StagePolicy` validation/serialization | `src/docspec/domain/plans.py:69`, `:96` | Required singular IDs/digests → closed policy | Requires SHA-256 digests, immutable distinct processor IDs, and exact wire keys. Missing pins have no default. |
| `ProcessingPlan.create`, identity and governing content | `src/docspec/domain/plans.py:146`, `:192`, `:208` | Policy plus other plan inputs → plan ID and impact data | Both stage digests are included in both forms. `from_dict` accepts the new 2.0 format only (`:228`). |
| `stage_policy` | `src/docspec/runtime/__init__.py:30` | Optional stage objects → `StagePolicy` | Uses the same default-constructor helper as runtime composition. |
| `_stage_implementations`, `_compose_local_run` | `src/docspec/runtime/composition.py:155`, `:165` | Typed objects/plan → existing services | Preserves explicitly supplied objects, validates their pins before opening storage, then injects them into execution. Processor agreement checks remain separate. |
| `configured_stage_policy`, `verify_stage_implementations` | `src/docspec/application/stage_identity.py:14`, `:33` | Actual IDs/settings plus expected policy → policy or refusal | Centralizes descriptor reading and complete plan agreement; invalid descriptors refuse. |
| `verify_extraction_identity`, `verify_segment_identity` | `src/docspec/application/stage_identity.py:43`, `:52` | Selected pair and emitted record → agreement/refusal | Compares both child ID and configuration/policy digest. Runtime and checkpoint paths share these checks. |
| Standard extractor `selected_identity` and `extract` | `src/docspec/processing/extraction.py:180`, `:236`, `:263`, `:287`, `:309` | Captured file/bytes → representation and receipt | Text/HTML/XML/JSON/image expose their declared pair and emit that same pair through `_passthrough_result` (`:533`), including a configured subclass. Existing source-byte/evidence validation remains. |
| `DefaultExtractorRegistry` digest, selection and execution | `src/docspec/processing/extraction.py:477`, `:495`, `:508`, `:514` | Actual child implementations and media metadata → aggregate pin/selected child | Hashes every actual child ID/settings. Both identity selection and extraction use `_select`. Dispatcher ID versions routing rules. |
| `LazyPypdfExtractor` construction/digest/selection | `src/docspec/processing/extraction.py:348`, `:361`, `:370` | Distribution metadata/options → configured adapter pin and selected parser pair | Pins availability/version/separator/whitespace without importing the optional parser. Output identity names the pinned parser version, distinct from the adapter ID. |
| PDF `_read_pages`, `extract`, `verify` | `src/docspec/processing/extraction.py:454`, `:379`, `:420` | PDF bytes → checked page output | Refuses unavailable or mismatched loaded parser versions before parsing. Verification also checks the output configuration digest; existing page evidence reconstruction remains. |
| Segmenter leaf selection | `src/docspec/processing/segmentation.py:103`, `:137`, `:175`, `:205` | Representation → selected pair | Paragraph/page/record/image return the pair their existing segmentation methods emit. |
| `BoundedSegmenter.policy_digest`, `selected_identity` | `src/docspec/processing/bounded_segmentation.py:977`, `:981` | Frozen settings and actual token counter → selected pair | Reuses `_require_matching_counter` on identity reads, so counter mutation refuses before new work or reuse. Existing `_bound` also checks before counting. |
| `DefaultSegmenterRegistry` digest/selection/execution | `src/docspec/processing/segmentation.py:262`, `:277`, `:280`, `:283` | Actual children, bounded routing and representation kind → aggregate/selected pair | The aggregate includes child policies and active bounded kinds. Selection and execution share `_select`. No plugin hierarchy is added. |
| `SegmentationReceipt` | `src/docspec/processing/segmentation.py:37`, `:67` | Selected child pair plus segment IDs → closed receipt | Retains a required policy digest even for an empty result; accepts 2.0 only. |
| Planner requested stages and impact | `src/docspec/application/planner.py:107`, `:472`, `:506` | Prior/current plan → work classification | Replaces only processor IDs for processor-only work; retains both new pins in non-processor governing content. Stage differences produce full `REPAIR`, not processor-only execution. |
| `prepare_base_reprocessing` | `src/docspec/application/base_reprocessing.py:60` | Processor-only entry/current plan → verified base reuse | Requires complete extraction/segmentation policy agreement while allowing only the requested processor subset. |
| `verify_configuration`, `execute_store` | `src/docspec/application/execution.py:180`, `:115` | Current stage objects/plan/store → verified execution or refusal | Checks live configuration before loading/reusing a store. Existing retry, accepted-failure, processor and data-use checks remain. |
| `_execute_entry` stage branches | `src/docspec/application/execution.py:211` | Captured bytes or saved frontier → stage output/checkpoint | Checks configuration around stage calls, selected extraction pair before persistence (`:308`), and selected segmenter stability plus every result before segment persistence (`:357`). Writes the selected pair into segmentation receipts, including empty output (`:377`). |
| `EntryCheckpointVerifier.verify_entry` | `src/docspec/application/execution_checkpoints.py:98` | Saved entry/current plan → verified frontier | Checks current stage configuration, selected extractor pair, selected segmenter pairs and receipt policies alongside existing lineage, bytes and ordered-frontier checks. Empty receipts still select and check the child. |
| `PreparedLocalRun._require_handoff` and callers | `src/docspec/runtime/execution.py:63`, `:68`, `:74` | Prepared handoff → admitted task source/execution | Checks live stage configuration before task work; task-source admission also covers zero-task `.run()`. |
| Store serialization/member loading | `src/docspec/domain/jobs.py:310`, `src/docspec/adapters/storage/stores.py:179`, `:255` | Store/entry bytes ↔ saved references | Uses store format 2.0 and entry schema `/2.0`; the installed profile declares both matching schemas (`src/docspec/storage_profiles/local-document-store-v1.json:13`). The outer saved-store manifest layout is unchanged. |

## 3. Data flow and invariants

**Configuration is part of work identity.** Actual object properties enter `configured_stage_policy`, then the plan, entry identities and store/task population. Planning removes only processor-specific fields when deciding whether reuse may be processor-only. A changed stage digest therefore cannot accidentally take the unchanged-input or processor-only branch (`src/docspec/application/planner.py:480`). The new runtime builder does not create another saved descriptor or run ledger.

**Registry identity and selected output identity serve different purposes.** The aggregate describes the configured choices available to a run; `selected_identity(input)` identifies the implementation selected for one input. The default registries use the same routing function for that selection and actual execution. Routing code changes require a dispatcher-ID version change; runtime routing options and actual child settings are included in the aggregate (`src/docspec/processing/extraction.py:480`, `src/docspec/processing/segmentation.py:228`).

**Mutation is checked before reuse as well as new execution.** Runtime checks actual configuration before task-source/worker entry, and the application checks it before processing or verifying a stored frontier. This catches changed objects even when no stage method would run. The bounded counter correction matters because its frozen declared settings alone would not expose a mutated live tokenizer. The format checks validate declared configuration and output consistency; they do not prove arbitrary injected implementation behavior.

**Accepted outputs carry the selected pair.** Extracted representations and receipts agree through the existing `ExtractionResult` checks plus the selected extractor comparison. Segmentation validates all results before persisting any of that invocation, and its receipt retains the child policy even with zero segments. Checkpoint admission verifies the same pairs and the existing source/representation lineage (`src/docspec/application/execution_checkpoints.py:138`, `:185`, `:228`).

**Completed retry and checkpoint admission remain distinct.** The runtime's already-sealed fast path verifies current configuration and the existing delivery receipt; it does not replay extraction/segmentation or deeply reopen every output through `EntryCheckpointVerifier` (`src/docspec/runtime/execution.py:92`, `src/docspec/application/delivery.py:52`). The operations guide now states this limit (`docs/operations.md:43`). Direct checkpoint verification retains the stronger per-entry checks.

**Dependency direction remains inward.** Domain knows no concrete stage/runtime. Processing's new protocol annotations add a dependency on ports, not adapters/vendor software; the import map explicitly allows that inner edge (`tests/conformance/test_import_directions.py:31`). Reading PDF distribution metadata uses the standard library; parser code loads only when PDF processing is selected.

## 4. Test behavior and concrete edge cases

These are inspected assertions and static expectations, not test results run by this reviewer.

| Tests | Expected behavior and supporting trace |
| --- | --- |
| `test_stage_configuration_pins_change_plan_identity_and_round_trip`, `tests/test_domain.py:306` | Each digest independently changes plan ID and governing content and round-trips through 2.0. |
| `test_old_or_incomplete_stage_pins_are_not_accepted`, `tests/test_domain.py:317` | Missing or malformed configuration digests and superseded plan format refuse through the closed required shape. |
| `test_selected_children_match_outputs_for_every_default_route`, `tests/test_processing_stage_identity.py:26` | Each fixture route's actual extraction/segmentation output agrees with selected child identities, distinct from registry IDs. Existing route-suffix tests also inspect selected identity (`tests/test_processing_pipeline.py:109`). |
| Configured passthrough/PDF tests, `tests/test_processing_stage_identity.py:46`, `:59`, `:99` | Configured subclasses emit their declared pair; PDF option/version/availability changes alter pins without importing the parser; changed or unidentified loaded versions refuse before parsing. |
| Bounded child and mutation tests, `tests/test_processing_stage_identity.py:121`, `:141` | Bounded settings change the registry digest, actual bounded output matches the selected child, and changed counter version refuses both direct and registry identity reads without segmentation. |
| Empty receipt test, `tests/test_processing_stage_identity.py:153` | Zero segments still retain child ID/policy; old version, missing policy and invalid digest refuse. |
| `test_configured_stage_changes_rebuild_and_matching_settings_reuse`, `tests/test_runtime_stages.py:92` | Real custom stages execute once and recover without repeated work; unchanged base yields zero tasks. Changed declared extraction or segmentation settings produce a different plan/store and full `REPAIR`, with new output/configuration verified. It explicitly expects reacquisition. |
| Mutation tests, `tests/test_runtime_stages.py:137`, `:160` | New-task, sealed-task and zero-task paths reject mutated configuration before further fetch/work; direct application execution also refuses. |
| `test_empty_segmentation_checkpoint_binds_selected_child_policy`, `tests/test_runtime_stages.py:173` | A real zero-segment run retains its selected policy. A separately saved, internally valid receipt with only a changed policy digest refuses checkpoint admission. |
| `test_wrong_selected_child_output_refuses_before_persisting_it`, `tests/test_runtime_stages.py:204` | Independently varies ID versus digest for extraction and segmentation; disagreement records artifact-integrity failure without persisting the rejected output. |
| Installed runtime probe, `tests/support/installed_runtime_probe.py:64` | Injects custom extraction/segmentation with real fetcher/processor objects, checks retained record IDs/digests, preserves one execution across saved recovery, and refuses changed stage settings before repeating work. It remains run by the parent-owned isolated-wheel test. |
| Existing producer and recovery migrations | Offline/shared fixture producers derive real stage pins; pure storage/planner fixtures supply explicit inert pins. Counting/crash wrappers forward the actual delegate descriptors and selection. Processor-only entries now retain all non-processor pins using `replace`; prior checkpoint/frontier assertions remain. |

Coverage is adequate for the changed production paths on static inspection. The review does not qualify real PDF quality across parser releases, plugin correctness, performance, active cancellation, or selective reuse after a stage change.

## 5. Findings and dispositions

- **Resolved — correctness:** A bounded segmenter's frozen settings digest could conceal a changed live counter during sealed/no-task reuse. `policy_digest` now invokes the existing agreement check (`src/docspec/processing/bounded_segmentation.py:978`); direct and registry tests cover the refusal.
- **Resolved — correctness/schema accuracy:** New required stage fields initially lived under old document-store/entry schema labels. Both member writer/reader and installed profile now declare `/2.0` consistently. The unchanged outer manifest and backend identity need no parallel compatibility mechanism.
- **Resolved — test coverage:** Output identity tests initially changed both fields together. Independent ID-only and digest-only cases now protect the new configuration checks (`tests/test_runtime_stages.py:203`).
- **Resolved — dependency-test accuracy:** Processing now uses port types; the import map explicitly admits `processing → ports` while preserving core restrictions.
- **Resolved — documentation accuracy:** Guides distinguish checkpoint admission from completed retries; the decision record notes the PDF adapter's selected parser identity rather than claiming every leaf's adapter/output IDs are identical (`docs/history/2026-09-11-stage-injection-architecture.md:16`).
- **Observation — justified limitation:** PDF availability/version affects even text-only plans using the default registry. The guide explains both this conservative invalidation and the explicit `TextExtractor` choice (`docs/python-runs.md:106`). D15 can later reduce unnecessary stage work; this change should not claim that reuse now.

The approach adds a small explicit protocol and shared checks around existing data rather than introducing a plugin loader, descriptor hierarchy, or new experiment ledger. That complexity directly supports valid reuse and traceable outputs for Python contributors. No further architectural mechanism is needed for this D13 scope.

## 6. Conclusion

VERDICT: APPROVE.

The patch achieves the bounded D13 behavior: actual configured extraction/segmentation can be injected, changed settings invalidate the plan, selected output identities are checked, and saved work cannot silently ignore mutated stage settings. Coverage of changed paths: ADEQUATE. Confidence: HIGH on static evidence. Parent-owned full-suite and installed-wheel execution are separate gates; this review does not establish D15, D38, full lifecycle/refactor completion, or sibling repository adoption.

## Parent validation record

The parent ran `uv run --frozen --extra dagster pytest -q` against the complete
stable source/test changes: **972 passed, one live integration test deselected,
112.84 seconds**. This includes the isolated installed-wheel probe outside the
checkout, custom-stage output pin checks, saved recovery, existing cumulative
budget/checkpoint tests, and the new ID-only/digest-only refusal cases. The
implementation agents also reported 70 focused processing tests and 149 focused
domain/runtime/recovery tests passing; these overlap the complete suite and are
not added to its count.

Ruff over `src`, `tests`, and `examples` and `git diff --check` passed. Maintained
local documentation link targets were checked separately. These are local
regression and package-installation results; CI, release publication, live source
integration, deployed scheduler behavior, and broader experiment acceptance were
not established by this gate.
