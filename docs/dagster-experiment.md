# Schedule Core operations with Dagster

Dagster schedules operations over the ordinary Core lifecycle. A native resource
supplies `DagsterRuntime`: the workspace's `CoreOperations`, a task source yielding
`ScheduledOperation` records, and a producer resolver for pinned definitions.
Each scheduled record identifies its request, selection and output target.

```sh
uv run --frozen --extra dagster python -m examples.dagster_experiment --output /absolute/new-dagster-example
```

The [example](../examples/dagster_experiment.py) uses native resources, mapped
operations and worker processes. Task payloads carry bounded Core metadata;
large dataset values stay in the stores. Resources reconstruct the actual
implementations in each worker.

Dagster owns retries, cancellation, process isolation and event logs. Core owns
execution identities, progress, publication and correspondence. Re-executing an
already completed selection recovers its original selected result. An explicit
Core suspension requires the continuation API and verifier; it is not treated
as a finished mapped operation.

The [adapter](../src/docspec/adapters/dagster.py) imports Dagster only when selected.
[Adapter tests](../tests/test_dagster_adapter.py) and
[installed example tests](../tests/test_dagster_experiment.py) cover native
execution and reuse. This example is bounded behavior evidence, not a capacity
qualification.
