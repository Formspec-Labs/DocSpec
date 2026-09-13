# Inspect existing evidence through one supported view

Decision: add a storage-only `open_local_inspection` factory and an application
`InspectionView`. Python callers and `docspec inspect` share the view. The CLI
reuses an existing run request for locations and accepted producers. It requires
no new inspection configuration format, persisted dataset object, dashboard,
or execution service.

This addresses [D19](../dataset-experiments-todo.md#d19) and the inspection part
of [D04](../dataset-experiments-todo.md#d04). The solutions architect subagent
recommended this shape after tracing current catalog readers, run receipts,
saved stores, and processor evidence. A separate evidence agent confirmed the
available facts and their limits. Independent code review and executed tests
are recorded separately.

## The user needs three different populations

Source catalog coverage describes supplied inputs. The work ledger describes
scheduled entries, which omit unchanged and unselected items. Reconciled active
layers or a retained release describe the complete resulting dataset, including
untouched inherited entries. A live plan observation describes each latest
saved job separately and does not invent an immutable attempt identity.

Mixing these populations would make a zero-task incremental run look empty or
make inherited data look newly processed. Reports label each population and
leave unsupported selection totals unavailable. Source coverage requires its
own explicitly accepted producer; document acceptance cannot authorize a source.

## Reuse readers and verification without reconstructing plugins

`summary`, `source`, `records`, `read_blob`, and `compare` expose existing evidence.
Exact run references use their pinned store revisions. Live observations read
the planned ledger and verify initial references before considering newer
revisions. Retained results use the existing catalog verifier. Complete run
result claims also require the shared required-layer and logical-link checks;
`stateful` alone cannot establish successful completion.

Plugin-independent receipt loading, output links, and processor receipt checks
are shared with checkpoint recovery. Recovery still owns selected-plugin and
replay checks. Calling the full recovery verifier for inspection was rejected:
users should be able to inspect historical evidence without reconstructing an
old processor environment.

The local adapters now accept `create=False` for existing-root construction.
This removes manual `object.__new__` storage setup and avoids staging-directory
creation during reads. It is an initialization choice, not a security restriction
on every adapter method. Inspection exposes only read operations.

An admitted retained reader supplies its exact active layers to run inspection;
matching those layers avoids repeating the full logical and blob verification.
A standalone run reference still needs those checks before a complete-result
claim. Rejected and stateless runs remain separately labeled incomplete results.

## Compare meaningful values and preserve their origins

Comparison matches stable source-item IDs and separates input, requested
configuration, byte/value/coordinate content, outcomes, and exact provenance.
Exact record comparison alone was rejected because delivery identifiers can
change while document content remains identical. Per-item dispositions supply
requested stages for mixed retained results; the current plan is insufficient.

Content fingerprints preserve recorded input associations (`inputIds` and
representation/segment `fileId`) to detect values moved between inputs. Changed
input identities can mark a content difference even when bytes agree. A new
semantic identity normalization system was rejected as unnecessary for D19.

Full comparison streams each layer into the existing bounded disposable SQLite
workspace, then joins ordered source identities. It avoids an all-items Python
dictionary and repeated full-layer scans for every source. Sample bounds limit
returned details, not the scan needed to establish complete totals. Existing
record-profile limits bound scratch; no scratch file becomes dataset evidence.

## Report only costs and completion that the evidence establishes

Processor attempts count recorded calls independently of cache disposition.
A cache hit can follow execution when another writer wins insertion. Result
resource use is grouped by origin so inherited observations are not presented
as newly incurred cost. Money, token use, total elapsed run time, and work lost
before checkpointing remain unavailable. Fixed evidence timestamps cannot
measure elapsed execution.

Pending, partial, complete, unrequested, and inapplicable stages remain distinct.
An output count of zero does not itself prove completion; receipts establish
empty segmentation and processor outcomes. Rejected runs remain incomplete
results. These distinctions preserve the value of the existing evidence without
adding another run ledger or attempting semantic quality assessment.
