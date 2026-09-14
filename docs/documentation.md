# Documentation guide and ownership

Start with the [README](../README.md), run the
[offline walkthrough](offline-walkthrough.md), then choose the guide for the
behavior you want to use or change. [Contributing](../CONTRIBUTING.md) covers
setup, code ownership, and focused tests.

Maintainers edit these guides directly alongside their code changes.

| Reader's task | Maintained guide |
| --- | --- |
| Understand the inputs, processing flow, outputs, and checks | [Current architecture](architecture.md) |
| Read the general dataset, provenance, retention, and reuse model | [Core model — editor’s draft](core-model.md) |
| Set up a checkout and choose a bounded contribution | [Contributing](../CONTRIBUTING.md) |
| Capture, repair, process and compare a small supplied-record experiment | [Offline walkthrough](offline-walkthrough.md) |
| Implement a pinned local processor with literal quote evidence | [Phrase matching example](phrase-matching-example.md) |
| Change catalog selection, policy input, or processing evidence | [Catalog and processing](catalog-and-processing.md) |
| Admit a pinned catalog once and stream objects or dictionaries | [Public catalog reading](catalog-evidence.md) |
| Build a catalog from provider data or bounded supplied records | [Catalog inputs](catalog-inputs.md) |
| Filter retained publisher topics without fetching documents | [GAO topics](gao-topics.md) |
| Inspect retained comment fields and attachment candidates | [SpicyRegs comments](spicyregs-comments.md) |
| Inject provider acquisition and process retained XML later | [GovInfo bill example](govinfo-bill-example.md) |
| Build from complete MODS metadata and process one annual section | [Annual CFR example](govinfo-cfr-example.md) |
| Catalog retained committee metadata with its source evidence | [FEC committee example](fec-committees.md) |
| Retain alternative results, understand resume, and choose the current result | [Dataset experiments](experiments.md) |
| Configure an experiment, retain its result, and recover saved work | [Python runs](python-runs.md) |
| Inject components through native resources and execute with Dagster | [Dagster experiment](dagster-experiment.md) |
| Choose markup, visible text, PDF, or image handling and understand source coordinates | [Representations](representations.md) |
| Configure document transports and understand acquisition evidence | [Fetchers](fetchers.md) |
| Retry accepted failures while preserving completed inputs | [Repair failed work](repairing-failures.md) |
| Understand SDK requests, item attempts, and native task retries | [Retry ownership](retry-ownership.md) |
| Add a source, fetcher, processor, Dagster resource, or storage adapter | [Extensions](extensions.md) |
| Inspect saved work, output, failures, reuse, or differences | [Inspection](inspection.md) |
| Export active results and read them without the original workspace | [Result exports](result-exports.md) |
| Account for shared inputs and preview local blob storage | [Retention preview](retention-preview.md) |
| Understand recovery, publication, retention, compaction, or qualification | [Operations](operations.md) |
| Distinguish regression results, capacity claims, and publication evidence | [Qualification](qualification.md) |
| Change schemas or sealed fixtures | [Schema maintenance](schema-maintenance.md) |
| Understand supported identity values and the shared encoder | [Canonical JSON](canonical-json.md) |
| Choose a catalog, fixture, mint, or reporting tool | [Tool inventory](../tools/README.md) |

The Core model defines semantics; the [architecture guide](architecture.md)
describes the implementation. Core owns document processing, reuse, inspection,
cleanup, and portable export.

Planning records remain available for maintainers: the
[implementation plan](core-model-implementation-plan.md) records component choices,
the [completed task list](core-model-implementation-tasks.md) records delivery,
and the [ownership map](core-model-implementation-map.md) records replaced owners.
The [consensus record](history/2026-09-13-core-model-consensus.md) preserves planning
evidence. Dated reviews and measurements apply to their recorded code revision.

A catalog can be used without document acquisition or search. The Python runtime
retains capture, extraction, segmentation, and processing results for later use.
The [inspection API and commands](inspection.md) explain completed and unfinished
work through existing checkpoints and receipts. The offline walkthrough exercises
capture-first work, failure repair, later processing and retained alternatives.
Broader scale and acceptance exercises remain separate.

Retain results in the workspace for later experiments. Use the optional
[result export](result-exports.md) when another consumer needs an independent
dataset. Core checks retained state; Rulespec verifies portable artifact membership and
bytes, and DocSpec admits their Core meaning. The [architecture comparison](architecture.md#what-comes-out) identifies
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
