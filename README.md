# DocSpec

DocSpec builds document datasets whose inputs, processing results, and evidence
can be inspected and reused. Build a source catalog, capture selected bytes,
extract and segment documents, then run processors over retained inputs.

The runtime uses the [Core model](docs/core-model.md): immutable occurrences,
keyed states, explicit requests, actual execution attempts, and retained results.
One implementation owns publication, reuse, current selection, and cleanup for
both document processing and general dataset operations.

## Start here

```sh
uv sync --frozen --python 3.12
uv run --frozen docspec --help
uv run --frozen python -m examples.offline_demo --output /absolute/new-experiment
```

See [contributor setup](CONTRIBUTING.md), the [offline walkthrough](docs/offline-walkthrough.md),
and the [documentation index](docs/documentation.md). The example runs locally
and exercises capture, processing, repair, and reuse without a network service.

## Current entry points

| Task | Entry point |
| --- | --- |
| Build or read a source catalog | `build_local_catalog`, `open_local_catalog`, and `preview_local_catalog` accept a filesystem path or `CoreWorkspace`, explicit policy and producer acceptance. The `source-catalog` CLI exposes the same catalog owners. |
| Create and revise keyed datasets | `CoreWorkspace.create`, `revise`, and `rows`; CLI `state create`, `state revise`, and `state rows`. |
| Capture and process documents | `CoreWorkspace.documents(fetcher=...)` returns a `DocumentPipeline`. Import source items with `import_sources`, then `run` capture, extraction, segmentation, and selected processors. CLI `document import` and `document run` cover local-file work. |
| Resolve or resume an operation | `workspace.operations.resolve` checks reuse before executing a producer. `resume` verifies an explicit checkpoint; `recover` handles completed publication. See [operations](docs/operations.md). |
| Inspect and compare | `workspace.inspect` reads recorded identity, outcomes and availability; `workspace.compare` compares keyed states. CLI `inspect` and `compare` use those owners. |
| Select or remove retained data | `workspace.maintenance` performs expected-current selection and policy-authorized, resumable cleanup. CLI `select`, `remove`, and `resume-removal` expose these actions. |
| Export for independent reading | `workspace.export` copies a selected state and required evidence into a pinned Rulespec artifact. `open_result_export` admits it without the original workspace. See [result exports](docs/result-exports.md). |

`CoreWorkspace` is a context manager exported from `docspec.runtime`. Source
catalogs remain useful before any document is downloaded. Document processing
uses the same Core requests, results, and reuse checks as other operations;
there is no separate document cache or release ledger.

## Storage and execution

SQLite stores authoritative metadata and progress. Immutable Parquet stores bulk
occurrences and state membership; DuckDB handles relational bulk work. The
content-addressed blob store retains opaque bytes. Small Parquet row groups
are packed into larger files. Incremental selections evaluate changed members
and reuse saved comparison evidence when exact checks establish equal values.
Shared canonical encoding
and SHA-256 preserve identity and comparison meaning across these boundaries.

Changed full-state comparisons still require reading the complete comparison
stream, and revisions rewrite touched membership buckets. See
[record storage](docs/record-storage.md) for these costs and resource settings.

Python connects the native components and validates control records. Dagster is
an optional scheduler over the same Core lifecycle; it owns worker management,
retries, cancellation, and events. See [architecture](docs/architecture.md),
[record storage](docs/record-storage.md), and [Dagster](docs/dagster-experiment.md).

## Boundaries

Source providers own publisher access and literal source facts. DocSpec owns
catalog policy, document capture and processing, retained attempts, and source
evidence. Processors own domain interpretation. Rulespec supplies shared
canonical-byte and artifact-verification rules. Search indexing and serving
remain separate consumer responsibilities.

The optional SpicyDocs reader uses pinned public wheels and source artifacts.
[Catalog inputs](docs/catalog-inputs.md) describes that integration and supplied
records. Provider-specific guides cover [GovInfo bills](docs/govinfo-bill-example.md),
[annual CFR](docs/govinfo-cfr-example.md), [GAO topics](docs/gao-topics.md),
[FEC committees](docs/fec-committees.md), and [comments](docs/spicyregs-comments.md).

Normalized catalog dates remain strings whose format callers must check.
Field provenance belongs to each field's `sourcePaths`; an item-level label
does not establish where every field came from.

Status: internal, unpublished; no license selected.
