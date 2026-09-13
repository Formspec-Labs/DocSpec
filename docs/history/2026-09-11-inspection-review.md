# D19 inspection — independent static review

Reviewed repository: `/Users/mikewolfd/Work/DocSpec`.

Scope: the storage foundation committed as `6521e69` and the accompanying uncommitted D19 application, runtime, CLI, tests, and maintained documentation, relative to `bf38ef1`. Review used `/Users/mikewolfd/.agents/skills/semi-formal-code-review/SKILL.md`. This is static analysis: I read implementations, their important callers/callees, and test assertions, but ran no tests or builds. Parent-owned execution evidence must be recorded separately. The unrelated untracked catalogue-cleaning history was neither read nor changed.

All six material findings have been corrected. No material finding remains open. This report distinguishes saved evidence from execution replay and scheduled work from complete resulting data.

## 1. Patch summary

The patch exposes `open_local_inspection` and `InspectionView` through `docspec.runtime`, with `summary`, `source`, `records`, `read_blob`, and `compare` operations. `docspec inspect` delegates to those operations using the existing request's plan, workspace, and accepted producers. It constructs storage readers rather than execution plugins (`src/docspec/runtime/inspection.py:21`, `src/docspec/cli/inspection.py:16`).

Storage adapters can open existing roots without creating staging directories. Actual control/store reads and streamed blobs enforce byte bounds. The existing `document-catalog` readers now use normal constructors rather than `object.__new__` (`src/docspec/adapters/storage/files.py:26`, `src/docspec/cli/catalog.py:33`).

The application distinguishes live observations, exact reconciled runs, and retained results. A complete result includes inherited items even when this run scheduled no work. Complete run admission shares release-layer verification; retained admission reuses its verified reader. Receipt checks move into shared helpers while checkpoint recovery retains implementation and blob checks (`src/docspec/application/inspection.py:42`, `src/docspec/application/inspection_runs.py:27`, `src/docspec/application/execution_checkpoints.py:69`).

## 2. Function trace

Paths below are relative to the repository root.

| Function / method | File:line | Inputs → output | Verified behavior |
| --- | --- | --- | --- |
| `_storage_root`, `_contained` | `src/docspec/adapters/storage/files.py:26`, `:39` | Root, locator, creation flags → contained path | Existing non-symlink roots required when creation is disabled; parent creation remains explicit. |
| `_read_exact` | `src/docspec/adapters/storage/files.py:61` | Locator, optional allowance → bytes | Checks actual size before opening and caps reads at allowance plus one byte. |
| Storage constructors | `src/docspec/adapters/storage/blobs.py:22`, `controls.py:25`, `stores.py:84`, `records.py:97`, `catalog.py:115` | Roots, profiles, `create` → adapters | `create=False` suppresses root/staging creation. It is an initialization choice, not a restriction on every adapter method. |
| Control `load` | `src/docspec/adapters/storage/controls.py:53` | Exact artifact reference → canonical object | Applies actual byte bound before digest, identity, kind, and closed-shape checks. |
| Store `load`, `revisions`, `latest_with_observed_at` | `src/docspec/adapters/storage/stores.py:204`, `:281`, `:319` | Exact reference or store ID → saved revision | Bounded reads also cover discovery, before a fabricated reference could bypass actual-size checks. |
| Blob `read` | `src/docspec/adapters/storage/blobs.py:100` | Blob reference, allowance → chunks | Refuses declared oversize and file growth before yielding excess bytes; full consumption checks digest. |
| `_profile_limit`, `_local_profiles` | `src/docspec/runtime/storage.py:32`, `:39` | Installed descriptions and plan pins → limits/admitted profiles | Positive limits; exact machine-pin equality; only supported local implementations. |
| `_local_storage`, `_local_stores` | `src/docspec/runtime/storage.py:55`, `:101` | Roots/profiles/producer → adapters | Common storage composition; `create=False` path constructs no execution services. |
| `_local_document_catalog` | `src/docspec/cli/catalog.py:33` | CLI storage arguments → catalog | Normal read-mode constructors replace manually initialized instances. |
| `open_local_inspection` | `src/docspec/runtime/inspection.py:21` | Plan, workspace, accepted producer, optional exact reference → view | Mutually exclusive run/release pins; read-mode storage; separate optional source admission; disposable comparison scratch. |
| `InspectionView.__init__` | `src/docspec/application/inspection.py:42` | Admitted dependencies and requested reference → view | Retained reader opens first; supplied plan must match saved plan; source summary must match pinned source identity/digest. |
| `load_run` | `src/docspec/application/inspection_runs.py:27` | Run reference, plan, repositories → receipt/profile | Pins saved plan, source, base, planned population, ledger types/profiles, execution evidence, final stores, and outcome counts. |
| `_verify_active_result`, `verify_blob` | `src/docspec/application/inspection_runs.py:84`, `:109` | Nonrejected stateful run → admission/refusal | Requires complete layers, consistent partition policy, pinned blob profile, logical links, and referenced blobs. |
| `verify_expected_layers` | `src/docspec/application/commit.py:105` | Layer tuple and plan → admission/refusal | Shared with release verifier; requires core/current derived layers while preserving inherited per-item policies. |
| `DocumentReleaseVerifier.verify` / receipt links | `src/docspec/application/commit.py:188`, `:274` | Retained release → admission/refusal | Existing full logical/blob admission; exact run/release layer and root equality; rejects stateless/rejected runs. |
| `open_reader` | `src/docspec/adapters/storage/catalog.py:193` | Exact release reference → verified reader | Existing artifact and accepted-producer admission precede reader construction. |
| `work_stores`, `_work_stores` | `src/docspec/application/inspection_runs.py:119`, `inspection.py:106` | Plan or exact run → store stream | Live mode observes latest revisions after planned admission; run mode loads only exact sealed references and checks total. |
| `phase`, `result_scope`, `complete_active_state` | `src/docspec/application/inspection.py:87`, `:95`, `:103` | Admitted view state → explicit labels | Rejected and stateless work cannot claim a complete active result. |
| `_entry` | `src/docspec/application/inspection.py:109` | Source ID → exact matching work entry or none | Uses selection/store lookup for a run; checks exact entry ID and sealed plan ownership; closes live scan. |
| `_sample_limit`, `summary` | `src/docspec/application/inspection.py:27`, `:134` | Nonnegative limit → counts and capped details | Separates source coverage, scheduled work, and result counts; does not infer all matching selection items. |
| `source` | `src/docspec/application/inspection.py:196` | Source ID and limit → work plus output evidence | Inherited data can have no scheduled work; counts all matching rows while capping samples and closing streams. |
| `layer_kinds`, `records` | `src/docspec/application/inspection.py:223`, `:231` | Kind and optional source → row stream | Uses verified release/staged layers or saved checkpoint delivery rows; refuses unknown layers and forwards closure. |
| `read_blob`, `compare` | `src/docspec/application/inspection.py:258`, `:265` | Explicit byte allowance / second view → stream/report | Positive bounded byte API; comparison delegates to shared application implementation. |
| `load_stage_receipts` | `src/docspec/application/execution_evidence.py:47` | Exact saved receipt references → loaded receipts | Duplicate and unknown receipts refuse; semantic identity must match reference. |
| `verify_stage_receipt_outputs` | `src/docspec/application/execution_evidence.py:76` | Entry and loaded receipts → extraction/segmentation receipts | Exact output ownership/order/digests/identity/configuration; empty segmentation completion comes from its receipt. |
| `verify_processor_receipts` | `src/docspec/application/execution_evidence.py:140` | Entry, plan, segments, max attempts → verified result map | Shared existing request/result, retry, prerequisite, cache-origin, and derived-output checks. |
| `EntryCheckpointVerifier.verify_entry` | `src/docspec/application/execution_checkpoints.py:69` | Saved checkpoint and plan → verified frontier | Continues selected implementation, source prefix, blob, coordinate, and executable frontier checks around the shared receipt helpers. |
| `_stage`, `_processor_evidence`, `entry_evidence` | `src/docspec/application/inspection_evidence.py:29`, `:46`, `:101` | Saved entry/evidence and limit → evidence report | Distinguishes requested/completed/partial states, retained origin, recorded calls, failures, and reported resource use without plugins. |
| `_changes`, `_work` | `src/docspec/application/inspection_comparison.py:21`, `:33` | Values/work stores → differences/scratch rows | Stable source identity and recorded effective settings remain explicit; per-item work retains execution mode and requested processors. |
| `_content`, `_configuration`, `_spool_result` | `src/docspec/application/inspection_comparison.py:55`, `:75`, `:81` | Result rows → bounded sortable evidence | Separates values/coordinates/input associations from exact provenance and per-item requested settings; streams each layer once. |
| `_result_signatures`, `_compare_rows` | `src/docspec/application/inspection_comparison.py:110`, `:133` | Ordered scratch streams → counts/capped changes | Streaming grouped fingerprints and merge join; closes streams on success/error; no all-items dictionary. |
| `compare_views`, nested `execution` | `src/docspec/application/inspection_comparison.py:168`, `:183` | Two views → comparison | Result comparison requires two complete states; exact saved worker/scheduler differences are separate from plan differences. |
| `_open_view`, `_cmd_inspect` | `src/docspec/cli/inspection.py:16`, `:33` | Existing request/CLI options → report | Delegates to public view; rejects negative cap; bounded records sample reads one extra row and always closes. |
| `_view_arguments`, `add_inspection_command` | `src/docspec/cli/inspection.py:57`, `:67` | Parser → four commands | Same reference exclusivity for both comparison sides; parser registered at `src/docspec/cli/parser.py:77`. |
| `_cmd_run_active` | `src/docspec/cli/runs.py:159` | Request and observation limits → liveness report | Store-only read composition; exact plan ownership; bounded diagnostic examples with exact overall/class counts; timestamps remain observations. |

The only new outer composition direction is `runtime.inspection` importing storage adapters. Application modules use ports/domain/shared application checks; no runtime-to-CLI or core-to-adapter dependency was introduced (`tests/conformance/test_import_directions.py:164`).

## 3. Data flow and invariants

1. **Read operations do not create dataset state.** The public factory passes `create=False`; all exported view methods read existing evidence. Comparison creates only temporary workspace scratch, removed by its context manager (`runtime/inspection.py:43`, `application/inspection_comparison.py:172`). The adapter flag does not turn the Python process into a read-only security boundary.
2. **References and acceptance remain caller choices.** The factory receives independent source/document producers. Source coverage is admitted only when the source producer is supplied. Retained views pass their exact release pin through the existing catalog verifier; runs load exact control/store references (`runtime/inspection.py:25`, `:48`; `application/inspection_runs.py:39`).
3. **Saved plan and run data remain authoritative.** The caller's plan equals the saved plan; source/base/planned population and execution ledgers agree. Observing a plan does not invent a new attempt ID. Exact runs do not ask for latest revisions (`application/inspection_runs.py:45`, `:117`).
4. **Work is not complete dataset population.** Scheduled entries omit unchanged/unselected inherited items. Complete state is admitted from its layers; rejected/stateless/live views are labeled incomplete. A zero-task successor retains nonempty results (`application/inspection.py:95`, `:176`).
5. **The retained shortcut does not create a new trust flag.** `admitted_layers` comes from the already-opened verified reader and must exactly equal the run's staged layers. The existing release verifier already binds the same run, active layers, blob roots, partition policy, and successful state (`application/inspection.py:62`; `inspection_runs.py:77`; `commit.py:281`). It avoids a second full logical/blob scan.
6. **Inspection does not establish replayability.** Shared checks bind saved receipt identities and output relationships. Recovery still checks actual selected plugins and blobs. Inspection explicitly marks replayability and unavailable observations, rather than synthesizing them (`execution_checkpoints.py:76`; `inspection_evidence.py:101`; `docs/inspection.md:126`).
7. **Cost comes from its actual evidence.** Attempts count recorded calls even when the final result is a cache hit. Result-reported resource use is grouped by this-run/cache/base origin. Money, tokens, lost pre-checkpoint work, and whole-run duration remain unavailable (`inspection_evidence.py:60`, `:70`; `inspection.py:181`).
8. **Comparison preserves meaning-bearing associations.** It compares stable source IDs, recorded input identities, requested settings, values/coordinates/associations, outcomes, and exact row provenance. `fileId`/`inputIds` changes can count as content changes even when bytes agree; that limit is explicit (`inspection_comparison.py:55`, `:199`; `docs/inspection.md:97`). It does not claim causal explanation or semantic quality evaluation.
9. **Bounds have distinct meanings.** Sample limits cap returned details, not the scan needed for totals. Existing profile limits bound records/scratch; blob reads require explicit allowance. Full stream consumption completes checksum validation; early callers must close (`inspection.py:258`; `cli/inspection.py:44`; `docs/inspection.md:66`).

Exploration hypotheses resolved: read composition avoids creation (confirmed); stateful alone proves completeness (refuted and corrected); provenance-free value multisets preserve input meaning (refuted and corrected); shared receipt checks can serve inspection without constructing plugins (confirmed, with replay limitations retained).

## 4. Test behavior and edge cases

These are inspected assertions and static expected behavior, not execution results from this reviewer.

| Test / evidence | File:line | Expected path and asserted behavior |
| --- | --- | --- |
| Read construction without staging | `tests/test_readonly_storage.py:35` | Existing empty roots retain the same paths, sizes, and mtimes. |
| Missing roots | `tests/test_readonly_storage.py:44` | Read construction refuses rather than creating them. |
| Real retained read/compare | `tests/test_readonly_storage.py:51` | Reads a real result without changing workspace state. |
| Growing blob, both allowances | `tests/test_storage_adapters.py:88` | Appends after the first chunk; no bytes beyond the reference escape; limit/integrity refusal selected appropriately. |
| Oversized actual control | `tests/test_storage_adapters.py:125` | Small reference cannot authorize a larger file; patched `Path.open` proves refusal occurs before opening. |
| Oversized store discovery | `tests/test_storage_adapters.py:175` | Latest/revisions paths both refuse before reading oversized bytes. |
| Public reader without execution | `tests/test_inspection_runtime_cli.py:46` | Execution composition trap, exact run/release comparison, workspace snapshot unchanged. |
| Independent source acceptance | `tests/test_inspection_runtime_cli.py:61` | Explicit source producer exposes coverage; different producer refuses. |
| All CLI operations | `tests/test_inspection_runtime_cli.py:70` | Summary/source/records/compare share public views; zero cap retains counts/truncation; negative cap errors. |
| Live interrupted capture | `tests/test_inspection.py:63` | Real partial work reports pending/completed evidence while active state remains incomplete. |
| Later processing and zero work | `tests/test_inspection.py:95` | Reused capture/new processing and nonempty unchanged result remain distinct; blob bytes and limits inspected. |
| Mixed inherited/requested policies | `tests/test_inspection.py:126` | Per-source stages and complete change counts survive bounded output sampling. |
| Same content, new delivery | `tests/test_inspection.py:152` | Provenance changes while capture content stays equal. |
| Exact revisions and tampering | `tests/test_inspection.py:164` | Latest lookup is forbidden for exact run; changed saved bytes refuse. |
| Bounded comparison cleanup | `tests/test_inspection.py:177` | Each result layer streams once; tiny scratch bound refuses and leaves no scratch file. |
| Early closure | `tests/test_inspection.py:198` | Closing live records propagates to the work iterator. |
| Resealed incomplete results | `tests/test_inspection.py:218` | Empty required layers and orphaned files refuse admission rather than appearing as complete results. |
| Consistently wrong partitions | `tests/test_inspection.py:237` | Repartitions every work/result layer, reseals a self-consistent receipt, and specifically refuses disagreement with the pinned plan. |
| Actual rejected result | `tests/test_inspection.py:262` | Saved failures remain inspectable; scope is rejected output; complete-state comparison is unavailable. |
| Actual stateless result | `tests/test_inspection.py:278` | Saved file evidence is accessible, but no complete state or result comparison is claimed. |
| Swapped input/value associations | `tests/test_inspection.py:300` | Same values attached to different unchanged segments produce `contentChanged=True`. |
| No-plugin receipt inspection and samples | `tests/test_inspection_evidence.py:51` | Does not call plugin verification; zero cap preserves counts and unavailable facts. |
| Cache hit with/without recorded call | `tests/test_inspection_evidence.py:77` | Retained result origin does not erase a real attempt or invent one. |
| Capture prefix reuse | `tests/test_inspection_evidence.py:105` | Reused files and new downstream work counted independently. |
| Unaffected processor reuse | `tests/test_inspection_evidence.py:129` | Real changed-processor run distinguishes base result observations from current calls. |
| Terminal processor failure | `tests/test_inspection_evidence.py:162` | Failed attempt is visible without inventing successful result cost. |
| Page metadata | `tests/test_inspection_evidence.py:182` | Observed count comes from receipt metadata rather than representation count. |
| Receipt tampering | `tests/test_inspection_evidence.py:199` | Semantic reference and output-byte mismatch both refuse. |
| Zero diagnostic sample | `tests/test_run_active_view.py:487` | Overall/class counts remain exact; diagnostic detail count is zero and truncation explicit. |
| Installed public API | `tests/support/installed_runtime_probe.py:135`, `:153` | Capture and processed retained views, output IDs/configuration, comparison, and unchanged invocation counts use the installed public API. |

Coverage limits: no new exhaustive inspection-factory matrix for wrong document producer, wrong plan, or every profile pin variation was added. I traced those paths through exact plan comparison, existing catalog artifact acceptance, and `_local_profiles`; the existing profile conformance test exercises the shared pin-drift refusal (`tests/conformance/test_profile_descriptions.py:112`). This is a bounded coverage observation, not evidence that those paths accept invalid data. Performance at corpus scale, concurrent filesystem adversaries, and plugin replayability are not established by these tests or this review.

**Known scale limitation, not introduced by D19:** bounded report samples, streamed comparison, and profile-limited scratch do not make every admission path constant-memory or cheap. Opening a retained result still runs the existing full release verifier; its `verified_blobs` set grows with distinct retained blob references (`src/docspec/application/commit.py:199`, `:206`). Full admission also reads referenced content. The new admitted-layer shortcut avoids repeating the logical/blob traversal but does not remove that existing cost. No large-corpus qualification is claimed. Representative resource measurement and any resulting redesign belong to the existing D37 capacity/conformance work (`docs/dataset-experiments-todo.md:714`), rather than blocking the useful bounded inspection feature.

## 5. Findings

**F1 — resolved, correctness.** `run active` accessed nonexistent `store.plan.artifact_id`; it now checks `store.plan_id` (`src/docspec/cli/runs.py:215`). Real active-state fixtures cover it. Redundant planned-store loading was also removed because planned iteration already verifies its references.

**F2 — resolved, correctness/resource bounds.** Store discovery and control loading could read an oversized actual file before the reference-based bound took effect. `_read_exact(max_bytes=...)` is now used by control load and store load/latest/revisions; deterministic before-open tests cover both discovery variants and control reads (`files.py:61`, `controls.py:56`, `stores.py:339`, storage tests above).

**F3 — resolved, correctness/resource bounds.** A growing blob could previously yield beyond its declared size or requested allowance. The reader now bounds each read and refuses excess before yielding (`src/docspec/adapters/storage/blobs.py:118`). Both equal and larger caller allowances are covered.

**F4 — resolved, correctness.** `stateful=True` alone allowed a resealed empty result, and actual rejected runs, to appear complete. Required/logical layer and blob admission now precedes complete run claims; rejected and stateless views remain incomplete (`inspection_runs.py:71`, `:84`; `inspection.py:95`). The retained reader shortcut preserves exact layer binding while avoiding duplicated logical/blob work. Missing-layer, orphan, rejected, and stateless tests cover the corrected cases.

**F5 — resolved, correctness.** Comparing only a multiset of derived values could miss values swapped among unchanged inputs. Content fingerprints now retain derived `inputIds` and representation/segment `fileId`; the swapped-input regression and user documentation state the resulting meaning and limits (`inspection_comparison.py:63`, `:71`; `tests/test_inspection.py:300`; `docs/inspection.md:97`). This fixes the concrete ambiguity without introducing a semantic normalization system.

**F6 — resolved, correctness/configuration consistency.** Run-only active admission now requires `policy.bucket_count == plan.partition_count`, matching retained admission (`inspection_runs.py:95`, `commit.py:302`). The real negative fixture repartitions every work/result layer consistently, reseals the run, and specifically asserts the plan mismatch refusal (`tests/test_inspection.py:237`). This closes the configuration-consistency gap with one equality check.

## 6. Conclusion

VERDICT: APPROVE

The implementation otherwise achieves D19's bounded inspection and comparison intent. It provides useful evidence for what changed, what work was scheduled/reused, what failed, and which recorded input/settings/output differences accompany two results. Python and CLI share one view, readers reuse existing authority, and the extra modules separate admission, evidence interpretation, comparison, and outer storage composition rather than adding a second lifecycle.

Coverage of changed paths: ADEQUATE for the bounded feature, with the explicit matrix/performance limits above. The separate D16 representation guide added alongside final documentation work is outside this D19 review. Confidence: HIGH for traced static behavior; runtime outcomes are separate evidence.

D19 acceptance is supported for the delivered CLI/API and its stated evidence limits. This does not complete D04's broader planning/export conveniences, D20's whole-workflow interruption/cancellation proof, or D38's full installed experiment acceptance exercise. Source-wide matching-selection counts, historical plugin replay, semantic quality/causation, and unavailable costs remain explicit limits rather than implied capabilities.


### Parent-reported executed validation — separate from this static review

The parent reports 1,059 tests passed and one live integration test deselected in 142.64 seconds, including the isolated installed-wheel probe. That full run preceded the final partition-count equality check and its additional test. The final focused inspection gate then passed 28 tests in 10.05 seconds with that check/test included. The parent also reports Ruff and `git diff --check` passed. I did not execute or independently rerun those commands. These outcomes establish the exercised fixture behavior; they do not establish remote CI, publication, or large-corpus qualification.
