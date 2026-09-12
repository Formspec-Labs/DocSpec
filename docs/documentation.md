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
| Build a synthetic catalog and verify one local application release | [Offline walkthrough](offline-walkthrough.md) |
| Change catalog selection, policy input, or processing evidence | [Catalog and processing](catalog-and-processing.md) |
| Admit a pinned catalog once and stream objects or dictionaries | [Public catalog reading](catalog-evidence.md) |
| Retain alternative results, understand resume, and choose the current result | [Dataset experiments](experiments.md) |
| Prepare, execute, and recover a run using typed Python objects | [Python runs](python-runs.md) |
| Add a processor, execution backend, sink, or storage adapter | [Extensions](extensions.md) |
| Understand recovery, publication, retention, compaction, or qualification | [Operations](operations.md) |
| Change schemas or sealed fixtures | [Schema maintenance](schema-maintenance.md) |
| Choose a catalog, fixture, mint, or reporting tool | [Tool inventory](../tools/README.md) |
| Plan the dataset experiment workflow and further simplification | [Dataset experimentation to-do list](dataset-experiments-todo.md) |

A catalog can be used without document acquisition or search. The Python runtime
retains capture, extraction, segmentation, and processing results for later use.
Checkpoints and receipts describe completed and unfinished work; unified attempt
inspection remains [D19](dataset-experiments-todo.md#d19). The offline walkthrough exercises one
application release, without demonstrating every intended stopping point or
the complete iterative workflow.

Choose the output guide before choosing a verifier. Application release state
and portable document bundles currently both use version `2.0`, with different
shapes and checks. The [architecture comparison](architecture.md#what-comes-out)
names each path. Optional export and shared structural verification are open
work in [D26–D27](dataset-experiments-todo.md#d26), so an application commit does
not imply that a portable bundle was produced.

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
