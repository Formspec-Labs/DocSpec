# Preview a catalog and grow a dataset

`preview_local_catalog` explains an admitted catalog before any document is
fetched. It shows the catalog policy's selection counts and reasons, samples of
the exact candidate rows, and optional changes from a previous catalog. Use it
after [building a catalog](catalog-inputs.md) and before starting document work.

```python
from docspec.runtime import preview_local_catalog

preview = preview_local_catalog(
    new_catalog.reference, workspace,
    producer=accepted_catalog_producer,
    previous_ref=old_catalog.reference,
    sample_limit=20,
    max_sample_bytes=1024**2,
)
print(preview["catalog"]["catalogSelection"])
print(preview["comparison"]["counts"])
for row in preview["sample"]:
    print(row["documentId"], row["selection"], row["candidateRenditions"])
```

Omit `previous_ref` for a first catalog. Both references must be available in
this workspace and pass the explicit producer acceptance. Previewing creates no
directories, tasks, or current pointers. It uses the existing admitted reader
and reads every row, even when samples are disabled.

The comparison counts added, changed, unchanged, and `removedFromCatalog`
items by their full source-qualified IDs. A changed-row sample names the
changed fields and records before/after values. Selection and interpretation
changes stay distinct from candidate changes: a new policy explanation does
not necessarily require new document bytes. A missing field and an explicit
`null` are different; such differences include `olderPresent` and `newerPresent`.

Counts describe the complete catalogs. The current-row and changed-row samples
each have their own item and byte cap. `sampleBytes` measures the sum of their
canonical entry bytes; summary metadata and report structure are outside that
sample cap. `sampleTruncated` says that details were omitted. A large row can
be omitted while a later smaller row fits. Zero limits keep counts without
samples. For complete detail, use the existing `open_local_catalog(...).iter_mappings()`
stream and exhaust or close it.

## Review selection and actual work separately

A catalog policy selects candidate documents and records why other rows are
excluded, unavailable, deleted, or failed. Applications choose which complete source state to import for processing.
A catalog preview does not predict processing cost.

Import admitted `SourceCatalogItem` rows into a Core source state, then run the
chosen document stages through `CoreWorkspace.documents`. Inspect selections and
results afterward with `workspace.inspect`, and compare resulting states with
`workspace.compare`. The current runtime resolves work in bounded batches;
there is no separate saved planner report that predicts every task or its cost.

Catalog preview counts the full source universe independently of processing.
[Inspection](inspection.md) reports actual retained results, availability and
attempt progress. A failed run may still have useful completed stages available
for the next requested run.

## Build successive full snapshots

Each catalog defines the whole chosen dataset universe. To grow a dataset,
build the new full snapshot with both existing and new items. The existing
`supersedes` argument records which catalog it follows:

```python
from rulespec_artifacts import Supersedes
from docspec.runtime import build_local_catalog

new_catalog = build_local_catalog(
    (full_source_snapshot,), workspace,
    policy=chosen_catalog_policy, catalog_id=dataset_catalog_id,
    producer=catalog_output_producer, max_scratch_bytes=128 * 1024**2,
    supersedes=Supersedes(
        old_catalog.reference.catalog_id, old_catalog.reference.digest,
        "Expanded the dataset and revised selection",
    ),
)
```

Succession records provenance; it does not merge inputs or select a current
catalog. A prior item omitted from the new catalog is absent from the newly imported
source state. Earlier retained states remain immutable. Preview labels it `removedFromCatalog`
because absence does not prove publisher deletion. This also applies to an
`observed-crawl`: that scope describes incomplete upstream observation, not an
instruction to append records or preserve omitted items. Include every item
you intend the successor to retain.

One chosen policy can accept multiple input parts when its existing selectors
support them. For example, disjoint supplied-record snapshots in the same
namespace and source version can form one catalog. Repeated qualified IDs
refuse under the supplied-record policy; DocSpec does not pick a version or
winner. Other policies can have an explicit collision rule, whose explanation
remains source evidence. The supplied-record policy selects one namespace and
version; combining arbitrary policies or namespaces needs a policy that
deliberately supports them. A source-local ID in a different namespace has a
different qualified identity and must not be substituted for the first.

## Reuse unchanged document work

Catalog descriptions can change while acquisition inputs stay the same.
DocSpec compares the qualified source ID, source-issued version, active state,
and complete candidate list, including candidate pins and metadata. When these
match, a changed title or catalog-policy explanation can replace the current
source row while reusing verified captured files and the deepest compatible
processing prefix. Unchanged processors do not run again.

The new result keeps the exact new source description. Its reused files retain
their original acquisition timestamps, downloader identity, task references,
and observed transport version. A source metadata refresh is not a new
acquisition. Changed material acquisition inputs require new capture; changed stage settings
are checked through the shared Core correspondence rules.
Missing or corrupt promised evidence refuses reuse.

A permanent failed item is not retried merely because catalog metadata or an
unrelated interpretation changed. Until an explicit retry or a relevant failed
input change admits repair, the retained result keeps that item's previous
description and failure. This can produce an intentional mixed result whose
failed item has older metadata than the new catalog. See
[failed-item repair](repairing-failures.md) for retry selection and retained-prefix
behavior.
