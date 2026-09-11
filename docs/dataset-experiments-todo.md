# Dataset experimentation and simplification to-do list

DocSpec should make it easy to build a catalog, fetch selected documents, retain
them, run chosen processors now or later, and compare iterations while reusing
unchanged work. This checklist organizes the next changes around that outcome.
Across DocSpec and its source provider, the goal is to maintain each shared
capability once and reuse it through installed packages, reducing duplicate
implementation, testing, configuration, and documentation effort.

**Status: 0 of 50 items complete.** Compiled on 2026-09-11 against merged revision
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

- [ ] **D01 · P0 · Align the product explanation and current behavior.** Update
  README, architecture, and decision status around catalog building, capture,
  optional processing, iteration, and optional export. Explain what is available
  through supported entry points versus only through internal composition.
  **Done when:** a reader can identify inputs, outputs, useful stopping points,
  and present gaps without reconstructing previous discussions. Correct README's
  claim that no separate portable structural path exists.

- [ ] **D02 · P0 · Define dataset, run, and attempt clearly.** Decide how a
  persistent dataset relates to catalog versions, acquisition work, processing
  attempts, and a selected base. Reuse existing plans, references, stores, and
  receipts before adding persistent models. **Done when:** processing retained
  inputs later creates an attributable attempt; alternatives can share a base
  without overwriting one another; “resume this attempt” and “start another
  experiment” have distinct, documented behavior. Include a dependency sketch.

- [ ] **D03 · P0 · Provide one simple runtime configuration.** Replace the
  ordinary user's eight storage roots and six profile documents with a workspace
  location, useful defaults, explicit limits, and selected implementations. Keep
  advanced overrides where they serve an actual backend. **Done when:** an
  installed example runs from a small configuration, checks invalid settings
  before work, and records the effective configuration needed to reproduce it.
  Depends on D02; see [request composition](../src/docspec/cli/requests.py).

- [ ] **D04 · P0 · Expose the lifecycle through a supported application API.**
  Connect existing services behind clear operations for catalog construction,
  capture, processing retained inputs, resume, inspection, and export. Compose
  the CLI and executors through that same supported path. **Done when:** a caller
  can fetch and process together, fetch first and process later, or work with a
  catalog alone without manually assembling every internal service. Avoid a
  wrapper that only renames calls without simplifying their use. Depends on D02–D03.

- [ ] **D05 · P0 · Establish a small reference experiment.** Extend the offline
  walkthrough with several documents, a selected exclusion, a recoverable failure,
  and a useful processor. Build it through D04 as a running example while the
  remaining items land. **Done when:** the installed package demonstrates initial
  capture and a later processing attempt, with understandable output and explicit
  remaining limitations. D38 supplies the complete acceptance exercise.

## 2. Build catalogs and acquire inputs through clear interfaces

- [ ] **D06 · P0 · Support provider inputs and caller-supplied records.** Make the
  source interface usable for SpicyDocs, SpicyRegs-derived data, another source,
  and bounded local records. Define the required source identity and
  provenance for supplied records without inventing acquisition evidence.
  **Done when:** one provider example and one local-record example build catalogs
  through public interfaces without private imports or sibling checkouts. Reuse
  [source ports](../src/docspec/ports/source_catalog.py). SpicyDocs S17/S18 track
  the specific GAO-topic and retained public-comment-table examples; they extend
  this interface without requiring a provider package move or blocking the
  initial local example.

- [ ] **D07 · P0 · Make catalog iteration and selection inspectable.** Expose
  previewable additions, changes, exclusions, selected candidates, and reasons
  before acquisition. Decide how a dataset accepts successive or multiple source
  inputs, including source-qualified identity and collisions. **Done when:** an
  operator can explain a selection change and grow a dataset without silently
  conflating records or refetching unchanged selections. Depends on D02 and D06;
  use the existing catalog policy and succession machinery.

- [ ] **D08 · P1 · Carry source collection outcomes into the dataset.** With
  SpicyDocs, expose requested scope and existing acquisition counts/failures
  through its public reader, then retain those facts in DocSpec's source
  description. DocSpec owns the explicit policy for accepting partial input.
  **Done when:** empty observations, rejected records, unresolved collection, and
  successful input remain distinguishable through ordinary APIs. Preserve count
  units and source evidence; upstream failures are distinct from failures of
  selected document processing. Coordinates with SpicyDocs S01–S02 and S09.

- [ ] **D09 · P1 · Add public catalog admission and bounded row access.** Expose
  an admitted, pinned catalog through supported object and validated mapping
  access as needed by current consumers. Keep full producer re-derivation separate
  from ordinary opening and bind access to the artifact actually checked.
  **Done when:** the SpicySearch catalog integration can use public APIs without
  private row readers or repeated full derivation, while changed or invalid
  artifacts are refused. See [the facade](../src/docspec/source_catalog.py) and
  [the recorded consumer request](history/2026-09-05-reader-api-requests.md).

- [ ] **D10 · P1 · Qualify the current installed source integration.** Coordinate
  source schema, policy, receipt fields, and producer-label acceptance with the
  actual SpicyDocs release being tested. Replace the old integration wheel pin
  deliberately and retire historical acceptance branches where unnecessary.
  **Done when:** an isolated installed-package check proves success, partial-input
  handling, and clear refusal of unsupported inputs against the chosen current
  producer. No automatic migration layer or SpicyRegs code move is required.
  Depends on D08; coordinates with SpicyDocs S05.

- [ ] **D11 · P0 · Make fetcher injection practical.** Expose explicit selection
  and composition of local, HTTPS, S3, and caller-provided fetchers through the
  supported run API. Keep credentials outside retained configuration and isolate
  optional dependencies. **Done when:** the reference experiment swaps fetchers
  without editing application internals; routing errors are clear; fetched bytes,
  source identity, limits, and outcomes are retained. See
  [existing fetchers](../src/docspec/adapters/content_fetchers/).

- [ ] **D12 · P1 · Connect source-specific body validation when a route needs it.**
  For a selected Federal Register/GovInfo route, assess and reuse SpicyDocs'
  existing identity and soft-404 checks alongside bounded transport and capture.
  DocSpec still chooses the candidate. **Done when:** a demonstrated route accepts
  the intended document and explains wrong-document or placeholder refusals while
  retaining needed evidence. If no current experiment needs this route, record
  the deferral. Do not move source code into SpicyRegs to complete this item.
  Depends on D11; coordinates with SpicyDocs S19.

## 3. Make processing repeatable, replaceable, and useful

- [ ] **D13 · P0 · Expose processor, extractor, and segmenter injection.** Provide
  explicit composition of chosen implementations through D04, with convenient
  defaults. Validate that the declared plan and actual implementations match.
  **Done when:** a contributor runs a custom processor and replaces extraction or
  segmentation without editing the CLI's internals. Preserve dependency ordering,
  result validation, and optional imports. An unrestricted dynamic plugin loader
  is not needed to satisfy this outcome. See [extensions](extensions.md).

- [ ] **D14 · P0 · Demonstrate a meaningful optional processor.** Add a small
  example that produces inspectable results beyond byte/word statistics. A
  processor using pinned RefSpec resources is a suitable candidate; choose the
  actual use and resource before committing to an integration. **Done when:**
  users can inspect results and supporting evidence, change configuration or the
  resource pin, and run a new attempt. Domain interpretation stays in the
  processor; local fixtures are clearly distinguished from live-resource proof.
  Depends on D13.

- [ ] **D15 · P0 · Prove selective reuse across experiments.** Trace which source
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

- [ ] **D16 · P0 · Repair only the work that needs another attempt.** Carry final
  failure classification and completed-stage evidence into planning. Retry
  temporary failures under explicit policy; retry unchanged deterministic
  failures when selected or their relevant inputs change. **Done when:** a failed
  processor can be repaired without refetching valid content, repeated permanent
  failures do not loop on every unchanged successor, and retained failures remain
  visible. See [planner](../src/docspec/application/planner.py); depends on D15.

- [ ] **D17 · P0 · Make extracted representations an explicit choice.** Connect
  existing markup, visible-text, PDF, and segmentation capabilities to the normal
  workflow with clear supported defaults. Preserve exact captures independently
  of derived text. **Done when:** an experiment can choose a supported
  representation and inspect its source coordinates; HTML/XML markup retention,
  PDF extraction limits, and image handling are accurately described. Neither
  OCR nor every format must be implemented to close this item. Depends on D13.

- [ ] **D18 · P1 · Replace weak quality proxies with justified checks.** Review
  portable retention floors and other text-quality gates against concrete bad
  extractions. Separate observed extraction quality from the user's policy for
  continuing an experiment or exporting results. **Done when:** useful checks
  distinguish empty, truncated, and misleading extraction for supported formats;
  removed thresholds have a recorded rationale and replacement evidence. Preserve
  failure accounting; reduced validation must not silently imply completeness.
  Depends on D17 and informs D26.

- [ ] **D19 · P0 · Let users inspect and compare attempts.** Provide bounded
  summaries and result access for selection, capture, extraction, processor
  outputs, failures, reuse, and relevant costs. Compare attempts by stable input
  identity and effective configuration; expose detailed evidence on demand.
  **Done when:** users can answer what changed, what reran, what failed, and why
  two results differ without reading internal files. A useful CLI/API is enough;
  a dashboard is optional. Depends on D02 and D15; incorporate D08's source
  outcomes when available and label unavailable source facts explicitly.

## 4. Keep execution reliable while reducing repeated machinery

- [ ] **D20 · P0 · Prove pause, interruption, and resume for the new workflow.**
  Exercise capture, extraction, processing, delivery, and final state updates.
  Reuse checkpoints after verification and restore cumulative budgets.
  **Done when:** interrupted work resumes without accepting partial output,
  double-counting work, or repeating verified stages; cancellation actually
  reaches active work where the backend promises it. Reuse the existing recovery
  tests and add only missing cases introduced by D04.

- [ ] **D21 · P1 · Complete the Dagster composition of the same workflow.** Keep
  Dagster optional and reuse the same plans, injected components, execution
  services, and result checks. **Done when:** a documented installed example
  demonstrates dispatch, interruption/retry, and result reconciliation with the
  same logical results as local execution, including processing retained inputs.
  State backend-specific limits; avoid a second dataset state model. Depends on
  D04, D13, and D20; see [the adapter](../src/docspec/adapters/dagster.py).

- [ ] **D22 · P2 · Decide whether to replace the separate acquisition campaign runner.**
  Adapt the existing task model only as needed to prove one SpicyDocs acquisition
  task through DocSpec before deleting a runner. **Done when:** either a smaller
  replacement preserves source ordering, subprocess cancellation, locks, bounded
  resources, pinned receipts, and stale-resume refusal and the superseded runner
  is removed,
  or a reasoned deferral names the unmet need. Keep source acquisition functions
  usable independently. This is separate from moving them into SpicyRegs.
  Depends on D20–D21; coordinates with SpicyDocs S20–S21.

- [ ] **D23 · P1 · Assign retries and outcome accounting to explicit owners.**
  Review nested transport, processor, and scheduler retry loops; give each layer
  a bounded purpose under one effective run policy. **Done when:** repeated
  attempts consume the expected budget and produce attributable outcomes.
  Acquisition observations, selected documents, scheduled tasks, and exported
  text retain their distinct counts and meanings. Share common mechanics where
  useful without forcing every failure into one universal ledger. Depends on
  D08, D16, and D20; informs D22.

- [ ] **D24 · P0 · Simplify durable state updates and finalization.** Trace which
  current commit, reconciliation, and publication steps are needed at each useful
  stopping point. Reuse existing verified transitions behind D04. **Done when:**
  a usable capture or processing attempt can be retained without mandatory
  portable export; incomplete state is recognizable; immutable writes and
  concurrent base checks prevent silent replacement of another result. Depends
  on D02 and D20; see [commit](../src/docspec/application/commit.py).

- [ ] **D25 · P1 · Make retention safe for shared experiment inputs.** Recheck
  maintenance and deletion reachability against multiple attempts and shared
  bases. Expose a preview of what would be retained or removed through existing
  maintenance mechanisms. **Done when:** keeping an attempt preserves all inputs
  and evidence it needs; pruning an obsolete attempt cannot damage another;
  interrupted cleanup is recoverable. This item does not authorize deleting
  existing user datasets. Depends on D02 and D24; see
  [maintenance](../src/docspec/application/maintenance.py).

## 5. Export and consume results without a second production pipeline

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

- [ ] **D27 · P1 · Give generic artifact structure one implementation owner.**
  Compare the portable builder/verifier with the existing Rulespec container.
  Delegate generic membership, byte integrity, and structural checks when they
  meet the required behavior; retain DocSpec's document semantics and coverage
  checks. **Done when:** D26 uses one supported container path, duplicate
  structural machinery is removed, and any retained difference has a concrete
  reason. Coordinate current readers when changing the format. Depends on D26's
  design and D28; see [artifact adapter](../src/docspec/adapters/platform_artifact.py).

- [ ] **D28 · P1 · Resolve canonical encoding before sharing identity code.**
  Compare DocSpec's arbitrary-integer and Unicode ordering rules with Rulespec's
  safe-integer and UTF-16 ordering rules. Make one decision with SpicyDocs S30:
  establish values required by supported sources/processors, then choose an
  exact shared representation and one emission implementation. Start with
  Rulespec's supported rules; an optimization test alone does not establish a
  need to widen them. Preserve DocSpec's useful domain conversion and validation.
  **Done when:** shared fixtures cover number boundaries, Unicode key ordering,
  duplicate keys, and domain conversion; required source values survive exactly;
  current producers/consumers agree on changed identities and schema pins; and
  the replaced emitter is removed without a compatibility mode. Do not round
  values or indiscriminately stringify them. If required value domains prevent
  convergence, record the evidence and defer the shared-emitter change in both
  lists; retaining separate encoders does not complete it. See
  [identity](../src/docspec/domain/identity.py).

- [ ] **D29 · P1 · Provide public document-result admission and reading.** Expose
  a supported, bounded reader for D26's exports and connect the named SpicySearch
  consumer through installed packages. Return verifiable identity information
  instead of requiring manual transcription of a verification claim.
  **Done when:** a fresh exported dataset is admitted and read through public
  APIs, tampering is refused, and evidence resolves to retained inputs. Update
  the consumer directly; no legacy import shim is required. Depends on D26–D28.

- [ ] **D30 · P2 · Retire tools only after their current purpose is replaced.**
  Reassess the historical release builder, sample tools, and fixture restamper
  after D26. Integrate reusable production behavior into its package owner;
  retain necessary research or reproduction tools with explicit scope.
  **Done when:** the [tool inventory](../tools/README.md) identifies one supported
  route per task and genuinely superseded implementations are removed with
  provenance preserved in Git. A normal export does not automatically replace
  a historical reproduction recipe or fixture generator.

## 6. Simplify code ownership and contributor effort

- [ ] **D31 · P2 · Resolve the remaining cross-repository duplication candidates.**
  Recheck CourtListener listing grammar, blob writes, and generic publication
  helpers against actual callers. Directory publication already shares a
  Rulespec helper; blob writers differ on known versus computed digests, reuse,
  and returned evidence. **Done when:** each candidate has one implementation
  where that reduces total code, or a documented reason to remain separate.
  Avoid a new shared package without a demonstrated saving. Defer changes whose
  only value is the future SpicyRegs move; coordinates with SpicyDocs S14/S22.
  Keep dataset capture and transactions in DocSpec. Choose the shared physical
  writer's owner around independent source callers, using suitable existing
  primitives where possible; do not make provider storage depend on DocSpec's
  lifecycle solely to share a writer. Record justified deferrals in both lists.
  Include the compiled schema-gate mechanics shared with SpicySearch. Reuse
  generic validation without merging product-specific schemas or error meaning;
  D47 handles the search/engine dataset definitions separately.

- [ ] **D32 · P1 · Remove configuration that repeats facts or promises no enforcement.**
  Review role/profile declarations, governance identifiers, provider metadata,
  and optional cache settings after D03 and D13. Derive facts from selected
  implementations where possible; keep output-affecting identities and enforced
  limits explicit. **Done when:** users supply only meaningful choices,
  declarations match actual runtime behavior, and each retained abstraction
  serves the experiment workflow. Lack of today's CLI caller alone does not
  make processor dependencies or resource pins dead code.

- [ ] **D33 · P2 · Review current file and function outliers by responsibility.**
  Reassess catalog CLI coordination, checkpoint verification, base reprocessing,
  delivery indexing, and execution control flow after workflow changes settle.
  Review large schemas and segmentation algorithms separately. **Done when:**
  long functions expose understandable steps, shared rules have clear owners,
  and any split reduces the context needed for a change. Follow the existing
  length review guidelines; avoid chains of tiny wrappers or shared-state mixins.

- [ ] **D34 · P2 · Remove superseded paths, declarations, and dependencies.**
  Search source, tools, tests, exports, registrations, profile strings, and known
  installed consumers after each replacement. Recheck obsolete source-policy
  declarations with the SpicyDocs owner: tagging eligibility and processor
  selection are downstream choices, not publisher facts. **Done when:** each
  removed path has a named replacement or an explicit retirement decision,
  unused dependencies leave packaging, and dynamic hooks remain functional.
  Move declarations only to an actual consumer; coordinates with SpicyDocs S11–S16.

- [ ] **D35 · P2 · Simplify tests around observable behavior.** Reuse the prior
  refactor's focused suites and support helpers. Retire assertions for deliberately
  removed behavior; reduce redundant tests that only mirror implementation.
  **Done when:** meaningful checks cover evidence, reuse, failures, bounded work,
  and public interfaces without making internal moves expensive. Keep selectors
  and installed-package probes current. Do not add tests solely to justify small,
  reversible file moves or deletions.

- [ ] **D36 · P1 · Keep one contributor path through the new workflow.** Update
  the walkthrough, extension guide, task-to-code map, operations guide, schema
  instructions, and decision index as their corresponding changes land.
  **Done when:** a contributor can add a source adapter, fetcher, or processor;
  run a later attempt; inspect results; and find the relevant checks using
  maintained documentation. Retire superseded instructions and keep historical
  measurements labeled with their revisions. Depends on the interfaces above.

## 7. Verify user value and qualify the claims we keep

- [ ] **D37 · P1 · Separate regression gates from capacity and conformance claims.**
  Map the nine previously partial qualification requirements to the clarified
  product scope. Keep and implement useful requirements; explicitly revise or
  retire unjustified ones. Choose representative workload sizes instead of
  treating the entire 100k/1m/5m ladder as an automatic prerequisite for every
  contribution. **Done when:** ordinary checks are clear, retained capacity
  claims have pinned resource/recovery evidence, and missing qualification stays
  visible. Changing the scope cannot be reported as passing an unrun check.
  See [the recorded conformance report](history/2026-09-11-maintainability-conformance.json).

- [ ] **D38 · P0 · Prove the complete experiment loop through installed packages.**
  Starting from the reference experiment, build a provider catalog, fetch once,
  run a meaningful processor, change its configuration or resource pin, and
  process retained inputs again. Add input, target failed work, interrupt/resume,
  and compare with a clean rebuild. **Done when:** observed calls prove selective
  reuse, results and gaps are inspectable, and ordinary use needs no private
  imports, ad hoc file editing, or sibling checkout. Keep export/Dagster checks
  in D29/D21 so they do not block useful local experiments. Depends on D05–D07,
  D11, D13–D17, D19–D20, and D24.

- [ ] **D39 · P1 · Obtain independent architecture and code review of the implementation.**
  Give reviewers the clarified purpose, changed interfaces, and acceptance
  evidence. Use a solutions architect for judgment and independent semi-formal
  code reviews for implemented changes. **Done when:** findings on ownership,
  unnecessary complexity, correctness, and evidence gaps are resolved or
  explicitly recorded, and the final approach distinguishes consensus from open
  disagreement. Review throughout implementation, then consolidate the result.

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

- [ ] **D41 · P1 · Choose shared implementations for repeated work.**
  For the candidates above, name current callers, exact symbols/files, intended
  public capability, and the duplication or contributor burden removed.
  **Done when:** each real overlap has a selected implementation to reuse and a
  named copy or duplicate maintenance step to remove; differences that justify
  separate code are recorded. Package placement can remain explicitly undecided.
  Connect this inventory to D12/D31/D34 instead of creating a competing review.
  Owners: DocSpec and the source-provider maintainers.

- [ ] **D42 · P2 · Consolidate source rules behind the provider's public API.**
  Address demonstrated overlap in publisher identity, enumeration, raw field
  interpretation, and listing grammar. Split those facts from DocSpec's dataset
  selection and normalization policy. **Done when:** DocSpec can reuse the chosen
  rules through an installed wheel, with exact values, source evidence, strict
  pagination/completeness checks, and refusal cases preserved. Reuse the existing
  SpicyDocs owner where it already suffices; moving it to SpicyRegs is optional.
  Depends on D41; owners: source provider with DocSpec adapter changes.

- [ ] **D43 · P1 · Expose source releases and outcomes through the provider wheel.**
  Define or confirm supported access to profiles, pinned source descriptions,
  records, renditions, outcomes, and bounded evidence/failure inspection. Keep
  full source replay with its producer and ordinary reader admission bounded.
  **Done when:** D06/D08/D10 use these public capabilities without importing
  implementation modules or opening internal ledgers. Build on today's
  `spicy_docs.source_native` API; do not create a second reader solely to rename
  its package. Owners: source provider and DocSpec.

- [ ] **D44 · P2 · Package source-aware acquisition for fetcher injection.**
  Where an experiment needs a publisher-specific route, expose its locators,
  identity checks, and bounded acquisition through the provider wheel. A thin
  DocSpec adapter connects it to the fetcher interface. **Done when:** D11/D12 can
  use the selected provider without copied source rules, circular imports, or a
  second generic HTTP/storage framework. The provider remains usable without
  DocSpec; DocSpec retains selection, effective budgets, and experiment state.
  Depends on D41; owners: source provider and DocSpec.

- [ ] **D45 · P1 · Make the wheel dependency reproducible and proportionate.**
  Define the supported public imports and version, record the built wheel digest
  and source revision, and declare installation through DocSpec's optional source
  integration. Keep offline source reading independent of unrelated analytics,
  server, or live-acquisition dependencies where feasible. **Done when:** a clean
  environment can install the pinned packages and import the selected API without
  sibling paths or undeclared extras; core DocSpec still works without the provider.
  Record wheel identity separately from source dataset/artifact pins. Review the
  chosen provider's actual packaging instead of trusting an old `dist/` file.
  Coordinate S31's consumption of the DocSpec wheel: qualify both directions
  through the composition boundary above, with independent source-only and core
  DocSpec installs. D04/D09/D11/D13/D19/D21/D29 supply the selected public APIs;
  do not create a second API inventory or require every optional stage at once.

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

- [ ] **D47 · P1 · Share search-dataset definitions and identifier normalization.**
  Reuse one installed API for search field roles/scopes, typed columns and
  schemas, identifier normalization, and the corresponding identity-bearing
  policies. Separate those from Engine's query weights and SQL. **Done when:**
  the current Search builder and Engine reader use one implementation, a field
  change no longer requires parallel authored definitions, and Engine still
  rejects unsupported or damaged datasets. Keep shared definition imports
  independent of build/runtime modules. Prefer the existing Search wheel over
  creating another package. Owners: SpicySearch and SpicyEngine.

- [ ] **D48 · P1 · Support bounded dataset recipes beyond segment processors.**
  Extend D04/D13 using the existing catalog workspace, artifact, and execution
  primitives. Accept pinned catalog/layer/resource inputs and named output
  partitions without inventing a document capture or segment. **Done when:**
  an ordinary retained-input experiment and a metadata-only dataset build share
  attempt/reuse accounting. Global census and lookup dependencies invalidate
  affected outputs correctly. State whether resume reuses whole completed builds
  or completed partitions; do not promise the latter without implementing it.
  Keep scheduler messages small references. Depends on D02–D04 and D15.

- [ ] **D49 · P1 · Run the existing search preparation as an injected dataset recipe.**
  Connect the current direct builder's metadata preparation, collision census,
  optional topic joins, typed rows, and publication to D48. Preserve the full
  requested catalog population, including records not selected for body capture.
  **Done when:** DocSpec runs and records the build without importing search
  policy into its core, the current Engine direct loader consumes its output,
  and a later experiment can reuse retained inputs. Keep search recipe identity
  distinct from DocSpec runner identity; a different runner alone does not
  require renaming the artifact producer. Depends on D09, D47–D48.
  Owners: DocSpec with SpicySearch composition and Engine admission checks.

- [ ] **D50 · P1 · Prove the sharing saves work and retire the replaced paths.**
  Compare the integrated recipe with the chosen current direct builder using
  fixed inputs and independent expected cases for source evidence, population,
  collisions, conflicting dates, repeated text, and topic eligibility. Exercise
  changed global dependencies, interruption, installed wheels, and index-only
  rebuilding. **Done when:** results and accounting meet those checks, scans,
  preparation calls, memory/scratch and output work are recorded, and named
  duplicate build/export mechanisms are removed. If shared execution adds a
  parallel ledger or cannot remove repeated lifecycle work, retain the Search
  recipe's standalone runner and limit consolidation to D09/D31/D47. Record that
  narrower decision explicitly; do not claim the broader integration complete.
  Depends on D49; owners: all three products.

## Suggested delivery order

1. **Usable local experiment:** D01–D07, D11, D13–D17, D19–D20, D24, and D38.
   Build D05 early and extend it as each capability becomes usable.
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

The canonical source checklist is `spicy-docs/docs/simplification-todo.md`,
reviewed at planning revision `5f04771` for this September 11 sync. The earlier
session file is historical. A solutions architect independently compared that
checklist with DocSpec `da5e227`; this sync reconciles package choice, both wheel
directions, storage ownership, encoding, and the optional search extension.
It records plan agreement, not implementation or runtime validation.

The mapping below appears in both checklists. D items detail DocSpec work; S
items retain source work and cross-product acceptance. These are linked work,
not duplicate implementations or blanket blocking dependencies. Completing one
item closes a mapped item only when its own acceptance criteria are also met.

| SpicyDocs items | DocSpec items | Coordination and implementation lead |
| --- | --- | --- |
| S01–S02, S09 | D08, D43 | Source owner exposes outcomes and coverage; DocSpec carries them and selects partial-input policy. |
| S03, S06–S08, S10 | D08, D23, D43 | Source owner fixes replay, reporting, retained refusals, and discovery; DocSpec consumes relevant facts, with distinct count units. |
| S04 | D01, D36 | Each product documents its actual behavior and useful stopping points. |
| S05, S23 | D10, D27–D29, D36–D37, D45–D46 | Qualify current formats and installed packages; share applicable evidence, not unrun completion claims. |
| S11–S16, S25 | D30–D35, D41–D42 | Inventory real callers and repeated work; keep package placement open and remove copies after selected replacements work. |
| S17–S18 | D06–D08, D38, D43, D46 | Source owners and DocSpec prove GAO-topic and captured-comment examples; source-only use remains independent. |
| S19, S26 | D04, D06–D07, D11–D12, D44 | DocSpec accepts independent inputs/fetchers; providers retain publisher routes and identity checks. |
| S20 | D20–D21, D23 | DocSpec and executor owners agree bounded retries, interruption, and cumulative accounting. |
| S21 | D22 | Replace the selected experiment campaign only; independently useful source publishing remains separate. |
| S22 | D31, D41 | Decide one useful physical writer with its callers; DocSpec owns dataset capture and transactions. |
| S24 | D38–D40 | Reuse independent review and workflow evidence; simulated personas do not complete D40's human exercise. |
| S27 | D07, D13–D19, D38 | DocSpec exposes processing, dataset growth, dependency-aware reuse, and comparison. |
| S28 | D02–D04, D19–D21, D32 | DocSpec simplifies configuration and public references while retaining one dataset lifecycle. |
| S29 | D02, D19, D24–D29, D38 | Retained catalog/capture/processing stages are useful independently; portable export has separate admission checks. |
| S30 | D28 | DocSpec, Rulespec, and source owners make one exact-value encoding decision; unresolved convergence is deferred in both lists. |
| S31 | D04, D09, D11, D13, D19, D21, D29, D45–D46 | Replace dataset loops with public DocSpec wheel APIs; compose independent provider APIs without circular package dependencies. |
| Optional search extension | D47–D50 | Search owns dataset definitions and transformations, DocSpec supplies reusable execution, and Engine owns indexing/serving. Adoption must remove duplicate lifecycle work. |

Source-side repairs and optional raw/table outputs stay in the source backlog;
DocSpec does not take ownership of every acquisition repair. D47–D50 do not gate
source-only use, ordinary document experiments, or S20's Dagster example.

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
