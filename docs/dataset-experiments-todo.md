# Dataset experimentation and simplification to-do list

DocSpec should make it easy to build a catalog, fetch selected documents, retain
them, run chosen processors now or later, and compare iterations while reusing
unchanged work. This checklist organizes the next changes around that outcome.
Across DocSpec and its source provider, the goal is to maintain each shared
capability once and reuse it through installed packages, reducing duplicate
implementation, testing, configuration, and documentation effort.

**Status: 9 of 51 local implementation items complete.** D47 is a moved-task
reference; D51–D52 retain the named dataset examples moved here from SpicyDocs.
Compiled on 2026-09-11 against merged revision
`dd18fb364acdc383643bacf52a108c92e0173aef`. This is a plan, not evidence that the
capabilities below have been implemented or validated. Some already exist inside
application services; those items call for making them usable and proving the
whole workflow, not rebuilding them.

The later [search-catalog investigation](history/2026-09-11-search-catalog-consolidation.md)
adds D47–D50 using the inspected SpicySearch and SpicyEngine candidate branches.
Those items propose shared build execution and definitions, not moving native
indexing or serving into DocSpec.

The [previous maintainability checklist](maintainability-todo.md) remains the
record of the first refactor: 30 of 31 items are complete. Its unfinished human
contributor exercise carries forward as D40. Do not reopen completed mechanical
splits or documentation consolidation without a new, concrete problem.

## Decisions guiding this work

- **Keep DocSpec as the dataset experimentation platform.** Catalog construction,
  retained inputs, processing history, selective execution, and usable results
  belong together. Processor dependencies, configuration identities, checkpoints,
  and executor adapters support this purpose.
- **Reuse source-provider code through an installed wheel.** That provider may
  remain SpicyDocs or become SpicyRegs. Keep the package/repository choice open;
  decide ownership by responsibility. Existing SpicyDocs integrations can supply
  inputs now. D41–D46 identify reusable source capabilities and later handoffs;
  moving code or choosing a repository is not a prerequisite for DocSpec's
  experiment workflow.
- **Expose DocSpec's dataset capabilities through its own wheel.** Source and
  experiment tools can replace their dataset loops with supported DocSpec APIs.
  Connect the two products in an optional integration or experiment caller;
  independent source code must not depend on DocSpec. Separate modules alone do
  not prevent a circular package dependency. Coordinate both wheel directions
  with SpicyDocs S31 and the dependency map below.
- **Support useful stopping points.** A catalog, captured documents, and a
  processing attempt can each be useful outputs. Export is optional. Every
  experiment need not end in a searchable release.
- **Allow deliberate breaking changes.** This is greenfield work with no legacy
  support requirement. Change formats, identities, APIs, or prior decisions when
  that improves the product; update current producers and consumers together.
  Preserve exact source evidence and useful history. Compatibility retirement
  does not require deleting retained datasets.
- **Keep interpretation with its processor.** A processor may use RefSpec
  resources or another implementation. DocSpec records and executes it; the
  processor owns its domain meaning. Dagster schedules tasks; DocSpec determines
  the work and checks the results.
- **Judge complexity by the work it saves or the errors it prevents.** Retain
  bounded execution, verifiable reuse, attributable evidence, complete failure
  accounting, and safe publication. Simplify configuration, repeated validation,
  indirection, and overlapping implementations where the same guarantees survive.

This revises the earlier reviews' recommendations to merge DocSpec and SpicyDocs,
make search export the mandatory completion path, or defer processor extensibility
because the installed command currently uses only a statistics processor. The
user's clarified experimentation purpose takes precedence over those proposals.

## How to use this list

**Tasks live where their implementation changes.** This checklist owns DocSpec
work. A cross-repository effort has a local task in each affected repository;
other lists link to it as a dependency. A reference or moved ID is not another
implementation checkbox. Update status and acceptance evidence at the destination.
Changing a source provider, shared library, Search builder, or Engine requires
its destination task; consuming an unchanged package does not create work there.

P0 establishes the usable experiment workflow. P1 completes reliability,
inspection, and consumer access. P2 addresses broader consolidation and measured
maintenance costs. Dependencies take precedence over priority labels.

Each item names an outcome and a completion check. DocSpec owns implementation
unless another owner is named. Items requiring judgment must record the selected
approach, alternatives, user benefit, and a solutions architect subagent's review
before implementation. Independent semi-formal code reviews assess the resulting
changes. A reviewer can disagree; record the resolution rather than claim consensus
where none exists.

Use logical commits, update completed entries with implementation and validation
evidence, and keep local checks, CI, merge, and release status separate. A decision
to defer a conditional item is a documented deferral, not completed implementation.

## 1. Make the experiment workflow usable

<a id="d01"></a>

- [x] **D01 · P0 · Align the product explanation and current behavior.** Update
  README, architecture, and decision status around catalog building, capture,
  optional processing, iteration, and optional export. Explain what is available
  through supported entry points versus only through internal composition.
  **Done when:** a reader can identify inputs, outputs, useful stopping points,
  and present gaps without reconstructing previous discussions. Correct README's
  claim that no separate portable structural path exists.
  **Completed September 11:** README, architecture, documentation index, and
  Decision 0002's status now distinguish intended experiment use from current
  entry points, mandatory extraction/segmentation, processor-only reruns, and
  the separate portable verifier. Independent review approved these claims and
  preserved the dated intent with a visible correction. Local links checked;
  this documentation does not qualify the unimplemented workflow. Commit `4cda52b`.

<a id="d02"></a>

- [x] **D02 · P0 · Define dataset, run, and attempt clearly.** Decide how a
  persistent dataset relates to catalog versions, acquisition work, processing
  attempts, and a selected base. Reuse existing plans, references, stores, and
  receipts before adding persistent models. **Done when:** processing retained
  inputs later creates an attributable attempt; alternatives can share a base
  without overwriting one another; “resume this attempt” and “start another
  experiment” have distinct, documented behavior. Include a dependency sketch.
  **Completed September 11:** [the experiment guide](experiments.md) defines
  these names, reference relationships, and identical-plan versus resume behavior.
  `retain_release` preserves alternatives independently of `select` and its
  explicit expected current; the CLI exposes both. Tests run two processor
  alternatives from one base without refetching, preserve exact no-op state,
  and cover stale selection, concurrent retention, damaged dependencies, and
  compaction recovery. Independent architecture and code reviews approved the
  approach. Core implementation: `f813458`. The complete installed lifecycle
  remains D03–D05/D38; no additional dataset or run ledger was introduced.

<a id="d03"></a>

- [x] **D03 · P0 · Provide one simple runtime configuration.** Replace the
  ordinary user's eight storage roots and six profile documents with a workspace
  location, useful defaults, explicit limits, and selected implementations. Keep
  advanced overrides where they serve an actual backend. **Done when:** an
  installed example runs from a small configuration, checks invalid settings
  before work, and records the effective configuration needed to reproduce it.
  Accepted verifier policy remains independently configured; do not derive
  acceptance authority from the supplied artifact or selected implementation.
  Depends on D02; see [request composition](../src/docspec/cli/requests.py).
  **Progress, September 11:** installed profiles now have one canonical home in
  `src/docspec/storage_profiles/`, with checked built-in/local selection and a
  default `docspec profile list`. Version `2.0` local requests now use one
  workspace, optional root/profile overrides, and one-worker defaults while
  keeping limits and verifier acceptance explicit. Effective configuration uses
  the existing worker and execution records. A focused test prepares equivalent
  implicit/explicit settings and proves identical references and successful
  resume. Workspace, CLI, offline-example, Dagster, conformance, and isolated-wheel
  checks passed (76 tests); independent review approved the foundation. The
  ordinary caller's plan/implementation configuration was completed below.

  **Runtime progress:** the [Python run API](python-runs.md) now accepts typed
  settings and the same workspace directly. An isolated installed-wheel probe
  builds a catalog, injects a fetcher and processor, runs once, and recovers the
  same result without caller plan/request files. Invalid settings refuse before
  planning.

  **Completed September 11:** `prepare_local_experiment` now builds the existing
  plan from the catalog, workspace, explicit limits and accepted producers, and
  selected stage/processor objects. `PreparedLocalRun.plan` exposes that exact
  plan. Python and CLI share local execution defaults. Processor policy errors
  refuse before output storage is created; execution rechecks the same rules.
  Advanced prebuilt plans and real storage overrides remain supported. No new
  configuration format, implicit base selection, or acceptance authority was
  introduced. See the [architecture decision](history/2026-09-11-local-experiment-setup-architecture.md)
  and [independent review](history/2026-09-11-local-experiment-setup-review.md).

  The focused and isolated installed-wheel gate passed 131 checks, including
  capture then later processing, equivalent explicit settings, dependency-order
  normalization, before-write refusal, source coordinates, and saved recovery.
  The regression suite then passed 1,089 tests, with one live integration test
  deselected and one example-import warning. Ruff and diff checks passed.
  D04's catalog/export convenience and D38's complete exercise remain separate.

<a id="d04"></a>

- [ ] **D04 · P0 · Expose the lifecycle through a supported application API.**
  Connect existing services behind clear operations for catalog construction,
  capture, processing retained inputs, resume, inspection, and export. Compose
  the CLI and executors through that same supported path. **Done when:** a caller
  can fetch and process together, fetch first and process later, or work with a
  catalog alone without manually assembling every internal service. Avoid a
  wrapper that only renames calls without simplifying their use. Depends on D02–D03.

  **Agreed approach:** move the existing CLI composition into an outer
  `docspec.runtime` package that accepts typed plans, a workspace, policies, and
  implementations. The CLI and Python callers use that one path. Keep core
  application services independent of concrete adapters; do not add another
  workspace model, run ledger, or plugin loader. A solutions architect approved
  this direction. Capture-only completion was the next implementation step;
  moving composition alone did not complete D04.

  **Runtime progress:** `prepare_local_run` and `PreparedLocalRun` now provide
  preparation, local execution, task dispatch, reconciliation, and saved-handoff
  recovery. CLI execution and the offline example use this same path. The
  superseded CLI-local composition/execution modules were removed. Existing
  plans, ledgers, handoffs, and receipts remain the saved state. Independent
  architecture and code reviews approved this bounded extraction.

  **Capture-first progress:** `stage_policy(stop_after=...)` now requests a
  contiguous prefix; `PreparedLocalRun.retain()` keeps its result through the
  existing finalization path. A later plan reuses verified captures or
  representations and runs the remaining stages. The superseded processor-only
  execution loop was removed; one checkpointed loop serves all prefixes.
  Export convenience and simpler catalog/plan composition remain open; see the
  [architecture decision](history/2026-09-11-capture-prefix-architecture.md).

  **Inspection progress:** `open_local_inspection` opens current saved work,
  exact reconciled runs, or retained results using existing storage. The CLI
  uses the same application view. It provides summary, per-source evidence,
  streamed records/bytes, and comparisons without reconstructing execution
  plugins. D19 records the delivered acceptance and validation.

<a id="d05"></a>

- [ ] **D05 · P0 · Establish a small reference experiment.** Extend the offline
  walkthrough with several documents, a selected exclusion, a recoverable failure,
  and a useful processor. Build it through D04 as a running example while the
  remaining items land. **Done when:** the installed package demonstrates initial
  capture and a later processing attempt, with understandable output and explicit
  remaining limitations. D38 supplies the complete acceptance exercise.

## 2. Build catalogs and acquire inputs through clear interfaces

<a id="d06"></a>

- [ ] **D06 · P0 · Support provider inputs and caller-supplied records.** Make the
  source interface usable for SpicyDocs, SpicyRegs-derived data, another source,
  and bounded local records. Define the required source identity and
  provenance for supplied records without inventing acquisition evidence.
  **Done when:** one provider example and one local-record example build catalogs
  through public interfaces without private imports or sibling checkouts. Reuse
  [source ports](../src/docspec/ports/source_catalog.py). [D51](#d51) and
  [D52](#d52) own the GAO-topic and retained public-comment-table examples; they extend
  this interface without requiring a provider package move or blocking the
  initial local example.

<a id="d07"></a>

- [ ] **D07 · P0 · Make catalog iteration and selection inspectable.** Expose
  previewable additions, changes, exclusions, selected candidates, and reasons
  before acquisition. Decide how a dataset accepts successive or multiple source
  inputs, including source-qualified identity and collisions. **Done when:** an
  operator can explain a selection change and grow a dataset without silently
  conflating records or refetching unchanged selections. Depends on D02 and D06;
  use the existing catalog policy and succession machinery.

<a id="d08"></a>

- [ ] **D08 · P1 · Carry source collection outcomes into the dataset.** Consume
  the provider's public outcomes and retain them in DocSpec's source description.
  DocSpec owns the explicit policy for accepting partial input. **Done when:**
  empty observations, rejected records, unresolved collection, and successful
  input remain distinguishable through ordinary DocSpec APIs. Preserve count
  units and source evidence; upstream failures remain distinct from selected
  document-processing failures. Provider implementation lives in
  [SpicyDocs S01](../../spicy-docs/docs/simplification-todo.md#s01) and
  [S09](../../spicy-docs/docs/simplification-todo.md#s09).

<a id="d09"></a>

- [x] **D09 · P1 · Add public catalog admission and bounded row access.** Expose
  an admitted, pinned catalog through supported object and validated mapping
  access as needed by current consumers. Keep full producer re-derivation separate
  from ordinary opening and bind access to the artifact actually checked.
  **Done when:** the SpicySearch catalog integration can use public APIs without
  private row readers or repeated full derivation, while changed or invalid
  artifacts are refused. See [the facade](../src/docspec/source_catalog.py) and
  [the recorded consumer request](history/2026-09-05-reader-api-requests.md).
  **Completed September 11:** `admit_snapshot` and
  `open_admitted_source_catalog` expose repeatable object/mapping access without
  repeated generic admission or full producer derivation. Tests bind consumed
  bytes to admitted identities, preserve mapping/object refusal parity, and
  prove bounded reads and early closure (107 catalog tests passed). Independent
  review approved the implementation; [the guide](catalog-evidence.md) states
  admission-time membership and temporary-disk limits. SpicySearch adoption and
  its installed-wheel qualification remain in [SC01](../../spicysearch/PLAN.md#sc01);
  these local checks do not establish a capacity or end-to-end speed claim.
  Commit `45883d7`.

<a id="d10"></a>

- [ ] **D10 · P1 · Qualify the current installed source integration.** Coordinate
  source schema, policy, receipt fields, and producer-label acceptance with the
  actual SpicyDocs release being tested. Replace the old integration wheel pin
  deliberately and retire historical acceptance branches where unnecessary.
  **Done when:** an isolated installed-package check proves success, partial-input
  handling, and clear refusal of unsupported inputs against the chosen current
  producer. No automatic migration layer or SpicyRegs code move is required.
  Depends on D08; coordinates with SpicyDocs S05.

  **Current producer progress, September 11:** the adapter now selects the public
  `CURRENT_PRODUCER_PRODUCT = "spicy-docs"` reader, which owns current source
  format/policy admission. The exact SpicyDocs `0.2.0` wheel from `296f20d` and
  Rulespec Artifacts `1.0.12` are pinned in the installed-package test. Ordinary
  installation resolves both packages' dependencies; installed bytes match the
  provider wheel. The isolated test publishes Federal Register and Regulations.gov
  document/docket/comment releases, builds and reuses catalogs, and verifies them
  in a separate environment without SpicyDocs. All 41 focused integration and
  package checks passed. D10 remains open for D08's partial-input handling;
  updating the shared wheel does not complete D28's encoder or D31's writer
  adoption. Receiver qualification and upstream publication remain distinct.

<a id="d11"></a>

- [ ] **D11 · P0 · Make fetcher injection practical.** Expose explicit selection
  and composition of local, HTTPS, S3, and caller-provided fetchers through the
  supported run API. Keep credentials outside retained configuration and isolate
  optional dependencies. **Done when:** the reference experiment swaps fetchers
  without editing application internals; routing errors are clear; fetched bytes,
  source identity, limits, and outcomes are retained. See
  [existing fetchers](../src/docspec/adapters/content_fetchers/).

  **Runtime progress:** callers can supply a fetcher object through the supported
  Python API. Its declared identity is pinned during preparation and checked
  again before fetching and recovery. Acquisition metadata must match the
  configured fetcher and requested task before any chunks are read; refusals
  close the stream. The installed example proves custom injection and reuse.
  Convenient HTTPS/S3 composition and route qualification remain open.

<a id="d12"></a>

- [ ] **D12 · P1 · Connect source-specific body validation when a route needs it.**
  For a selected Federal Register/GovInfo route, assess and reuse SpicyDocs'
  existing identity and soft-404 checks alongside bounded transport and capture.
  DocSpec still chooses the candidate. **Done when:** a demonstrated route accepts
  the intended document and explains wrong-document or placeholder refusals while
  retaining needed evidence. If no current experiment needs this route, record
  the deferral. Do not move source code into SpicyRegs to complete this item.
  Depends on D11; coordinates with SpicyDocs S19.

## 3. Make processing repeatable, replaceable, and useful

<a id="d13"></a>

- [x] **D13 · P0 · Expose processor, extractor, and segmenter injection.** Provide
  explicit composition of chosen implementations through D04, with convenient
  defaults. Validate that the declared plan and actual implementations match.
  **Done when:** a contributor runs a custom processor and replaces extraction or
  segmentation without editing the CLI's internals. Preserve dependency ordering,
  result validation, and optional imports. An unrestricted dynamic plugin loader
  is not needed to satisfy this outcome. See [extensions](extensions.md).

  **Completed September 11:** `stage_policy` derives the existing plan value
  from selected objects; `prepare_local_run` injects processors, extractors, and
  segmenters through the supported API. Stage IDs and effective settings affect
  plan identity and reuse decisions. Registries retain their selected children's
  output identities, including empty segmentation. Configuration changes refuse
  stale recovery before work; at this milestone a newly pinned plan used full
  repair. D15 below records the subsequent prefix-reuse implementation.
  PDF versions are pinned without importing the optional parser during setup.
  The [architecture decision](history/2026-09-11-stage-injection-architecture.md)
  records the chosen interface and rejected alternatives; the
  [independent review](history/2026-09-11-stage-injection-review.md) records its
  assessment separately from execution evidence. The full local suite passed
  972 tests, including custom-stage execution/recovery through an isolated wheel;
  one live integration test was deselected. Ruff passed. Plans, stores, and
  ordinary segmentation receipts used format `2.0` at this milestone. The
  capture-first revision advances plans/stores to `3.0` and dispositions to
  schema `2.0`; ordinary segmentation receipts remain `2.0`. No legacy reader
  is provided.

<a id="d14"></a>

- [ ] **D14 · P0 · Demonstrate a meaningful optional processor.** Add a small
  example that produces inspectable results beyond byte/word statistics. A
  processor using pinned RefSpec resources is a suitable candidate; choose the
  actual use and resource before committing to an integration. **Done when:**
  users can inspect results and supporting evidence, change configuration or the
  resource pin, and run a new attempt. Domain interpretation stays in the
  processor; local fixtures are clearly distinguished from live-resource proof.
  Depends on D13.

<a id="d15"></a>

- [x] **D15 · P0 · Prove selective reuse across experiments.** Trace which source
  selections, captured bytes, extraction settings, segmentation settings,
  processor versions/configuration, resource pins, prerequisites, and applicable
  policy affect each result. Simplify duplicate identity bookkeeping where
  possible. **Done when:** changing one processor reruns it and affected
  dependents; adding a processor reuses retained inputs; changing extraction
  reuses valid captures; changing segmentation reuses the valid representation.
  Rerun affected downstream work and verify unchanged work before reuse. Compare
  each route with a clean rebuild; the existing processor-only route does not
  establish extraction/segmentation-change reuse.
  Depends on D02 and D13; build on
  [base reprocessing](../src/docspec/application/base_reprocessing.py).
  **Progress, September 11:** D02's real processing test proves two processor
  alternatives share captures, representations, and segments. Reconciliation now
  retains verified base blob roots for a zero-work stateful result, while a
  stateless result keeps no inherited roots/layers. These cases do not establish
  changed-extractor or changed-segmenter reuse, which remains open.

  **Completed September 11:** changed extraction reuses captures, changed
  segmentation reuses representations, and processor changes reuse segments and
  unaffected results. Every entry keeps its full requested stages and separately
  names processors that need to run. The planner reads each inherited document's
  policy, so mixed-stage results do not inherit the latest plan's stage choices
  accidentally. Selection chooses items without invalidating unchanged inputs.
  Other governing policy changes and previously failed items still use full
  repair. See [the recorded approach](history/2026-09-11-capture-prefix-architecture.md).
  Changed extraction, segmentation, and added/replaced processors now compare
  against clean active-document state. Exact capture provenance remains distinct
  from newly acquired evidence. The [independent review and executed checks](history/2026-09-11-capture-prefix-review.md)
  support this acceptance: 1,025 full-suite passes, two stale test-metadata
  failures corrected, then all 15 focused follow-up checks passed. The full
  suite deselected one live integration test; failed-item repair remains D16.

<a id="d16"></a>

- [ ] **D16 · P0 · Repair only the work that needs another attempt.** Carry final
  failure classification and completed-stage evidence into planning. Retry
  temporary failures under explicit policy; retry unchanged deterministic
  failures when selected or their relevant inputs change. **Done when:** a failed
  processor can be repaired without refetching valid content, repeated permanent
  failures do not loop on every unchanged successor, and retained failures remain
  visible. See [planner](../src/docspec/application/planner.py); depends on D15.

<a id="d17"></a>

- [x] **D17 · P0 · Make extracted representations an explicit choice.** Connect
  existing markup, visible-text, PDF, and segmentation capabilities to the normal
  workflow with clear supported defaults. Preserve exact captures independently
  of derived text. **Done when:** an experiment can choose a supported
  representation and inspect its source coordinates; HTML/XML markup retention,
  PDF extraction limits, and image handling are accurately described. Neither
  OCR nor every format must be implemented to close this item. Depends on D13.

  **Completed September 11:** the [representation guide](representations.md)
  and [running example](../examples/representation_choices.py) connect HTML/XML
  visible text to the normal runtime using the existing parsers. Derived whole
  blocks retain honest enclosing source ranges; captures keep their exact bytes.
  Immutable settings pin the chosen parser and mapping behavior. The guide
  explains default markup retention, optional PDF extraction, image handling,
  encoding, block-size limits, and the absence of OCR/browser rendering.
  The solutions architect approved reusing existing parsers and whole-block
  evidence instead of introducing new coordinate semantics. The
  [independent review](history/2026-09-11-representation-choice-review.md)
  approved the final implementation. D03's 131-check local gate includes the
  installed visible-text lifecycle and evidence checks; these bounded fixtures
  do not establish semantic completeness or corpus-scale performance.

<a id="d18"></a>

- [ ] **D18 · P1 · Replace weak quality proxies with justified checks.** Review
  portable retention floors and other text-quality gates against concrete bad
  extractions. Separate observed extraction quality from the user's policy for
  continuing an experiment or exporting results. **Done when:** useful checks
  distinguish empty, truncated, and misleading extraction for supported formats;
  removed thresholds have a recorded rationale and replacement evidence. Preserve
  failure accounting; reduced validation must not silently imply completeness.
  Depends on D17 and informs D26.

<a id="d19"></a>

- [x] **D19 · P0 · Let users inspect and compare attempts.** Provide bounded
  summaries and result access for selection, capture, extraction, processor
  outputs, failures, reuse, and relevant costs. Compare attempts by stable input
  identity and effective configuration; expose detailed evidence on demand.
  **Done when:** users can answer what changed, what reran, what failed, and why
  two results differ without reading internal files. A useful CLI/API is enough;
  a dashboard is optional. Depends on D02 and D15; incorporate D08's source
  outcomes when available and label unavailable source facts explicitly.

  **Completed September 11:** the supported [inspection API and commands](inspection.md)
  separate admitted source coverage, scheduled work, and complete active results.
  They expose capture/extraction/segmentation progress, derived output, failures,
  verified reuse, recorded processor calls and elapsed time, and result-reported
  resource use by origin. Unsupported selection totals, money, token counts,
  lost work, and total run duration remain explicitly unavailable. Source coverage
  requires independent producer acceptance.

  Comparisons join stable source IDs and separate input, configuration, content
  and recorded input associations, outcomes, and exact provenance. Per-source
  evidence explains mixed-stage inherited results. Exact run references use
  pinned revisions; rejected/stateless runs remain incomplete; complete result
  claims require existing logical checks. No new dataset ledger or plugin loader
  was added. The [architecture decision](history/2026-09-11-inspection-architecture.md)
  records the choices and the [independent review](history/2026-09-11-inspection-review.md)
  records their assessment.

  Local validation passed 1,059 full-suite tests, with one live integration test
  deselected. A final partition-setting check and adversarial regression then
  passed all 28 focused checks; Ruff and diff checks passed. The installed wheel
  probe now uses this API to inspect and compare capture-first/later-processing
  results without internal storage assembly. Storage opening and hard read
  bounds were committed separately as `6521e69`. This proves the bounded fixture
  workflows, not large-corpus qualification, remote CI, or publication.

## 4. Keep execution reliable while reducing repeated machinery

<a id="d20"></a>

- [ ] **D20 · P0 · Prove pause, interruption, and resume for the new workflow.**
  Exercise capture, extraction, processing, delivery, and final state updates.
  Reuse checkpoints after verification and restore cumulative budgets.
  **Done when:** interrupted work resumes without accepting partial output,
  double-counting work, or repeating verified stages; cancellation actually
  reaches active work where the backend promises it. Reuse the existing recovery
  tests and add only missing cases introduced by D04.

  **Progress:** local preparation and saved-handoff loading now derive one
  worker description from the actual fetcher, roots, policies, accepted
  producers, sink, partition settings, and evidence timestamp. Changed settings
  refuse recovery before fetching; unchanged settings reuse completed work.
  Fifteen new identity cases and 61 related workspace, CLI, acquisition, and
  Dagster checks pass. This closes the existing local reconstruction gap;
  the full D04 interruption workflow remains open. Configurable stages are
  tracked in D13. Commit `c41b00b`.

  **Runtime progress:** public task execution now admits the exact initial
  reference from the verified planned-store ledger before loading or executing
  work. A bounded disposable lookup avoids rescanning the ledger for every task
  in one prepared worker. Real running/sealed revision tests prove only
  unfinished work executes. Changed deadlines/limits and unsupported saved
  operations refuse recovery; admission rechecks the deadline before execution.
  Zero-task runs need no lookup, and failure/context exit releases scratch.
  Active cancellation and aggregate concurrent scratch accounting remain open.

  **Capture-prefix progress:** multi-candidate capture recovery and recovery
  after new extraction/segmentation of reused inputs now use the same verifier
  and execution loop. Cumulative page/frame costs come from verified extraction
  receipts rather than being inferred from output boundaries. Reused prefix
  work is excluded from new-work counters; resumed new work charges once.

  D13 adds actual stage configuration checks before direct execution,
  checkpoint admission, completed-task reuse, and zero-task execution. Tests
  refuse changed live settings and mismatched selected-child evidence. These
  checks preserve the existing checkpoint and cumulative-budget tests; they do
  not complete active cancellation or the whole interruption exercise.

<a id="d21"></a>

- [ ] **D21 · P1 · Complete the Dagster composition of the same workflow.** Keep
  Dagster optional and reuse the same plans, injected components, execution
  services, and result checks. **Done when:** a documented installed example
  demonstrates dispatch, interruption/retry, and result reconciliation with the
  same logical results as local execution, including processing retained inputs.
  Injected resources reconstruct in workers; locking, stage completion, active
  cancellation, and source-refusal meaning survive dispatch.
  State backend-specific limits; avoid a second dataset state model. Depends on
  D04, D13, and D20; see [the adapter](../src/docspec/adapters/dagster.py).

<a id="d22"></a>

- [ ] **D22 · P2 · Qualify DocSpec as the selected experiment campaign executor.**
  Adapt DocSpec's task model only as needed to prove one supplied acquisition
  task through its existing executor. **Done when:** the local integration
  preserves source ordering, subprocess cancellation, locks, bounded resources,
  pinned references, and stale-resume refusal. If it cannot simplify the selected
  workflow, record the unmet need and defer replacement. Source-side caller
  changes and runner deletion belong to
  [SpicyDocs S21](../../spicy-docs/docs/simplification-todo.md#s21), after this
  acceptance evidence exists. Independent source publishing stays usable.
  Depends on D20–D21; no SpicyRegs move is required.

<a id="d23"></a>

- [ ] **D23 · P1 · Assign retries and outcome accounting to explicit owners.**
  Review nested transport, processor, and scheduler retry loops; give each layer
  a bounded purpose under one effective run policy. **Done when:** repeated
  attempts consume the expected budget and produce attributable outcomes.
  Acquisition observations, selected documents, scheduled tasks, and exported
  text retain their distinct counts and meanings. Share common mechanics where
  useful without forcing every failure into one universal ledger. Depends on
  D08, D16, and D20; informs D22.

<a id="d24"></a>

- [x] **D24 · P0 · Simplify durable state updates and finalization.** Trace which
  current commit, reconciliation, and publication steps are needed at each useful
  stopping point. Reuse existing verified transitions behind D04. **Done when:**
  a usable capture or processing attempt can be retained without mandatory
  portable export; incomplete state is recognizable; immutable writes and
  concurrent base checks prevent silent replacement of another result. Depends
  on D02 and D20; see [commit](../src/docspec/application/commit.py).

  **Completed September 11:** capture and processing results use the existing
  reconciliation, immutable retention, and explicit selection operations.
  Completion checks only the requested prefix. Each disposition records that
  request; inherited processor records must match their own document's request.
  Shortening selected documents removes their superseded descendants while
  preserving other documents, including across later generations. Requested
  processors retain an empty layer when no records are produced. No separate
  capture ledger, export requirement, or publication step was added.
  The [review and execution evidence](history/2026-09-11-capture-prefix-review.md)
  cover incomplete-request refusal, mixed-result preservation, guarded selection,
  installed capture/retain/later-process/retain, and recovery without refetching.
  D20's active cancellation remains separate unfinished work.

<a id="d25"></a>

- [ ] **D25 · P1 · Make retention safe for shared experiment inputs.** Recheck
  maintenance and deletion reachability against multiple attempts and shared
  bases. Expose a preview of what would be retained or removed through existing
  maintenance mechanisms. **Done when:** keeping an attempt preserves all inputs
  and evidence it needs; pruning an obsolete attempt cannot damage another;
  interrupted cleanup is recoverable. This item does not authorize deleting
  existing user datasets. Depends on D02 and D24; see
  [maintenance](../src/docspec/application/maintenance.py).

## 5. Export and consume results without a second production pipeline

<a id="d26"></a>

- [ ] **D26 · P1 · Build optional exports from retained experiment results.**
  Replace the campaign-specific route for normal use with a maintained export
  operation that reads verified retained state. Give internal experiment state
  and portable output distinct names and unambiguous format identities.
  **Done when:** export applies an explicit admission policy, preserves required
  refusals and complete accounting, and reuses valid retained results. An
  experiment may retain failures that prevent its admission as a consumer
  dataset. Repeat or interrupted export has explicit behavior. Export remains
  an optional stage.
  Depends on D17–D18 and D24.

<a id="d27"></a>

- [ ] **D27 · P1 · Adopt the shared generic artifact implementation.** Compare
  DocSpec's portable builder/verifier needs with Rulespec's existing container.
  Delegate generic membership, byte integrity, and structural checks when they
  meet the required behavior; retain document semantics and coverage checks here.
  **Done when:** D26 uses one supported container path, DocSpec's replaced
  structural machinery is removed, and any retained difference has a concrete
  reason. Needed shared-library changes are owned by
  [Rulespec RS02](../../rulespec/TODO.md#rs02); consumer changes are owned by
  [SpicySearch SC01](../../spicysearch/PLAN.md#sc01). Depends on D26's design and
  D28; see [artifact adapter](../src/docspec/adapters/platform_artifact.py).

<a id="d28"></a>

- [ ] **D28 · P1 · Adopt the agreed shared canonical encoder in DocSpec.**
  Supply required source/processor value cases to the encoding decision in
  [Rulespec RS01](../../rulespec/TODO.md#rs01), including arbitrary integers and
  Unicode key ordering. Retain DocSpec's useful domain conversion and validation
  context. **Done when:** DocSpec uses the selected shared emitter, required
  values survive exactly, shared boundary/duplicate-key/domain-conversion
  fixtures pass, and current identities/schema pins match the chosen rules.
  Remove the replaced emitter without a compatibility mode; never round values
  or indiscriminately stringify them. If required value domains prevent shared
  emission, record the evidence and defer adoption. Rulespec owns the common
  implementation; [SpicyDocs S30](../../spicy-docs/docs/simplification-todo.md#s30)
  owns source-producer adoption. See [identity](../src/docspec/domain/identity.py).

<a id="d29"></a>

- [ ] **D29 · P1 · Provide public document-result admission and reading.** Expose
  a supported, bounded reader for D26's exports. Return verifiable identity
  information instead of requiring manual transcription of a verification claim.
  **Done when:** installed DocSpec APIs admit and read a fresh exported dataset,
  refuse tampering, and resolve evidence to retained inputs. Publish the consumer
  example and exact wheel interface needed by
  [SpicySearch SC01](../../spicysearch/PLAN.md#sc01), where Search's adoption and
  private-import removal are tracked. No legacy import shim is required.
  Depends on D26–D28.

<a id="d30"></a>

- [ ] **D30 · P2 · Retire tools only after their current purpose is replaced.**
  Reassess the historical release builder, sample tools, and fixture restamper
  after D26. Integrate reusable production behavior into its package owner;
  retain necessary research or reproduction tools with explicit scope.
  **Done when:** the [tool inventory](../tools/README.md) identifies one supported
  route per task and genuinely superseded implementations are removed with
  provenance preserved in Git. A normal export does not automatically replace
  a historical reproduction recipe or fixture generator.

## 6. Simplify code ownership and contributor effort

<a id="d31"></a>

- [ ] **D31 · P2 · Remove DocSpec's side of demonstrated shared-code duplication.**
  Recheck DocSpec's CourtListener listing grammar, blob writes, publication
  helpers, and compiled schema-gate mechanics against their actual callers.
  Directory publication already shares a Rulespec helper. Blob writers differ
  on known/computed digests, reuse, bounds, and returned evidence. **Done when:**
  DocSpec uses the selected shared implementations and removes its replaced
  code, or records a justified deferral. Keep dataset capture, transactions,
  product schemas, and error meaning here. Shared physical storage must serve
  independent provider callers without depending on DocSpec's lifecycle.
  Destination work lives in [SpicyDocs S14](../../spicy-docs/docs/simplification-todo.md#s14)
  and [S22](../../spicy-docs/docs/simplification-todo.md#s22),
  [Rulespec RS02–RS03](../../rulespec/TODO.md#rs02), and
  [SpicySearch SC02](../../spicysearch/PLAN.md#sc02). D47 links the separate
  search-definition work. Avoid creating a package solely for a future move.

<a id="d32"></a>

- [ ] **D32 · P1 · Remove configuration that repeats facts or promises no enforcement.**
  Review role/profile declarations, governance identifiers, provider metadata,
  and optional cache settings after D03 and D13. Derive facts from selected
  implementations where possible; keep output-affecting identities and enforced
  limits explicit. **Done when:** users supply only meaningful choices,
  declarations match actual runtime behavior, and each retained abstraction
  serves the experiment workflow. Lack of today's CLI caller alone does not
  make processor dependencies or resource pins dead code.

<a id="d33"></a>

- [ ] **D33 · P2 · Review current file and function outliers by responsibility.**
  Reassess catalog CLI coordination, checkpoint verification, base reprocessing,
  delivery indexing, and execution control flow after workflow changes settle.
  Review large schemas and segmentation algorithms separately. **Done when:**
  long functions expose understandable steps, shared rules have clear owners,
  and any split reduces the context needed for a change. Follow the existing
  length review guidelines; avoid chains of tiny wrappers or shared-state mixins.

<a id="d34"></a>

- [ ] **D34 · P2 · Remove superseded paths, declarations, and dependencies.**
  Search source, tools, tests, exports, registrations, profile strings, and known
  installed consumers after each replacement. Recheck obsolete source-policy
  declarations with the SpicyDocs owner: tagging eligibility and processor
  selection are downstream choices, not publisher facts. **Done when:** each
  removed path has a named replacement or an explicit retirement decision,
  unused dependencies leave packaging, and dynamic hooks remain functional.
  Move active experiment choices into DocSpec only where they have a local consumer.
  Source-side declaration removal is [SpicyDocs S12](../../spicy-docs/docs/simplification-todo.md#s12);
  this task removes only DocSpec code and dependencies.

<a id="d35"></a>

- [ ] **D35 · P2 · Simplify tests around observable behavior.** Reuse the prior
  refactor's focused suites and support helpers. Retire assertions for deliberately
  removed behavior; reduce redundant tests that only mirror implementation.
  **Done when:** meaningful checks cover evidence, reuse, failures, bounded work,
  and public interfaces without making internal moves expensive. Keep selectors
  and installed-package probes current. Do not add tests solely to justify small,
  reversible file moves or deletions.

<a id="d36"></a>

- [ ] **D36 · P1 · Keep one contributor path through the new workflow.** Update
  the walkthrough, extension guide, task-to-code map, operations guide, schema
  instructions, and decision index as their corresponding changes land.
  **Done when:** a contributor can add a source adapter, fetcher, or processor;
  run a later attempt; inspect results; and find the relevant checks using
  maintained documentation. Retire superseded instructions and keep historical
  measurements labeled with their revisions. Depends on the interfaces above.

## 7. Verify user value and qualify the claims we keep

<a id="d37"></a>

- [ ] **D37 · P1 · Separate regression gates from capacity and conformance claims.**
  Map the nine previously partial qualification requirements to the clarified
  product scope. Keep and implement useful requirements; explicitly revise or
  retire unjustified ones. Choose representative workload sizes instead of
  treating the entire 100k/1m/5m ladder as an automatic prerequisite for every
  contribution. **Done when:** ordinary checks are clear, retained capacity
  claims have pinned resource/recovery evidence, and missing qualification stays
  visible. Changing the scope cannot be reported as passing an unrun check.
  See [the recorded conformance report](history/2026-09-11-maintainability-conformance.json).

  **Inspection follow-up:** D19's fixture checks establish read behavior and
  bounded samples/joins, not capacity at corpus scale. Full retained admission
  still uses the existing `DocumentReleaseVerifier.verify` set of distinct blob
  identities, which grows with the result. Measure that memory and verification
  cost when selecting representative capacity claims; replace the set with an
  appropriately bounded implementation if the supported workload requires it.

<a id="d38"></a>

- [ ] **D38 · P0 · Prove the complete experiment loop through installed packages.**
  Starting from the reference experiment, build a provider catalog, fetch once,
  run a meaningful processor, change its configuration or resource pin, and
  process retained inputs again. Add input, target failed work, interrupt/resume,
  and compare with a clean rebuild. **Done when:** observed calls prove selective
  reuse, results and gaps are inspectable, and ordinary use needs no private
  imports, ad hoc file editing, or sibling checkout. Keep export/Dagster checks
  in D29/D21 so they do not block useful local experiments. Depends on D05–D07,
  D11, D13–D17, D19–D20, and D24.

<a id="d39"></a>

- [ ] **D39 · P1 · Obtain independent architecture and code review of the implementation.**
  Give reviewers the clarified purpose, changed interfaces, and acceptance
  evidence. Use a solutions architect for judgment and independent semi-formal
  code reviews for implemented changes. **Done when:** findings on ownership,
  unnecessary complexity, correctness, and evidence gaps are resolved or
  explicitly recorded, and the final approach distinguishes consensus from open
  disagreement. Review throughout implementation, then consolidate the result.

  **Progress:** the [foundation review](history/2026-09-11-experiment-foundation-review.md)
  records independent approval of the bounded documentation, profiles,
  workspace, retained alternatives, catalog-reader, and recovery changes through
  `c41b00b`. Review findings were resolved. The combined local suite passed
  907 tests, with one live integration test deselected; Ruff passed. Subsequent
  API and lifecycle work still requires independent review and acceptance.

  The [public runtime review](history/2026-09-11-public-runtime-review.md) records
  independent approval of the next extraction, injected dependency checks, exact
  task membership, and installed-caller coverage. Its execution record separates
  942 passing full-suite tests from one stale-selector failure, then records all
  25 focused checks passing after that selector mapping was corrected. Ruff and
  diff checks pass. This does not complete whole-workflow acceptance or D39.

  The [stage-injection review](history/2026-09-11-stage-injection-review.md)
  covers D13's configured stages and current producer migrations. The complete
  local suite passed 972 tests with one live integration test deselected,
  including the updated installed-wheel probe. Its architecture decision and
  review record the full-rebuild behavior at that milestone. The subsequent
  capture-prefix work extends D15 reuse; its separate review and execution
  evidence appear in the [capture-prefix review](history/2026-09-11-capture-prefix-review.md).
  That review approved D15 and D24 after resolving inherited-output ownership,
  unused-cache construction, receipt-based accounting, and the missing
  processor-addition clean comparison. Local checks are recorded there;
  broader D39 acceptance remains open.

<a id="d40"></a>

- [ ] **D40 · P1 · Have an unfamiliar human complete a contribution.** Carry
  forward E5 from the earlier checklist. Ask a human to follow the experiment
  walkthrough and make one small source/fetcher/processor change using the guides.
  **Done when:** record where they got stuck, the context and steps required,
  improve the confusing parts, and verify the improvement with them. A blind
  agent review is useful evidence but does not complete this human exercise.
  Depends on D36 and D38.

## 8. Reuse source-provider code through installed wheels

This section identifies code that belongs with the source provider and that
DocSpec should consume as a package dependency. **SpicyDocs versus SpicyRegs is
an open packaging decision.** Retaining SpicyDocs separately is a valid outcome.
Complete useful integration against the current provider; move responsibilities
between repositories later only when that helps. The proposed API capabilities
below are not a claim that today's SpicyRegs wheel already supplies them.

Prioritize repeated source parsing/validation and reader integration first.
Assess campaign execution and blob storage against the differing behaviors
recorded in D22/D31 before sharing them. Reuse producer fixtures and evidence for
package integration while retaining DocSpec's own catalog/experiment assertions.
Success means fewer implementations and fewer places to update the same rule;
renaming packages or moving unchanged duplication does not achieve it.

Wheel reuse works in both directions at the application boundary. The independent
provider package uses shared primitives and remains usable without DocSpec. An
optional DocSpec integration or a separate experiment caller uses both packages'
public APIs and injects source operations and fetchers. That caller can replace
dataset-specific source-tool loops through DocSpec's wheel (SpicyDocs S31).
Check declared package dependencies as well as imports before choosing where the
caller lives; moving imports between modules does not remove a package cycle.

| Responsibility | Intended owner and how DocSpec uses it |
| --- | --- |
| Publisher clients, enumeration, pagination, source record identity, literal field meaning, and discovery coverage | Source provider. DocSpec calls public source operations or reads their retained output. Current implementations include SpicyDocs `sources/` and SpicyRegs `sources/`; assess overlap before choosing one. |
| Federal Register/GovInfo URL derivation, printed-document identity, MODS resolution, and soft-404 checks | Source provider. Reuse the existing SpicyDocs `sources/federal_register/body_sources.py` through a wheel when the selected route needs it. DocSpec owns candidate preference and selection. |
| CourtListener bulk-listing grammar, object metadata, and pagination completeness | Source provider. Consolidate the relevant parts of DocSpec's `tools/courtlistener_bulk_source.py` with the source implementations. DocSpec retains experiment selection and checks against its captured input pins. |
| Source-native schemas, collection outcomes, faithful source publication, source-specific replay, and bounded public reading | Source provider, using Rulespec's generic artifact machinery. DocSpec consumes verified records, renditions, scope, and outcome evidence through the wheel. |
| Source-field descriptions and native vocabulary relationships | Source provider where they describe publisher facts. Tagging eligibility, processor choice, and inferred meaning belong to their downstream consumer. |
| Dataset catalogs, normalization/selection policy, retained attempts, generic extraction/segmentation, processor composition, reuse, comparisons, and run budgets | DocSpec. Source-aware policies may call provider helpers for publisher facts; do not move the whole catalog policy merely because it names a source. |
| Generic artifact membership, byte checks, and existing atomic publication primitives | Rulespec. A provider wheel should reuse this owner rather than become another generic artifact library. |
| Governed reference resources and their domain interpretation | RefSpec supplies resources; the chosen processor owns its interpretation. They need not become source-provider features. |

<a id="d41"></a>

- [ ] **D41 · P1 · Inventory DocSpec's reusable source dependencies.** Name
  DocSpec callers, exact symbols/files, required public capabilities, and the
  copies or repeated maintenance steps that sharing would remove. **Done when:**
  each local candidate has a selected provider dependency and removal target,
  or a documented reason to stay separate. Package placement can remain open.
  Connect the inventory to D12/D31/D34. Provider inventory and handoffs live in
  [SpicyDocs S25](../../spicy-docs/docs/simplification-todo.md#s25) and
  [SpicyRegs SR01](../../spicy-regs/PLAN.md#sr01); recording them here is not their
  implementation task.

<a id="d42"></a>

- [ ] **D42 · P2 · Replace DocSpec's copied publisher rules with provider APIs.**
  Separate publisher identity, enumeration, literal fields, and listing grammar
  from DocSpec's dataset selection and normalization policy. **Done when:** local
  callers use the chosen installed provider API, exact values/evidence and
  pagination/refusal behavior survive, and superseded local rules are removed.
  Source implementation belongs to [SpicyDocs S14](../../spicy-docs/docs/simplification-todo.md#s14),
  [S25–S26](../../spicy-docs/docs/simplification-todo.md#s25), or selected
  [SpicyRegs SR03](../../spicy-regs/PLAN.md#sr03) handoffs. A provider move remains
  optional. Depends on D41.

<a id="d43"></a>

- [ ] **D43 · P1 · Consume public source releases and outcomes.** Adapt DocSpec
  to supported provider access for profiles, pinned descriptions, records,
  renditions, outcomes, and bounded evidence/failure inspection. **Done when:**
  D06/D08/D10 use these APIs without implementation imports or internal ledgers.
  Keep ordinary opening bounded and full source replay with its producer.
  Provider API work is tracked in [SpicyDocs S01](../../spicy-docs/docs/simplification-todo.md#s01)
  and [S26](../../spicy-docs/docs/simplification-todo.md#s26); retained public-table
  facts/API work is in [SpicyRegs SR02](../../spicy-regs/PLAN.md#sr02). Reuse the
  current public reader instead of inventing another solely to rename a package.

<a id="d44"></a>

- [ ] **D44 · P2 · Adapt provider acquisition to DocSpec fetcher injection.**
  Where an experiment needs a publisher-specific route, connect the installed
  provider's locators, identity checks, and bounded acquisition to DocSpec's
  fetcher interface. **Done when:** D11/D12 use a thin adapter without copied
  source rules, circular imports, or another HTTP/storage framework. Keep
  selection, effective budgets, and experiment state here. Provider wheel and
  route implementation lives in [SpicyDocs S19](../../spicy-docs/docs/simplification-todo.md#s19).
  Depends on D41.

<a id="d45"></a>

- [ ] **D45 · P1 · Package DocSpec's public APIs and optional provider integration.**
  Publish the selected D04/D09/D11/D13/D19/D21/D29 APIs through DocSpec's wheel;
  record its version, source revision, and exact wheel digest. Declare the
  optional provider integration without a circular package dependency. **Done
  when:** a clean install exposes the selected APIs without sibling paths or
  undeclared extras; core DocSpec works without a provider; and the optional
  integration installs the pinned provider through supported imports. Keep code
  identity separate from source-data pins. Provider packaging lives in
  [SpicyDocs S26](../../spicy-docs/docs/simplification-todo.md#s26), and replacing
  source-tool dataset loops lives in [S31](../../spicy-docs/docs/simplification-todo.md#s31).
  Use existing offline reading without requiring unrelated analytics/server
  dependencies. Do not require every optional stage at once.

<a id="d46"></a>

- [ ] **D46 · P1 · Prove the wheel handoff, then remove replaced code.** Run a
  small captured-source fixture through the installed provider and DocSpec:
  read outcomes, build the catalog, acquire a selected document through the
  supplied fetcher where needed, and process retained content. **Done when:**
  packaged schemas/resources are present, evidence and failures survive, missing
  or unsupported packages fail clearly, and no repository-relative imports are
  required. Then remove superseded copies and update pins/docs. Reuse D10/D38
  checks; if the provider later moves to SpicyRegs, rerun this same handoff and
  switch current imports directly without a legacy fallback chain. Depends on
  D42–D45 for the capabilities actually selected.

## 9. Share search-catalog construction with the experiment workflow

The [independent investigation](history/2026-09-11-search-catalog-consolidation.md)
found a newer direct Parquet builder and compatible Engine loader in candidate
worktrees. Recheck their revisions before implementation. Keep one search recipe
and one search definition API; use DocSpec for shared attempt and dataset work.

<a id="d47"></a>

- **D47 · Moved to destination repositories.** The authoritative tasks for
  search-dataset definitions and identifier normalization are
  [SpicySearch SC03](../../spicysearch/PLAN.md#sc03) and
  [SpicyEngine EC01](../../spicyengine/PLAN.md#ec01). This ID is a dependency
  reference, not a DocSpec implementation checkbox or completed work.

<a id="d48"></a>

- [ ] **D48 · P1 · Support bounded dataset recipes beyond segment processors.**
  Extend D04/D13 using the existing catalog workspace, artifact, and execution
  primitives. Accept pinned catalog/layer/resource inputs and named output
  partitions without inventing a document capture or segment. **Done when:**
  an ordinary retained-input experiment and a metadata-only dataset build share
  attempt/reuse accounting. Global census and lookup dependencies invalidate
  affected outputs correctly. State whether resume reuses whole completed builds
  or completed partitions; do not promise the latter without implementing it.
  Keep scheduler messages small references. Depends on D02–D04 and D15.

<a id="d49"></a>

- [ ] **D49 · P1 · Qualify DocSpec's host API with an installed search recipe.**
  Use the supplied recipe to check D48's generic input/output references, full
  requested catalog population (including records not selected for body capture),
  global dependencies, and result reuse. **Done when:** DocSpec runs and records
  the build without search policy in its core and retains inputs for a later
  attempt. Record DocSpec runner identity separately from the recipe's semantic
  producer identity. Search implements the recipe in
  [SC04](../../spicysearch/PLAN.md#sc04); Engine admission is tracked in
  [EC02](../../spicyengine/PLAN.md#ec02). Depends on D09, D48, and the supplied
  recipe; no second search transformation implementation belongs here.

<a id="d50"></a>

- [ ] **D50 · P1 · Prove DocSpec's shared execution removes repeated work.**
  Compare local execution/reuse accounting against the supplied recipe runner
  with pinned inputs and independent expected cases. Exercise global dependency
  changes, interruption, installed wheels, and retained-input reuse; record
  scans, recipe calls, memory/scratch, and output work. **Done when:** local
  evidence supports the chosen execution path and any superseded local code
  is removed. Search owns transformation parity and build-path retirement
  in [SC05](../../spicysearch/PLAN.md#sc05); Engine owns index-only rebuilding in
  [EC03](../../spicyengine/PLAN.md#ec03). If integration merely adds another ledger,
  record a narrower decision and defer D49–D50; retain useful D09/D31 API reuse.
  Depends on D49. Require linked SC05/EC03 evidence before claiming the broader
  consolidation saved work; local ownership creates no deletion quota here.

## 10. Prove the named source-to-dataset examples

<a id="d51"></a>

- [ ] **D51 · P1 · Demonstrate GAO topic preservation in a dataset example.**
  Carry a literal topic from a small retained publisher page through a catalog
  and a supported result or processor. An exact topic filter is sufficient;
  search publication is optional. **Done when:** matching, missing, and unexpected
  topic cases retain provenance and demonstrate selection or analysis without
  inferring requirements from a label. Add only fields/byte access this example
  needs. Provider fixture/field work lives in
  [SpicyDocs S17](../../spicy-docs/docs/simplification-todo.md#s17). Depends on
  D06–D08 and applicable public processor APIs; report offline versus live use.

<a id="d52"></a>

- [ ] **D52 · P1 · Build a catalog from retained SpicyRegs public-comment data.**
  Implement the DocSpec adapter/example over a bounded retained table input.
  Preserve exact input identity, field provenance, coverage assumptions, and
  distinctions from other Regulations.gov representations. **Done when:** the
  installed workflow builds and reads the catalog, reports unavailable fields
  and rejected rows, and exposes supported document candidates before optional
  fetching or processing. Reuse SpicyRegs data without rebuilding its publication
  pipeline. Provider requirements live in [SpicyRegs SR02](../../spicy-regs/PLAN.md#sr02)
  and applicable source coverage in [SpicyDocs S09–S10](../../spicy-docs/docs/simplification-todo.md#s09).
  Depends on D06–D08/D43 and the selected public input API; no package move is required.

## Suggested delivery order

1. **Usable local experiment:** D01–D07, D11, D13–D17, D19–D20, D24, and D38.
   Build D05 early and extend it as each capability becomes usable. D51–D52
   provide the specific GAO and public-comment follow-ups.
2. **Explain results and finish integrations:** D08–D10, D18, D21,
   D23, D25–D29, D32, and D36–D37. Decide encoding before implementing the
   container consolidation. Source-provider work can proceed independently.
3. **Remove the machinery that replacements make unnecessary:** D22, D30–D31,
   and D33–D35. D12 follows demonstrated demand for its source route.
4. **Review each slice and confirm contribution value:** D39 throughout;
   D40 once the documented workflow is ready.
5. **Reuse provider capabilities through a wheel:** D41/D43/D45 can accompany
   current source integration; D42/D44/D46 follow actual consumer needs. A later
   SpicyDocs/SpicyRegs repository move remains optional and independent.
6. **Share search preparation:** D09/D47 provide immediate API/definition reuse.
   Prove D48–D50 alongside the experiment workflow before generalizing the runner
   further or deleting the current working build path.

## Evidence and coordination notes

The September 11 blind product, simplification, cross-repository coordination,
and duplication reviews supplied the candidate findings. They were static
reviews, not runtime or production validation. This list revises their priorities
using the user's later clarification. Current implementation references include
[Decision 0002's experiment purpose](decisions/0002-shared-execution-and-the-acquisition-gap-ledger.md),
[the architecture guide](architecture.md), and the linked code above.

A solutions architect subagent reviewed this revised checklist, including the
open package choice and wheel reuse. Its corrections clarified export admission,
reuse after extraction/segmentation changes, first-phase dependencies, and the
minimum task adaptation needed before campaign consolidation. The final review
found no material scope mistakes. This validates the plan's reasoning, not its
unimplemented behavior.

The September 11 sync compared SpicyDocs `5f04771` with DocSpec `da5e227`;
commits `40921d3` and `3e3e43e` recorded that planning sync. The owner's later
instruction makes each destination repository authoritative for its tasks.
The former mirrored mapping table is replaced by these links. No implementation
was completed by moving or splitting a task.

| Destination | Authoritative work and local connection |
| --- | --- |
| [SpicyDocs checklist](../../spicy-docs/docs/simplification-todo.md) | Source outcomes/coverage S01/S09–S10 → D08/D43; publisher rules S14/S19/S25 → D12/D42/D44; public provider wheel S26 → D43/D45; local caller/runner retirement S21/S31 → D22/D45. |
| [SpicyRegs plan](../../spicy-regs/PLAN.md#sr01) | SR01 reviews local overlap; SR02 supplies public-comment input facts/APIs for D52; SR03 implements only selected SpicyRegs handoffs. Package moves remain optional. |
| [Rulespec backlog](../../rulespec/TODO.md#rs01) | RS01 owns shared encoding → D28/S30; RS02 owns needed artifact capabilities → D27; RS03 assesses shared physical writes → D31/S22. |
| [SpicySearch plan](../../spicysearch/PLAN.md#sc01) | SC01 adopts DocSpec readers; SC02 shares schema-gate mechanics; SC03 owns search definitions; SC04 supplies the recipe; SC05 proves preparation parity and removes Search copies. |
| [SpicyEngine plan](../../spicyengine/PLAN.md#ec01) | EC01 adopts shared definitions; EC02 qualifies recipe output admission; EC03 proves index-only rebuilding and removes only replaced Engine preparation. |

D47 is retained only as a link to SC03/EC01. D51–D52 own the DocSpec examples
previously described in source-side S17/S18. Source-only use and ordinary local
experiments do not depend on D48–D50 or the search-recipe tasks. Each repository
owns its own package checks, contributor documentation, reviews, and removals.
RefSpec is a supplied resource option in D14; this plan assigns it no new
implementation merely because DocSpec consumes its existing package.

A static size scan at this baseline found 147 Python files and 40,495 lines under
`src/docspec/`, with six files above 800 lines. The largest include scale and
source-catalog schemas, bounded segmentation, processor/content models, and the
catalog CLI. Long control-flow candidates include checkpoint verification,
base reprocessing, delivery indexing, execution, and reconciliation. These counts
guide D33; they do not establish that the code should be split or removed.

For every completed item, record the behavior delivered, its commit/PR, relevant
checks, and remaining limits beside the checkbox. Do not count historical green
tests, the earlier merged refactor, or this planning commit as validation of the
new experiment workflow.
