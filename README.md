# DocSpec

DocSpec is a Python toolkit for repeatable document dataset experiments. It
builds selection catalogs, captures document bytes, and retains processing work
with the identities and evidence needed to check and reuse it. The goal is to
try different processors, inspect failures, add inputs, and compare results
without repeating unchanged work.

The intended workflow is:

1. **Choose inputs and build a catalog.** A source adapter supplies records,
   candidate document locations, and provenance. A catalog policy records
   selection, exclusions, normalized metadata, and the source fields behind each
   interpretation. The catalog is useful before any document is fetched.
2. **Capture selected documents.** An injected fetcher supplies bounded byte
   streams. DocSpec retains the captured bytes, source identity, acquisition
   evidence, and outcomes independently of derived text.
3. **Process now or later.** Choose extraction, segmentation, and optional
   processors. A later attempt reuses retained inputs and unaffected work,
   while recording the implementation, configuration, and resources that changed.
4. **Inspect and compare retained attempts.** Keep results and failures
   attributable to their inputs. Extend the dataset or retry selected work
   without overwriting earlier evidence.
5. **Export when a consumer needs it.** Portable output applies its own checks.
   Catalog construction and document experiments do not require a search build.

The public Python runtime connects this workflow. [Decision 0002](docs/decisions/0002-shared-execution-and-the-acquisition-gap-ledger.md#what-docspec-is-for)
records the experiment-platform purpose. The
[implementation checklist](docs/dataset-experiments-todo.md) separates existing
mechanisms from missing interfaces and qualification.

## What you can use today

| Task | Current entry point and limits |
| --- | --- |
| Build and read a catalog | [`build_local_catalog` and `open_local_catalog`](docs/catalog-inputs.md) use a workspace, explicit policy and producer, and pinned inputs. Choose bounded supplied records or the optional installed SpicyDocs adapter. Catalog-only work creates no document-processing state; the `source-catalog` CLI also remains available. |
| Capture, extract, and segment documents | [`prepare_local_experiment`](docs/python-runs.md) builds the plan from a catalog, workspace, explicit limits, accepted producers, and selected implementations. Choose where to stop, retain the result, and process it later. Advanced callers can supply their own plan through `prepare_local_run`; the CLI uses the same runtime. |
| Choose fetchers and repair failures | [Configure local, HTTPS, S3, or custom fetchers](docs/fetchers.md). [Retry selected failed work](docs/repairing-failures.md) from verified completed stages; unchanged permanent failures remain visible until explicitly retried or relevant inputs change. |
| Resume and inspect a run | The Python runtime prepares, executes, reconstructs saved work, and reconciles results without caller-written request files. `run prepare`, `start`, `resume`, `reconcile`, `active`, and `status` provide command access. |
| Retain and choose alternative results | `PreparedLocalRun.retain()` or `document-release retain` keeps a verified result without changing current. `document-catalog select` chooses a retained result against an explicit expected current reference. Alternatives keep their original pinned base; see [experiment identities and selection](docs/experiments.md). |
| Reuse inputs and compare results | Changed extraction reuses captures; changed segmentation reuses representations; changed processors reuse segments and unaffected processor results from an explicit verified base. [`open_local_inspection` and `docspec inspect`](docs/inspection.md) explain scheduled work, complete results, reuse, failures, and differences. The full installed experiment exercise remains [D38](docs/dataset-experiments-todo.md#d38). |
| Export and independently read a dataset | [`export_local_result` and `open_result_export`](docs/result-exports.md) preserve active records, captured and derived bytes, and processing evidence without executing work again. Choose retained evidence or require nonempty text; both preserve complete outcome accounting. |

Capture-only results can be retained and used by a later processing plan through
the [Python runtime](docs/python-runs.md#capture-first-and-process-later).
Each result records its requested stages, and completion checks that prefix.
Dataset-wide recipes, such as search preparation, remain an
[extension task](docs/dataset-experiments-todo.md#d48).

Dagster is an optional execution adapter. It schedules DocSpec's work; DocSpec
owns dataset meaning, reuse, and result checks. The [installed Dagster example](docs/dagster-experiment.md)
uses native resources to inject implementations and native task execution,
cancellation, and re-execution.

## Retained state and portable output

A retained result references the workspace's verified record layers, blobs,
plans, and receipts so later experiments can reuse them. An optional
[result export](docs/result-exports.md) copies the active dataset and its typed
evidence into an independently readable Rulespec container. It needs neither
the original workspace nor its execution plugins.

Rulespec checks generic artifact membership and byte integrity. DocSpec checks
document identities, outcomes, source coordinates, processing evidence, and the
chosen text requirement. See [the output comparison](docs/architecture.md#what-comes-out).

## Quick start

Start with [contributor setup and focused tests](CONTRIBUTING.md) and the
[current architecture](docs/architecture.md). The
[documentation index](docs/documentation.md) links to maintained guides for
catalog evidence, extensions, and operations.
Then run the [offline walkthrough](docs/offline-walkthrough.md). It builds a
catalog of four supplied records, retains captures, repairs a missing-file
failure, and compares a pinned phrase processor's settings and reference data.
Later processing reuses captured bytes. It needs no network during execution;
portable export and large datasets have separate qualification.

```sh
uv sync --frozen --python 3.12
uv run --frozen pytest          # offline, standalone
uv run --frozen ruff check .
uv run --frozen docspec --help  # the one CLI
```

## Where things are

| | |
| --- | --- |
| Domain model (documents, segments, evidence) | `src/docspec/domain/` |
| Format adapters + source access | `src/docspec/adapters/` |
| Processing / segmentation | `src/docspec/processing/` |
| Dataset planning, execution, reuse, and publication | `src/docspec/application/` |
| Shared Python and CLI run setup | `src/docspec/runtime/` |
| Installed storage and delivery profiles | `src/docspec/storage_profiles/` |
| Conformance fixtures | `conformance/`, `fixtures/` |
| Decision records | `docs/decisions/` |
| Measurements and incidents | `docs/history/` |
| Experiment workflow and current usability gaps | [Dataset experimentation to-do list](docs/dataset-experiments-todo.md) |
| Contributor improvements and code cleanup | [Maintainability to-do list](docs/maintainability-todo.md) |

## Boundaries

DocSpec owns dataset catalogs and selection policy, document capture,
normalization, segmentation, retained attempts, and evidence addresses.
Processors own their domain interpretation; RefSpec can supply pinned reference
resources. Rulespec owns shared artifact structure and canonical byte rules;
DocSpec retains its document and experiment validation.

Source providers own publisher access, literal source facts, and independent
raw-data publication. The current integration uses the installed SpicyDocs
reader. Whether selected provider capabilities remain in SpicyDocs or move to
SpicyRegs is an [open ownership choice](docs/dataset-experiments-todo.md#d41),
not a prerequisite for DocSpec experiments. Reuse public wheel APIs and pinned
artifacts; provider packages remain independently usable.

SpicySearch owns search-dataset preparation and search semantics. SpicyEngine
owns native indexing and interactive serving. Proposed use of DocSpec to execute
Search's dataset recipes is tracked in
[D48–D50](docs/dataset-experiments-todo.md#d48); it does not move indexing or
serving into DocSpec.

## What the published schemas do not promise

Two facts consumers have depended on that no schema states, both verified
against `src/docspec/schemas/source_catalog/1.0/source-item.schema.json`:

- **Normalized dates are unconstrained strings.** `publicationDate`,
  `commentCloseDate` and `lastUpdatedDate` are each `null` or a non-empty
  string — no `format`, no `pattern`. Anything comparing them as `YYYY-MM-DD`
  text is relying on a convention this schema does not enforce, and a source
  emitting another shape is schema-valid. Check before comparing, or state the
  assumption where you compare.
- **Field provenance is per field, not per item.** Every entry of
  `interpretations[].result.fields[]` carries `sourcePaths`: the distinct
  source paths that produced that one value. "Where did this field come from"
  is answered there, not inferred from the item.

**Status:** internal, unpublished; no license selected.
