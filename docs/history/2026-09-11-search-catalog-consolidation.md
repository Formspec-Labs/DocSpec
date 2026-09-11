# Search catalog construction and DocSpec

**Recommendation: use DocSpec to run and retain search-dataset builds, with the
search transformations supplied as an injected dataset recipe.** Share the
catalog reader, dataset definitions, and repeated build mechanics. SpicyEngine
continues to construct its native index and run searches against the completed
dataset.

The benefit is one place to manage inputs, attempts, failures, reuse, and results.
A user can try another preparation policy or reference resource against retained
inputs, compare the outputs, and rebuild an index without repeating preparation.
Moving files between repositories alone would not deliver that benefit.

This is a static investigation requested on 2026-09-11, using separate SpicySearch
builder, SpicyEngine consumer, and independent architecture subagents. The parent
agent checked DocSpec's actual extension interfaces and selected cross-product
implementations. No builds, tests, corpus processing, services, or index operations
were run. No implementation changes were made in any of these products.

## What we inspected

The user's repository reference was resolved to the local `spicysearch` and
`spicyengine` projects. Main branches do not contain all the newer dataset work.
The review therefore distinguishes main from implemented candidate worktrees.

| Code | Reviewed revision and limits |
| --- | --- |
| DocSpec | `32b856ae021eaf3a15afa72ae94ad02e7ca4287c`; code matches merged `dd18fb3`, with the new planning document. Unrelated untracked catalogue findings were left alone. |
| SpicySearch main | `10824d44ab31260e081b66b8da54678132b9ffe0`, clean when inspected; metadata snapshot/composition path. |
| SpicySearch direct dataset | `8f5a909afbe7fa23e86bf5f7a91b53f706939b3c`; retained direct JSONL builder. Local planning/history changes do not establish runtime success. |
| SpicySearch Parquet candidate | `429a518efd43d32093ca3bfc2b2d26a7d2091aec`, clean when inspected; direct typed Parquet builder used for the proposed integration. |
| SpicyEngine main | `5fa14f465cedf334704b8847ddf5dd096af5036e`, clean when inspected; snapshot export plus combined JSONL loading. |
| SpicyEngine direct/Parquet candidates | Direct JSONL `d6b6aa2`, Parquet `8da32c5`, combined candidate `12ddf52`. The combined worktree changed during review. The architect bounded its follow-on to committed `d9fce6f7409b93e23c146e9a2edf00f01261f36a`; later edits are outside this review. |

Below, `Search candidate:` means paths within SpicySearch at `429a518`;
`Engine candidate:` means committed `d9fce6f`, except where `12ddf52` is stated.
These revisions are inspection evidence, not a claim of merge, deployment,
successful indexing, or current runtime health.

## What catalog construction actually does

The direct search builder starts with one or more pinned DocSpec `SourceCatalog`
artifacts and optional verified topic outputs. It does not fetch document bodies
or run a RefSpec processor. It first examines the catalog population and stages
identifier collisions and topic lookups in SQLite. It then prepares each subject,
joins eligible topic results, constructs the search row, validates it, and writes
typed Parquet. Finally it publishes a Rulespec artifact containing declared
partitions, schemas, input pins, preparation identity, counts, and optional
vocabulary lookup data.

Evidence: `Search candidate:src/spicysearch/build/search_dataset.py:129–224,
246–310,338–377`; preparation in `source_catalog_metadata.py:2121–2325`;
row construction in `build/native_record.py:27–161`.

The search population includes catalog records that were not selected for body
acquisition. Their metadata and exclusion/failure dispositions remain useful.
Routing this build through DocSpec's document-acquisition selection would change
the population. Search's plan explicitly distinguishes the full catalog universe
from its selected file subset (`SpicySearch main:PLAN.md:1303–1321`). DocSpec's
document view maps unavailable/failed catalog items to excluded processing state
([source catalog model](../../src/docspec/domain/source_catalog.py#L348)).

SpicyEngine main renders existing snapshot tables into another JSONL export.
The newer candidate already avoids that pass: it admits the direct dataset and
builds its native index from the exact declared Parquet partitions. A new DocSpec
integration should reuse that loader, not build another export-and-convert route.
Evidence: `SpicyEngine main:tools/export_catalogues.py:61–85,113–184`;
`Engine candidate:dataset.py:47–155`, `catalogue.py:57–65`.

## Where sharing would reduce work

| Candidate | What to share or remove | What remains separately owned |
| --- | --- | --- |
| Catalog admission and row iteration | Finish DocSpec's public, admitted, located mapping reader; remove Search's version-guarded private-reader workaround. | Search's own preparation rules and output checks. Existing backlog D09 owns the reader change. |
| Search row definitions | Maintain fields, scopes, identifier roles, dates, and logical/physical schemas once in an installed search-definition API. Both recipe and engine use it. | Engine SQL, analyzer configuration, query weights, and index resource settings. |
| Exact identifier normalization | Reuse one normalizer and identity-bearing policy through the installed API, with conformance cases. | Source identifier facts and user query interpretation remain distinct responsibilities. |
| Attempt and output lifecycle | Reuse DocSpec's retained inputs, run identity, failure accounting, result reuse, and finalization through a dataset-level entry point. | The search recipe's census, joins, field eligibility, enrichment choices, and semantic validation. |
| Schema validation mechanics | Review the two compiled-fast-accept/Python-diagnostic implementations for a small shared implementation, preserving each caller's error behavior. | Schema ownership and product-specific acceptance rules. A new framework is unnecessary. |
| Artifact integrity and publication | Continue using the existing Rulespec artifact and atomic-publication library. | Independent producer and consumer checks at their actual trust boundaries. Those checks are not duplicate authority merely because both read bytes. |
| Older reconstruction routes | After parity is demonstrated, retire superseded snapshot-to-native export and repeated build bookkeeping. | Search quality evaluation and any explicitly retained research/reproduction use. |

Exact evidence for these candidates:

- `SpicySearch main:src/spicysearch/platform_source_catalog.py:18–32,66–79`
  imports DocSpec's private row iterator. Its comment about a future public API
  is not implementation evidence; the current
  [DocSpec reader](../../src/docspec/adapters/catalog_artifact/reader.py#L32)
  still lacks the requested public mapping access.
- `Search candidate:src/spicysearch/artifacts/native_fields.py:7–89` repeats
  roles, scopes, dates, and columns held in `Engine candidate:catalogue.py:3–48`.
  The latter mixes those definitions with query weights. Engine's
  `dataset.py:75–96` separately assembles the expected row definition and
  normalization policy. Producer `identifier_normalization.py:12–41` and
  Engine's committed `12ddf52:engine.py:303–306` implement the same normalization
  steps; use their behavior as the consolidation target.
- [DocSpec's schema gate](../../src/docspec/adapters/catalog_artifact/schemas.py#L40)
  and `Search candidate:src/spicysearch/schema_gate.py:23–56` share the same
  compiled-accept/fallback design, but expose different APIs and diagnostics.
- The direct builder already calls shared `prepare_metadata_subject` and
  `native_record` implementations. Do not count these as duplicated functions
  merely because both the old and new routes call them.
- Direct publication already delegates to Rulespec through
  `Search candidate:src/spicysearch/verified_files.py:732–758`; Engine candidate
  imports that same artifact library in `dataset.py:8–11`.

## The missing DocSpec capability

DocSpec's current processor execution is per segment. It constructs a request
from exactly one segment, requires `docspec-segment/1`, and resolves prerequisites
for that same segment. `StoreTask` requires a saved `DocumentStore`. Neither is
an honest representation of a metadata-only, catalog-wide join.

Evidence: [processor requests and input checks](../../src/docspec/application/processor_rules.py#L44),
[processor execution](../../src/docspec/application/processor_runtime.py#L65), and
[task inputs](../../src/docspec/domain/execution.py#L245).

DocSpec does have useful foundations: its catalog policy workspace provides
bounded lookup storage and staged input passes, while document execution already
has checkpoints, resource accounting, and processor reuse. The catalog policy
currently returns `SourceCatalogItem`; it does not produce arbitrary search rows.
Reuse these foundations without forcing a search table into a document segment
or extending the source-catalog schema with engine-specific fields.
Evidence: [catalog workspace and input access](../../src/docspec/ports/source_catalog.py#L127),
[catalog policy output](../../src/docspec/ports/source_catalog.py#L283), and
[existing processor reuse test](../../tests/test_processor_reprocessing.py#L114).

The smallest proposed extension is a bounded **dataset recipe**: a supplied
implementation receives pinned inputs/resources and an owned workspace, then
returns named output files/partitions and its verification result. DocSpec tracks
the attempt and reuse; the recipe defines the computation and output meaning.
  The initial unit can be a whole build with reuse of a completed matching result.
Partition-level resume is additional behavior to implement and prove, not an
automatic property of wrapping the existing builder.

Global dependencies matter. A changed identifier census or vocabulary can alter
rows in a locally unchanged partition. Dependency tracking must account for that
before claiming selective reuse. Keep data in bounded readers/workspaces and
pass references to workers; do not collect a catalog in one Python list.

## Proposed division of responsibility

| Component | Responsibility |
| --- | --- |
| Source provider, whether SpicyDocs or SpicyRegs | Publisher facts, source acquisition, source identity/coverage, and source-aware validation; reused through installed wheels. |
| DocSpec | Catalog and retained dataset state, injected recipe execution, attempts, dependency-aware reuse, budgets, failure accounting, inspection, comparison, and optional publication. |
| Search recipe and shared definition API | Search field selection, metadata preparation, topic joins/eligibility, row schema, identifier normalization, and search-dataset semantic checks. Reuse the current implementation. |
| SpicyEngine | Supported-dataset admission, native index, analyzer/SQL/ranking settings, activation and rollback, queries, filters, and display. |
| Rulespec / RefSpec | Existing generic artifact machinery / governed reference resources and public readers, respectively. |

The search recipe can live in the existing Search wheel. No new repository or
shared-package project is required merely to draw this boundary. DocSpec core
should not import SpicySearch, SpicyEngine, or SereneDB; a composition root supplies
the recipe. Engine consumes immutable outputs and shared definitions, not the
DocSpec workspace.

**Runner identity and semantic producer identity are different.** Executing a
Search recipe in DocSpec need not rename the artifact producer to `docspec`.
Keep the recipe's output kind and semantic verifier unless their meaning changes;
record the DocSpec implementation separately as execution evidence. The current
Engine candidate explicitly admits the `spicysearch` producer and verifier
version 2 (`dataset.py:60–64`). A runner change alone does not require a new format.

## Smallest useful proof and decision limit

Start by sharing the public catalog reader and search definitions. Then run the
existing direct preparation recipe through the smallest useful DocSpec dataset
entry point. Demonstrate:

1. A metadata-only search build and an ordinary retained-document experiment use
   the same attempt/reuse mechanism, with no fake capture or segment records.
2. The prepared population, rows, dispositions, source coordinates, repeated text,
   date conflicts, identifier collisions, and topic eligibility match the intended
   behavior. Include independent expected cases, not only old/new equivalence.
3. Changing recipe configuration or a global lookup invalidates affected work;
   unchanged completed work is reused at the declared granularity. An interrupted
   build is never reported as complete.
4. The current Engine direct loader admits the resulting dataset. Rebuilding its
   index with different settings requires no fetch or dataset preparation.
5. A clean installed-package setup works without sibling source paths. Record
   input scans, preparation calls, memory/scratch use, and bytes written for a
   representative workload; explain any new scan or state store.
6. The change deletes a named duplicate production path or lifecycle mechanism.

The strongest counterargument is that the direct builder is already a cohesive
two-pass routine with completed-output reuse. A general framework could add more
interfaces and state than it removes. If the proof cannot serve both concrete
workflows while eliminating repeated lifecycle work, keep the build callable in
Search and limit consolidation to readers, definitions, and shared primitives.

The three reviewers agree on shared lifecycle, an injected dataset-level recipe,
and keeping native indexing/serving in Engine. They do not establish that the
larger extraction is already worthwhile in implementation. That remains subject
to the proof above. These conclusions extend the
[to-do list](../dataset-experiments-todo.md) as D47–D50 and refine D09/D31.
