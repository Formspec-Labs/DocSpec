# Run a document experiment from Python

`docspec.runtime.prepare_local_run` accepts a `ProcessingPlan`, a
`LocalWorkspace`, and explicit execution choices. It builds the same services
used by the CLI. Callers do not need to write plan or run-request JSON files.
The API captures selected documents and runs the requested extraction,
segmentation, and processing stages. It returns a checked run receipt reference;
`prepared.retain(run_reference)` keeps the result without selecting it as current.

Build or open the input catalog through
[`docspec.source_catalog`](catalog-evidence.md). A plan pins that catalog, its
selected base result, processing choices, policies, and work limits. The
workspace provides storage locations and installed profile descriptions.

## Prepare and run

Given a plan, an absolute workspace path, and independently chosen producer
acceptance, prepare the run with existing domain values:

```python
from docspec.domain.execution import ExecutionLimits
from docspec.runtime import prepare_local_run
from docspec.workspace import LocalWorkspace

limits = ExecutionLimits(
    worker_count=1,
    max_concurrency_per_worker=1,
    max_in_flight=1,
    max_scratch_bytes_per_worker=4 * 1024**3,
    max_network_bytes_per_task=8 * 1024**3,
    request_rate_limit_per_second=100,
    max_provider_concurrency=4,
    max_task_attempts=1,
    retry_initial_delay_milliseconds=0,
    retry_max_delay_milliseconds=0,
)
settings = dict(
    retry_policy=retry_policy,
    accepted_failure_policy=accepted_failure_policy,
    source_catalog_producer=accepted_source_producer,
    document_release_producer=accepted_document_producer,
    execution_limits=limits,
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

Execution limits bound the runner; the plan's `WorkLimits` bound document work.
The runner's network allowance must cover one planned store's byte allowance.
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
processor results. Downstream work runs again. Changed source items and other
governing policies conservatively require full work. A damaged promised prefix
refuses reuse instead of silently fetching replacement evidence.

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
settings, result sink, evidence timestamp, execution limits, or deadline are
refused. Verified completed work is reused. A saved handoff and a `resume`
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

Those bound methods also fit the optional `DagsterRuntime` adapter. Each worker
must reconstruct its dependencies and verify the saved handoff. This interface
does not itself establish a complete deployed Dagster example or active-work
cancellation guarantees.

Before running a task, the worker checks its exact input reference against the
verified planned-store ledger. A bounded temporary SQLite lookup makes repeated
checks efficient. The saved ledger remains the authority; interrupted or removed
lookup files can be rebuilt. `run()` releases this scratch space even if
execution fails. Direct task callers use the context manager or call `close()`
after their workers stop; later use rebuilds the lookup. Its page allowance is
capped by the declared execution scratch limit and ledger size limits. This
does not establish aggregate scratch accounting across all simultaneous work.

## Current limits and checks

The returned `ArtifactRef` identifies a `RunReceipt`: it accounts for planned
tasks, selected items, retained layers, byte references, failures, and coverage.
Keeping it does not select a current application release or create a portable
export. [Retention and selection](experiments.md) remain explicit operations.
[Inspect and compare results](inspection.md) through `open_local_inspection`.
Simpler plan construction and portable export convenience remain checklist work.

Custom processors, fetchers, extractors, and segmenters use this public runtime.
The default `stage_policy()` still requests extraction and segmentation with no
processors. Use `stop_after="capture"` to retain only source files. Processing
plans and document stores use format `3.0`; disposition records use schema `2.0`
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
