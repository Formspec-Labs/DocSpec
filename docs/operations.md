# Recovery, publication, and maintenance

DocSpec records planned work and durable evidence so another process can verify
what happened. Follow immutable references through that evidence before retrying,
publishing, compacting, or assessing scale. See the
[architecture guide](architecture.md) for the distinction between application
retained state and independent result exports. The lifecycle and maintenance
services below operate on application release state.

Start with the [inspection API or commands](inspection.md) for saved progress,
per-document evidence, or differences between results. They open existing state
without constructing execution services.

## Recover the planned work and its verified progress

[`RunPlanner`](../src/docspec/application/planner.py) seals an explicit run ledger
and complete task population. A loose store file that is not referenced by the
planned work does not become a runnable task merely because it exists on disk.
When examining a store, distinguish its newest revision from its whole history:
`latest()` admits the newest stored revision, while `revisions()` enumerates
the history. The local [store adapter](../src/docspec/adapters/storage/stores.py)
owns those reads.

[`load_latest_store()`](../src/docspec/application/store_state.py) first loads
the requested immutable reference, then considers a later revision. That order
prevents a damaged task reference from being hidden by a newer store. Recovery
must verify retained checkpoints before using them as evidence of completed work.

Capture and extraction checkpoints retain ordered progress. Segmentation and
processor layers have complete-stage boundaries. The
[checkpoint verifier](../src/docspec/application/execution_checkpoints.py)
checks retained artifacts and returns the verified frontier; the executor
restores cumulative retry and work budgets before starting another attempt.
Partial failure receipts remain evidence of attempted work, including when the
current policy permits a terminal failure. Do not reset accounting on resume.
See [stage recovery](../tests/test_stage_checkpoint_recovery.py) and
[processor-only recovery](../tests/test_processor_only_checkpoint_recovery.py).

For a saved execution handoff, local task execution and reconciliation also
compare the reconstructed worker with its retained description. Storage roots,
fetcher identity/configuration, extraction and segmentation settings, acceptance policies, sink, partition settings,
and the fixed evidence timestamp must match. The
[worker recovery tests](../tests/test_local_worker_identity.py) exercise refusal
before fetching when those values change. The
[stage tests](../tests/test_runtime_stages.py) also cover configuration changes
after preparation, including completed-task reuse and zero-task successors.
When saved entries are opened as checkpoints, each stage output must match the
selected implementation and its settings. Completed-task retries check current
settings and the existing delivery receipt without replaying extraction or
segmentation.

## Deliver and reconcile before publishing

[`StoreDeliveryService`](../src/docspec/application/delivery.py) checks the record
stream and sink receipt before sealing completion. A failed attempt can leave
immutable data whose receipt was never saved. Retry therefore depends on stable
record identity and sink behavior, not on an assumption that nothing was written.

[`RunReconciler`](../src/docspec/application/reconcile.py) compares returned task
results with the exact planned population. It handles arrival order independently
of planned order, collapses identical duplicate results, and rejects conflicting
ones. Every planned task needs an admissible result and verified durable output.
It rechecks delivery evidence before assembling the run receipt.

For incremental layers, replacing a touched partition must retain unaffected rows
inside that partition. Verify the base layer's schema and partition policy before
combining it with new rows. Scratch SQLite workspaces support bounded sorting and
merging; they do not become authoritative release references. See
[reconciliation workspace tests](../tests/test_reconciliation_workspace.py) and
[incremental equivalence](../tests/conformance/test_incremental_equivalence.py).

[`ReleaseCommitService`](../src/docspec/application/commit.py) verifies the
assembled release before retaining it. `document-release retain` returns a
readable immutable result without changing the current selection. Include every
result you need in retention inputs; the current pointer identifies only the
present choice.

`document-release commit` retains the result, then selects it only if current
still equals its pinned base. If selection fails, the verified result remains
retained; repeating retention returns its exact reference. Inspect the current
result before making another choice. `document-catalog select` accepts an
explicit expected current reference and verifies the retained candidate before
updating the pointer. Selecting an alternative preserves its original input
lineage. A retained artifact does not establish that selection succeeded; see
[the experiment guide](experiments.md) for requests and receipt meanings.
Local retention and selection hold an exclusive catalog lock through verification;
a competing writer fails promptly and can retry after that operation finishes.
Verification can take time for a large result.

## Build retention evidence before inspecting garbage candidates

Use `build_local_retention_set` to save the blob dependencies of explicitly
retained results and checkpoints. It composes
[`BlobRetentionSetService`](../src/docspec/application/maintenance.py) through the
supported runtime API. Results supply their saved plans; standalone stores
require their plan references, available as `prepared.handoff.processing_plan`.
The service follows required predecessors and plan bases, so a shorter successor
also preserves earlier bytes still required by retention and selection checks.
Opaque profile state requires the profile's
[reachability interface](../src/docspec/ports/profile_state_reachability.py) to
identify its referenced blobs. Omitting those references could misclassify live
data as garbage. See the [retention walkthrough](retention-preview.md).

`preview_local_blob_inventory` and `docspec blob-store gc --dry-run` use one
implementation to inspect a supplied retention set and verify each listed blob.
They report inventory relative to that set; they do not independently prove
that an imported set accounts for every root, discover other attempts, or delete
anything. Build a fresh set containing every required result and checkpoint.
Its minimum-age filter controls which unreferenced blobs become candidates.
Check [the command](../src/docspec/cli/blobs.py) and
[retention tests](../tests/test_maintenance.py) before changing these rules.

## Compact physical storage while preserving document state

[`ReleaseCompactionService`](../src/docspec/application/maintenance.py) rewrites
physical record layers without recapturing content or rerunning processors. It
creates a distinct successor with a maintenance handoff containing zero document
tasks. The source and successor must have identical logical state digests, and
the receipt accounts for the rewritten layers. Normal release verification and
expected-current publication still apply.

Use `docspec document-release compact --help` for the request interface. The
[CLI implementation](../src/docspec/cli/releases.py),
[maintenance models](../src/docspec/domain/maintenance.py), and
[compaction tests](../tests/test_maintenance.py) establish the required evidence.
A smaller physical layout alone does not establish equivalent document state.

## Separate local checks from qualification evidence

Tests establish behavior for their actual inputs and environment. A valid
`ScaleProfile` or a parsed `ScaleResult` is also only one part of qualification:
the [scale model](../src/docspec/domain/scale.py) checks pins, workload declarations,
inputs, limits, and reported evidence, while the qualification workflow must
produce and verify the actual artifacts.

The [conformance matrix](../conformance/test-matrix.json) currently declares the
scale requirement partial. The [specification](../conformance/specification.json)
requires ordered campaigns at 100,000, 1,000,000, and at least 5,000,000 items,
along with the applicable clean-revision and package evidence. A focused test,
synthetic timing run, or historical receipt does not establish that those
campaigns completed for a later release. Keep local validation, remote CI,
qualification, and publication status separate in reviews and run reports.
