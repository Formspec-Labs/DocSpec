# Use DocSpec from Python

`CoreWorkspace` is the public local runtime. It connects the SQLite metadata
ledger, immutable content, bulk state storage, and the common operation
lifecycle. Closing and reopening the workspace preserves exact result selections.

## Create and revise keyed values

```python
from docspec.domain import core
from docspec.runtime import CoreWorkspace

with CoreWorkspace(workspace_path) as workspace:
    workspace.create("original", [
        ("notice", {"url": "https://example.org/notice", "title": "First title"}),
        ("other", {"url": "https://example.org/other", "title": "Other title"}),
    ])
    workspace.revise(core.Revision(
        format_version=1, revision_id="remove-other", base_state_id="original",
        result_state_id="revised",
        edits=(core.Remove(sequence=1, member_key="other"),),
    ))
    print(workspace.compare("original", "revised"))
```

`create` accepts a one-shot iterator of `(key, JSON value)` pairs. Keys identify
members; occurrence IDs identify immutable values. The writer consumes payloads
through bounded batches and spools only compact membership addresses. Equal
values may have different occurrence IDs. Revision edits have explicit sequence
numbers; an invalid intermediate edit refuses the revision.

`workspace.rows(state_id)` streams `(key, Entity)` pairs in deterministic key
order. Close the iterator when stopping early. `compare` computes complete
counts in DuckDB and returns a bounded sample; it distinguishes a changed
occurrence from changed value content.

Run the [two-field example](../examples/core_values.py) with
`python -m examples.core_values /path/to/new-workspace`. It performs immutable
title and URL edits, keeps the original result for the title change, executes
again for the URL change, then closes and reopens the exact retained selection.

For independent value changes, use `prepare_value_edits` from
`docspec.application.core_edits`. Supply at most 256 `(occurrence_id, patch)` pairs
within the shared byte limits. It returns one prepared operation and the
`ValueEdit` records needed by a membership revision. Publish the operation before
applying that revision. Each replacement retains its own usage, generation and
derivation evidence; a failed patch refuses the batch. `prepare_value_edit` uses
the same implementation for one replacement.

## Execute and reuse work

Use `workspace.operations.run(definition, request, producer)` for a fresh
attempt. Use `resolve` with a selection ID, target and reuse policy to choose a
matching retained result or execute the same producer. A producer receives an
operation context: read its declared inputs, generate or adopt outputs, and
record actual derivation relationships there.

A request declares its material dependencies. Whole inputs, named fields and
state-member fields use the same typed comparison rules. A matching hash finds
candidates; dependency adequacy, exact comparison evidence, availability and
policy determine whether a candidate can be selected. Different results may
coexist for the same comparison key.

A selection ID identifies a particular choice and its retry. Repeating it
recovers that exact choice. Supply another selection ID and `fresh=True` for
another observation. `workspace.inspect("selection", selection_id)` shows the
requested inputs, selected result and original execution request. Output
availability comes from ledger metadata; inspecting it does not audit every
output byte.

For verified continuation and publication recovery, use the same
`CoreOperations.resume` and `recover` owner. Checkpoints and interrupted
publication journals remain protected while the recorded operation is
recoverable. A failed attempt remains visible even when a later attempt succeeds.

## Capture first and process later

```python
from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.application.document_processors import content_statistics_processor
from docspec.domain.content import CandidateFile, SourceItem
from docspec.runtime import CoreWorkspace

with CoreWorkspace(workspace_path) as workspace:
    documents = workspace.documents(fetcher=LocalFileContentFetcher(input_path))
    documents.import_sources([
        SourceItem("notice", "1", (CandidateFile("text", "notice.txt", "text/plain"),)),
    ], state_id="catalog")
    documents.run("catalog", run_id="captured", extract=False, segment=False)
    documents.run("catalog", run_id="processed",
                  processors=(content_statistics_processor(),))
```

The second run reuses usable captures. Extraction, segmentation and processor
graphs use Core requests, results and selections. Retained segment values keep
positions and source coordinates. Choose other extractor and segmenter objects
through `workspace.documents`; their implementation settings become material
operation definitions.

The pipeline groups candidate lookup and publication across documents at each
stage. `max_source_bytes` and `max_generated_rows` optionally limit newly fetched
bytes and generated bulk rows across attempts at the same run. Core checkpoints
preserve those counts on handled failure; reused work is uncharged, and zero
permits reuse only. Source, configuration and limits are pinned to the run ID.
An abrupt kill without a checkpoint leaves consumption uncertain, so continuation
refuses to reset it. A new run ID deliberately starts a new budget and can reuse
completed stages. The history preserves failures and successful empty results.

The [offline walkthrough](../examples/offline_demo.py) proves failure repair,
reopening, configuration and vocabulary changes, unchanged upstream work, exact
quote offsets, and agreement with a clean rebuild. The
[representation walkthrough](../examples/representation_choices.py) compares
markup with visible text while preserving source evidence.

Source catalogs remain independently usable through `build_local_catalog`,
`open_local_catalog`, and `preview_local_catalog`. These accept a workspace or a
path and use the existing source-catalog readers and policies. A catalog-only
operation does not create a Core ledger.

## Export retained evidence

`workspace.export(state_id, destination, producer=producer,
max_output_bytes=limit)` writes an independently admitted Rulespec artifact and
returns its `ArtifactPin`. The producer description must contain pinned
implementation identifiers. Open it with `docspec.result_export.open_result_export`,
supplying the expected pin, producer and byte limit. Its `rows`, `record`, and
`read_blob` methods work without the original workspace.

The default scope is the selected state and its required evidence. For a complete
document run, pass `additional_roots=documents.retained_roots(run_state_id)`.
This includes the run's exact source and stage selections; arbitrary JSON fields
are not interpreted as retention links. The artifact records its explicit roots.

## Select current and remove authorized content

`workspace.maintenance.select_current(update_id, dataset, target,
expected_current)` changes a current pointer only if its previous value still
matches. A target is a `(kind, identity)` pair. Selecting another state preserves
the earlier state and its history.

Content removal requires an explicitly retained `core.RetentionPolicy`. Its
closed description names `remove` keys and a `collect_unreferenced` boolean.
`remove_under_policy` refuses unsupported scope and outstanding retention
obligations. It records authorization, availability changes and each physical
outcome. Use `resume(update_id)` after interruption. Historical identities and
provenance remain even when authorized bytes have been removed.

## Use the same operations from commands

`docspec state create`, `state revise`, `state rows`, `execute`, `retain`,
`inspect`, `compare`, `select`, `remove`, `resume-removal`, and `export` call the same
runtime. `docspec document import` and `document run` expose the document stages;
`docspec source-catalog` preserves independent source tooling. Each command's
`--help` lists its inputs.

The [task list](core-model-implementation-tasks.md) records the remaining caller,
conformance and capacity work. Passing a small example establishes behavior;
full-path throughput and memory qualification remain explicit acceptance gates.
