# Core implementation ownership and acceptance map

C01 completed 2026-09-13 against source revision `e7c2d70` on `docs/core-model`.
This is the implementation starting map for the [Core tasks](core-model-implementation-tasks.md).
The [spec](core-model.md) defines meaning; the [plan](core-model-implementation-plan.md)
and [adopted decisions](history/2026-09-13-core-model-consensus.md#current-adopted-decisions)
define the selected implementation. The ownership tables preserve the C01
baseline and retirement decisions. The task list records current completion;
the live retirement notes below distinguish removed owners from work still
being replaced.

## One owner for each rule

Keep callers → runtime → application → ports/domain, with concrete adapters
connected by runtime composition, as described in [CONTRIBUTING](../CONTRIBUTING.md#organize-code-for-its-reader).
An owner means one implementation of a rule; it does not require a new service,
class, port, or file for every table row. Existing application modules may become
the shared owners. Domain checks can execute through DuckDB batch queries.

| Required behavior | C01 baseline | Target owner and task | Preserve; replace or retire |
| --- | --- | --- | --- |
| Immutable values, distinct occurrences, keyed roots; Core §3 | Document-specific [content records](../src/docspec/domain/content.py), `src/docspec/domain/release.py` | Domain records and common admission: C02/C04; root admission: C10 | Preserve source keys and literal values. Replace document-stage prerequisites for dataset membership. A changed value gets a new occurrence; `SourceItem.identity` remains a source key, never an immutable value identity. |
| Typed records and supplied payload schemas | Handwritten domain `from_dict`/`to_dict`; [catalog schema admission](../src/docspec/adapters/catalog_artifact/schemas.py) | C04 owns fixed msgspec records and jsonschema-rs admission; semantic checks stay with the relevant application owner | Replace duplicate structural parsing and Python jsonschema refusal validation for the same schemas. Keep source-policy meaning and actionable refusal details. |
| Canonical value and correspondence bytes; Core §§6–7 | [Identity functions](../src/docspec/domain/identity.py), [framing](../src/docspec/adapters/framing.py) | Shared Rulespec decoder/encoder; C05 owns versioned DocSpec comparison encodings | Preserve duplicate-key refusal and SHA-256. Encode every changed/new value once per required encoding; reuse unchanged admitted bytes. Remove alternate identity encoders. |
| Bounded content exchange | [RecordStorage](../src/docspec/ports/record_storage.py), [Parquet adapter](../src/docspec/adapters/storage/records.py), [record workspace](../src/docspec/ports/record_workspace.py) | Same storage boundary, Arrow batches and DuckDB relations: C03/C06 | Keep partition pruning, physical integrity and stream closure. Replace internal row parsing/re-encoding and per-item spooling where batches express the work. Row conveniences call this path. |
| Exact artifact bytes and readiness; Core §5 | [BlobStore](../src/docspec/ports/blob_store.py), [local adapter](../src/docspec/adapters/storage/blobs.py), [S3 adapter](../src/docspec/adapters/s3_blob.py) | Existing blob boundary: C07; policy authorization: C18 | Keep exact bytes, streamed reads and put-if-absent. Implement file/directory durability, in-flight protection and bounded backend deletion. Neither the port nor retention preview currently deletes. |
| Durable authoritative records | `src/docspec/adapters/storage/controls.py`, `src/docspec/adapters/storage/catalog.py`; `src/docspec/adapters/processor_cache.py` is derived | One SQLite adapter behind application metadata operations: C08 | Replace control/manifest authority and the one-winner cache's role in result discovery. Content manifests may remain data or exports. Add failure/progress, evidence, policy and imported-root writes to the same authority. |
| Successful retention and publication; Core §5 | `src/docspec/application/commit.py`, manifest catalog, `src/docspec/application/execution_checkpoints.py` | Shared application publisher: C09; transaction mechanics: C08 | Preserve retained alternatives, complete input/output recovery and stale-current refusal. Publish only after content is ready; enforce actual generation/usage/derivation validity at admission. Remove separate release/result publication decisions. |
| Operation definitions, attempts and outcomes; Core §§2, 4 | `src/docspec/application/execution.py`, `src/docspec/application/processor_runtime.py`, `src/docspec/application/reconcile.py` | Common application lifecycle: C11; document integration: C19 | Preserve requested/executed distinctions, successful empty output, null, failures and adopted results. Separate publication retry, verified continuation and fresh producing attempts. Replace overlapping stage/processor attempt and progress state machines. |
| Value edits and membership revisions; Core §3 and §9 | `src/docspec/application/planner.py` and active document layers; no general revision resolver | Shared value-edit and membership rules: C12/C13; DuckDB performs relational bulk work | A value patch derives an occurrence before membership puts it at a key. Add strict ordered put/remove semantics and invalid-edit refusal; replace source-specific changed-state inference for shared behavior. |
| Whole, field and member-field dependencies; Core §§5.2, 6.1 | `src/docspec/application/processor_rules.py`, `src/docspec/application/base_reprocessing.py` | One selected-value evaluator: C14, using C05 encoding | Preserve types, absent/null, missing members, duplicates, material keys/identities and meaningful positions. Replace stage-specific field comparison with the shared evaluator; keep exact origin and binding-specific retention. |
| Dependency adequacy and affected results; Core §6 | `src/docspec/domain/processors.py`, planner invalidation | Application adequacy rules and scoped ledger queries: C16; graphlib ordering: C11 | Add retained omissions/corrections, uncertainty and evidence version checks. Replace custom ordering/traversal where graphlib or scoped SQL expresses it. Adequacy precedes execution policy. |
| Multiple matching results and exact reuse associations; Core §7 | ProcessorRuntime, base reprocessing and one-result-per-key SQLite cache | Shared correspondence/selection owner: C17 | Preserve the original generating execution and the exact chosen result. Replace immutable-winner cache policy and scattered eligibility checks with many-candidate lookup and current-request selection records. |
| Checkpoint materialization; Core §3.3 | `src/docspec/application/maintenance.py` creates an equivalent successor release | State representation owner: C15 | Preserve recoverable history and values while physical rewrites keep the same logical state ID. Remove the compaction-only logical successor and its release/current updates. |
| Guarded current selection and policy removal; Core §5.6 | Manifest catalog compare-and-swap; `src/docspec/application/maintenance.py`; `src/docspec/adapters/blob_inventory.py` | Application selection/retention policy owner: C18; transactional evidence: C08; byte removal: C07 | Preserve shared references, retention accounting and stale-selection refusal. Add cited immutable policies, durable removal intent/outcomes and recovery; replace manifest current authority and release-only reachability. |
| Direct calls, CLI, inspection, adapters and export | [Runtime](../src/docspec/runtime/__init__.py), [CLI](../src/docspec/cli), [Dagster](../src/docspec/adapters/dagster.py), [result export](../src/docspec/adapters/result_export) | Existing runtime composition: C20; thin optional adapters: C21 | Preserve document/source capabilities through common Core records. Replace old signatures/configuration directly; Dagster owns scheduling and native retries, while DocSpec owns retained semantic meaning. |
| Recoverable PROV interpretation and behavioral conformance; Core §§1–9 | `src/docspec/application/execution_evidence.py`, [regression map](../conformance/test-matrix.json) | C02 independent fixtures, C09 admission, C24 complete ledger checks; C25 capacity | Preserve useful regression assertions and pytest/JUnit. Add Core/Keyed-State checks; remove checks exclusive to retired formats. No PROV exporter or separate test runner. |

The six public metadata operations remain those in plan §4. C08 also owns the
internal durable writes named there; a failed attempt does not have to pass the
successful-retention publisher to record its failure. DuckDB receives bounded
consistent metadata batches and never becomes a second ledger writer.

## Document workflows that must survive

These are behavioral acceptance seeds. Update their callers and assertions to
the new records; do not keep old wire shapes solely to keep tests green. C02
adds independent Core fixtures; C19/C21 adapt the existing capabilities and C24
checks the assembled installed package.

| User workflow | Existing executable evidence to carry forward | New-path acceptance |
| --- | --- | --- |
| Supplied records or a pinned source catalog, usable without fetching | [Local catalog tests](../tests/test_local_catalogs.py), [installed source reader](../tests/test_source_catalog_installed_wheel.py) | Preserve selected/excluded populations, collection outcomes, literal metadata and field provenance; admit catalog values as roots without fabricated capture activities. |
| Capture now, extract/segment/process later | `tests/test_capture_lifecycle.py`, [processing pipeline](../tests/test_processing_pipeline.py) | Preserve stopping points, exact source bytes, extraction choices, coordinates and complete result membership. Later processing fetches nothing already retained and usable. |
| Change a processor or resource; change unrelated metadata | `tests/test_processor_reprocessing.py`, [source snapshots](../tests/test_source_catalog_snapshot.py) | Only affected work executes. A title-only revision preserves URL-only correspondence, including across all/named members; a relevant URL change invalidates it. |
| Retain A, retain B, select A; repair failed inputs | `tests/test_run_active_view.py`, `tests/test_failed_item_repair.py` | Keep alternatives and exact reuse selections, with successful empty/null and failure distinguished. A fresh attempt has its own identity. |
| Interrupt, reopen and recover | `tests/test_stage_checkpoint_recovery.py`, `tests/test_checkpoint_blob_admission.py`, `tests/test_execution_backends.py` | Preserve verified completed work and cumulative limits. Add crash points around blob readiness, ledger commit and deletion; never report partial publication as success. |
| Inspect, compare and export without rerunning | `tests/test_run_active_view.py`, [exports](../tests/test_result_export.py), [export admission](../tests/test_result_export_admission.py) | Preserve complete values, exact source associations, typed evidence and independent reading; distinguish metadata open, consumed-data checks and complete audit. |
| Run locally or through optional services | [Dagster example](../tests/test_dagster_experiment.py), [S3 tests](../tests/test_s3_blob_adapter.py), [package boundary](../tests/test_package_boundary.py) | Same retained meaning through both execution routes; core installs and imports without optional providers or services. |
| Use named source examples | [GAO](../tests/test_gao_topic_example.py), [public comments](../examples/spicyregs_comments.py), [GovInfo bills](../tests/test_govinfo_bill_installed_wheel.py), [annual CFR](../tests/test_govinfo_cfr_example.py), [FEC](../tests/test_fec_committees_example.py) | Preserve each example's selected population, source facts and evidence through installed packages. Keep acquisition and publisher facts with the provider; normalization/selection and dataset results stay here. |

## Direct replacement and retirement

Each implementing task removes the old path as its callers move. C23 audits this
map for completeness; it is not a reason to accumulate two runtimes until the
end. No user datasets are deleted by code retirement. Existing on-disk workspaces
are not imported or migrated; the new implementation admits fresh Core data.

| Current API or format to replace | Direct replacement and removal owner |
| --- | --- |
| `ProcessingPlan`, `StoreTask`, `DocumentStore` revisions, planned-store ledger, `ExecutionHandoff`, stage/processor control receipts | C11/C19 replace their shared lifecycle roles with operation definitions, attempts, batch progress, results and selections. C20/C21 update runtime, CLI and worker callers; C23 removes exclusive control persistence/readers and obsolete configuration. Preserve bounded worker references and actual failure evidence. |
| `DocumentRelease` and `LocalManifestDocumentCatalog` as authoritative state/publication APIs; local `release.json` and filesystem current pointer as authority | C09/C10/C18 introduce shared publication, states and guarded ledger selection. C20 moves public catalog/inspection callers; C21 adapts portable exports. Remove the old authority after cutover; export manifests remain independent artifacts. |
| `LocalSqliteProcessorResultCache` and `ProcessorResultCache` winner semantics | C17/C19 move all reuse callers to the multi-result index and selection owner, then remove the obsolete port/adapter and cache configuration. No cache migration or dual write. |
| Internal `Iterable[Mapping]` → canonical JSON → Arrow → fetched rows → parsed dictionaries handoffs | C06/C19 replace relational handoffs with bounded batches. Keep a row convenience interface only over that same path; retained blobs and already canonical payloads are not re-encoded during exchange. |
| Source-item identity used as a whole immutable value identity; compaction creating a new logical release | C10/C19 separate member keys from occurrences. C15 retains logical state identity through physical rewrites. Remove checks that require those old identity couplings. |
| Handwritten fixed-record serializers and generated schema files for replaced records | C04/C19 move producers and readers together. C23 removes only superseded schemas and registrations, including affected `src/docspec/storage_profiles` and `tests/test_machine_files.py`. Source input and document payload definitions that still carry product meaning remain with their owners. |
| Duplicate schema engines, graph loops, old implementation strings and compatibility wrappers | C04 removes Python `jsonschema` once all its current callers use the chosen validator; C11/C16 replace graph algorithms; C20/C23 update direct imports, examples, tools, registrations, dependency metadata and lock together. Retain the existing blob mechanisms and shared Rulespec package. |

For each row, completion evidence must name removed symbols/files and updated
callers or explain which reduced document adapter remains. Check static imports,
profile implementation strings, CLI registrations, examples, tools, tests and
installed-package consumers. Git preserves old code and fixtures. No legacy
reader, alias, old-format exporter or migration framework is added.

## Live retirement notes — 2026-09-14

C23 has removed 60 superseded production modules and ten obsolete machine-profile declarations. The common Core
publisher, operation lifecycle, state storage and maintenance now own the rules
that previously lived in document planning, execution, reconciliation, release
commit, current selection and compaction services. The old runtime composition,
preparation, handoff and inspection modules are gone. No compatibility aliases
or second metadata writer were added.

- `adapters/execution.py` retains only bounded workers and input-stream closure.
  Its old task/profile verifier and backend, `domain/execution.py` and
  `ports/execution_backend.py` were removed.
- Old document store/catalog adapters, processor cache, result-sink lifecycle,
  blob inventory and their superseded ports were removed. Package exports no
  longer import the retired application and domain graphs.
- The shared blob and Parquet adapters remain. Local blob and record deletion
  use the same verified, durable unlink helper. Core cleanup preserves shared
  files, records intent before changing availability, and records resumable
  outcomes for each physical target. Available entity rows, including state
  members pinned by a reference, pin their current source layers; a state
  protects its own layers through its manifest. Authorized removal names states,
  not their unpinned members, and can reclaim bulk values while retaining the
  ledger identity, digest and provenance of every row. Exact restoration
  validates the original digest.
- Explicit checkpoints and pending publication journals remain protected while
  their attempts are recoverable. Retention uses the publisher's existing
  obligation checks for both retained roots and staged journal records. A
  successful retained result releases its redundant recovery journal for
  removal under an explicit orphan policy.
- The obsolete `LocalWorkspace`, `ProfileRegistry` and their machine profile
  declarations were removed after source catalog conveniences moved to direct
  paths/Core workspaces. Runtime constructors now select the actual adapters.
- Corrected dependency evidence retains its observation receipt and evaluated
  comparison. Historical input bindings remain described without permanently
  pinning whole input bytes after an authorized removal; a new selector or
  unresolved omission still cannot borrow that comparison.
- Source acquisition, source-catalog readers/builders, catalog preview,
  extraction, segmentation and evidence coordinates remain product behavior.
  Their implementations are retained rather than replaced with lifecycle
  compatibility code.

Portable exports now use Core's retained records and shared physical inventory.
Their disposable record index reuses `RecordWorkspace`; the old affected-row
reconciliation table and methods were removed, and the SQLite adapter now lives
in `adapters/record_workspace.py`.
The old control repository, execution-evidence model, processing plans, processor
cache model and release records were removed after their last production callers
moved. Processor data-use, provider observation, output validation and credential
checks now have direct shared owners; their useful behavior remains.

Current removal/recovery evidence lives in
[Core maintenance tests](../tests/test_core_maintenance.py),
[checkpoint tests](../tests/test_core_checkpoints.py),
[publication tests](../tests/test_core_publisher.py) and
[content protection tests](../tests/test_core_content_protection.py). Native
storage assertions were preserved in
[record tests](../tests/test_storage_records_catalog.py) and
[blob tests](../tests/test_storage_adapters.py), while their obsolete document
store/catalog cases were removed. Source-catalog interruption fixtures were
moved out of the old execution fixture closure without changing their checks.
The old tests were retired by behavior rather than kept alive through removed
production APIs. This mapping covers the removed lifecycle fixtures and tests;
retired format-specific assertions have no replacement requirement.

| Retired test groups | Current behavior and evidence |
| --- | --- |
| Workspace/profile registry, machine descriptions, planner, task handoff, stage selection, worker identity | Core record admission, direct runtime construction, operation graphs and bounded execution: `test_core_records.py`, `test_core_runtime_cli.py`, `test_core_documents.py`, `test_core_execution.py`. Old profile formats and saved store-task shapes are removed. |
| Capture lifecycle, experiment retention, catalog audit, release selection, inspection, platform artifacts and dispositions | Publication, current selection, retained evidence and inspection: `test_core_publisher.py`, `test_core_maintenance.py`, `test_core_runtime_cli.py`. Old release/run-specific rendering is removed. |
| Application pipeline, failed-item repair, prefix reuse, processor cache/reprocessing, old runtime stages/base readers | Common correspondence and correction checks plus document capture/extract/segment/processor reuse: `test_core_reuse.py`, `test_core_dependencies.py`, `test_core_documents.py`. No second document cache remains. |
| Checkpoint admission, processor/stage recovery, result sinks, incremental/recovery conformance | Original-attempt continuation, partial-event preservation, authoritative failure history and guarded publication: `test_core_continuation.py`, `test_core_execution.py`, `test_core_maintenance.py`. |
| Release/store/sink integrity, release export, read-only composition and old CLI | Core admission, selected-root portable export and direct Core CLI: `test_core_records.py`, `test_core_publisher.py`, `test_result_export.py`, `test_result_export_admission.py`, `test_core_runtime_cli.py`. |
| Old work-budget and prefix-accounting tests | Store/profile counter formats are removed. The document run uses an ordinary Core operation and checkpoint for cumulative source-byte/produced-row accounting. `test_document_run_budgets.py` verifies fresh-process failure/resume, charged failed work, uncharged reuse, zero-output attempts, pinned limits and refusal after uncheckpointed hard kills. A new run ID explicitly starts a new budget. Per-file, shared control/engine and processor limits remain. |

Native layer conformance now constructs the actual Parquet adapter directly;
partition reuse, ordered identities and tamper checks remain. Extraction and
segmentation tests retain source-coordinate and content-identity checks. S3 tests
now use Core for capture, reopen, reuse, expected-digest rejection and separate
SDK retry counts from actual document attempts. Credential checks remain against
the shared security owner. Source catalog fixtures were moved to dedicated test
support instead of importing the retired execution graph.

C24 is complete for the package pinned in its
[acceptance evidence](core-model-implementation-tasks.md#c24--complete-conformance-regression-and-installed-package-checks). C03 records operation baselines; C25 records measured performance and its limits.
Passing semantic tests does not establish capacity.

## Checklist ownership

The Core task list is the sole sequence for this replacement. The
[dataset checklist](dataset-experiments-todo.md) retains completed work as history
and routes overlaps below; it does not schedule the same implementation twice.

| Earlier item | Current disposition |
| --- | --- |
| D31 shared-code cleanup | C04 owns validator consolidation; C07 retains and completes the existing blob adapter; C23 checks remaining duplicates. The earlier conditional Rulespec writer adoption is closed out of this plan. Rulespec owns any independent upstream fix; it is no Core prerequisite. |
| D37 conformance/capacity | C24 owns assembled regression/conformance and installed-package checks; C25 owns the targets below. Prior failed trials remain failed historical evidence. |
| D48/D54 lifecycle and cleanup | C11 owns shared attempts/dependencies/progress; C19 moves the document workflow; C21 adapts Dagster; C23 audits retirement. Earlier Dagster-only authority and prototype/adoption sequencing are superseded. |
| D22 acquisition campaign integration; D49/D50 external search recipe qualification | Remain separate, externally scoped items. C21 preserves current adapters; it does not qualify an unsupplied campaign or search recipe. Search/indexing and provider runner retirement remain in their repositories. |
| Maintainability E5 → D40 | D40 remains the one owner for an unfamiliar human's contribution exercise. C20/C25 update the contributor path but cannot claim that human exercise. |
| Completed D01–D56 and maintainability items | Keep their dated evidence. Their useful behaviors enter the preservation table; their old APIs and formats do not constrain Core. Do not reopen mechanical refactors without a concrete need. |

## Implementation acceptance — 2026-09-14

The user directed: “wrap this testing up, things seem good enough?” The
implementation closes with the current strict regression and installed-package
gates, the completed representative Core/recovery/concurrency checks, the
larger-than-memory check already running, and final documentation/diff checks.
Finish that independent value check; start no further large workloads or repeated
benchmark trials for this refactor. Functional requirements and correctness
checks remain unchanged.

C03 owns reproducible operation baselines, including precisely labeled historical
measurements. C25 owns the current measured results, their limitations, and the
implementation documentation. The expanded capacity matrix below is retained as
a reference for a future performance claim, not as an implementation completion
condition. This changes the scope of qualification, not the recorded limits or
results: the current 100-batch run reconciles every change but fails its original
time and memory targets. Unrun long-history, full document-capacity and repeated
trials remain unqualified; they are not represented as passing or as unimplemented
features. See the [current evidence](history/probes/2026-09-14-core-bounded-writer-capacity.json)
and [C24](core-model-implementation-tasks.md#c24--complete-conformance-regression-and-installed-package-checks).

## Capacity targets fixed before tuning

These are the original engineering capacity budgets, not measured Core results.
They remain falsifiable targets for capacity claims; the implementation acceptance
amendment above ends the expanded qualification work for this refactor. The document workload sizes
and limits carry forward the [frozen Parquet trial](history/2026-09-12-parquet-capacity-observations.md).
The Core workload below adds the generic operations that trial never exercised.
Any later budget change needs an explicit rationale and a new recorded target;
keep the original failed observation.

### Inputs and operations

| Input | Pinned definition | Required operations |
| --- | --- | --- |
| Document control and capacity | Existing [recipe](../tests/support/capacity_experiment.py): `text16`/`markup16` smoke, `text512` control, `text4096` (4,096 files, 83,886,080 captured bytes, 20,480 segments) and `markup256` (288 files, 99,090,432 bytes, 3,712 segments) | Build/verify catalog, capture, prefix/resume processing, inspect, change the phrase resource, run a separate clean control and compare every result. Keep excluded inputs. Add title-only revision, failed-input repair and A→B→A selection from the behavioral fixtures. |
| Generic Core full workload | `core-bulk-v1`: 1,048,576 keyed JSON members, each with an 8,192-byte ASCII body plus URL, title, integer position and typed nested metadata. Body bytes alone total 8 GiB. Keys are zero-padded decimal ordinals. | Admit and durably publish a root; evaluate whole/field/all-member/named-member selections; execute a URL-dependent operation; revise values and membership; publish/select/reopen; audit; checkpoint and compare to a clean materialization. Qualify a separate larger-than-working-memory selected-value case under the amended budget below. |
| Small edits and long history | From `core-bulk-v1`: separate title-only and URL edits to the first 1,024 keys; valid ordered put/remove edits; then 1,000 revisions of one title field, cycling through those keys | Measure independent title/URL effects and exact reuse; recover before/after a checkpoint; preserve source positions and retained sort rules. Named selection requests include 1,024 present keys and one absent key. |
| Writer, reader and cleanup contention | Same retained Core state; one writer submits 100 publication batches of 1,024 changed members while four readers repeatedly open metadata and request named fields. Run a separate cleanup/publication race over shared and new blob references. | Reconcile every acknowledged batch after reopen; detect stale current updates; measure reader latency and backlog. Retained/in-flight references survive cleanup; interrupted removal recovers from durable evidence. |

C02's [streamed generator](../tests/support/core_workload.py) fixes these inputs
before C03 measures or tunes production. For Core body block `b` of member `i`, concatenate
the lowercase SHA-256 hex of ASCII `core-bulk-v1:{i}:{b}` for `b = 0..127`.
Use URL `https://example.invalid/{i}`, title `title-{i}`, position `i`, and a
repeating metadata set containing absent fields, null, integer `1`, string `"1"`,
booleans, nested arrays/objects, Unicode and U+001F. Include 1,024 pairs whose
entire values are equal but whose occurrence IDs differ; copy the preceding
member's value into each odd member among the first 2,048 keys. Keys have seven
decimal digits; the generator pins the eight-case metadata cycle. C02's lifecycle
fixtures fix edit and provenance semantics; C03's recipe instantiates the capacity edits. Run selected-member evaluation
with both no ordering rule and a retained position/key tie-break rule.

### Time, memory and storage limits

Use a local SSD and one application worker; record the actual machine, filesystem,
OS, Python, wheel/dependency hashes and native thread count. The reference class
is the existing 48 GiB arm64 development host. Pin DuckDB to one native thread
and a 6 GiB engine memory setting; measure total process memory independently.
Use SQLite WAL with `synchronous=FULL`, foreign keys and the planned content
durability sequence. Never substitute NORMAL durability to pass a time budget.

| Measured operation | Elapsed-time ceiling | Process peak resident memory |
| --- | ---: | ---: |
| Each document build, source verification, capture, changed-resource or clean run | 1,800 s | 1 GiB |
| Document processing prefix plus resume active time | 1,800 s combined | 1 GiB per process |
| Document fresh inspection or complete comparison | 300 s each | 1 GiB |
| Core full admission through durable publication | 1,800 s | 20 GiB |
| Core complete-state recovery, full selected-value evaluation, full audit, checkpoint, or complete comparison | 600 s each | 12 GiB |
| Core 1,024-member edit through publication and reuse decision | 120 s | 2 GiB |
| Core metadata-only open; named 1,025-member field evaluation | 5 s; 10 s respectively | 2 GiB |
| Core 1,000-revision creation; recovery before checkpoint | 1,800 s total; 600 s recovery | 2 GiB |
| Contention run | 600 s total; reader metadata-open 95th percentile ≤ 5 s | 2 GiB per process, 8 GiB summed peak across the run |

**Memory amendment, 2026-09-13:** The user authorized more memory and prioritized
a fast working implementation over the original 512 MiB engine/2 GiB process
target. Wide canonical-byte sorting failed at 512 MiB/one thread and 4 GiB/four
threads. With 8 GiB/one thread, the complete 8,758,829,870-byte comparison stream
finished in 38.7 seconds at 9.28 GiB peak process memory. Keep the original failed
observations; the new 12 GiB process allowance accommodates measured native
overhead. The existing full fixture no longer exceeds the process allowance,
so it does not prove the larger-than-working-memory requirement. That additional
qualification remains in C03/C25. The [two-copy recipe](core-larger-than-memory.md) prepares a separate 16 GiB body population with unchanged source values and distinct identities; its smoke checks are not a capacity claim. Small-operation and handoff limits still apply.

**Native setting, 2026-09-14:** The process targets stay unchanged. The initial
8 GiB checkpoint completed but exceeded 12 GiB RSS; a
[4 GiB trial](history/probes/2026-09-14-core-checkpoint-four-gib.json) refused for
insufficient native memory. A [6 GiB trial](history/probes/2026-09-14-core-checkpoint-six-gib.json)
completed checkpointing in 167.72 seconds and checkpoint plus full audit in
356.98 seconds, at 11.73 GiB peak RSS. Use the shared 6 GiB default for final
qualification. This is a measured tuning candidate: the trial repacked an
already checkpointed layout, reused entity publication units, and overlapped
source edits. Fresh workspaces, pinned packages and repeated trials remain
required; these observations do not close C25.

**Full-admission memory amendment, 2026-09-14:** The user authorized more memory
and prioritized speed and simplicity. With the shared sorted reader and a
2,048-row partition-write flush threshold, the
[million-member build](history/probes/2026-09-14-core-flush-threshold-full-build.json)
completes in 242.71 seconds at 15.50 GiB RSS. A separate physical-reader path
[takes 392.10 seconds](history/probes/2026-09-14-core-physical-reader-full-build.json)
at 12.12 GiB and increases transactions from 16,395 to 68,771. Remove that
additional path and set the full-admission allowance to 20 GiB, including
headroom above the measured peak. Both observations still fail their original
12 GiB target. Other operation limits remain unchanged; the separate 16 GiB
body population still exceeds its selected-value evaluation's 12 GiB allowance.
This amendment does not replace fresh-package and repeated qualification.

Document main workspace plus temporary storage keeps its 8 GiB allowance; the
clean control has a separate 8 GiB allowance. Core main plus scratch has 80 GiB;
the independent clean workspace has a separate 80 GiB allowance. Measure scratch
and retained bytes separately. Generation and independent oracle checks are timed
and must succeed, but have no latency target; they cannot contribute cached
answers to the timed production operation. Their memory use is reported separately.

Bound application handoff batches by both 2,048 rows and 8 MiB of encoded payload,
whichever is reached first, with at most two pending batches per stream. Exercise
a separate single-value boundary case up to the existing 8 MiB record ceiling
and explicit refusal above it. Engine intermediates may spill; never interpret
an engine setting as a process-memory guarantee. Record sampled storage as
sampled, and use allocation accounting or a filesystem quota before claiming a
hard peak limit. Capacity measurements do not require a new quota subsystem.

### Scan, conversion and reuse limits

- Metadata-only opens scan zero payload rows and decode no member values.
- Within one admitted reader lifetime, a title-only edit or URL-only reevaluation
  reads zero opaque body blobs and does not decode or re-encode unaffected
  occurrence payloads. Only the changed members' values and newly required selected-value structures may need
  canonical encoding. Fresh-process integrity admission is measured separately;
  it is never silently skipped to meet the warm-reader target.
- Named-member queries perform key-directed selection. Logical payload rows
  emitted from storage stay within the requested present population; record
  physical row-group reads separately. After admission, a 1,024-member field edit
  may scan at most 64 MiB of payload columns, excluding already retained compact
  selection/index columns. Arrange this fixture's edited keys contiguously;
  this is not a claim about arbitrary scattered-key locality.
- Full operations allow at most three logical passes over their required input
  payload columns, including within-operation verification. Record physical
  integrity reads, SQL spill rereads and total bytes independently. No full base
  scan occurs once per member, request or edit. Full-body selections must consume
  all selected bytes even when cached short fingerprints would be faster.
- Resolve the 1,000-revision history in batch passes over the base and ordered
  edit relation, not 1,000 full base scans. Checkpointed recovery reads the
  checkpoint and its suffix, while original history remains recoverable.
- First evaluation counts all necessary shared decoding/encoding work. Repeated
  comparison can reuse verified selected bytes/digests. Report Python calls,
  allocations, converted rows/bytes, SQL calls, transaction count, fsync calls,
  and new durable files alongside elapsed time. No per-record query or transaction
  is used for relational bulk lookup/publication.
- Title-only changes execute zero URL-dependent operations. Relevant URL changes
  execute only affected operations. Changed phrase resources repeat processors,
  with zero upstream fetch/extract/segment calls; successful resumed work is not
  repeated outside the explicitly unfinished batch. Check exact outcomes against
  independent fixtures and the clean run, not counts alone.

The original full-capacity protocol requires one fresh-process trial and three
trials with explicitly recorded cache state; every trial must satisfy correctness,
time and memory limits before claiming that full qualification. The acceptance
amendment above removes this repeated matrix from the current implementation gate. Report all runs,
including timeouts; a fresh process does not imply a cold OS cache. Preserve the
native time/RSS output, query profiles, measurement scope, input/recipe pins,
actual result references and complete oracle comparisons using the existing
[qualification procedure](qualification.md#qualify-a-capacity-claim).

## C01 completion evidence

The ownership and retirement tables cover the plan's shared behaviors, preserve
the document workflows, route overlapping checklists and fix workload budgets
before tuning. No runtime code, dependency or retained data changes in C01.
C23 closes retirement evidence; C24 and C25 establish behavior and capacity.
