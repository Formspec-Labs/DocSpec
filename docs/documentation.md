# Documentation guide and ownership

Start with the [README](../README.md) for DocSpec's purpose: build a catalog,
capture selected documents through an injected fetcher, process retained inputs
now or later, and inspect comparable attempts. That is the intended experiment
workflow. The README distinguishes current entry points from the interfaces and
qualification still tracked in the
[dataset experimentation checklist](dataset-experiments-todo.md).

Maintainers edit `README.md`, `CONTRIBUTING.md`, and the guides directly under
`docs/`. Update the relevant guide, code links, and contributor task map in the
same change as the behavior or file move they describe.

| Reader's task | Maintained guide |
| --- | --- |
| Understand the inputs, processing flow, outputs, and checks | [Current architecture](architecture.md) |
| Set up a checkout and choose a bounded contribution | [Contributing](../CONTRIBUTING.md) |
| Capture, repair, process and compare a small supplied-record experiment | [Offline walkthrough](offline-walkthrough.md) |
| Implement a pinned local processor with literal quote evidence | [Phrase matching example](phrase-matching-example.md) |
| Change catalog selection, policy input, or processing evidence | [Catalog and processing](catalog-and-processing.md) |
| Admit a pinned catalog once and stream objects or dictionaries | [Public catalog reading](catalog-evidence.md) |
| Build a catalog from provider data or bounded supplied records | [Catalog inputs](catalog-inputs.md) |
| Retain alternative results, understand resume, and choose the current result | [Dataset experiments](experiments.md) |
| Configure an experiment, retain its result, and recover saved work | [Python runs](python-runs.md) |
| Inject components through native resources and execute with Dagster | [Dagster experiment](dagster-experiment.md) |
| Choose markup, visible text, PDF, or image handling and understand source coordinates | [Representations](representations.md) |
| Configure document transports and understand acquisition evidence | [Fetchers](fetchers.md) |
| Retry accepted failures while preserving completed inputs | [Repair failed work](repairing-failures.md) |
| Understand SDK requests, item attempts, and native task retries | [Retry ownership](retry-ownership.md) |
| Add a processor, execution backend, sink, or storage adapter | [Extensions](extensions.md) |
| Inspect saved work, output, failures, reuse, or differences | [Inspection](inspection.md) |
| Export active results and read them without the original workspace | [Result exports](result-exports.md) |
| Account for shared inputs and preview local blob storage | [Retention preview](retention-preview.md) |
| Understand recovery, publication, retention, compaction, or qualification | [Operations](operations.md) |
| Change schemas or sealed fixtures | [Schema maintenance](schema-maintenance.md) |
| Understand supported identity values and the shared encoder | [Canonical JSON](canonical-json.md) |
| Choose a catalog, fixture, mint, or reporting tool | [Tool inventory](../tools/README.md) |
| Plan the dataset experiment workflow and further simplification | [Dataset experimentation to-do list](dataset-experiments-todo.md) |

A catalog can be used without document acquisition or search. The Python runtime
retains capture, extraction, segmentation, and processing results for later use.
The [inspection API and commands](inspection.md) explain completed and unfinished
work through existing checkpoints and receipts. The offline walkthrough exercises
capture-first work, failure repair, later processing and retained alternatives.
Broader scale and acceptance exercises remain separate.

Retain results in the workspace for later experiments. Use the optional
[result export](result-exports.md) when another consumer needs an independent
dataset. Both use Rulespec's shared artifact checks; DocSpec checks the document
meaning. The [architecture comparison](architecture.md#what-comes-out) identifies
their different purposes and readers.

The [decision index](decisions/README.md) distinguishes accepted rules, later
amendments, and implementation gaps. Code and executable checks establish
current behavior; a maintained explanation should link to those owners without
creating a competing specification. When behavior and an accepted rule differ,
record the gap explicitly.

`docs/history/` retains measurements, incidents, and provenance. Each report
describes its recorded inputs and revisions; it is not automatic evidence about
today's code or a later deployment. The maintainability
[to-do list](maintainability-todo.md) and [evidence](maintainability-progress.md)
track the refactor and its local checks.

The former generated wiki was consolidated into these guides on 2026-09-11.
Its useful operational explanations were checked against current owners; stale
code summaries and duplicate navigation were retired. The
[consolidation record](history/2026-09-11-wiki-consolidation.md) retains exact
generation metadata, a file inventory, and the Git revision containing the tracked
snapshot. There is one maintained documentation tree. Generated output should
not overwrite these guides or become a second current-behavior authority.
