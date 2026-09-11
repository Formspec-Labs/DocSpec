# Contributor maintainability to-do list

Make a small contribution understandable and verifiable without reconstructing
earlier AI conversations. Keep the existing domain, application, interface, and
adapter boundaries; reduce the context needed to work inside them.

This checklist combines the contributor-experience review and the code
organization, duplication, and dead-code review from 2026-09-11. It records
work and its completion status. Mark an item complete when its acceptance
criteria pass. [Local evidence](maintainability-progress.md) records completed
work; add the implementing pull request (PR) when one exists.

**Evidence and priorities**

The reviewed revision is `b1736e9`. Source links and measurements below refer to
that revision; update them when code moves. The existing untracked
`docs/history/2026-09-09-catalogue-cleaning-findings.md` is separate work.

| Observation | Review baseline |
| --- | --- |
| Python under `src/docspec/` | 85 files; 39,298 lines; median 233 lines |
| Large production files | 27 over 500 lines; 10 over 1,000; 5 over 2,000 |
| Documentation | 28 wiki Markdown files; 10,117 lines. Tracked Markdown under `docs/`: 8,300 lines |
| Wiki overview navigation | 14 broken relative links |
| Test coupling | 9 test modules import from other test modules; 15 import statements |
| Local default test run | 874 passed, 1 failed, 3 skipped, 1 deselected in 91.35 seconds |
| Failed check | Portability test finds personal absolute paths in `tools/summarize_attachment_sample.py` |
| Other local checks | Ruff and `docspec --help` passed |

These are local observations from the preceding reviews. They do not establish
current CI, full conformance, scale qualification, or release status. Dead-code
searches covered repository source, tools, tests, and documentation; they cannot
prove that an external consumer never imports a public symbol.

The maintainer subsequently clarified that legacy support is unnecessary.
Unused legacy entry points and predecessor readers may be retired; current
workflows, source evidence, and reproducible fixture provenance remain required.

P0 restores the default development baseline. P1 removes immediate contribution
barriers and resolves small cleanup decisions. P2 restructures larger areas after
their interfaces and checks are clear. P3 measures the result and addresses
remaining candidates only when the evidence supports a change.

**A. A reliable first contribution**

- [x] **A1 · P0 · Remove personal paths from attachment reporting.** Replace the
  hard-coded corpus, selection, and output locations in
  [the reporting tool](../tools/summarize_attachment_sample.py#L22) with explicit
  command arguments and documented defaults where appropriate. Preserve the
  report calculations. **Done when:** the existing repository-boundary test
  passes, the tool accepts caller-supplied paths, and its help explains inputs
  and output. Add a focused argument-handling check if existing tests do not
  cover the new behavior.

- [x] **A2 · P1 · Repair documentation navigation.** Fix the 14 `wiki/`-prefixed
  links in [the wiki overview](../wiki/overview.md#L113), then check links in the
  maintained onboarding pages. **Done when:** each link resolves from the page
  containing it and the README reaches the contributor guide and overview.

- [x] **A3 · P1 · Add `CONTRIBUTING.md`.** Explain Python 3.12 and `uv` setup,
  focused tests, the full local suite, optional dependencies, expected generated
  file changes, and the PR workflow. Link to detailed material rather than
  duplicating it. **Done when:** a new contributor can follow the guide from a
  checkout without needing personal directories, credentials, or sibling repos.

- [x] **A4 · P1 · Add a tested offline walkthrough.** Provide a few small local
  inputs and a supported path through catalog creation, processing, publication,
  and verification. Explain each input, what happens, the resulting files, and
  expected verification output. Reuse existing builders and interfaces.
  **Done when:** the exact documented example runs with installed dependencies
  and no network access or private corpus, writes to a caller-selected temporary
  directory, and is exercised by CI. Repeating it has documented behavior.

- [x] **A5 · P1 · Map contribution tasks to code and tests.** In the contributor
  guide, cover changing an extractor, segmenter, source policy, processor,
  storage adapter, CLI command, and published schema. For each, name the entry
  point, a representative implementation, relevant checks, and any public
  behavior that must remain stable. **Done when:** a contributor can identify a
  bounded starting point for each task without reading the whole pipeline.

- [x] **A6 · P1 · Align local checks with CI.** Document the commands already
  used by [CI](../.github/workflows/ci.yml), including the Dagster extra, lock
  verification, wheel checks, and integration-test selection. Explain the
  advisory conformance step and its incomplete qualification requirements.
  **Done when:** local commands are reproducible and a passing local suite is
  clearly distinguished from conformance, publication, and scale evidence.

**B. Current guidance and documentation ownership**

- [x] **B1 · P1 · Publish one maintained explanation of current behavior.**
  Reuse and trim the existing overview as the human entry point. Explain inputs,
  catalog construction, processing, outputs, verification, and component
  ownership. Link directly to specifications and executable checks for exact
  rules. **Done when:** readers can understand the supported flow before
  consulting historical decisions; the guide introduces no competing schema or
  behavioral specification.

- [x] **B2 · P1 · Make decision precedence and implementation status explicit.**
  Identify current, superseded, and accepted-but-unimplemented rules. Start with
  [Decision 0001](decisions/0001-document-release-2-0.md#L15), which supersedes
  parts of the implementation specification, and
  [Decision 0006](decisions/0006-publishable-releases-with-recorded-failures.md#L4),
  whose recorded status requires implementation verification. Preserve the
  history and point current guidance at the applicable rule. **Done when:** a
  contributor can tell which rule governs a change and what remains proposed.

- [x] **B3 · P1 · Label and route generated documentation.** Show the generation
  date and source revision from [wiki metadata](../wiki/metadata.json) on its
  entry page. Explain which pages maintainers edit and which are regenerated.
  Make the maintained entry guide's ownership explicit if it is removed from
  generation. **Done when:** readers can identify snapshot material immediately,
  and regeneration preserves maintained contributor guidance.

- [x] **B4 · P1 · Document schema and fixture maintenance.** Map each schema
  family to its authoritative definition, generator or deliberate editing
  procedure, installed location, and equality checks. Explain the fixture
  restamper's `--check` and deliberate regeneration modes, including the frozen
  predecessor corpus. **Done when:** a schema contributor knows what to edit,
  regenerate, review, and test; behavior-preserving refactors keep sealed fixture
  bytes and published identifiers unchanged.

**C. Duplicate and unused code**

- [x] **C1 · P1 · Remove the confirmed unused private definitions.** Recheck
  references, then remove `_object_path` from
  [source catalog storage](../src/docspec/adapters/source_catalog_store/pinned_fs.py),
  `_SHA256_HEX_RE` from [storage](../src/docspec/adapters/storage/files.py),
  `_ESTIMATE_FIELDS` from [the planner](../src/docspec/application/planner.py#L33),
  and `_UNIVERSE_ROWS` from
  [Regulations.gov policy](../src/docspec/application/regulations_gov_catalog.py#L74).
  Correct the outdated `_UNIVERSE_ROWS` docstring and remove imports made unused
  by the deletion. **Done when:** lint and the relevant existing checks pass,
  with no remaining references to the removed private definitions.

- [x] **C2 · P1 · Decide whether to maintain or retire `DocSpecApplication`.**
  Search known consumer code and packaging entry points before changing this
  exported interface. Its
  [reconciliation method](cleanup-decisions.md#retire-the-unused-application-wrapper) advertises
  `Iterable[StoreRef]`; the
  [underlying service](../src/docspec/application/reconcile.py#L313) requires
  `StoreTaskResult`. No repository caller constructs the wrapper. **Done when:**
  either the wrapper has an explicit supported purpose, correct types, and a
  meaningful public-use test, or its export and documentation are retired with
  the consumer impact resolved. Unknown external use remains an explicit limit
  on the removal decision.

- [x] **C3 · P1 · Consolidate UTF-8 offset calculation.** Replace
  [visible text's `_byte_offsets`](../src/docspec/processing/visible_text.py#L574)
  with the existing
  [`utf8_byte_offsets`](../src/docspec/processing/artifacts.py#L62), whose body is
  identical. **Done when:** one implementation serves extraction and segmentation,
  and existing non-ASCII evidence and segmentation tests preserve exact offsets.

- [x] **C4 · P1 · Consolidate S3 error interpretation.** Give
  [blob storage](../src/docspec/adapters/s3_blob.py#L47) and
  [content fetching](../src/docspec/adapters/content_fetchers.py#L307) one shared
  `_provider_error_identity` implementation owned by the S3 adapter area.
  **Done when:** both callers retain their own operation-specific decisions,
  existing error tests pass, and core imports still avoid optional SDKs.

- [x] **C5 · P1 · Consolidate CLI input and output helpers.** Share the identical
  `_emit` behavior in [the main CLI](../src/docspec/cli_io.py) and
  [catalog CLI](../src/docspec/cli_io.py). Review their overlapping
  JSON reading and root validation helpers at the same boundary. The readers
  currently enforce size limits differently; choose and test the intended bounded
  read behavior explicitly. **Done when:** output encoding, secret handling,
  error types/messages, file validation, and exit codes remain specified and
  covered; any intentional behavioral correction is distinct from code movement.

- [x] **C6 · P2 · Share catalog digest setup and result assembly.** Consolidate
  repeated digest definitions and final result construction in the
  [serial path](../src/docspec/adapters/catalog_artifact/derivation.py) and
  [parallel path](../src/docspec/adapters/catalog_artifact/derivation.py).
  Retain their different scheduling, streaming, and fallback mechanics.
  **Done when:** serial, parallel, and fallback behavior preserves ordering,
  exact digests, diagnostics, bounded resource use, and engine reporting. Use the
  existing equivalence tests and a representative performance comparison.

- [x] **C7 · P3 · Resolve the remaining small duplication candidates.** Review
  `_member` in the release builder and fixture restamper, and `to_member` and
  `policy_digest` in the two source policies. Prefer an existing owner or a small
  shared function when both callers enforce the same rule. Keep source-specific
  policy semantics distinct. **Done when:** each candidate has either one clear
  implementation or a short reason for remaining separate; no general utility
  layer or policy inheritance hierarchy is added just to save a few lines.

- [x] **C8 · P3 · Audit dormant public helpers and dynamic entry points.**
  Investigate `ProfileRegistry.to_inventory`, `PinnedCorpus.run_roots`, and the
  CourtListener tool's `build_catalog` and `capture_digest_of`. The review found
  no repository callers, which makes them candidates rather than confirmed dead
  public APIs. Include exports, strings, callbacks, tool usage, and known external
  consumers in the check. **Done when:** each is retained with a supported use,
  retired with its consumer impact resolved, or explicitly left unresolved.
  Framework callbacks such as parser handlers stay classified by their runtime
  registration, not by direct-call counts.

**D. Code organization and file length**

Split by responsibility within the current layers. Preserve public imports where
they remain supported. Each area below can use a mechanical move followed by a
separate simplification; keep those changes reviewable independently.

- [x] **D1 · P2 · Separate shared document-release rules from verification.**
  Split the 3,316-line
  [release verifier](../src/docspec/adapters/document_release/verify.py) into
  coherent identity/format rules, member reading, coverage calculations, and
  validation. The [builder](../tools/build_document_release.py#L102) and fixture
  restamper should consume the shared rules directly. **Done when:** one owner
  defines each rule; builder and verifier retain independent entry points;
  release IDs, digests, accepted/rejected fixtures, and diagnostics remain stable.

- [x] **D2 · P2 · Separate source-catalog artifact responsibilities.** Split the
  2,511-line [artifact module](../src/docspec/adapters/catalog_artifact/)
  into building, reading, verification, and derivation responsibilities. Coordinate
  with C6 so moves and shared-rule changes do not overlap unpredictably.
  **Done when:** the public `docspec.source_catalog` interface still works, worker
  functions remain usable in external processes, and catalog build, succession,
  reuse, recovery, and installed-wheel tests preserve behavior.

- [x] **D3 · P2 · Separate CLI command groups and local setup.** Restructure the
  2,276-line [CLI](../src/docspec/cli/) into command groups, common request/output
  handling, and local dependency setup. Build on C5. Keep one `docspec` command
  and explicit places where adapters are connected to application services.
  **Done when:** existing commands, help, JSON responses, exit codes, portable
  task execution, and installed entry points remain compatible. Update dependency
  boundary tests deliberately for the new layout.

- [x] **D4 · P2 · Simplify source-policy conversion.** Break the 403-line
  Regulations.gov `_item_from_row` into named steps for joins, normalization,
  selection, and provenance. Apply the same review to its comment conversion
  and the 245-line Federal Register `_item_from_row`, sharing only rules that
  actually have the same meaning. **Done when:** a policy decision can be read
  and tested locally; field order, source paths, selection precedence, reason
  codes, and exact catalog output remain unchanged.

- [x] **D5 · P2 · Split local storage implementations.** Separate the 2,001-line
  [storage module](../src/docspec/adapters/storage/) into blob, document-job,
  record, and document-catalog implementations. Keep shared file operations
  narrowly owned and preserve supported imports. **Done when:** each adapter can
  be understood independently and existing checks preserve atomic publication,
  immutable writes, path containment, bounded merging, and stale-base rejection.

- [x] **D6 · P2 · Reduce the responsibilities of `StoreExecutionService`.**
  Separate coordination, checkpoint verification, processor execution, and
  reprocessing in the 1,867-line
  [execution module](../src/docspec/application/execution.py). Start with
  `_verify_entry_checkpoint` (252 lines) and `_reprocess_entry` (296 lines).
  **Done when:** each responsibility has explicit inputs and outputs; recovery
  still verifies retained work, reuses completed stages, restores cumulative
  budgets, and performs only the required processor work. Avoid splitting one
  shared mutable state machine across mixins merely to shorten the file.
  The service now coordinates 705 lines; read-only verification, processor
  runtime, shared rules/evidence, and base preparation have explicit owners
  described in [the architecture guide](architecture.md#what-happens-to-it).

- [x] **D7 · P2 · Simplify the remaining long control-flow functions.** Review
  `_load_build_command_receipt` in the catalog CLI (300 lines),
  `_validate_root_bindings` in the release verifier (266), and the catalog
  builder's `build` method (230). Coordinate each change with its module split.
  **Done when:** validations and workflow steps have clear names, preserved error
  precedence, and focused checks for the behavior they own.

- [x] **D8 · P3 · Review remaining size outliers by responsibility.** Assess
  `source_catalog_store.py` (1,701 lines), `domain/scale.py` (1,560),
  `bounded_segmentation.py` (1,104), and `domain/source_catalog.py` (1,077).
  Include large schemas and historical docstrings in the assessment, while
  distinguishing declarations from complex control flow. **Done when:** each
  remaining outlier has a coherent responsibility or a justified split; useful
  rationale and normative schema definitions remain discoverable.
  [Recorded decisions](cleanup-decisions.md#large-modules-reviewed-by-responsibility)
  explain the store split and the retained schema, scale, and segmentation families.

**E. Tests and contribution conventions**

- [x] **E1 · P1 · Give shared test setup an explicit home.** Move reusable setup
  out of imports between `test_*.py` modules into focused support modules,
  building on [the existing helpers](../tests/helpers.py). Start with `_run`,
  `_write_source`, `_captured`, and the in-memory planner collaborators.
  **Done when:** test modules no longer depend on private helpers in other test
  modules, affected tests retain their assertions, and fixture setup remains
  explicit enough for a contributor to understand.

- [x] **E2 · P2 · Make the largest test files easier to navigate.** Organize
  catalog snapshot, release verification, and source-policy tests by observable
  behavior. Preserve conformance selectors and any dynamically referenced test
  identifiers when files move. **Done when:** the contributor map points to
  focused suites, shared setup uses E1, and the same required checks still run.

- [x] **E3 · P1 · Adopt a short code organization guide.** Record the existing
  dependency direction, ownership of shared helpers, intended public APIs, and
  proposed length guidelines: ordinary modules around 200–500 lines, review above
  800, and scrutiny for functions above 60–80 lines. Treat these as review prompts
  with justified exceptions. **Done when:** reviewers evaluate responsibility,
  coupling, and control flow alongside size; declarations and generated material
  have an explicit exception policy. Place this in contributor guidance.

- [x] **E4 · P1 · Define a small PR review checklist.** Ask for the behavior or
  maintainability improvement, affected entry points, validation, public-format
  impact, and any follow-up left open. Keep mechanical moves, behavior changes,
  and public API retirements easy to distinguish. Identify the reviewer for
  cross-component or format changes using actual maintainer roles.
  **Done when:** a reviewer can assess a contribution without chat history and
  the checklist works equally for human- and AI-authored code.

- [ ] **E5 · P3 · Measure whether contribution actually became easier.** Have
  someone unfamiliar with the implementation follow the walkthrough, locate one
  change, explain the governing rule, modify it, and run the focused checks.
  Record confusing steps, files needed, and time spent; resolve the observed
  friction. **Done when:** the exercise succeeds without private context and the
  remaining friction is captured as specific work. Reduced line counts alone do
  not close this item.

**Suggested delivery order and dependencies**

| Order | Work | Dependency or sequencing note |
| --- | --- | --- |
| 1 | A1–A4: baseline, navigation, guide, walkthrough | A1 establishes the passing default baseline; keep the first contribution usable |
| 2 | A5–A6, B1–B4, E3–E4: guidance and review conventions | These can proceed alongside localized cleanup |
| 3 | C1, C3–C5, E1: unused internals, exact duplicates, test setup | Finish common helpers before moving their consumers |
| 4 | C2: public-wrapper decision | Resolve interface intent before changing its callers or exports |
| 5 | D1–D6 and C6: major module refactors | One responsibility at a time; keep code moves separate from changed logic |
| 6 | D7, E2: long functions and test navigation | Coordinate with the relevant module split; update conformance selectors |
| 7 | C7–C8, D8, E5: remaining candidates and contributor exercise | Close candidates with evidence, including a reasoned decision to retain code |

**Validation and completion rules**

Use the existing tests first. Add tests for the walkthrough, corrected public
behavior, or uncovered risks. Mechanical moves and tiny deletions should reuse
the checks that already establish behavior. Run focused suites while editing;
run the full baseline and applicable packaging checks before completing a PR.

| Changed area | Starting checks |
| --- | --- |
| Imports, modules, public exports, installation | `tests/test_package_boundary.py`, `tests/conformance/test_import_directions.py`, `tests/test_source_catalog_installed_wheel.py`; CI wheel checks |
| CLI and local composition | `tests/test_cli.py`, `tests/test_run_active_view.py`, `tests/test_execution_backends.py`, `tests/test_dagster_adapter.py` |
| Source policies and catalog artifacts | `tests/test_catalog_policy.py`, `tests/test_regulations_gov_*.py`, `tests/test_cross_filed_collapse.py`, `tests/test_source_catalog_*.py`; choose a focused suite from [the contributor map](../CONTRIBUTING.md#find-a-bounded-change) |
| Release rules and builder | `tests/test_document_release_*.py`, `tests/test_canonical_encoding_equivalence.py`; fixture restamper `--check`. The contributor map separates admission, identity, format, text-body, and index checks. |
| Storage and S3 | `tests/test_storage_adapters.py`, `tests/test_storage_records_catalog.py`, `tests/test_source_catalog_storage.py`, `tests/test_source_catalog_build_safety.py`, `tests/test_source_catalog_succession.py`, `tests/test_s3_blob_adapter.py`, `tests/test_content_fetchers.py` |
| Extraction and segmentation | `tests/test_visible_text.py`, `tests/test_processing_pipeline.py`, `tests/test_bounded_segmentation.py`, `tests/conformance/test_evidence_roundtrip.py` |
| Execution and recovery | `tests/test_stage_checkpoint_recovery.py`, `tests/test_processor_only_checkpoint_recovery.py`, `tests/test_processor_reprocessing.py`, `tests/test_work_budget.py`, `tests/test_processor_cache.py`, `tests/conformance/test_incremental_equivalence.py` |
| Schemas and profiles | `tests/test_machine_files.py`, `tests/test_package_boundary.py`, `tests/test_scale_profile.py`, `tests/test_profile_registry.py` |

For refactors, preserve exact source bytes, evidence coordinates, record and
release identities, deterministic ordering, failure accounting, recovery,
resource bounds, optional-dependency isolation, and installed-package behavior.
Keep source-specific meanings with their source policies and generic artifact
rules with their existing owner. Intentional changes to those behaviors need
their own stated outcome and validation.

An item closes when its stated outcome is verified, relevant checks pass, public
compatibility decisions are recorded, and the contributor map follows any moved
code. Link the PR and its evidence next to the completed checkbox. Keep local
validation, CI, conformance, merge, and release status distinct.
