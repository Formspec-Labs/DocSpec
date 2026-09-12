# Derive the existing plan from the user's selected implementations

Decision: add `prepare_local_experiment` to the supported runtime. It accepts the
source catalog, workspace, explicit work limits, accepted producers, timestamp,
deadline, and selected implementations. It derives the existing `ProcessingPlan`
and delegates `prepare_local_run`, returning the same `PreparedLocalRun`.

This completes the setup shape requested by D03 without introducing another
configuration class, serialized experiment, processor registry, or run ledger.
Advanced callers keep the prebuilt-plan API. Source catalog construction remains
a separate operation; preparing processing does not silently build a catalog or
select the current result as its base.

## Derive identities from the objects that will run

`runtime/experiments.py` snapshots processor descriptions from their supplied
objects, validates the existing `ProcessorSet`, and orders it by its established
dependency order. One shared stage selector constructs only requested defaults.
The same selected extractor and segmenter objects supply the plan pins and go to
the existing runtime verifier. The high-level helper and public `stage_policy`
share that selection code.

There is no second identity recipe. Policies, profile pins, stage settings,
processor descriptions, source catalog and base references enter the ordinary
plan. Existing worker and execution records retain the effective runtime choices.
`PreparedLocalRun.plan` returns the actual composed plan for inspection and reuse.

## Defaults simplify setup while preserving explicit limits and authority

The default local plan retains all outputs, uses local-content data policy,
accepts no failures, and uses one partition. Its retry attempt limit comes from
the supplied work limits. Installed local profiles come from the workspace's
profile directory; advanced profile and storage overrides remain checked.

`runtime/defaults.py` is the shared source of local execution defaults for Python
and the CLI. It preserves the existing CLI allowances and worker behavior rather
than quietly increasing them for a large work request. Invalid combinations
still refuse before document work begins. Processor retry, data-use, and external
execution compatibility use one pure check shared by preparation and execution.
Preparation rejects incompatible choices before constructing output storage;
execution repeats the same check when using or recovering prepared work.

Both accepted producers, the evidence timestamp, deadline, and retained base
remain explicit caller choices. Default construction does not infer acceptance
from artifacts or select a new wall-clock identity. Reconstructing an identical
experiment recovers the same planned work; saved-handoff overrides follow the
existing refusal rules.

## Verify both convenience and equivalence

The focused cases compare derived and explicitly constructed plans, handoffs,
and run references, including a reversed dependency graph; prove single
construction of default stage objects; exercise
capture followed by processing without fetching again; and reject invalid
settings before dataset writes. The installed-wheel probe uses the small setup
outside the checkout, with no caller plan or request files. Its later processing
uses HTML visible-text extraction and block segmentation, verifies the retained
captured bytes and source coordinates, and recovers without another fetch.
Independent static
review and executed gates are recorded separately from this design decision.
