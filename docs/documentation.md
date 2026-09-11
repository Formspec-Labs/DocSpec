# Documentation ownership

Maintainers edit `README.md`, `CONTRIBUTING.md`, and the guides directly under
`docs/`. Update the relevant guide, code links, and contributor task map in the
same change as the behavior or file move they describe.

| Reader's task | Maintained guide |
| --- | --- |
| Understand the inputs, processing flow, outputs, and checks | [Current architecture](architecture.md) |
| Set up a checkout and choose a bounded contribution | [Contributing](../CONTRIBUTING.md) |
| Run one complete local example | [Offline walkthrough](offline-walkthrough.md) |
| Change catalog selection, policy input, or processing evidence | [Catalog and processing](catalog-and-processing.md) |
| Add a processor, execution backend, sink, or storage adapter | [Extensions](extensions.md) |
| Understand recovery, publication, retention, compaction, or qualification | [Operations](operations.md) |
| Change schemas or sealed fixtures | [Schema maintenance](schema-maintenance.md) |
| Choose a catalog, fixture, mint, or reporting tool | [Tool inventory](../tools/README.md) |

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
