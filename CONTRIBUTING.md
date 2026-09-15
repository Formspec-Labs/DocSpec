# Contributing to DocSpec

Start with one observable behavior, its implementation, and its focused tests.
The [current architecture](docs/architecture.md) explains the processing flow;
the [decision index](docs/decisions/README.md) identifies the rules that govern it.

## Set up a checkout

Use Python 3.12, `uv` and Docker for the local Iceberg catalog. The checked-in lock and vendored
`rulespec-artifacts` wheel make development independent of sibling checkouts.
Development also installs the pinned SpicyDocs reader so source integrations run
in the default test suite. The built core wheel keeps SpicyDocs optional.
From the repository root:

```sh
uv python install 3.12
uv sync --frozen
uv run --frozen docspec --help
uv run --frozen python tools/with_iceberg.py pytest tests/test_processing_pipeline.py
```

The default suite excludes tests marked `integration`. It uses local inputs and
test doubles for providers and an actual local Iceberg REST catalog for storage.
The helper mounts the checkout and its temporary directory, then removes its
container after the command. See [record storage](docs/record-storage.md). Optional dependencies may cause documented
skips; the full regression command appears below under “Check the same things as CI.” `uv sync --frozen --extra dagster` enables the real Dagster adapter test.
Select the same extra on subsequent `uv run` commands to keep it installed.
Other extras are `http`, `s3`, `pdf`, and `tokens`; install only those needed for
the adapter you are exercising. Core imports must work without them.

Run the [offline walkthrough](docs/offline-walkthrough.md) to build and grow a
catalog, capture documents, repair a failure, and compare later processing
attempts. Try the [GAO topic filter](docs/gao-topics.md) for metadata-only work or
the [GovInfo bill example](docs/govinfo-bill-example.md) or
[annual CFR example](docs/govinfo-cfr-example.md) for injected source
fetching and later XML processing. Use [result exports](docs/result-exports.md)
for independent consumers.

## Find the implementation and its tests

| Task | Start here | Representative implementation and focused checks | Preserve |
| --- | --- | --- | --- |
| Add a source adapter | `ports/source_catalog.py`, [catalog inputs](docs/catalog-inputs.md) | `adapters/spicy_docs_source_native.py`, `adapters/supplied_records.py`; `tests/test_spicy_docs_source_native.py`, `tests/test_local_catalogs.py`, `tests/test_source_catalog_installed_wheel.py` | Admitted source identity, literal fields, collection outcomes, bounded evidence |
| Add a fetcher | `ports/content_fetcher.py`, [fetchers](docs/fetchers.md) | `examples/govinfo_bill_fetcher.py`; `tests/test_runtime_s3_fetcher.py`, `tests/test_govinfo_bill_installed_wheel.py` | Bounded streams, source and transport identity, exact bytes, cleanup, retained failure evidence |
| Extract a format | `ports/extractor.py`, `processing/extraction.py` | `processing/visible_text.py`; `tests/test_visible_text.py`, `tests/test_processing_pipeline.py` | Exact source bytes, byte offsets, extraction identity, evidence round trips |
| Change segmentation | `ports/segmenter.py`, `processing/segmentation.py` | `processing/bounded_segmentation.py`; `tests/test_bounded_segmentation.py`, `tests/conformance/test_segmentation.py` | Deterministic order, size bounds, source coordinates |
| Change a source policy | `application/catalog_policy.py` | `application/regulations_gov_catalog/`; `tests/test_catalog_policy.py`, the focused Regulations.gov suites below, and `tests/test_cross_filed_collapse.py` | Source meaning, selection precedence, field provenance, reason codes, catalog digests |
| Add a processor | `application/documents.py`, `adapters/document_processor.py`, `domain/processor_policy.py` | `application/document_processors.py`; `tests/test_core_document_providers.py`, `tests/test_core_documents.py` | Declared material inputs, graph order, resources, allowed fields, observed evidence and limits |
| Change storage | `ports/blob_store.py`, `ports/record_storage.py`, `ports/core_ledger.py` | `adapters/storage/`; `tests/test_core_ledger.py`, `tests/test_core_states.py`, `tests/test_core_maintenance.py` | Immutable writes, shared reachability, publication ordering, bounded batches and expected-current selection |
| Change a command | `cli/parser.py` connects Core commands; source commands remain in `cli/source_catalog.py` and `cli/catalog_policy.py` | `tests/test_core_runtime_cli.py`, `tests/test_cli_io.py`, `tests/test_catalog_policy_cli.py` | Shared Python meaning, bounded JSON I/O, errors and installed entry points |
| Change a published schema | [Schema maintenance](docs/schema-maintenance.md) | `tests/test_schema_validation.py`, `tests/test_package_boundary.py` | Closed shapes, canonical bytes, current version/identity rules |
| Change result export or consumer admission | [Result exports](docs/result-exports.md), `adapters/result_export/` | `tests/test_result_export.py`, `tests/test_result_export_admission.py` | Complete outcomes, exact bytes and typed evidence, shared generic verification, independent reading |

Source paths in this table are relative to `src/docspec/`. Shared test setup
lives in focused `tests/support/` modules . Import setup
from there; test modules should not import other test modules.

For catalog and result changes, choose the suite for the behavior:

| Behavior | Focused suites under `tests/` |
| --- | --- |
| Catalog identity and unchanged-payload reuse | `test_source_catalog_snapshot.py` |
| Catalog source admission, policy rows, and row encoding | `test_source_catalog_policy.py`, `test_source_catalog_rows.py` |
| Catalog filesystem safety, publication recovery, and pointer advancement | `test_source_catalog_storage.py`, `test_source_catalog_build_safety.py`, `test_source_catalog_succession.py` |
| Serial and spawned-worker catalog derivation | `test_source_catalog_workers.py` |
| Catalog command builds and artifact verification | `test_source_catalog_cli_build.py`, `test_source_catalog_cli_verify.py` |
| Regulations.gov joins/provenance, selection, and comments | `test_regulations_gov_catalog.py`, `test_regulations_gov_selection.py`, `test_regulations_gov_comments.py` |
| Result export, complete active population and independent reading | `test_result_export.py` |
| Product evidence after shared container admission | `test_result_export_admission.py` |
| Canonical identity values and framing | `test_canonical_encoding_equivalence.py`, `test_framing.py` |
| Extraction quality limits and concrete misleading outputs | `test_extraction_quality.py` |

Each suite imports only the setup it needs. Family setup lives in
`tests/support/source_catalog_builds.py`, `source_catalog_cli.py`,
`catalog_publication.py`, and `regulations_gov.py`. Shared pytest fixtures are
registered explicitly in the suites that use them.

For a local experiment, open `CoreWorkspace` from `docspec.runtime`. Its
`documents(fetcher=...)` pipeline imports source items and runs the chosen stages.
`workspace.operations` supplies the common operation and reuse lifecycle;
`workspace.maintenance` owns selection and cleanup. See [Python runs](docs/python-runs.md)
and the [guide index](docs/documentation.md).

## Organize code for its reader

The dependency direction is callers → runtime → application → ports and domain, with
adapters implementing the ports. Deterministic processing code depends on domain
types. Only composition code connects concrete adapters to application services.
`tests/conformance/test_import_directions.py` checks the complete allowed map.
Keep optional SDK imports at the adapter that selects them.
The existing PDF profile in `processing/extraction.py` lazily calls
`spicy_docs.extraction.pypdf` for raw page reading. This specific shared import
is allowed; DocSpec keeps representation, formatting, and evidence policy.

Give each module one explainable responsibility. Put a shared rule with the
component that owns it, and share it only when the callers mean the same thing.
Reuse the existing owner, refactor it, or replace it. Give shared behavior one
implementation. Prefer a small function with explicit inputs and outputs over
a new utility layer, inheritance hierarchy, or collection of mixins. Source-specific policy
decisions stay with their source. Separate scheduling mechanics from common
digest rules; preserve streaming and checkpoint verification where they protect
resource limits and recovery.

Aim for ordinary modules around 200–500 lines. Review modules above 800 lines
and functions above 60–80 lines for mixed responsibilities. These are review
prompts, not pass/fail limits. Schemas, declarative models, and cohesive parsing
tables may be longer; record why they belong together. Splitting a declaration
across files just to satisfy a counter makes it harder to read.

Keep current entry points (`docspec.source_catalog`, application services, CLI
commands, and concrete adapter selection) coherent during moves. Update
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
uv sync --frozen --extra dagster --extra s3
uv run --frozen --extra dagster --extra s3 ruff check .
uv run --frozen --extra dagster --extra s3 python tools/with_iceberg.py pytest --require-regression-map \
  --junitxml=/tmp/docspec-pytest.xml
uv build --out-dir dist
```

CI runs pytest once, requires all mapped regression cases to complete, and
retains native JUnit, console output, the lockfile, and built wheels for that
checkout. The [qualification guide](docs/qualification.md) distinguishes these
checks from actual capacity measurements and publication evidence.

To reproduce CI's installed-wheel check in an empty environment:

```sh
wheel_check=$(mktemp -d)
uv venv --python 3.12 "$wheel_check/venv"
uv pip install --python "$wheel_check/venv/bin/python" vendor/rulespec_artifacts-*.whl vendor/spicy_docs-*.whl dist/docspec-*.whl
"$wheel_check/venv/bin/python" -c 'import docspec'
"$wheel_check/venv/bin/docspec" --help
```

Use a clean `dist/` containing the wheel being reviewed. Supply the vendored
Rulespec Artifacts and SpicyDocs wheels explicitly: these pinned dependencies are not available
from the public package index, and `uv` source overrides do not travel inside
the built DocSpec wheel. The focused
`tests/test_source_catalog_installed_wheel.py` also exercises catalog behavior
from an installed package. For live service checks, inspect the specific test's
configuration first and explicitly select it with `pytest -m integration`;
the default test run supplies no live credentials.

## Install the source reader

DocSpec requires the pinned SpicyDocs core wheel for XML, HTML and image-header
reading. Network acquisition, PDF decoding and other processing extras remain
optional. SpicyDocs stays independently usable and does not depend on DocSpec.

For a built wheel and an empty verification environment:

```sh
uv pip install --python "$wheel_check/venv/bin/python" \
  --find-links dist --find-links vendor 'docspec==0.5.0'
```

Outside the checkout, supply the DocSpec, Rulespec Artifacts and SpicyDocs wheels
from the same checked build; `vendor/` holds the dependency wheels. `uv sync`
selects the required reader automatically. Package pins identify code; source
artifact pins identify the data a catalog reads.

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
Use the [tool inventory](tools/README.md) for current source research, sampling,
reporting and schema scripts.
