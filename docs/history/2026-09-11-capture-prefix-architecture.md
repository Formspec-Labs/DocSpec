# Capture first and process retained inputs later

Decision: represent useful stopping points in the existing processing plan,
retain their results through the existing catalog, and seed ordinary execution
with the verified prefix of a selected base. This serves contributors who want
to acquire inputs once and compare extraction, segmentation, and processor
configurations without downloading the same documents again.

This decision follows the dataset-experiment intent in
[D04, D15, and D24](../dataset-experiments-todo.md) and extends the
[stage-injection decision](2026-09-11-stage-injection-architecture.md).
It records architecture judgment; independent review and execution results are
recorded separately. It does not claim the complete lifecycle checklist is done.

## One requested policy, explicit work to run

`StagePolicy` uses nullable extractor ID/configuration-digest and segmenter
ID/policy-digest pairs. Each pair is either complete or absent. Segmentation
requires extraction, and processors require segmentation. Thus a capture-only
plan requests no downstream stage; extraction-only requests just the extractor;
segmentation adds its segmenter; processing adds the selected processor graph.

A separate persisted terminal-stage flag was rejected because it would repeat
those choices and allow contradictions. The public `stage_policy(stop_after=...)`
helper offers a readable construction option while storing only the resulting
policy. Composition constructs and verifies requested implementations only.
An unrequested stage is distinct from incomplete or failed requested work.

Every `DocumentEntry.requested_stages` describes the complete result policy.
The separate `processor_ids_to_run` tuple names the ordered processor work for
that entry. This replaces the old processor-only convention that narrowed the
requested policy even though the result retained unaffected processor outputs.
Only reuse from segments may run a subset of the full graph.

The existing disposition record includes the complete requested policy. A
selective run can inherit items from several older plans, so the newest release
plan cannot stand in for each retained item's policy. This field belongs in the
existing per-item result evidence, not in another state ledger.

## Reuse the existing execution path

`EntryExecutionMode` names fresh work or reuse from captures, representations,
or segments. Planning compares each unchanged item's retained policy with the
requested policy:

| Requested change | Retain | Run again |
| --- | --- | --- |
| Add or change extraction | Captured files | Requested extraction and descendants |
| Add or change segmentation | Files, representations, extraction receipts | Requested segmentation and processors |
| Add, change, or remove processors | Files, representations, segments, upstream receipts, unaffected processor results | Invalidated processor graph |
| Shorten the stopping point | Only the requested prefix | Any remaining requested work |
| Identical selected behavior | Existing active records | Nothing |

`prepare_base_reprocessing` verifies the exact source item, candidate population,
base stage pins, blobs, and receipt coverage. Layer reads are bounded and their
record-ID order is normalized to candidate, representation, and segment order.
Only the reusable prefix and valid unaffected results seed the entry. The normal
checkpoint verifier and executor then resume remaining work. A separate capture
runner and a second processor-only execution loop were rejected.

Existing `CAPTURED` disposition denotes successful completion of the stages
actually requested. Completion comes from verified candidate coverage and stage
receipts. In particular, an empty segmentation receipt proves that segmentation
ran and emitted no segments; absence of output rows alone does not prove this.
Capture-only runs can checkpoint after any candidate without requiring an
adjacent representation. Recovered runs keep already-completed new work as well
as their retained prefix.

Budget restoration distinguishes reused inputs from stages performed by this
run. Reuse does not charge acquisition, extraction, or segmentation again.
Current work still has cumulative limits, and reading retained inputs still has
byte verification, collection bounds, memory reservations, and deadlines.
Initial extraction and recovered work read the same page/frame observations
from extraction receipts; output boundaries do not reconstruct execution cost.

## Selection chooses items; their inputs determine reuse

Changing `selection` changes which items enter a run. It does not, by itself,
change an unchanged item's output. Therefore selection remains in exact plan
identity but no longer forces full rebuilding through the global governing
comparison. Each selected item's source description and requested policy decide
reuse. Other governing changes remain conservative full repairs in this slice.

This matters when shortening A and later B in a mixed result: each selected item
can retain its verified captures while the unselected item keeps its own prior
policy and outputs. A filter change must not cause another download of those
unchanged inputs.

## Retain mixed results without weakening derived-output checks

The existing delivery integrity index records each item's requested processor
IDs. Every derived row must match a processor requested by that row's source
item. This admits inherited results whose processor is absent from the newest
plan while refusing unexplained outputs. The index remains disposable SQLite
verification state; it is not a new authority or retained ledger.

Release verification continues to require core layers and current processor
layers. Reconciliation creates a current processor's declared empty layer when
the valid graph emits no records, including a clean run with no segments. Extra
nonempty processor layers are admitted only through per-item authorization;
empty unplanned extras are refused.

Reconciliation considers every active processor layer absent from the current
plan for retirement, including layers inherited across several generations.
It preserves unselected rows and removes affected descendants. Retired empty
layers are dropped even when no partition was touched. Looking only at the
immediately preceding plan would leave superseded layers behind.

`PreparedLocalRun.retain(run_ref)` calls the existing release service. Capture
and processing results use the same immutable retention, exact-base checks,
and separate guarded current selection. Portable export remains optional.

## Limits and required evidence

This is a contiguous stage-prefix workflow, not an arbitrary task graph. A
previously failed item may still take conservative full repair; stage-aware
failed-item repair remains D16. Aggregate stage configuration changes can
invalidate more than one selected child; finer child-specific reuse is not
promised here.

Acceptance requires real capture/retain/later-process runs with no refetch;
changed-stage comparisons against clean results; interruption and recovery after
new extraction and segmentation of retained inputs; empty-output completion;
mixed per-item stage policies; selective retirement across generations; bounded
budget restoration; and refusal of missing, altered, or mismatched base evidence
before new downstream work. Installed-package validation must exercise the same
public path.

Implementation ownership remains in the existing planner, base-reprocessing,
execution/checkpoint, delivery/reconciliation, and runtime modules. No new
dataset ledger, persisted cursor, plugin loader, or parallel capture pipeline
was introduced.

The main implementation seams are the
[per-item planner](../../src/docspec/application/planner.py),
[verified base preparation](../../src/docspec/application/base_reprocessing.py),
[entry checkpoint verifier](../../src/docspec/application/execution_checkpoints.py),
[logical delivery verifier](../../src/docspec/domain/delivery.py),
[active-layer reconciliation](../../src/docspec/application/reconcile.py), and
[public retained-run operation](../../src/docspec/runtime/execution.py).
