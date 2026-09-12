# Run a document experiment from Python

`docspec.runtime.prepare_local_run` accepts a `ProcessingPlan`, a
`LocalWorkspace`, and explicit execution choices. It builds the same services
used by the CLI. Callers do not need to write plan or run-request JSON files.
The current API captures, extracts, segments, and optionally processes selected
documents. It returns a checked run receipt reference.

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
supplies no processors. Extraction and segmentation currently use the default
registries pinned by the plan.

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
roots, fetcher identity/configuration, policies, accepted producers, partition
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
Unified result inspection and simpler plan construction remain checklist work.

Custom processor and fetcher injection use this public runtime. Custom extractor
and segmenter injection remains gated on configuration-aware stage pins. An
empty processor list still performs extraction and segmentation; capture-only
completion is not yet available.

The [installed runtime check](../tests/support/installed_runtime_probe.py) reuses
the offline example's source fixture outside the checkout. It builds a catalog,
executes a processor, inspects retained layers, resumes without refetching or
rerunning the processor, and refuses changed worker settings. The
[package test](../tests/test_package_boundary.py) installs the built wheel into
an isolated environment before running that check. This qualifies the bounded
Python path; broader experiment acceptance remains
[D38](dataset-experiments-todo.md#d38).
