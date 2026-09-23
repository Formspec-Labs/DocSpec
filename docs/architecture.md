# Current architecture

DocSpec uses one Core runtime for general keyed datasets and document work.
Source catalogs are independently usable inputs. Capture, extraction,
segmentation and processors become Core operations with explicit requests,
actual attempts, retained outputs and selected results.

## What goes in?

A source adapter supplies literal records, candidate files, identity and evidence.
The catalog policy decides the document universe, normalization, selection and
exclusion. [Catalog construction](../src/docspec/adapters/catalog_artifact/builder.py)
retains those decisions in a verified artifact; the
[reader](../src/docspec/adapters/catalog_artifact/reader.py) admits its pinned rows.
These owners remain separate from document acquisition.

`CoreWorkspace.create` imports keyed JSON values. `DocumentPipeline.import_sources`
imports `SourceItem` or `SourceCatalogItem` values. A document fetcher supplies
bounded byte streams and observed transport identity; extraction and segmentation
preserve content identities and evidence coordinates.

## What happens to it?

[CoreWorkspace](../src/docspec/runtime/core.py) assembles the existing blob and
Iceberg snapshots, DuckDB writer and bulk engine, SQLite ledger, publisher, operation runner,
and maintenance owner. It closes their resources as a context manager.

[Core operations](../src/docspec/application/core_execution.py) record each actual
attempt and its progress. The shared reuse evaluator checks declared material
inputs, available comparison evidence and current policy before selecting a
retained result. Reuse records an association with the original result rather
than creating another generation event. Failed work remains authoritative
history even when no successful result is retained.

DocSpec records which retained field values it actually evaluated. After a
revision, it compares member identities in bulk, evaluates changed members and
reuses unchanged field values. This also works after reopening. Temporary changes
stay bounded; explicitly retaining a selection uses the same DuckDB Iceberg writer.
Missing or unavailable prior values cause ordinary evaluation of the parent data.

[DocumentPipeline](../src/docspec/application/documents.py) batches captures and
processing stages through those same operations. Changed extraction can reuse
capture; changed segmentation can reuse extraction; processor changes reuse
unchanged prerequisites. Definitions pin settings and resources. There is no
second document planner, cache, or publication lifecycle.

## Where does it live?

| Owner | Data and responsibility |
| --- | --- |
| SQLite Core ledger | Identity, provenance links, retention, availability, actual progress, current selections and removal intents |
| Iceberg record/state storage | Pinned occurrence, membership and selected-value snapshots; DuckDB writes changed rows and positional deletes through a REST catalog |
| DuckDB | Relational joins, sorting, state resolution, comparison and bulk selection |
| Content-addressed blob store | Retained opaque values, capture bytes and recovery documents |
| Shared encoder and admission | Exact value types, canonical bytes, SHA-256, typed metadata and supplied JSON Schemas |

Bulk members remain in native layers; the ledger does not create a per-member
SQLite graph. A member gains a ledger row only when a publication references it
by identity. Physical representation is separate from logical state identity.
See [record storage](record-storage.md) and the
[implementation plan](core-model-implementation-plan.md).

## What comes out?

A retained state names immutable occurrences. A retained result records outputs
and their actual execution; an explicit selection associates a request with the
chosen result. Current selection changes only against the caller's expected
previous value. Retaining an alternative does not make it current.

[Portable exports](result-exports.md) contain a selected state and its required
Core metadata and physical bytes. Additional historical roots are explicit.
Rulespec checks membership and bytes; DocSpec admits the Core records and their
retention obligations. The independent reader needs the artifact, expected pin
and accepted producer, without the original workspace or producer callbacks.

## How do we check it?

Record admission, publication, reuse and maintenance share their semantic owners.
Tests cover independent reference behavior, batch boundaries, actual byte loss,
restoration, interrupted publication and cleanup, and installed-package reading.
The [task list](core-model-implementation-tasks.md) records remaining acceptance;
[qualification](qualification.md) separates regression evidence from capacity.

Dependencies flow from callers through runtime and application into ports and
domain rules. Adapters implement ports; optional SDKs load where selected.
Dagster owns scheduling and worker lifecycle. Core owns dataset meaning.
