# Native Dagster dependency injection

Decision: inject the existing `PreparedLocalRun` through native Dagster
resources. Keep document identity, checkpoint verification, reconciliation, and
retention in DocSpec. Let Dagster own scheduling and its saved configuration.
The solutions architect and independent reviewer agreed on this boundary.

## Why change it

The previous factory registered only one resource, which prevented callers from
registering separate fetcher, processor, and workspace resource dependencies.
Its extra `DagsterRuntime` wrapper repeated methods already available on the
prepared run. Every handoff also claimed a local-threaded execution profile,
even when native Dagster workers executed it. Several saved concurrency, rate,
network, and retry settings were never enforced by those workers.

## Agreed implementation

`build_dagster_definitions(resource_defs, *, executor_def=None, retry_policy=None)`
passes native resources into the existing dynamic job. The `docspec_runtime`
generator resource yields a prepared run inside its context manager. Dagster
constructs dependencies in each process; generator teardown closes temporary
DocSpec task-index files. Emission explicitly closes its source iterator on
early exit. Task/result bytes remain bounded reference messages.

Native output metadata identifies the handoff, execution profile, task, and
result. The handoff does not contain a Dagster run ID: native reexecution can
change scheduler settings while reconstructing the same document work. Native
configuration and events supply scheduler evidence; DocSpec does not copy them
into a second scheduling record.

Execution profile format `2.0` pins actual worker composition, deadline,
`maxTaskIndexBytes`, and cache references. `ExecutionLimits` retains only local
`worker_count`, local `max_in_flight`, and `max_task_index_bytes`. Only the index
bound enters the saved profile. Local concurrency may change on recovery;
changed deadline, index bound, roots, producers, and plugin pins still refuse.
Local request format `3.0` removes superseded fields without compatibility
fallbacks. Existing task/handoff wire formats stay unchanged.

Delete the unused `ExternalExecutionBackend`, `SerializedTaskDispatcher`, and
`ExecutionBackend` protocol. Keep the small local runner and `StoreTaskHandler`.
The process-boundary conformance fixture calls its existing CLI subprocess seam
directly; it does not need a production dispatcher abstraction.

## Bounds and qualification

Keep native dynamic mapping, multiprocess execution, RetryPolicy, persistent
instance/IO manager, events, cancellation, and reexecution. Do not add a public
`.collect()` reconciliation step: native collection materializes all results,
and a per-message size bound does not bound their aggregate. The installed
example has an explicitly tiny population and uses the existing reconcile and
retain operations.

Focused checks cover native resource dependencies and teardown, source iterator
closure, event references, lazy optional imports, worker-profile refusal, and
local concurrency changes without handoff changes. The installed native example
qualifies actual injected fetcher/processor use, retained captures, a failed
worker with durable checkpoints, successful sibling reuse, and final logical
results. Test results belong in the change report; this decision note does not
claim an unrun deployment qualification.

The adapter adds no scheduler state, resource framework, plugin loader, or second
DocSpec execution path. The task-index bound applies to each prepared worker's
temporary lookup; it is not aggregate memory, disk, or network accounting.

The implementation uses pinned Dagster 1.13.16. Its native dependency and
resource-lifetime behavior was checked against installed code and the official
[resource API](https://docs.dagster.io/api/dagster/resources) and
[execution API](https://docs.dagster.io/api/dagster/execution).
