# Inspect work and compare results

Use `docspec.runtime.open_local_inspection` to see what a plan scheduled, what
the saved attempt produced or reused, and what its retained dataset contains.
The same view supplies the `docspec inspect` commands. Reading a result requires
no fetcher, processor, extractor, segmenter, or execution deadline.

## Choose the evidence to inspect

```python
from docspec.runtime import open_local_inspection

view = open_local_inspection(
    plan,
    workspace,
    document_release_producer=accepted_document_producer,
    release_ref=result_reference,
)
report = view.summary(sample_limit=10)
```

The plan must match the chosen result exactly. The workspace locates existing
storage and the plan's installed profile descriptions. Missing roots are a path
or setup error; inspection creates no dataset directories or execution state.

| Input choice | What it shows | Limit |
| --- | --- | --- |
| Neither result reference | The plan's jobs and each latest saved revision | An observation during execution, not an atomic snapshot or an identified completed attempt |
| `run_ref=...` | One reconciled run and its exact saved job revisions | A rejected run does not establish a complete active dataset |
| `release_ref=...` | One verified retained result, plus the run that produced it | Untouched inherited items belong to the result, but were not scheduled by this run |

Supply at most one reference. Existing output roots with no planned ledger
report `not-planned`. `docspec run active` can also report that condition before
output roots exist.

The report separates three populations:

- **Source catalog:** admitted input coverage, when separately requested.
- **Work:** the entries scheduled by this plan, including repairs and removals.
- **Result:** the complete reconciled or retained dataset, including untouched
  inherited entries. Live checkpoints are labeled incomplete work.

An unchanged successor can therefore have zero scheduled entries and a nonempty
result. Scheduled entries do not count every item matching the plan's filter.
That count is unavailable from the saved work ledger alone.

Source coverage is optional. Pass
`source_catalog_producer=accepted_source_producer` to admit the exact source
catalog with its independently accepted producer. DocSpec does not borrow document
producer acceptance or infer acceptance from an artifact's own labels. Without
that choice, the report explicitly marks source coverage unavailable.

## Read one item's evidence and output

```python
detail = view.source(source_item_id, sample_limit=5)
for row in view.records("representations", source_item_id=source_item_id):
    print(row["payload"])
```

`source` shows the exact job reference, requested stages, saved failures,
processor attempts and results, and bounded samples of that item's output rows.
Each result disposition carries the stages requested for that particular item.
The newest plan's stages do not describe all inherited items in a mixed result.

`records` streams a selected layer. The `layer_kinds` property lists available
layers, including `files`, `representations`, `segments`, `dispositions`,
`failures`, and requested `derived:...` layers. Rows include the existing blob
references and evidence coordinates. Use `read_blob(blob_reference,
max_bytes=allowance)` to stream the referenced bytes within an explicit limit.
Exhaust iterators to complete their checks, or close them when stopping early.

Sample limits cap returned details; zero gives counts without samples. Complete
totals still require scanning the relevant saved population. Byte reads obey
their explicit limit. Comparisons use disposable SQLite scratch, bounded by
the record profile, in the system temporary directory. That scratch is removed
on success or failure and never becomes retained dataset state.

## Compare two views

```python
before = open_local_inspection(
    older_plan, older_workspace,
    document_release_producer=accepted_document_producer,
    release_ref=older_result,
)
comparison = before.compare(view, sample_limit=10)
```

Views can use different plans, workspaces, and accepted producers. Comparison
matches stable source-item IDs. It distinguishes source input, requested
configuration, content and evidence coordinates, outcomes, and exact stored
provenance. A different delivery receipt can change provenance while document
content stays the same. Work comparison describes scheduled entries; result
comparison requires complete active results on both sides.

Content comparison preserves recorded input associations, including derived
`inputIds` and representation/segment `fileId` values. Moving the same value to
a different input is therefore a content change. Changed input IDs can also
produce that flag when the bytes agree; DocSpec does not normalize them into a
new semantic identity.

Plan and saved execution-setting differences are shown separately. Configuration
digests identify changed settings, but do not reveal custom settings that the
plugin never recorded. These differences explain the available evidence; they
do not prove a particular setting caused a quality change.

## Interpret reuse, failure, and cost

Stage summaries distinguish pending, partial, completed, unrequested, and
inapplicable stages. Counts distinguish newly saved outputs from reused prefixes.
They cannot reconstruct work lost before a checkpoint was saved.

Processor reports distinguish recorded calls from result origin. A cache hit
can follow a real call when another writer wins the cache update, so a cache
hit does not imply zero execution. Saved attempt receipts supply observed call
counts, failed attempts, and elapsed milliseconds. A failed attempt can precede
an eventual successful result.

Result-reported input/output bytes, duration, and external requests are grouped
by new, cached, or base-reused origin. Reused observations are not newly incurred
cost. Monetary cost, token use, lost transfers, and total wall-clock run duration
are unavailable. Fixed evidence timestamps are not timing measurements. Failure
details are the saved sanitized details, not the original provider error text.

Inspection checks saved identities and relationships without loading processing
plugins. Retained-result opening applies the existing release verifier. Inspection
does not establish that historical plugins remain runnable or that the output
has the semantic quality an experiment needs.

## Use the CLI

The command reuses an existing local run request for plan, locations, and accepted
producers. It does not execute that request or construct execution services.
Reference files contain the existing `ArtifactRef` or `DocumentReleaseRef` values.

```shell
docspec inspect summary --request run.json --release-reference result.json
docspec inspect summary --request run.json --run-reference run-ref.json --source-coverage
docspec inspect source --request run.json --release-reference result.json --source-item-id ITEM
docspec inspect records --request run.json --release-reference result.json --layer-kind files --sample-limit 5
docspec inspect compare --request old-run.json --release-reference old-result.json \
  --other-request new-run.json --other-release-reference new-result.json
```

Use `--other-source-coverage` for source admission on the second side of a
comparison. `run active` retains its filesystem-based liveness report, with
bounded stalled-job and diagnostic samples. Filesystem timestamps can indicate
when progress stopped; they cannot establish total execution duration.
