# Extend source, processing, execution, and storage

Put an extension at the owner of its behavior. Provider SDKs stay in adapters;
`CoreWorkspace` and application composition connect them to the shared lifecycle.
See [CONTRIBUTING](../CONTRIBUTING.md#find-a-bounded-change) for focused tests.

## Sources and fetchers

Use `SuppliedRecordSource` for bounded metadata mappings or implement
`SourceNativeRecordSource` for a provider reader. `describe()` identifies the
admitted source; record and rendition iterators expose literal facts and candidate
files. Dataset interpretation belongs in `SourceCatalogPolicy`. Preserve collection
outcomes, source namespaces, bounded evidence and per-field provenance.

A `ContentFetcher` returns `FetchStream` with bytes, observed transport identity
and cleanup. Pass it explicitly to `workspace.documents(fetcher=...)`. The
[fetcher guide](fetchers.md) and [GovInfo example](govinfo-bill-example.md) show
existing local, network and provider adapters.

## Extraction and segmentation

Implement `Extractor` or `Segmenter`, including `selected_identity(input)` and
their actual extraction/segmentation methods. Pass the objects directly to
`workspace.documents`. The selected implementation and settings become Core
operation definitions; outputs retain their own source and evidence identities.
Changes that affect output must change the relevant pinned settings.

## Processors

`DocumentProcessor` describes a named Core operation and its prerequisite names.
Its callback receives an operation context and declared input bindings. Use that
context to read required values, record actual uses and generate outputs.
`content_statistics_processor` is the built-in bulk example.

For bounded segment callbacks, use
[`provider_processor`](../src/docspec/adapters/document_processor.py). It connects
input-field permission, resource identity, processor limits, output schema and
provider evidence to Core. Shared rules live in
[`processor_policy`](../src/docspec/domain/processor_policy.py). Credentials stay
in live dependencies; reject secrets in durable evidence and diagnostics.
The [phrase matcher](phrase-matching-example.md) demonstrates pinned resources
and source-grounded outputs.

## Scheduling and storage

Dagster resources supply `DagsterRuntime` with `CoreOperations`, a stream of
`ScheduledOperation` values and a producer resolver. Native Dagster owns execution
and retries; the adapter delegates meaning and selection recovery to Core.

Storage extensions implement the existing blob, record or ledger ports. Bulk
members belong in native layers, not a per-member ledger graph. Publication,
retention and physical reachability remain shared owners. Do not add a second
cache, state resolver, metadata writer or compatibility API during an extension.
