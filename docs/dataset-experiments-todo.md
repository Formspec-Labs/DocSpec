# Dataset experimentation and simplification to-do list

DocSpec should make it easy to build a catalog, fetch selected documents, retain
them, run chosen processors now or later, and compare iterations while reusing
unchanged work. This checklist organizes the next changes around that outcome.
Across DocSpec and its source provider, the goal is to maintain each shared
capability once and reuse it through installed packages, reducing duplicate
implementation, testing, configuration, and documentation effort.

**Status: 45 of 52 local implementation items complete.** D47 is a moved-task
reference; D51–D52 retain the named dataset examples moved here from SpicyDocs.
Compiled on 2026-09-11 against merged revision
`dd18fb364acdc383643bacf52a108c92e0173aef`. Completed entries link their scoped
implementation and validation evidence; open entries state what remains to be
proved or supplied. A plan entry alone does not establish working behavior.

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
  Historical reproduction alone is not a reason to maintain an old code path;
  keep its provenance in Git and remove superseded implementations, fixtures,
  schemas, and instructions together.
- **Keep interpretation with its processor.** A processor may use RefSpec
  resources or another implementation. DocSpec records and executes it; the
  processor owns its domain meaning. Dagster schedules tasks; DocSpec determines
  the work and checks the results.
- **Use Dagster's execution facilities.** Scheduling, worker retries,
  cancellation, run monitoring, and executor/process management belong to
  Dagster. Keep the direct local path small. DocSpec owns document selection,
  verified stage checkpoints, retained input/output evidence, and reuse across
  dataset experiments; those rules must survive native Dagster interruption and
  re-execution. Do not build a parallel execution-control platform.
  Native Dagster resources inject chosen implementations and prepared DocSpec
  services. Native configuration controls managed execution; saved DocSpec
  evidence records the worker settings and limits it actually enforces.
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

- [x] **D04 · P0 · Expose the lifecycle through a supported application API.**
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

  **Catalog progress, September 11:** `build_local_catalog` and
  `open_local_catalog` now compose the existing builder and admitted reader from
  a workspace, chosen sources/policy, explicit producer, and scratch bound.
  Catalog-only work creates no document-processing state. D06 records the
  public supplied-record and installed-provider checks. Export convenience
  remains open under D26; catalog setup is now supported.

  **Completed September 12:** D26 adds the remaining public export operation.
  Catalog construction/reading, direct plan setup, capture, later processing,
  saved recovery, inspection, retention and export now have supported runtime
  entry points. The CLI uses that runtime for execution and inspection; export
  is available through Python. The isolated installed probe exercises the
  public lifecycle and independent export reading. D38 still owns the complete
  provider/growth acceptance exercise; D04 does not claim that qualification.

<a id="d05"></a>

- [x] **D05 · P0 · Establish a small reference experiment.** Extend the offline
  walkthrough with several documents, a selected exclusion, a recoverable failure,
  and a useful processor. Build it through D04 as a running example while the
  remaining items land. **Done when:** the installed package demonstrates initial
  capture and a later processing attempt, with understandable output and explicit
  remaining limitations. D38 supplies the complete acceptance exercise.

  **Completed September 11:** the [offline walkthrough](offline-walkthrough.md)
  uses four supplied documents, real local-file acquisition, an explicit run
  exclusion, a retained missing-file failure, and selective repair. It processes
  retained captures later, recovers a saved handoff, and compares configuration
  and resource alternatives with a clean result. The old synthetic source and
  fetcher classes and manual CLI setup were removed; current callers use public
  supplied records and local transport. All 33 focused checks passed, including
  the actual copied example running against an isolated installed wheel.
  Independent [architecture](history/2026-09-11-reference-experiment-architecture.md)
  and [code review](history/2026-09-11-reference-experiment-review.md) approved the
  bounded example. D38's complete growth/interruption exercise remains open.
  The combined regression then passed 1,216 tests with one live integration
  excluded and no warnings, before the next execution/source-outcome changes.

## 2. Build catalogs and acquire inputs through clear interfaces

<a id="d06"></a>

- [x] **D06 · P0 · Support provider inputs and caller-supplied records.** Make the
  source interface usable for SpicyDocs, SpicyRegs-derived data, another source,
  and bounded local records. Define the required source identity and
  provenance for supplied records without inventing acquisition evidence.
  **Done when:** one provider example and one local-record example build catalogs
  through public interfaces without private imports or sibling checkouts. Reuse
  [source ports](../src/docspec/ports/source_catalog.py). [D51](#d51) and
  [D52](#d52) own the GAO-topic and retained public-comment-table examples; they extend
  this interface without requiring a provider package move or blocking the
  initial local example.

  **Completed September 11:** `SuppliedRecordSource` snapshots bounded records
  with source-qualified identity, explicit coverage, exact raw fields, and
  proposed document candidates. Its policy uses the existing catalog format
  and records unavailable documents without inventing acquisition evidence.
  Public catalog helpers remove manual storage assembly; the existing optional
  SpicyDocs adapter is publicly exported. Empty catalogs now publish the blob
  directory needed by read-only opening. Producer validation and scratch limits
  refuse invalid setup before writes. See the [input guide](catalog-inputs.md),
  [architecture decision](history/2026-09-11-catalog-input-architecture.md), and
  [independent review](history/2026-09-11-catalog-input-review.md).

  The final focused gate passed 74 tests, including isolated installed wheels,
  public provider/supplied-record paths, malformed inputs, scratch bounds,
  empty publication, read-only opening, and recovery. These are offline checks;
  source outcomes and partial-input policy remain D08/D10, and observed-crawl
  coverage does not automatically preserve omitted items from a prior catalog.
  Commit `35d8516`; the subsequent combined regression passed 1,184 tests.

<a id="d07"></a>

- [x] **D07 · P0 · Make catalog iteration and selection inspectable.** Expose
  previewable additions, changes, exclusions, selected candidates, and reasons
  before acquisition. Decide how a dataset accepts successive or multiple source
  inputs, including source-qualified identity and collisions. **Done when:** an
  operator can explain a selection change and grow a dataset without silently
  conflating records or refetching unchanged selections. Depends on D02 and D06;
  use the existing catalog policy and succession machinery.

  **Completed September 11:** `preview_local_catalog` reports complete selection
  and change counts with bounded candidate/reason samples from exact admitted
  snapshots. Full-snapshot omissions and qualified-ID collisions remain explicit.
  Metadata-only successors retain verified acquisition and processing work while
  replacing the current source description; held failures still require an
  admitted retry. Catalog and result inspection share comparison helpers that
  distinguish absent fields, null, booleans, and numbers. See the
  [guide](catalog-iteration.md),
  [architecture decision](history/2026-09-11-catalog-iteration-architecture.md),
  and [independent review](history/2026-09-11-catalog-iteration-review.md).
  The final focused gate passed 98 tests, including real catalog growth, exact
  acquisition-provenance reuse, clean-result comparison, failure repair,
  ordering, sample bounds, and iterator closure. Ruff and diff checks passed.
  This does not qualify live providers or arbitrary mixed-policy composition.

<a id="d08"></a>

- [x] **D08 · P1 · Carry source collection outcomes into the dataset.** Consume
  the provider's public outcomes and retain them in DocSpec's source description.
  DocSpec owns the explicit policy for accepting partial input. **Done when:**
  empty observations, rejected records, unresolved collection, and successful
  input remain distinguishable through ordinary DocSpec APIs. Preserve count
  units and source evidence; upstream failures remain distinct from selected
  document-processing failures. Provider implementation lives in
  [SpicyDocs S01](../../spicy-docs/docs/simplification-todo.md#s01) and
  [S09](../../spicy-docs/docs/simplification-todo.md#s09).

  **Completed September 11:** catalog receipt 2.0 retains the provider's exact
  immutable description, reported outcome, and explicit acceptance choices.
  Partial and total record rejection require separate opt-ins before source
  iteration or output creation. Preview and source inspection preserve these
  observations separately from catalog-policy and document-processing failures.
  The current provider refuses unresolved collection before publication;
  DocSpec propagates that refusal. Supplied records report no invented outcome.
  The [architecture decision](history/2026-09-11-source-outcomes-architecture.md)
  and [independent review](history/2026-09-11-source-outcomes-review.md) cover
  exact evidence, bounded descriptions, saved recovery, and the consequence of
  omitted records in a full successor catalog. The combined gate passed 121
  checks with one installed-fixture failure; the corrected installed gate then
  passed both checks. D10 records the actual package boundary.

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

- [x] **D10 · P1 · Qualify the current installed source integration.** Coordinate
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

  **Completed September 11:** the isolated SpicyDocs 0.2.0 / Rulespec Artifacts
  1.0.12 check now publishes and consumes actual empty, partial-rejection and
  total-rejection sources, follows bounded original evidence, and refuses
  unresolved traversal. It distinguishes provider rejections from independently
  failed DocSpec metadata rules. Partial input requires explicit acceptance;
  unsupported reader capabilities refuse clearly. Both installed-package checks
  passed after correcting a new fixture's missing agency metadata. The normal
  installation and separate reader environment still require no sibling checkout.

<a id="d11"></a>

- [x] **D11 · P0 · Make fetcher injection practical.** Expose explicit selection
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

  **Completed September 11:** the router accepts any nonempty combination of
  local, HTTPS, and S3 fetchers, checks child acquisition evidence before
  accepting bytes, and derives its identity from effective delegate settings.
  Saved workers refuse changed settings even for sealed work or zero-task runs.
  An unpinned candidate may retain an observed transport version; required pins
  still refuse a missing or different observation. Ordinary public S3 catalog
  candidates now use HEAD followed by the existing conditional GET and exact
  response checks. Partial supplied pins never trigger a fresh observation.
  No catalog schema, client registry, or credential record was added. See
  [fetcher usage](fetchers.md), the [architecture decision](history/2026-09-11-fetcher-composition-architecture.md),
  the [router review](history/2026-09-11-fetcher-composition-review.md), and
  the [S3 review](history/2026-09-11-s3-acquisition-review.md).

  The transport gate passed 84 tests. It covers public local/S3 catalog
  lifecycles, HTTPS adapter/router fixtures, injected-client bounds and refusal,
  changed settings, retained evidence, and no refetch during later processing.
  Installed custom-fetcher injection remains covered by the package probe.
  The combined full regression passed 1,184 tests in 154.41 seconds, with one
  live integration test deselected and one known example-import warning. These
  checks qualify local behavior, not live remote availability or deployment.

<a id="d12"></a>

- [x] **D12 · P1 · Connect source-specific body validation when a route needs it.**
  For a selected Federal Register/GovInfo route, assess and reuse SpicyDocs'
  existing identity and soft-404 checks alongside bounded transport and capture.
  DocSpec still chooses the candidate. **Done when:** a demonstrated route accepts
  the intended document and explains wrong-document or placeholder refusals while
  retaining needed evidence. If no current experiment needs this route, record
  the deferral. Do not move source code into SpicyRegs to complete this item.
  Depends on D11; coordinates with SpicyDocs S19.

  **Completed September 12:** D53 supplies the selected GovInfo bill XML route.
  The installed SpicyDocs acquirer checks the offered version and body identity
  within the task's byte allowance. DocSpec retains exact accepted XML or the
  available refusal body and failure explanation. Installed checks refuse a
  different bill, an HTTP 200 HTML placeholder, and an unavailable response;
  each leaves an inspectable failed run. No local publisher validator was added.

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

- [x] **D14 · P0 · Demonstrate a meaningful optional processor.** Add a small
  example that produces inspectable results beyond byte/word statistics. A
  processor using pinned RefSpec resources is a suitable candidate; choose the
  actual use and resource before committing to an integration. **Done when:**
  users can inspect results and supporting evidence, change configuration or the
  resource pin, and run a new attempt. Domain interpretation stays in the
  processor; local fixtures are clearly distinguished from live-resource proof.
  Depends on D13.

  **Completed September 11:** the example-scoped
  [phrase matcher](phrase-matching-example.md) accepts exact pinned reference
  bytes through the existing processor interface. It returns literal quotes,
  original segment byte offsets, and unchanged enclosing source evidence.
  Case and resource changes produce four, three, and five matches in the
  walkthrough while reusing captures, representations, and segments. Bounds
  refuse overflow; an empty match list remains a successful result. The 33-case
  focused/installed gate checks real result admission, Unicode and overlap,
  resource identity, invalid inputs, bounded output, and the full example.
  Ruff and documentation link checks passed. Independent review approved it;
  this synthetic vocabulary does not qualify a live RefSpec resource or add a
  production classification service to DocSpec.

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
  Other governing policy changes used full repair at this milestone. D16 below
  extends verified-prefix reuse to accepted failures. See
  [the recorded approach](history/2026-09-11-capture-prefix-architecture.md).
  Changed extraction, segmentation, and added/replaced processors now compare
  against clean active-document state. Exact capture provenance remains distinct
  from newly acquired evidence. The [independent review and executed checks](history/2026-09-11-capture-prefix-review.md)
  support this acceptance: 1,025 full-suite passes, two stale test-metadata
  failures corrected, then all 15 focused follow-up checks passed. The full
  suite deselected one live integration test; failed-item repair remains D16.

<a id="d16"></a>

- [x] **D16 · P0 · Repair only the work that needs another attempt.** Carry final
  failure classification and completed-stage evidence into planning. Retry
  temporary failures under explicit policy; retry unchanged deterministic
  failures when selected or their relevant inputs change. **Done when:** a failed
  processor can be repaired without refetching valid content, repeated permanent
  failures do not loop on every unchanged successor, and retained failures remain
  visible. See [planner](../src/docspec/application/planner.py); depends on D15.

  **Completed September 11:** `selection.retryFailures` selects no retries by
  default, transient failures, or explicit retry of selected failed items.
  Relevant input/stage changes can also admit repair; changing an independent
  processor does not silently retry an unchanged permanent failure. Planning
  derives the complete prefix from existing retained evidence, records the
  execution mode and processor subset, and reuses only verified complete work.
  The disposition schema is now `docspec-disposition-record/3.0`, including the
  final failure and its checked evidence link. Successful repair leaves history
  in the prior retained result. Shared receipt parsing and test fixtures remove
  duplicated validation and setup. See the [repair guide](repairing-failures.md),
  [architecture decision](history/2026-09-11-failed-item-repair-architecture.md),
  and [independent review](history/2026-09-11-failed-item-repair-review.md).

  The final focused gate passed 86 tests covering real retained failures,
  clean-rebuild comparisons, extraction/segmentation/processor failures,
  unrelated changes, removed failing stages, empty output, late failure,
  interrupted repair, exact-input cache reuse, and budget recovery. No repeated
  capture/extraction/segmentation work is charged for inherited inputs. Repair
  checkpoints remain whole completed stages/processors; arbitrary partial-stage
  restart is not promised. No legacy disposition reader is added.
  Commit `6f4b8ab`; the subsequent combined regression passed 1,184 tests.

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

- [x] **D18 · P1 · Replace weak quality proxies with justified checks.** Review
  portable retention floors and other text-quality gates against concrete bad
  extractions. Separate observed extraction quality from the user's policy for
  continuing an experiment or exporting results. **Done when:** concrete cases
  establish which empty, truncated or misleading outputs byte and mapping
  checks detect, and which remain unknown;
  removed thresholds have a recorded rationale and replacement evidence. Preserve
  failure accounting; reduced validation must not silently imply completeness.
  Depends on D17 and informs D26.

  **Completed September 12:** the old retention-floor implementation and
  calibration chain are removed. Seven concrete quality cases show why a
  markup/text ratio cannot prove completeness: complete useful text can occupy
  under 1% of markup, while output missing a critical block can retain over 90%.
  Existing exact pins and named mapping checks still catch changed bytes and
  false declared slices; empty visible output remains explicit. An unpinned
  upstream omission remains unknowable from its shorter capture alone.
  The PDF case injects known pages to test evidence handling, not real parser
  completeness. The [export guide](result-exports.md) and
  [independent review](history/2026-09-12-portable-removal-review.md) record the
  revised, bounded acceptance: content suitability is explicit and semantic
  completeness is not established. No replacement scoring system was added.

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

- [x] **D20 · P0 · Prove pause, interruption, and resume for the new workflow.**
  Exercise capture, extraction, processing, delivery, and final state updates.
  Reuse checkpoints after verification and restore cumulative budgets.
  **Done when:** interrupted work resumes without accepting partial output,
  double-counting work, or repeating verified stages; cancellation actually
  reaches active work where the backend promises it. Reuse the existing recovery
  tests and add only missing cases introduced by D04.

  **Completed September 11:** the installed native multiprocess probe records
  a Dagster cancellation request and signals its parent while an extractor is
  active after a saved capture checkpoint. Dagster stops that child and closes
  its resources. Native re-execution runs only the unfinished task; the original
  successful sibling comes from native IO. Reconciliation preserves the exact
  captured payload, performs no repeat fetch, accounts for captured bytes once,
  and produces the same phrase values as the local reference.

  Existing checkpoint, prefix-budget, delivery, retention, and coordinator
  recovery checks cover the other stage and finalization boundaries. Changed
  worker settings, deadlines, task bounds, and invalid membership still refuse
  recovery. A custom local cancellation layer was removed before commit; no
  DocSpec run-control API or cancellation state was added. See the
  [native guide](dagster-experiment.md) and
  [independent review](history/2026-09-11-native-dagster-review.md).

  Qualification covers native multiprocess executor cancellation and recovery,
  not a launcher's transport, daemon/UI, hosted deployment, forced-kill cleanup,
  aggregate worker scratch, or uncheckpointed physical cost. D23 retains the
  broader retry/accounting review.

<a id="d21"></a>

- [x] **D21 · P1 · Complete the Dagster composition of the same workflow.** Keep
  Dagster optional and reuse the same plans, injected components, execution
  services, and result checks. **Done when:** a documented installed example
  demonstrates dispatch, interruption/retry, and result reconciliation with the
  same logical results as local execution, including processing retained inputs.
  Injected resources reconstruct in workers; locking, stage completion, active
  cancellation, and source-refusal meaning survive dispatch.
  Use Dagster's native scheduling, retries, cancellation, event storage, and
  worker management. State backend-specific limits; avoid a second dataset
  state or execution-control model. Depends on D04 and D13; qualify interruption
  and recovery jointly with D20. See [the adapter](../src/docspec/adapters/dagster.py).

  **Completed September 11:** `build_dagster_definitions` accepts native
  `resource_defs`; a yielding resource supplies `PreparedLocalRun` directly.
  Fetchers, processors, workspace and other components use native dependency
  injection. The documented installed example captures two documents, processes
  retained captures in a later native run, and reconciles results through the
  public runtime with local-value parity. The separate native stop/re-execution
  probe establishes D20's missing active-worker boundary and preserves accepted
  document failures without converting them into worker retries.

  Removed `DagsterRuntime`, `ExternalExecutionBackend`, the unused dispatcher
  and backend protocols, scheduler declarations and unenforced proxy limits.
  Execution profile `2.0` retains actual worker, task-index, deadline and cache
  evidence; local request `3.0` retains only meaningful local execution choices.
  Native events link the saved evidence to Dagster's run without putting its
  run ID in the document handoff. There are no legacy readers. See the
  [architecture decision](history/2026-09-11-native-dagster-di-architecture.md).

  The focused runtime gate passed 128 tests; the isolated installed native
  qualification passed, including actual child interruption, resource closure,
  sibling/checkpoint reuse, native IO, output parity and independent resource-pin
  refusal. Existing checkpoint/finalization and package/budget gates provide
  adjacent regression checks. These are local qualification results, not
  remote CI, publication or deployment evidence.

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

  **Scope review, September 12:** SpicyDocs S21/S31 currently defer caller
  replacement until a named workflow needs it. Keep this integration deferred
  until that task and its ordering, lock, cancellation and resume requirements
  are supplied. Existing native Dagster document execution is supporting
  evidence, not acquisition-campaign acceptance. No new executor is justified
  by the current callers.

<a id="d23"></a>

- [x] **D23 · P1 · Assign retries and outcome accounting to explicit owners.**
  Review nested transport, processor, and scheduler retry loops; give each layer
  a bounded purpose through explicit native and item policies. **Done when:** repeated
  attempts consume the expected budget and produce attributable outcomes.
  Acquisition observations, selected documents, scheduled tasks, and exported
  text retain their distinct counts and meanings. Share common mechanics where
  useful without forcing every failure into one universal ledger. Depends on
  D08, D16, and D20; informs D22.

  **Completed September 11:** the [retry guide](retry-ownership.md) identifies
  SDK requests, item attempts, native Dagster task retries, and source collection
  outcomes separately. S3 configuration 2.0 uses native `total_max_attempts`
  through `sdk_total_attempts`, defaulting to one request including the initial
  call. It replaces the option that silently permitted an additional request.
  Existing item loops remain because they record attributable attempts and
  accepted document failures; Dagster retains task retry ownership. The stale
  runner-network-allowance claim was removed. No universal transfer, billing,
  or cross-re-execution budget is claimed. All 102 focused checks passed,
  including real local HTTP requests through the SDK, combined item/SDK attempt
  counts, terminal replay without requests, processor receipts, logical charging,
  and native mapped-task retry. Independent
  [architecture](history/2026-09-11-retry-ownership-architecture.md) and
  [review](history/2026-09-11-retry-ownership-review.md) approved the correction
  without another retry service.

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
  D20 now qualifies active interruption through the native multiprocess executor.

<a id="d25"></a>

- [x] **D25 · P1 · Account for shared experiment inputs in retention previews.** Recheck
  maintenance and deletion reachability against multiple attempts and shared
  bases. Expose a preview of referenced and candidate blobs through existing
  maintenance mechanisms. **Done when:** explicit retained results and checkpoints
  preserve required local blob dependencies; removing one alternative from the
  root set leaves another's required bytes protected; interrupted previews are
  repeatable. This item does not authorize deleting existing user datasets.
  Depends on D02 and D24; see
  [maintenance](../src/docspec/application/maintenance.py).

  **Scope decision, September 11:** the independent architect approved a
  dependency inventory and read-only preview. The earlier destructive-pruning
  and cleanup-recovery criteria required an unimplemented deletion subsystem;
  those capabilities are outside this slice and are not claimed delivered.

  **Completed September 11:** `build_local_retention_set` composes the existing
  service, follows required predecessors and plan bases, and requires saved
  plan references for standalone checkpoints. Exact artifact pins distinguish
  different results of one logical plan. `preview_local_blob_inventory` and the
  CLI share the moved inventory implementation. The reader labels inventory
  relative to its supplied set; it does not rederive an imported set's complete
  roots or discover other attempts. Retention-set 2.0 records supplied plan
  references; no alternate format reader or deletion operation was added.
  [Architecture](history/2026-09-11-retention-preview-architecture.md),
  [review](history/2026-09-11-retention-preview-review.md), and the
  [walkthrough](retention-preview.md) explain this boundary. All 34 focused
  checks passed, including real same-plan alternatives, shorter successors,
  planned work using captured bases, shared-input preservation, interruption,
  and existing CLI/maintenance behavior. Fixture deletion was confined to
  temporary test data; no existing dataset was cleaned.

## 5. Export and consume results without a second production pipeline

<a id="d26"></a>

- [x] **D26 · P1 · Build optional exports from retained experiment results.**
  Replace the campaign-specific route for normal use with a maintained export
  operation that reads verified retained state. Give internal experiment state
  and portable output distinct names and unambiguous format identities.
  **Done when:** export applies an explicit admission policy, preserves required
  refusals and complete accounting, and reuses valid retained results. An
  experiment may retain failures that prevent its admission as a consumer
  dataset. Repeat or interrupted export has explicit behavior. Export remains
  an optional stage.
  Depends on D17–D18 and D24.

  **Completed September 12:** `export_local_result` copies the complete active
  dataset and typed evidence through Rulespec's shared container. Explicit
  `retained-evidence` and `nonempty-text` choices preserve all outcomes or refuse
  with counted, bounded reasons. Exact source-result pins distinguish different
  outcomes of one plan. Repeat export admits identical output; interrupted
  staging publishes nothing and no-replace publication protects a different
  destination. No execution or export checkpoint service was added. See the
  [guide](result-exports.md),
  [architecture](history/2026-09-12-result-export-architecture.md), and
  [independent review](history/2026-09-12-result-export-review.md).

  The final focused gate passed 79 tests covering independent reading, complete
  mixed-result populations, inherited owning plans, byte/metadata limits,
  resealed semantic corruption, interruption, existing evidence/recovery and
  extraction-quality characterization. The actual isolated installed probe
  passed separately with the original workspace and source unavailable and no
  repeated stage calls. D18/D27/D30 retain the old-path removal work.

<a id="d27"></a>

- [x] **D27 · P1 · Adopt the shared generic artifact implementation.** Compare
  DocSpec's portable builder/verifier needs with Rulespec's existing container.
  Delegate generic membership, byte integrity, and structural checks when they
  meet the required behavior; retain document semantics and coverage checks here.
  **Done when:** D26 uses one supported container path, DocSpec's replaced
  structural machinery is removed, and any retained difference has a concrete
  reason. Needed shared-library changes are owned by
  [Rulespec RS02](../../rulespec/TODO.md#rs02); consumer changes are owned by
  [SpicySearch SC01](../../spicysearch/PLAN.md#sc01). Depends on D26's design and
  D28; see [artifact adapter](../src/docspec/adapters/platform_artifact.py).

  **Completed September 12:** result exports use Rulespec's public root,
  manifest, membership, identity, byte verification and no-replace publication.
  The old portable verifier/support subtree and its exclusive schemas are
  removed. Current `platform_artifact.py` remains because retained workspace
  results use its DocSpec mapping through that same shared library. Document
  semantics reuse current logical and receipt validators. Shared admission
  checks actual manifest descriptor totals before opening payloads; no second
  local manifest validator was needed. D26's adversarial and installed checks
  and the [removal review](history/2026-09-12-portable-removal-review.md) establish
  this replacement; retained-state capacity qualification remains D37.

<a id="d28"></a>

- [x] **D28 · P1 · Adopt the agreed shared canonical encoder in DocSpec.**
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

  **Completed September 11:** DocSpec now uses the installed Rulespec Artifacts
  1.0.12 canonical emitter for identity JSON and framed records. The selected
  domain is exact safe integers and UTF-16 key ordering; unsupported metadata
  refuses without rounding or stringification. Exact captured document bytes
  remain unchanged. Both local emitters, the unused batch framer, and redundant
  portable integer walk were removed. Domain conversion and incremental framing
  retain their distinct useful purposes. The [guide](canonical-json.md),
  [architecture](history/2026-09-11-canonical-json-architecture.md), and
  [independent review](history/2026-09-11-canonical-json-review.md) record the
  intentional shared dependency and identity change. The combined gate passed
  138 checks, including installed raw-capture and source checks, with a stale
  textual-boundary assertion subsequently corrected. Follow-up import/package
  checks passed 13 checks; the remaining new export-facade allowance belongs to
  the concurrent D26 implementation. This does not claim a new full regression.

<a id="d29"></a>

- [x] **D29 · P1 · Provide public document-result admission and reading.** Expose
  a supported, bounded reader for D26's exports. Return verifiable identity
  information instead of requiring manual transcription of a verification claim.
  **Done when:** installed DocSpec APIs admit and read a fresh exported dataset,
  refuse tampering, and resolve evidence to retained inputs. Publish the consumer
  example and exact wheel interface needed by
  [SpicySearch SC01](../../spicysearch/PLAN.md#sc01), where Search's adoption and
  private-import removal are tracked. No legacy import shim is required.
  Depends on D26–D28.

  **Completed September 12:** `open_result_export` returns the shared verified
  pin, summary, streamed unchanged layer rows, verified blob streams/bytes, and
  exact typed evidence. It reads independently of the original workspace and
  execution plugins. The reader shares current logical/stage/processor checks,
  verifies exact segment slices and identity mappings, and reports named
  derived transformations as not replayed. Per-evidence reads currently require
  at most 64 MiB; large capture-only blobs stream within the caller's total
  artifact limit. D26's focused and installed checks include missing, extra,
  changed and resealed inconsistent members, wrong pin/producer, early iterator
  closure, and later tampering. These are bounded fixtures, not a corpus-capacity
  qualification or a semantic-completeness claim. Search adoption remains SC01.

<a id="d30"></a>

- [x] **D30 · P2 · Retire superseded tools and their exclusive support code.**
  Reassess the historical release builder, sample tools, and fixture restamper
  after D26. Integrate reusable production behavior into its package owner;
  retain a research tool only when it serves a current experiment need.
  **Done when:** the [tool inventory](../tools/README.md) identifies one supported
  route per task and superseded implementations are removed with provenance
  preserved in Git. The owner's September 12 clarification retires historical
  byte-for-byte reproduction and legacy consumer compatibility as requirements.

  **Completed September 12:** removed the fixed portable builder, calibration,
  PDF floor-population draw, campaign pin helper, fixture restamper, and their
  exclusive schemas, input pins, fixture trees and tests. The attachment sampler
  loses the floor-only branch and keeps its current unavailable-document sample.
  The [tool inventory](../tools/README.md) retains the remaining research, source,
  attachment and schema tools by actual purpose. Current retained-state,
  capture/processing, failure and independent export checks remain. Git preserves
  the retired implementation and data provenance; current docs and Decision
  0001's status direct contributors to the maintained workflow. See the
  [independent review and execution evidence](history/2026-09-12-portable-removal-review.md).
  Final regression: 1,071 passed, one live case deselected, and two existing
  fork-in-threaded-process warnings; Ruff, diff and 429 local-link checks passed.

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

  **Shared-writer deferral, September 12:** the attempted runtime delegation
  to Rulespec 1.0.12 was reverted before commit. A synchronized real-writer
  probe found that ordinary pending-hardlink cleanup by one writer makes a
  second writer reject unchanged identical bytes. The single qualification
  case failed in 1.58 seconds. DocSpec keeps its working writer and adds no
  local retry/locking workaround. Source-catalog transactions also need the
  shared API to accept an already-admitted root identity before writing.
  Both fixes are tracked in destination [Rulespec RS03](../../rulespec/TODO.md#rs03).
  The [decision and retained probe](history/2026-09-12-shared-blob-writer-architecture.md)
  and [independent review](history/2026-09-12-shared-blob-writer-review.md) record
  the evidence-backed deferral. Shared-writer adoption remains deferred.

  **Inventory follow-up, September 12:** CourtListener now uses the provider's
  public parser (D42). Atomic directory publication and canonical encoding use
  Rulespec's existing APIs (D28); retained export uses its container (D29).
  Rulespec `1.0.12` exposes no equivalent public row-validator helper. DocSpec
  instead removes its generic compiled-gate class and optional-engine fallback,
  directly using its required `jsonschema-rs` dependency and existing Python
  rejection diagnostics. The remaining function adds DocSpec's error meaning. Importing
  Search's wrapper would add the wrong dependency; creating another shared
  framework would add more than it removes. [Schema maintenance](schema-maintenance.md)
  names the engines and differential checks. RS02/SC02 retain any destination
  work; the demonstrated RS03 writer gap is the remaining adoption dependency.
  The catalog, publication, installed-provider and package-boundary gate passed
  **52 tests** in 20.60 seconds. Ruff and lock consistency passed.

<a id="d32"></a>

- [x] **D32 · P1 · Remove configuration that repeats facts or promises no enforcement.**
  Review role/profile declarations, governance identifiers, provider metadata,
  and optional cache settings after D03 and D13. Derive facts from selected
  implementations where possible; keep output-affecting identities and enforced
  limits explicit. **Done when:** users supply only meaningful choices,
  declarations match actual runtime behavior, and each retained abstraction
  serves the experiment workflow. Lack of today's CLI caller alone does not
  make processor dependencies or resource pins dead code.

  **Progress, September 11:** D21 removes saved scheduler declarations and seven
  unenforced limit fields. The remaining task-index bound names the actual
  temporary database it limits; local concurrency stays outside saved worker
  identity. Native Dagster configuration is authoritative for managed workers.
  The broader profile/governance/cache review remains open.

  **Profile cleanup, September 12:** storage-description format `2.0` removes
  `ProfileGovernance`, five repeated placeholder labels and the registry
  allowlist/API arguments. Those values were parsed and hashed but never
  enforced. Physical settings, limits, dependencies, secret checks and the
  implemented plan policies remain. The architect approved this narrow
  [decision](history/2026-09-12-profile-simplification-architecture.md).
  All 89 focused profile, conformance, CLI, workspace, worker and package checks
  passed, including the actual isolated installed runtime/export probe.
  Final independent review of this earlier slice is tracked under D39; its
  broader cache/declaration inventory is completed below.

  **Cache cleanup, September 12:** execution-profile format `3.0` removes
  configuration-only cache profile/state artifacts. They did not configure or
  restore the cache and were emitted even for capture-only runs. The existing
  result cache and verified processor-result evidence remain; worker references
  are verified directly at execution, recovery, reconciliation, and retention.
  The [decision](history/2026-09-12-profile-simplification-architecture.md#remove-cache-declarations-that-do-not-control-the-cache)
  distinguishes this removal from useful cache behavior. The focused gate
  passed **76 tests**, including actual installed local and native Dagster runs,
  cache hits, invalid-result repair, and cache outages. The earlier unused
  `NullProcessorResultCache` removal is committed as `3adc7fc`. The completed
  inventory appears below; D39 consolidates the independent reviews.

  Implementation: `7188e34`. The combined full suite subsequently passed
  **1,072 tests** with one live integration test deselected in 239.01 seconds.

  **Declaration follow-up, September 12:** removed the unused storage-profile
  `verifier_id`, `recommendedMemberBytes`, and `maxOpenStagingCommits` fields.
  No runtime selected that verifier or enforced those settings. Profile loading
  also checks the configuration digest once. Actual storage bounds, module and
  dependency selection, and conformance test references remain. The existing
  profile, workspace, worker, runtime and installed-package gate passed **92
  tests** in 19.33 seconds; Ruff passed. Independent review is consolidated in D39.

  **Completed locally September 12:** the final inventory removes handwritten
  verifier status and the three sink profiles' descriptive, unenforced limits.
  Actual storage bounds, synchronous acknowledgement behavior, processor
  dependencies and resource/stage identities remain. Runtime composition now
  opens SQLite only when a selected processor enables exact-input caching.
  All-disabled processing retains and recovers output without constructing a
  cache; a mixed processor set reuses the enabled processor and reruns the
  disabled one. The focused cache/profile/sink gate passed **37 tests**; the
  complete strict suite passed **1,078 tests**, with one live integration
  deselected, in 247.07 seconds and no warnings. The subsequent mixed-policy
  test strengthening passed both lifecycle tests in 1.70 seconds. No production
  code changed after the full run. The
  [final profile/cache review](history/2026-09-12-final-profile-cache-review.md)
  approves this slice with no material findings. D39 records the completed review consolidation.

<a id="d33"></a>

- [x] **D33 · P2 · Review current file and function outliers by responsibility.**
  Reassess catalog CLI coordination, checkpoint verification, base reprocessing,
  delivery indexing, and execution control flow after workflow changes settle.
  Review large schemas and segmentation algorithms separately. **Done when:**
  long functions expose understandable steps, shared rules have clear owners,
  and any split reduces the context needed for a change. Follow the existing
  length review guidelines; avoid chains of tiny wrappers or shared-state mixins.

  **Progress, September 12:** catalog CLI coordination fell from 858 to 399
  lines by removing the second command-receipt validation path, rather than
  spreading it across smaller modules. The [decision](cleanup-decisions.md#verify-the-catalog-once)
  identifies the existing artifact reader as its replacement. The installed
  source test now separates environment setup from its executable probe; the
  probe is ordinary Python that editors and Ruff can inspect (`d97bdab`).
  The [current responsibility review](cleanup-decisions.md#september-12-follow-up)
  covers all five production functions of at least 200 lines. Checkpoint
  verification, retained-base preparation, per-document execution, and release
  indexing each keep one related state transition together; the closed schema
  declarations remain beside their typed rows. The segmentation decision
  remains applicable; the later caller audit retired the unused scale family.
  No additional wrappers or dispatch framework are
  warranted by line count alone. This completes the local responsibility review;
  independent review is consolidated in D39.

<a id="d34"></a>

- [x] **D34 · P2 · Remove superseded paths, declarations, and dependencies.**
  Search source, tools, tests, exports, registrations, profile strings, and known
  installed consumers after each replacement. Recheck obsolete source-policy
  declarations with the SpicyDocs owner: tagging eligibility and processor
  selection are downstream choices, not publisher facts. **Done when:** each
  removed path has a named replacement or an explicit retirement decision,
  unused dependencies leave packaging, and dynamic hooks remain functional.
  Move active experiment choices into DocSpec only where they have a local consumer.

  **Progress, September 12:** removed the superseded source-catalog command
  receipt, validator, extra identity and CLI arguments, plus its unused
  root-file writer and failure-report hooks. The catalog's sealed build receipt,
  native-source admission, atomic publication, and full artifact reader remain.
  Source, tools, tests, and selector searches found no remaining runtime caller
  of the removed path. Older draft requirements are explicitly superseded by
  the maintained decision. Invocation details remain in the build report.
  Source-side declaration removal is [SpicyDocs S12](../../spicy-docs/docs/simplification-todo.md#s12);
  this task removes only DocSpec code and dependencies.

  **Completed local audit September 12:** Vulture `2.16` scanned production,
  tools, examples and tests; callers and registrations were traced before
  removal. Removed five unused production declarations: two cached staging
  paths, an unused JSON type alias, and two unused sets of diagnostic codes.
  Their active state, individual codes and measurement explanations remain.
  Removed the unused test retention constant, PyMuPDF, ijson, and a redundant
  development declaration of the core jsonschema dependency. The PDF extra
  retains pypdf, which the actual extractor loads. The shared encoder still
  uses msgspec, so that development accelerator remains.
  Parser callbacks, lazy exports, protocol methods, serialized enum values,
  generator failure controls and dataclass equality are active behavior, not
  removals justified by a static unused-name report. The focused package,
  publication, worker, identity, segmentation and extraction gate passed
  **164 tests** in 19.53 seconds, including installed-package checks. Ruff and
  the updated 79-package lock pass. Vulture is a one-off audit tool, not a new
  project dependency or blanket CI gate. Independent review is consolidated in D39.

<a id="d35"></a>

- [x] **D35 · P2 · Simplify tests around observable behavior.** Reuse the prior
  refactor's focused suites and support helpers. Retire assertions for deliberately
  removed behavior; reduce redundant tests that only mirror implementation.
  **Done when:** meaningful checks cover evidence, reuse, failures, bounded work,
  and public interfaces without making internal moves expensive. Keep selectors
  and installed-package probes current. Do not add tests solely to justify small,
  reversible file moves or deletions.

  **Progress, September 12:** D38's package check runs the same copied behavioral
  test as the local walkthrough, replacing duplicate summary assertions. The
  full suite at `7188e34` exposed two warnings from crash tests using `os.fork`
  after threaded tests. Those tests now use Python's standard `spawn` process
  mode, still terminate with `os._exit`, and retain the before/after-publication
  byte and retry assertions. A bounded join prevents a stuck child from hanging
  the suite. The affected execution/storage gate passed **27 tests** with
  deprecation warnings treated as errors. Broader test-ownership review is open.

  The catalog CLI cleanup replaces receipt-shape and duplicate-summary tests
  with direct pin, producer, tampering, read-only, relocation, and Python-built
  catalog checks. Existing publication-failure, concurrent-winner, shared-blob,
  and installed-provider checks still pass. The focused gate passed **78 tests
  in 25.96 seconds**, including actual installed wheels. Broader review is open.

  The combined suite at `0cfae80` passed **1,065 tests**, with one live test
  deselected, in 234.67 seconds and no warnings. The first full run found one
  stale comment-profile parser test using the removed CLI option; `78e9a40`
  updates that caller while retaining its profile-selection assertion.

  **Current simplification, September 12:** independent review identified
  historical-symbol and archive absence checks that made deliberate deletions
  expensive without proving current behavior. Removing them retains actual
  import boundaries, lazy optional imports, personal/sibling checkout path
  refusal and isolated wheel/resource checks. The seven remaining package
  tests passed in 11.97 seconds. Four installed suites now share one wheel
  build per pytest session while keeping separate environments, dependency
  absence checks and copied behavioral probes. The combined strict regression
  gate passed **1,076 tests**, with one live integration test deselected, in
  249.91 seconds and no warnings.

  **Completed locally September 12:** Python API fixtures now construct typed
  arguments directly in one existing support helper. Thirteen callers no longer
  write CLI JSON and invoke private command parsers just to obtain those values.
  The command writer remains a thin wrapper; its independent 1/1 execution
  defaults preserve the real CLI parity control. Runtime, recovery, worker,
  inspection, prefix, export, CLI and profile checks passed **169 tests** in
  48.98 seconds; Ruff and diff checks passed. No behavioral cases were removed.
  The [simplification review](history/2026-09-12-test-simplification-review.md)
  records the three changes and the corrected parity counterfactual.

<a id="d36"></a>

- [x] **D36 · P1 · Keep one contributor path through the new workflow.** Update
  the walkthrough, extension guide, task-to-code map, operations guide, schema
  instructions, and decision index as their corresponding changes land.
  **Done when:** a contributor can add a source adapter, fetcher, or processor;
  run a later attempt; inspect results; and find the relevant checks using
  maintained documentation. Retire superseded instructions and keep historical
  measurements labeled with their revisions. Depends on the interfaces above.

  **Completed locally September 12:** the README and contributor task map lead
  to the supplied-record, source-reader, fetcher, processor and native Dagster
  examples and their focused checks. The extension guide starts with the existing
  source/fetcher interfaces; the documentation index includes GAO metadata and
  bill acquisition. Removed stale statements that installed experiment checks
  and export convenience are still unimplemented. The schema guide names the
  current validators, and operations cover recovery, retention and selection.
  All **494 maintained-document local file targets** resolve. Installed examples
  exercise the documented APIs; the unfamiliar-human contribution remains D40,
  and independent review is consolidated in D39.

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

  **Regression simplification, September 12:** the
  [qualification guide](qualification.md) records all nine scope dispositions.
  Retired the custom pytest subprocess/report runner and duplicate test-status
  declarations. CI now runs one strict native pytest suite and retains JUnit,
  logs, the lockfile and wheels; mapped tests must all execute successfully.
  Profile descriptions name their verifier test ID without a hand-maintained
  verdict. The [architecture review](history/2026-09-12-regression-qualification-architecture.md)
  approves this approach, and the [hook review](history/2026-09-12-regression-hook-review.md)
  approves the native collection/report checks. The focused gate passed
  **35 tests** in 3.31 seconds. Representative capacity measurements, including
  retained-admission memory use, remain open; this scope revision claims no
  unrun workload result.

  **Unused declaration family retired, September 12:** removed `ScaleProfile`,
  `ScaleResult`, their CLI, schemas, generator and exclusive tests. They validated
  supplied declarations but had no execution or measurement consumer. The
  [qualification guide](qualification.md#qualify-a-capacity-claim) uses existing
  plan, handoff, run and release references with native measurements; work,
  execution and storage limits remain enforced. Removing this unused format
  claims no capacity result and leaves D37 open.
  Independent review approved the retirement. The post-cleanup strict suite
  passed **1,085 tests**, with one live integration test deselected, in 252.90
  seconds. This includes the control, checkpoint and blob-read simplifications.
  Ruff, lock consistency and all 270 local targets in the changed guides passed.

  **Earlier regression evidence, before scale-family retirement:** the complete
  strict suite passed **1,076 tests**, with one live integration test deselected,
  in 249.91 seconds and no warnings. Native JUnit and console
  evidence were retained locally. This includes all 19 regression-gate control
  cases, the real installed-package examples, native Dagster and local S3 SDK
  retries. Ruff, lock consistency and all 522 maintained-document local targets
  passed. CI configuration is updated; no remote CI run is claimed.

  **Measured repetition corrected, September 12:** a 128-document diagnostic
  profile recorded 128 full admissions of the same retained base, consuming
  161.344 seconds of its instrumented 214.627-second run. One prepared execution
  now shares one successfully admitted base reader and clears it on close.
  Fresh workers re-admit; used record members, blobs and receipts keep their
  existing checks. The [architecture decision](history/2026-09-12-base-reader-lifetime-architecture.md)
  and [independent review](history/2026-09-12-base-reader-lifetime-review.md)
  describe that observation boundary and approve the correction.
  All **12** new cases passed; the complete strict suite then passed **1,095
  tests**, with one live integration test deselected, in 274.61 seconds. Ruff,
  lock consistency and maintained-document local targets passed.
  The profile establishes repeated work, not unprofiled performance or the
  proposed 4,096-document capacity. Representative capacity remains open.
  The same unprofiled 64-document processing/retention probe improved from
  **28.112 to 12.962 seconds**; the corrected 128-document probe completed in
  **25.170 seconds**. Fresh retained inspection passed for both. The architecture
  report retains exact observations and their limited scope.

  **Duplicate control reads removed:** ten callers now use the existing verified
  `load` directly, avoiding a second complete read, hash and parse. The port
  documents that behavior, and the planner fixture follows it. Typed values,
  lineage and standalone verification remain checked. Independent review
  approved the change; **117 relevant tests** and Ruff passed. No end-to-end
  speedup is claimed for this small correction.

  **Redundant local row copies removed:** retained artifacts now contain one
  `release.json` member pointing to existing immutable layers. The unused
  mirrored rows and their comparison pass are gone; ordinary open still fully
  verifies original records, blobs, controls and lineage. Portable export keeps
  its independent copy. **64 focused tests** passed, including capture, retained
  reuse, original-data corruption, exact membership and mixed-plan exports.
  The [architecture guide](architecture.md) records the new local layout;
  derivation identities change with its declared output roles.
  An [installed 512-document comparison](capacity-workloads.md#local-comparison-after-removing-duplicate-row-storage)
  passed complete fixture checks in both versions. It eliminated 23,433,600
  bytes of mirrored rows; processing took 94.13 versus 87.90 seconds, while
  reopening and processing memory were essentially unchanged. These are single
  diagnostic observations; the larger capacity and recovery claims remain open.

  **Further validation simplification, September 12:** ordinary retained open
  now checks pinned metadata and linked controls. Explicit audit retains complete
  checks for retention, selection, export, maintenance and comprehensive inspection.
  Stage and retain reuse their successful audit within one operation instead of
  repeating dataset-wide scans; existing destinations still receive a fresh audit.
  [The guide](retained-catalog.md) states the later detection of unused corruption.
  PDF extraction parses once, checkpoints share identical full blob checks only
  within one invocation, and compaction receipts keep actual counts without fixed
  algorithm declarations. Independent reviews approved these changes. The strict
  suite passed **1,105 tests**, with one live integration deselected, in 227.97
  seconds; the final receipt-only cleanup then passed all **seven** maintenance
  tests. Ruff and lock checks passed. The revised recipe also passed installed
  text16 recovery and clean comparison; a measurement-only edit no longer forces
  dataset rebuilding, while processor-byte drift still refuses.
  [Installed metadata/audit measurements](capacity-workloads.md#metadata-opening-and-full-audit-are-separate-operations)
  on `3eced2f` returned identical release references and declared counts: opening
  the 4,096-document result took 0.11 seconds; auditing its complete retained data
  took 53.34 seconds. This measures separate scopes on a shared host, not a full
  workload qualification of the newer revision.
  The [installed text512 diagnostic and parser comparison](capacity-workloads.md#rerun-diagnostic-and-remaining-read-costs)
  then passed complete changed/clean equality. Profiling found 3,072 adjacent
  duplicate record-descriptor reads; `8331028` removes them within each lookup.
  Canonical parsing also validates the decoder's plain values directly with the
  existing shared encoder before freezing, eliminating an intermediate copy
  while retaining the same byte/scalar checks. Both changes received independent
  approval. The final strict suite passed **1,112 tests**, with one live
  integration deselected, in 221.94 seconds; focused checks and Ruff passed.
  Repeated per-source partition scanning remains under architectural review.

  **Frozen larger trial:** [recorded observations](history/2026-09-12-local-capacity-observations.md)
  qualify the original `a4a0e05` markup256 case within its declared local scope.
  Its complete changed-resource and clean results agree, with no repeated upstream
  calls. Text4096 has passed capture, recovery, inspection and complete processing
  checks, but its changed-resource operation exceeded the fixed 1,800-second
  allowance (exit 124). No completed changed result was returned; clean and
  comparison qualification did not run. Preserve this failed trial and its limits;
  a corrected implementation needs a fresh pinned trial. D37 stays open.

<a id="d38"></a>

- [x] **D38 · P0 · Prove the complete experiment loop through installed packages.**
  Starting from the reference experiment, build a provider catalog, fetch once,
  run a meaningful processor, change its configuration or resource pin, and
  process retained inputs again. Add input, target failed work, interrupt/resume,
  and compare with a clean rebuild. **Done when:** observed calls prove selective
  reuse, results and gaps are inspectable, and ordinary use needs no private
  imports, ad hoc file editing, or sibling checkout. Keep export/Dagster checks
  in D29/D21 so they do not block useful local experiments. Depends on D05–D07,
  D11, D13–D17, D19–D20, and D24.

  **Completed September 12:** the [reference walkthrough](offline-walkthrough.md)
  now publishes a five-row successor catalog and processes only its added input.
  The same test runs locally and against an isolated wheel, observing actual
  calls for capture, targeted repair, retained processing, saved recovery,
  configuration/resource alternatives, growth, and a clean rebuild. Exact
  phrase values, source slices, settings, dispositions, and failures agree;
  inspection preserves different delivery evidence and add/update classifications.
  Test instrumentation stays outside the example, and the package test reuses
  the behavioral test rather than duplicating its assertions.

  The installed SpicyDocs probe separately builds a valid Federal Register
  catalog, captures a controlled publisher HTML response through the real HTTPS
  fetcher, and produces three source-linked statistics records from retained
  visible text with one total request. Existing installed native Dagster checks
  cover process interruption and recovery; no new runner was added. The focused
  installed/package/catalog/Dagster gate passed **30 tests in 60.07 seconds**;
  the direct walkthrough passed separately. These fixture checks do not establish
  live publisher reliability or semantic quality. Final independent review is D39.

  Implementation: `8476f88`.

<a id="d39"></a>

- [x] **D39 · P1 · Obtain independent architecture and code review of the implementation.**
  Give reviewers the clarified purpose, changed interfaces, and acceptance
  evidence. Use a solutions architect for judgment and independent semi-formal
  code reviews for implemented changes. **Done when:** findings on ownership,
  unnecessary complexity, correctness, and evidence gaps are resolved or
  explicitly recorded, and the final approach distinguishes consensus from open
  disagreement. Review throughout implementation, then consolidate the result.

  **Completed locally September 12 for the implemented scope through `d3acc8f`:**
  the [review coverage map](history/2026-09-12-final-review-coverage.md) connects
  the implemented slices to their independent certificates. It identified and
  closed two remaining historical review gaps: the
  [configuration/catalog cleanup](history/2026-09-12-configuration-catalog-cleanup-review.md)
  and the [installed experiment loop](history/2026-09-12-installed-experiment-loop-review.md).
  The former also independently approves the typed-fixture cleanup. The
  [native regression integration](history/2026-09-12-native-regression-integration-review.md)
  and [final cache/profile cleanup](history/2026-09-12-final-profile-cache-review.md)
  have separate approvals. No material finding remains unresolved in these slices.

  Architecture consensus keeps dataset meaning, retained evidence and reuse in
  DocSpec; native Dagster owns managed execution, and shared libraries own their
  existing publication/encoding functions. The
  [recipe scope review](history/2026-09-12-native-recipe-scope-review.md) defers a
  generic host until a concrete caller demonstrates a missing rule. Search SC04
  now records native composition first in its destination repo. The measured
  shared-writer gap remains an explicit upstream dependency rather than a local
  workaround.

  The final production changes passed **1,078 tests**, with one live integration
  deselected, in 247.07 seconds and no warnings. A subsequent test-only
  mixed-cache extension passed both affected lifecycle tests in 1.70 seconds;
  production code did not change after the full run. Ruff, the dependency lock
  and diff checks passed. Each review distinguishes static analysis from these
  parent-executed results. Earlier progress notes describe their own revisions;
  this consolidation is the current review status.

  This completes review of implemented DocSpec work. D37's capacity measurements,
  D40's unfamiliar-human exercise, and conditional destination integrations keep
  their own acceptance criteria. No remote CI, publication, deployment or live
  provider reliability is claimed.

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

- [x] **D41 · P1 · Inventory DocSpec's reusable source dependencies.** Name
  DocSpec callers, exact symbols/files, required public capabilities, and the
  copies or repeated maintenance steps that sharing would remove. **Done when:**
  each local candidate has a selected provider dependency and removal target,
  or a documented reason to stay separate. Package placement can remain open.
  Connect the inventory to D12/D31/D34. Provider inventory and handoffs live in
  [SpicyDocs S25](../../spicy-docs/docs/simplification-todo.md#s25) and
  [SpicyRegs SR01](../../spicy-regs/PLAN.md#sr01); recording them here is not their
  implementation task.

  **Completed September 12 at `03df308`, updated after D42:** the caller inventory
  below selects existing owners. D42 records the CourtListener implementation
  and removal; completing an inventory alone does not establish adoption.

  | Current DocSpec caller | Reuse or keep decision |
  | --- | --- |
  | [`SpicyDocsSourceNativeAdapter`](../src/docspec/adapters/spicy_docs_source_native.py), `spicy_docs_source_profile` | Already consume public `source_native` and `source_native_profiles` for admitted records, renditions, outcomes, bounded evidence and failures. D43/D46 qualify the current wheel; no local source reader or replay engine is needed. |
  | [`BillContentFetcher`](../examples/govinfo_bill_fetcher.py), [`run_example`](../examples/govinfo_bills.py) | D12/D44/D53 use `BillAcquirer`, `BillAcquisitionBudget` and `select_bill_xml` from the same wheel. Provider owns URLs, parsing, identity and transport; the example owns the selected version, evidence destination and DocSpec mapping. |
  | [`courtlistener_bulk_source`](../tools/courtlistener_bulk_source.py): `parse_capture`, `build_source_items`, `coverage_for` | D42 now consumes SpicyDocs `sources.courtlistener_listing.BulkObject` and `parse_listing_page`. The copied parser, object class and filename/media-type rules are removed. DocSpec retains input pins, page-set consistency, dataset selection and coverage assertions; publisher values remain exact. |
  | [`FederalRegisterCatalogPolicy`](../src/docspec/application/federal_register_catalog.py), [`RegulationsGovCatalogPolicy`](../src/docspec/application/regulations_gov_catalog/policy.py) and its [`records`](../src/docspec/application/regulations_gov_catalog/records.py) | Keep catalog normalization, joins, source-path evidence, rendition preference, sampling and disposition policy here. Withholding reason codes and test-fixture exclusions are explicit dataset decisions over retained literal fields. The selected provider has no equivalent DocSpec policy API; moving these classes would couple it to the dataset model. |
  | [`fr_topic_receipt.fetch_live`](../tools/fr_topic_receipt.py), [`fetch_attachment_sample`](../tools/fetch_attachment_sample.py) | Keep these targeted research questions separate from production acquisition. Their exact topic projection and direct Regulations.gov API/attachment probing are not supplied by the current public provider API. Do not add a second general provider framework to share them. Retire or replace the study-specific code with its workflow under D34 when superseded. |
  | Generic catalog/artifact admission and physical blob storage | Use Rulespec Artifacts, not the source provider, for shared encoding and containers. D28/D29 already consume those APIs. D31 records the exact concurrency/root-pinning gap that prevents replacing the remaining local blob writer safely. |

  No DocSpec experiment currently calls the Federal Register/GovInfo body
  resolver or MODS route directly. SpicyDocs already owns those APIs; select and
  qualify one only when a dataset needs it, without copying its URL or XML rules.
  The concrete bill route satisfies D12 without introducing an unused second
  integration. GAO topics and SpicyRegs public tables retain their named D51/D52
  consumer tasks.

<a id="d42"></a>

- [x] **D42 · P2 · Replace DocSpec's copied publisher rules with provider APIs.**
  Separate publisher identity, enumeration, literal fields, and listing grammar
  from DocSpec's dataset selection and normalization policy. **Done when:** local
  callers use the chosen installed provider API, exact values/evidence and
  pagination/refusal behavior survive, and superseded local rules are removed.
  Source implementation belongs to [SpicyDocs S14](../../spicy-docs/docs/simplification-todo.md#s14),
  [S25–S26](../../spicy-docs/docs/simplification-todo.md#s25), or selected
  [SpicyRegs SR03](../../spicy-regs/PLAN.md#sr03) handoffs. A provider move remains
  optional. Depends on D41.

  **Completed September 12 for D41's selected candidates:** the CourtListener
  tool now uses the same installed SpicyDocs `0.3.0` wheel as the reader and bill
  example. Removing its duplicate parser and filename rules cuts the tool by
  **153 lines**. DocSpec retains its capture-pin, page consistency, selection and
  coverage behavior. New catalog versions preserve exact ETag strings, and URLs
  use provider escaping; the old normalization has no fallback path. The focused
  gate passed **39 tests**, with one live test deselected, in 21.10 seconds.
  This includes the retained 1,076-object listing, catalog publication, malformed
  source refusals, and isolated packages. See the
  [decision](cleanup-decisions.md#require-the-current-installed-source-reader).

<a id="d43"></a>

- [x] **D43 · P1 · Consume public source releases and outcomes.** Adapt DocSpec
  to supported provider access for profiles, pinned descriptions, records,
  renditions, outcomes, and bounded evidence/failure inspection. **Done when:**
  D06/D08/D10 use these APIs without implementation imports or internal ledgers.
  Keep ordinary opening bounded and full source replay with its producer.
  Provider API work is tracked in [SpicyDocs S01](../../spicy-docs/docs/simplification-todo.md#s01)
  and [S26](../../spicy-docs/docs/simplification-todo.md#s26); retained public-table
  facts/API work is in [SpicyRegs SR02](../../spicy-regs/PLAN.md#sr02). Reuse the
  current public reader instead of inventing another solely to rename a package.

  **Completed September 12, reusing D06/D08/D10:** the optional
  `SpicyDocsSourceNativeAdapter` calls the installed public source reader and
  profile API. It retains exact admitted descriptions and delegates records,
  renditions, per-record observations, bounded failures and evidence reads.
  The provider owns traversal and source-release admission; DocSpec adds no
  source-ledger parser or full replay. Missing outcome APIs refuse directly.
  The installed-wheel source test covers current source kinds, empty/partial/
  total outcomes, evidence bounds, source refusals and independent admission.
  It passed in the 1,071-test final regression for the export retirement.
  See [source-outcome review](history/2026-09-11-source-outcomes-review.md) and
  [catalog inputs](catalog-inputs.md). D38/D46 now qualify successful provider-body
  processing; D45 supplies optional dependency packaging. Reusing the completed
  public-reader work closes D43 without new code.

<a id="d44"></a>

- [x] **D44 · P2 · Adapt provider acquisition to DocSpec fetcher injection.**
  Where an experiment needs a publisher-specific route, connect the installed
  provider's locators, identity checks, and bounded acquisition to DocSpec's
  fetcher interface. **Done when:** D11/D12 use a thin adapter without copied
  source rules, circular imports, or another HTTP/storage framework. Keep
  selection, effective budgets, and experiment state here. Provider wheel and
  route implementation lives in [SpicyDocs S19](../../spicy-docs/docs/simplification-todo.md#s19).
  Depends on D41.

  **Completed September 12 for the selected bill route:** D53's
  `BillContentFetcher` delegates selection checks and bounded acquisition to
  the installed provider. It maps the result into `FetchStream` and saves source
  facts in caller-owned evidence files. The example uses ordinary experiment
  APIs and retains the existing catalog, capture, and processing ownership.
  Provider parsing, HTTP, retries and storage frameworks were not copied.

<a id="d45"></a>

- [x] **D45 · P1 · Package DocSpec's public APIs and optional provider integration.**
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

  **Completed September 12:** DocSpec `0.3.0` exposes the selected public APIs
  and adds the optional `docspec[spicy-docs]` reader. Core dependencies are
  unchanged; the optional extra selects SpicyDocs `0.2.0` without acquisition,
  analytics, PDF, or Dagster packages. Installation and tests share the same
  provider wheel in `vendor/`. The package gate passed 12 tests, including
  isolated core and provider installations. A separate clean installation of
  the built release passed dependency and public-import checks. The
  [wheel qualification](history/2026-09-12-wheel-qualification.md) records exact
  build and dependency identities. This is a locally built and qualified wheel,
  not an external registry publication. Final independent review is consolidated in D39.

  **Current dependency, September 12:** D53 advances the single optional provider
  wheel to SpicyDocs `0.3.0`. Source reading and bill acquisition share
  [one manifest](../vendor/spicy_docs.json); the old wheel and separate bill-test
  wheel are removed. The provider's acquisition extra is required only for the
  bill example. The core wheel still installs without SpicyDocs.

<a id="d46"></a>

- [x] **D46 · P1 · Prove the wheel handoff, then remove replaced code.** Run a
  small captured-source fixture through the installed provider and DocSpec:
  read outcomes, build the catalog, acquire a selected document through the
  supplied fetcher where needed, and process retained content. **Done when:**
  packaged schemas/resources are present, evidence and failures survive, missing
  or unsupported packages fail clearly, and no repository-relative imports are
  required. Then remove superseded copies and update pins/docs. Reuse D10/D38
  checks; if the provider later moves to SpicyRegs, rerun this same handoff and
  switch current imports directly without a legacy fallback chain. Depends on
  D42–D45 for the capabilities actually selected.

  **Completed September 12 for the current reader and bill fetcher:** the
  combined installed-wheel gate passed **24 tests in 22.43 seconds**. It covers
  source outcomes/evidence, catalog building and admission, successful body
  processing, explicit bill selection, source refusals, and later processing
  after closing the source client. The same provider wheel serves both paths;
  core imports remain provider-independent. D42 separately owns adoption and
  removal of the older CourtListener listing parser. This qualification does
  not claim live availability or collection-wide coverage.

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
  Compose the supplied computation with native Dagster assets/jobs and injected
  resources. Reuse DocSpec's public catalog readers and retained input references,
  plus Rulespec's existing artifact publication. Accept pinned catalog/layer/
  resource inputs and named output partitions without inventing a document
  capture or segment. Dagster owns execution attempts, retries, cancellation and
  run storage; do not add a recipe registry, scheduler or second run ledger.
  **Done when:** an ordinary retained-input experiment and a metadata-only build
  expose consistent input/result evidence and actual reuse accounting. Global
  census and lookup dependencies invalidate affected outputs correctly. State
  whether resume reuses whole completed builds or completed partitions; do not
  promise the latter without implementing it. Keep worker messages small
  references. Add a DocSpec helper only for behavior the real caller needs that
  these existing APIs do not supply. Depends on D02–D04, D15 and D21.

  **Scope review, September 12:** the current document adapter accepts real
  `StoreTask` work; arbitrary dataset computation can use Dagster's own jobs and
  resources directly. Defer this qualification until Search supplies its recipe
  and pinned inputs/outputs. Do not create a generic host or dataset-stage API
  to satisfy a circular planning prerequisite. Only an observed public-reader,
  publication or evidence gap justifies a DocSpec change.

<a id="d49"></a>

- [ ] **D49 · P1 · Qualify native Dagster composition with an installed search recipe.**
  Use the supplied recipe to check D48's generic input/output references, full
  requested catalog population (including records not selected for body capture),
  global dependencies, and result reuse. **Done when:** the native job uses
  DocSpec's public readers and retains inputs for a later attempt, with search
  policy owned by its recipe. Record execution identity separately from the recipe's semantic
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

- [x] **D51 · P1 · Demonstrate GAO topic preservation in a dataset example.**
  Carry a literal topic from a small retained publisher page through a catalog
  and a supported result or processor. An exact topic filter is sufficient;
  search publication is optional. **Done when:** matching, missing, and unexpected
  topic cases retain provenance and demonstrate selection or analysis without
  inferring requirements from a label. Add only fields/byte access this example
  needs. Provider fixture/field work lives in
  [SpicyDocs S17](../../spicy-docs/docs/simplification-todo.md#s17). Depends on
  D06–D08 and applicable public processor APIs; report offline versus live use.

  **Completed September 12:** the [GAO topic example](gao-topics.md) reads the
  current provider's admitted fields and evidence, maps them through existing
  supplied-record APIs, and filters the resulting catalog by exact label.
  Matching and unexpected topics preserve their literal values and original
  HTML references. Missing-topic input retains the provider refusal and creates
  no catalog. Repeated filters leave the catalog unchanged; absent report
  attachments remain absent. No core schema, processor type or runtime was added.

  The focused gate passed **20 tests in 23.69 seconds**, including the same seven
  GAO behavior checks against installed packages before HTTP support is installed,
  the command-line example, and the existing bill/provider package checks. The
  source fixtures come unchanged from SpicyDocs `8e485fe`. GAO and bill examples
  share the existing provider-identity helper; its function body is unchanged.
  This qualifies synthetic offline inputs, not live availability or full GAO
  coverage. Independent review is consolidated in D39.

<a id="d52"></a>

- [x] **D52 · P1 · Build a catalog from retained SpicyRegs public-comment data.**
  Implement the DocSpec adapter/example over a bounded retained table input.
  Preserve exact input identity, field provenance, coverage assumptions, and
  distinctions from other Regulations.gov representations. **Done when:** the
  installed workflow builds and reads the catalog, reports unavailable fields
  and rejected rows, and exposes supported document candidates before optional
  fetching or processing. Reuse SpicyRegs data without rebuilding its publication
  pipeline. Provider requirements live in [SpicyRegs SR02](../../spicy-regs/PLAN.md#sr02)
  and applicable source coverage in [SpicyDocs S09–S10](../../spicy-docs/docs/simplification-todo.md#s09).
  Depends on D06–D08/D43 and the selected public input API; no package move is required.

  **Completed September 12:** the [comment-table example](spicyregs-comments.md)
  uses the existing pinned SpicyDocs `0.3.0` public profile, retained Parquet
  evidence and bounded supplied-record policy. It preserves all 16 logical
  fields, nulls, diagnostics, original input pins and provider-declared attachment
  locations. Exact docket filtering and catalog preview create no document run
  or attachment capture. A missing comment identity refuses the whole source
  without fabricating a partial catalog or row-rejection ledger.
  The same five behavior tests run locally and outside the checkout against
  installed wheels; the focused gate passed **7 tests** in 13.58 seconds. Core
  and reader dependency-absence checks run before installing the provider's
  optional Parquet extra. Ruff and lock consistency passed.
  [Independent review](history/2026-09-12-spicyregs-comments-review.md) approved
  the workflow. Its observed upstream attachment-index limitation is tracked in
  [SpicyDocs P01](../../spicy-docs/docs/simplification-todo.md#public-comment-attachment-provenance);
  DocSpec preserves provider declarations and adds no local parser correction.
  This qualifies the bounded synthetic example, not live coverage or capacity.

<a id="d53"></a>

- [x] **D53 · P1 · Demonstrate an explicit GovInfo bill XML experiment.**
  Consume an installed SpicyDocs wheel, retain BILLSTATUS and every offered text
  version/format URL, select one package explicitly, and build a supplied-record
  catalog. Inject the provider fetcher, retain exact XML, and run a changed
  processor against those bytes after closing acquisition. **Delivered locally:**
  [the example and guide](govinfo-bill-example.md) implement this composition;
  [installed-wheel qualification](../tests/test_govinfo_bill_installed_wheel.py)
  checks offline reuse, source spans, selection, refusals, and provider identity.
  Qualified with SpicyDocs 0.3.0 from source `8e485fe`, pinned in the
  [wheel manifest](../vendor/spicy_docs.json).
  The originating worktree passed **11 installed-wheel and package-boundary
  checks**, its documented offline command, and changed-file Ruff. Its independent
  semi-formal review approved after distinguishing acquisition start from response
  observation. Integration reuses one provider wheel for bill acquisition and
  source-release reading; current-branch qualification is recorded below. This
  example does not complete GAO/public-comment work, collection-wide acquisition,
  or legal interpretation.

  Current-branch installed qualification passed **24 tests in 22.43 seconds**,
  including wrong-bill and placeholder refusals. The original
  [independent review](history/2026-09-12-govinfo-bill-handoff-review.md) covers
  the imported example at `4df1b44`; its provenance note distinguishes these
  later integration checks from that review.
  The current documented command also passed: two initial phrase matches,
  three after changing the phrase resource, and zero new captures in the later
  run. This is an offline fixture qualification, not a live publisher claim.

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
