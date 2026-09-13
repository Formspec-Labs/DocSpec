# Inspect shared experiment storage

Keep explicit result and checkpoint references, then build a retention set and
inspect the local blob inventory. This answers which stored bytes those roots
require, including their shared bases. The current pointer alone does not name
all alternatives you may want to keep.

```python
from docspec.runtime import build_local_retention_set, preview_local_blob_inventory

retention = build_local_retention_set(
    plan, workspace,
    document_release_producer=accepted_document_producer,
    retained_releases=(first_result, second_result),
    max_spooled_bytes=64 * 1024**2,
)
inventory = preview_local_blob_inventory(
    plan, workspace, retention,
    document_release_producer=accepted_document_producer,
    max_spooled_bytes=64 * 1024**2,
    sample_limit=20,
)
print(inventory["retainedByteCount"], inventory["candidateByteCount"])
```

For unfinished work, also pass exact saved `retained_stores` references and
their `retained_plans`. A prepared task exposes its original store as
`task.input_store`; its plan reference is `prepared.handoff.processing_plan`.
If you want to preserve a later checkpoint, retain that exact later store
revision. A planned store may contain no captured bytes yet, but its saved plan
can require a captured base. Missing or conflicting plan references refuse.

The plan argument selects local storage profiles. It does not automatically
select the plan's work or every attempt in the workspace. All selected roots
must use the same admitted local blob profile state. No execution plugins are
constructed for either operation.

The builder verifies selected results, checkpoints, their required predecessors
and bases, and exact blob references. It uses the existing record workspace to
deduplicate visits and refuses conflicting immutable references. Retention-set
format 2.0 records explicitly supplied plan references alongside result/store
roots. Previous retention-set formats are retired.

The inventory checks a supplied set and each listed blob. Its candidates are
objects unreferenced by that set at inspection time. An imported set's complete
relationship to its declared roots is not rederived by this reader. Build a
fresh set from all required roots; concurrent work and omitted alternatives can
change which bytes are needed. This is a storage preview, not deletion authority.

Building a set writes immutable retention evidence. Reading the inventory
changes no dataset files. Both use disposable SQLite scratch outside the
dataset; `max_spooled_bytes` bounds canonical record payloads, excluding SQLite
overhead. Samples and the SQLite memory cache are bounded separately. An
interrupted preview can be repeated; there is no deletion operation or
destructive-cleanup recovery claim.

[The runtime tests](../tests/test_retention_runtime.py) demonstrate shortened
successors, standalone planned work, shared alternatives, and interrupted
read-only inventory. Tests remove candidate blobs only inside disposable
fixtures to prove that the chosen roots remain usable. These tests do not
authorize cleanup of existing datasets or qualify corpus-scale capacity.
