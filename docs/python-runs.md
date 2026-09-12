# Run a document experiment from Python

`docspec.runtime.prepare_local_experiment` accepts a source catalog, workspace,
work limits, and the implementations you choose. It derives the existing
`ProcessingPlan` and prepares the same runtime used by the CLI. You do not need
to write plan files or repeat processor descriptions, stage digests, profile
documents, or policy digests. `prepared.plan` exposes the exact saved plan.

The prepared run captures selected documents and runs the requested stages.
`prepared.run()` returns a checked run receipt reference;
`prepared.retain(run_reference)` keeps the result without selecting it as current.
Call `prepare_local_run` directly when you already have a custom plan.

Build or open the input catalog through the
[catalog convenience API](catalog-inputs.md). A plan pins that catalog, its
selected base result, processing choices, policies, and work limits. The
workspace provides storage locations and installed profile descriptions.

## Start with a small configuration

Given an existing source catalog reference, an absolute workspace path, your
accepted producers, and a fetcher, capture the selected documents:

```python
from docspec.domain.plans import WorkLimits
from docspec.runtime import prepare_local_experiment
from docspec.workspace import LocalWorkspace

workspace = LocalWorkspace(workspace_path)
settings = dict(
    limits=WorkLimits(
        max_entries=2, max_estimated_bytes=16 * 1024**2,
        max_pages_or_frames=1000, max_segments=2000,
        max_processor_cost=2000, max_memory_bytes=128 * 1024**2,
        max_duration_seconds=60, max_attempts=3,
    ),
    source_catalog_producer=accepted_source_producer,
    document_release_producer=accepted_document_producer,
    completed_at=evidence_timestamp,
    deadline_epoch_seconds=deadline,
    content_fetcher=fetcher,
)
with prepare_local_experiment(
    source_catalog_reference, workspace, stop_after="capture", **settings,
) as captured:
    captured_result = captured.retain(captured.run())
```

Later, pass that result as the explicit base and choose the processing objects.
DocSpec derives their stage pins and processor graph, then reuses verified
captures:

```python
from docspec.processing.extraction import TextExtractor
from docspec.processing.segmentation import ParagraphSegmenter
from docspec.processing.processors import ContentStatisticsProcessor

with prepare_local_experiment(
    source_catalog_reference, workspace, base_release=captured_result,
    extractor=TextExtractor(), segmenter=ParagraphSegmenter(),
    processors=(ContentStatisticsProcessor(),), **settings,
) as processed:
    result = processed.retain(processed.run())
    effective_plan = processed.plan
```

This example selects source-native text. Choose a supported extractor for your
documents; see [representation choices](representations.md). A tuple of processor
objects supplies the graph; you do not supply a second description or ID map.
The default empty tuple adds no processor output. Requested extraction and
segmentation use their supported defaults when objects are omitted.

The helper selects installed local profiles, one record partition, retain-all
storage, local-content data use, and no accepted failures. Its default retry
policy uses the work limit's `max_attempts`. `local_execution_limits()` supplies
the same defaults as the CLI: one local worker, one in-flight task, and a 4 GiB
limit for each worker's temporary task-membership index. Worker count and
in-flight settings control only the direct `run()` helper. Dagster owns native
concurrency and task retries; fetchers own their transport limits. Work limits
remain explicit.

Supply `execution_limits`, `profiles`, `partition_count`, `retry_policy`,
`accepted_failure_policy`, `retention_policy`, or `data_use_policy` when your
experiment needs different settings. `selection` uses the existing plan filters.
Workspace root/profile overrides remain available through `LocalWorkspace`.
Configured processors must agree with the chosen retry and data-use policies;
preparation refuses a mismatch before creating run state. For example, when
`limits.max_attempts` differs from three, configure the processor with the same
`RetryPolicy(max_attempts=limits.max_attempts)` used by the experiment.

Producer acceptance, evidence timestamp, deadline, and a retained base are never
inferred from the input artifact, current catalog head, or wall clock. To resume
a saved handoff, reconstruct the same settings and pass `handoff_ref`; changed
settings refuse instead of overriding saved work. The installed-wheel
[lifecycle probe](../tests/support/installed_runtime_probe.py) exercises this
small configuration, capture reuse, HTML visible-text blocks and their source
coordinates, and exact recovery outside the checkout.

## Use an existing plan

Given a plan, an absolute workspace path, and independently chosen producer
acceptance, prepare the run with existing domain values:

```python
from docspec.runtime import local_execution_limits, prepare_local_run
from docspec.workspace import LocalWorkspace

settings = dict(
    retry_policy=retry_policy,
    accepted_failure_policy=accepted_failure_policy,
    source_catalog_producer=accepted_source_producer,
    document_release_producer=accepted_document_producer,
    execution_limits=local_execution_limits(),
    deadline_epoch_seconds=deadline,
    completed_at=evidence_timestamp,
    content_fetcher=fetcher,
    processors={processor.description.processor_id: processor},
)
workspace = LocalWorkspace(workspace_path)
prepared = prepare_local_run(plan, workspace, **settings)
run_reference = prepared.run()
```

The plan's retry and failure policies must agree with those supplied here. Its
processor descriptions must match the supplied objects. Omit `processors` to
use the built-in processors selected by the plan; an explicit empty mapping
supplies no processors. Requested extraction and segmentation use the default registries unless the
caller supplies other implementations with matching plan pins. Unrequested
stages construct no defaults and refuse supplied implementations.

Omit `content_fetcher` to read local files under the workspace's `sourceContent`
root. An injected fetcher supplies a nonempty `downloader_id` and a SHA-256
`configuration_digest`; its acquisition metadata describes the same configured
implementation. Credentials belong in the live fetcher, outside retained
configuration. Source-aware fetchers remain optional integrations.
See [fetcher composition and evidence](fetchers.md) for supported routing and
the distinction between required pins and observed transport versions.

Execution limits bound local concurrency and the task membership index; the
plan's `WorkLimits` bound document work. These are logical work limits, not
network-transfer or billing limits. See [retry ownership](retry-ownership.md).
`completed_at` is the fixed evidence timestamp for this attempt, distinct from
the wall-clock execution deadline. Producer acceptance is never inferred from
the input artifact's labels.

## Choose extraction and segmentation

Select the objects before constructing the plan. `stage_policy` derives their
identities and settings for the plan's `stages` field:

```python
from docspec.processing.extraction import TextExtractor
from docspec.processing.segmentation import ParagraphSegmenter
from docspec.runtime import stage_policy

extractor = TextExtractor()
segmenter = ParagraphSegmenter()
stages = stage_policy(
    extractor=extractor,
    segmenter=segmenter,
    processor_ids=(processor.description.processor_id,),
)
# Supply stages to ProcessingPlan.create alongside its other required inputs.
# Pass the same configured objects when preparing or recovering that plan.
settings.update(extractor=extractor, segmenter=segmenter)
```

Omitting these choices in both calls uses the same default constructors. A
registry is one configured stage that selects among individual implementations.
Its plan digest includes its routing and child settings; retained representations
and segments keep the identity of the child that actually produced them. Empty
segmentation also records the selected child's policy.

Custom implementations follow the [extension interfaces](extensions.md). Their
declared settings must include every choice that affects results. DocSpec checks
that the objects, emitted results, and recovered results agree with those
declarations; this does not prove arbitrary plugin code behaves correctly.

Changed settings require a new plan against an explicit retained base. For an
unchanged source item, changed extraction reuses captures; changed segmentation
reuses representations; changed processors reuse segments and unaffected
processor results. Downstream work runs again. Changed acquisition inputs and
other governing policies conservatively require full work. A catalog metadata
refresh can retain verified document work while publishing the fresh source
description; see [catalog iteration](catalog-iteration.md). A damaged promised
prefix refuses reuse instead of silently fetching replacement evidence.

The default extractor includes PDF support when the optional parser is installed.
Configuration reads its installed version without importing the parser. PDF
availability, version, separator, and whitespace settings affect the registry's
digest, including in a text-only plan using that registry. An explicit
`TextExtractor` avoids that PDF-dependent registry choice. Missing PDF support or
a loaded parser version that differs from its pin refuses PDF processing.

## Capture first and process later

Choose the stopping point when building the plan:

```python
capture_stages = stage_policy(stop_after="capture")
extraction_stages = stage_policy(stop_after="extraction", extractor=extractor)
segmentation_stages = stage_policy(
    stop_after="segmentation", extractor=extractor, segmenter=segmenter,
)
```

A capture plan has no processors and supplies no extractor or segmenter to
`prepare_local_run`. Run it and retain the result:

```python
with prepare_local_run(capture_plan, workspace, **capture_settings) as capture:
    capture_result = capture.retain(capture.run())
```

Build a new plan with `base_release=capture_result` and the desired stages.
Prepare that plan with those stage objects and run it through the same API.
The planner verifies the source selection and reuses captured files, including
original acquisition evidence. There is no separate delayed-processing ledger.
A shorter stopping point retains only the requested prefix in its new result;
the original result remains available with all of its outputs.

Each document's disposition records the full requested stages. This matters
when a retained result inherits documents completed under different plans.
A successful capture means every candidate was captured; extraction and
segmentation require their output receipts only when requested. Empty
segmentation still needs its selected-policy receipt.

## Resume or dispatch the same work

Save `prepared.handoff_ref` with its settings. Reconstruct the worker with that
reference and the same settings:

```python
recovered = prepare_local_run(
    plan, workspace, handoff_ref=prepared.handoff_ref, **settings,
)
run_reference = recovered.run()
```

Recovery verifies the retained handoff against the reconstructed worker. Changed
roots, fetcher identity/configuration, stage identities/settings, policies, accepted producers, partition
settings, result sink, evidence timestamp, task-index byte bound, or deadline
are refused. Local worker count and in-flight settings may change on recovery;
they do not change the saved worker identity. Verified completed work is reused. A saved handoff and a `resume`
planning option are mutually exclusive.

Without `handoff_ref`, `resume=None` uses an existing planned-store ledger when
present, `resume=True` requires it, and `resume=False` invokes planning. Repeating
an identical plan in the same stores recovers that work; it does not create an
independent identical trial. See [experiment identities](experiments.md).

External schedulers can use the same prepared object. Keep it open until all
workers have stopped, then release its temporary task lookup:

```python
with prepared:
    results = (
        prepared.execute_task(prepared.handoff, task)
        for task in prepared.task_source(prepared.handoff)
    )
    run_reference = prepared.reconcile(results)
```

For Dagster, pass native resource definitions to `build_dagster_definitions`.
The `docspec_runtime` resource yields the prepared run directly; its native
resource dependencies supply the fetcher, processors, and workspace:

```python
import dagster
from docspec.adapters.dagster import build_dagster_definitions

@dagster.resource(required_resource_keys={"workspace", "fetcher", "processors"})
def docspec_runtime(context):
    with prepare_local_experiment(
        catalog_ref, context.resources.workspace,
        content_fetcher=context.resources.fetcher,
        processors=context.resources.processors,
        handoff_ref=saved_handoff_ref, **experiment_settings,
    ) as prepared:
        yield prepared

# Supply your native resource definitions for these dependencies.
definitions = build_dagster_definitions({
    "workspace": workspace_resource,
    "fetcher": fetcher_resource,
    "processors": processors_resource,
    "docspec_runtime": docspec_runtime,
}, retry_policy=dagster.RetryPolicy(max_retries=1))
```

Dagster constructs those resources in each worker and closes the generator
resource when that worker finishes. Its executor, retries, cancellation,
reexecution, and event store remain authoritative. DocSpec output metadata links
the handoff, execution profile, task, and result to native events. The saved
handoff remains independent of Dagster run IDs, so native reexecution can use
the same prepared work.

Execution profile format `2.0` pins the actual worker composition, task-index
bound, deadline, and cache references. It makes no claim to preserve or enforce
Dagster's scheduler configuration. Native events and configuration provide that
evidence. Local-run request format `3.0` accepts `maxWorkers`, `maxInFlight`,
`maxTaskIndexBytes`, and required `deadlineEpochSeconds` execution settings.

The adapter streams bounded task and result messages. It does not collect all
results into a list. Reconcile the result stream through the existing API;
Dagster's `.collect()` is suitable only when the caller has independently bounded
the aggregate output, as in a small example.

Before running a task, the worker checks its exact input reference against the
verified planned-store ledger. A bounded temporary SQLite lookup makes repeated
checks efficient. The saved ledger remains the authority; interrupted or removed
lookup files can be rebuilt. `run()` releases this scratch space even if
execution fails. Direct task callers use the context manager or call `close()`
after their workers stop; later use rebuilds the lookup. Its page allowance is
capped by `max_task_index_bytes` and ledger size limits. This
does not establish aggregate scratch accounting across all simultaneous work.

## Current limits and checks

The returned `ArtifactRef` identifies a `RunReceipt`: it accounts for planned
tasks, selected items, retained layers, byte references, failures, and coverage.
Keeping it does not select a current application release or create a portable
export. [Retention and selection](experiments.md) remain explicit operations.
[Inspect and compare results](inspection.md) through `open_local_inspection`.
`prepare_local_experiment` constructs plans from typed source references and
injected implementations. Portable export convenience remains checklist work.

Custom processors, fetchers, extractors, and segmenters use this public runtime.
The default `stage_policy()` still requests extraction and segmentation with no
processors. Use `stop_after="capture"` to retain only source files. Processing
plans and document stores use format `3.0`; disposition records use schema `3.0`
and ordinary segmentation receipts use format `2.0`. Rebuild plans and prepared
work made with superseded shapes; there is no compatibility reader.

The [installed runtime check](../tests/support/installed_runtime_probe.py) reuses
the offline example's source fixture outside the checkout. It builds a catalog,
injects all four kinds of implementation, retains captures, processes them
without another fetch, inspects layers and stage identities, resumes without
repeating work, and refuses changed settings. The
[package test](../tests/test_package_boundary.py) installs the built wheel into
an isolated environment before running that check. This qualifies the bounded
Python path; broader experiment acceptance remains
[D38](dataset-experiments-todo.md#d38).
