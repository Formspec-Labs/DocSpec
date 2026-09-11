# Contributing to DocSpec

Start with one observable behavior, its implementation, and its focused tests.
The [current architecture](docs/architecture.md) explains the processing flow;
the [decision index](docs/decisions/README.md) identifies the rules that govern it.

## Set up a checkout

Use Python 3.12 and `uv`. The checked-in lock and vendored
`rulespec-artifacts` wheel make development independent of sibling checkouts.
From the repository root:

```sh
uv python install 3.12
uv sync --frozen
uv run --frozen docspec --help
uv run --frozen pytest tests/test_processing_pipeline.py
uv run --frozen ruff check .
uv run --frozen pytest
```

The default suite excludes tests marked `integration`. It uses local inputs and
test doubles for external services. Optional dependencies may cause documented
skips; a default pass is local validation, not full conformance or a published
release. `uv sync --frozen --extra dagster` enables the real Dagster adapter test.
Select the same extra on subsequent `uv run` commands to keep it installed.
Other extras are `http`, `s3`, `pdf`, and `tokens`; install only those needed for
the adapter you are exercising. Core imports must work without them.

Run the [offline walkthrough](docs/offline-walkthrough.md) to see a complete
catalog → processing → publication → verification flow using one local document.

## Find a bounded change

| Task | Start here | Representative implementation and focused checks | Preserve |
| --- | --- | --- | --- |
| Extract a format | `ports/extractor.py`, `processing/extraction.py` | `processing/visible_text.py`; `tests/test_visible_text.py`, `tests/test_processing_pipeline.py` | Exact source bytes, byte offsets, extraction identity, evidence round trips |
| Change segmentation | `ports/segmenter.py`, `processing/segmentation.py` | `processing/bounded_segmentation.py`; `tests/test_bounded_segmentation.py`, `tests/conformance/test_segmentation.py` | Deterministic order, size bounds, source coordinates |
| Change a source policy | `application/catalog_policy.py` | `application/regulations_gov_catalog/`; `tests/test_catalog_policy.py`, the focused Regulations.gov suites below, and `tests/test_cross_filed_collapse.py` | Source meaning, selection precedence, field provenance, reason codes, catalog digests |
| Add a processor | `ports/processor.py`, `domain/processors.py` | `processing/processors.py`; `tests/test_processor_reprocessing.py`, `tests/conformance/test_processor_contract.py` | Declared inputs/outputs, dependency order, stable IDs, retry and cache behavior |
| Change storage | `ports/blob_store.py`, `ports/record_storage.py`, `ports/document_catalog.py` | `adapters/storage/` (blobs, controls, stores, records, catalog), `adapters/s3_blob.py`; `tests/test_storage_adapters.py`, `tests/test_storage_records_catalog.py`, `tests/test_s3_blob_adapter.py` | Immutable writes, containment, atomic publication, bounded memory, stale-base rejection |
| Change a command | `cli/parser.py` registers commands; `cli/` groups their implementations; `cli/local.py` connects local services; `cli_io.py` owns bounded JSON I/O | `tests/test_cli.py`, `tests/test_cli_io.py`, `tests/test_run_active_view.py`, `tests/test_execution_backends.py`; catalog commands live in `cli/source_catalog.py` and `cli/catalog_policy.py`, with `tests/test_catalog_policy_cli.py` covering policy creation | Help, JSON shape, error/exit behavior, secret redaction, installed entry point |
| Change a published schema | [Schema maintenance](docs/schema-maintenance.md) | `tests/test_machine_files.py`, `tests/test_package_boundary.py`, `tests/test_document_release_schema_bundle.py` | Closed shapes, canonical bytes, version/identity rules, predecessor fixtures |

Source paths in this table are relative to `src/docspec/`. Shared test setup
lives in focused `tests/support/` modules and `tests/helpers.py`. Import setup
from there; test modules should not import other test modules.

For catalog and portable-release changes, choose the suite for the behavior:

| Behavior | Focused suites under `tests/` |
| --- | --- |
| Catalog identity and unchanged-payload reuse | `test_source_catalog_snapshot.py` |
| Catalog source admission, policy rows, and row encoding | `test_source_catalog_policy.py`, `test_source_catalog_rows.py` |
| Catalog filesystem safety, publication recovery, and pointer advancement | `test_source_catalog_storage.py`, `test_source_catalog_build_safety.py`, `test_source_catalog_succession.py` |
| Serial and spawned-worker catalog derivation | `test_source_catalog_workers.py` |
| Catalog command builds and verification receipts | `test_source_catalog_cli_build.py`, `test_source_catalog_cli_verify.py` |
| Regulations.gov joins/provenance, selection, and comments | `test_regulations_gov_catalog.py`, `test_regulations_gov_selection.py`, `test_regulations_gov_comments.py` |
| Portable release admission and complete diagnostics | `test_document_release_verify.py` |
| Portable identity, digest rules, and canonical encoding | `test_document_release_identity.py` |
| Minted portable rows and declared format | `test_document_release_wire_format.py` |
| Comment/attachment accounting and indexed byte ownership | `test_document_release_text_bodies.py`, `test_document_release_member_index.py` |

Each suite imports only the setup it needs. Family setup lives in
`tests/support/source_catalog_builds.py`, `source_catalog_cli.py`,
`document_release.py`, and `regulations_gov.py`. Shared pytest fixtures are
registered explicitly in the suites that use them.

For an embedded local runner, `docspec.cli.execution.run_local` accepts the same
closed request file as the CLI and an optional injected content fetcher. The
offline example exercises this entry point; preparation and worker helpers
inside `cli/` remain internal implementation details.

## Organize code for its reader

The dependency direction is commands → application → ports and domain, with
adapters implementing the ports. Deterministic processing code depends on domain
types. Only composition code connects concrete adapters to application services.
`tests/conformance/test_import_directions.py` checks the complete allowed map.
Keep optional SDK imports at the adapter that selects them.

Give each module one explainable responsibility. Put a shared rule with the
component that owns it, and share it only when the callers mean the same thing.
Prefer a small function with explicit inputs and outputs over a new utility
layer, inheritance hierarchy, or collection of mixins. Source-specific policy
decisions stay with their source. Separate scheduling mechanics from common
digest rules; preserve streaming and checkpoint verification where they protect
resource limits and recovery.

Aim for ordinary modules around 200–500 lines. Review modules above 800 lines
and functions above 60–80 lines for mixed responsibilities. These are review
prompts, not pass/fail limits. Schemas, declarative models, and cohesive parsing
tables may be longer; record why they belong together. Splitting a declaration
across files just to satisfy a counter makes it harder to read.

Keep current entry points (`docspec.source_catalog`, application services, CLI
commands, and profile implementation strings) coherent during moves. Update
their callers directly; this project does not require legacy import wrappers,
aliases, or obsolete format support.
An underscore is an internal-use signal, not proof that code is unused. Check
registrations, dynamic imports, tools, and known consumers before removal.
Comments should explain a rule, failure mode, measurement, or tradeoff that the
code cannot explain. Keep that reasoning next to its owner; link lengthy
experiments to `docs/history/` instead of repeating their transcripts.

## Check the same things as CI

For a focused edit, run its relevant tests and Ruff first. Before review, use
the [CI workflow](.github/workflows/ci.yml) as the command authority:

```sh
uv lock --check
uv sync --frozen --extra dagster
uv run --frozen --extra dagster ruff check .
uv run --frozen --extra dagster pytest
uv run --frozen --extra dagster docspec conformance run --root . \
  --specification conformance/specification.json \
  --matrix conformance/test-matrix.json \
  --output /tmp/docspec-conformance-report.json --class core
uv build --out-dir dist
```

The conformance command currently returns a nonzero status when required
evidence is incomplete; CI records it with `continue-on-error`. Read the report.
The required matrix still includes partial scale and package-release checks.
Local tests cannot substitute for ordered qualification campaigns or publication
evidence from an exact clean commit. Do not relabel missing evidence as a pass.

To reproduce CI's installed-wheel check in an empty environment:

```sh
wheel_check=$(mktemp -d)
uv venv --python 3.12 "$wheel_check/venv"
uv pip install --python "$wheel_check/venv/bin/python" vendor/rulespec_artifacts-*.whl dist/docspec-*.whl
"$wheel_check/venv/bin/python" -c 'import docspec'
"$wheel_check/venv/bin/docspec" --help
```

Use a clean `dist/` containing the wheel being reviewed. Supply the vendored
Rulespec wheel explicitly: this pinned dependency is not currently available
from the public package index, and `uv` source overrides do not travel inside
the built DocSpec wheel. The focused
`tests/test_source_catalog_installed_wheel.py` also exercises catalog behavior
from an installed package. For live service checks, inspect the specific test's
configuration first and explicitly select it with `pytest -m integration`;
the default test run supplies no live credentials.

## Submit a reviewable change

Explain the problem and resulting behavior, affected entry points, checks run,
and any remaining limits. Distinguish mechanical movement from changed behavior
and public API retirement. For schema or fixture changes, follow
[the maintenance procedure](docs/schema-maintenance.md); generated diffs and
identity changes need an explanation, not a routine refresh.

The current repository maintainer is `@mikewolfd`. Request maintainer review for
cross-component changes and product-owner review for changes to published meaning
or identity, following the accepted-by records in `docs/decisions/`. This repo has
no per-directory reviewer roster; do not infer ownership from who last generated
a file. The [PR template](.github/pull_request_template.md) captures the evidence
for human- and AI-authored changes alike.

Maintainers edit this guide and the guides under `docs/` directly. The
[documentation index](docs/documentation.md) links to catalog evidence,
extension guidance, operations, and historical provenance. Update the relevant
guide with a behavior or file move so the next contributor finds its current owner.
Use the [tool inventory](tools/README.md) for repository scripts, including the
historical portable mint recipe and its limits.
