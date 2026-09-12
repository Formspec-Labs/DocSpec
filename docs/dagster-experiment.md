# Run an experiment with Dagster resources

Dagster supplies workers, retries, interruption, run history, and dependency
injection. DocSpec selects document work, checks saved evidence, reuses completed
stages, and retains results. The same prepared DocSpec object serves a direct
Python caller and a native Dagster resource.

The [native example](../examples/dagster_experiment.py) takes a completed
[offline reference experiment](offline-walkthrough.md), captures its two selected
privacy/security documents into a new workspace, then processes those retained
captures. It compares phrase results with the reference's local execution.
Both phases have two actual tasks; the processing phase performs no new capture.

```sh
uv run --frozen --extra dagster python -m examples.offline_demo \
  --output /absolute/reference-experiment
uv run --frozen --extra dagster python -m examples.dagster_experiment \
  --reference /absolute/reference-experiment \
  --output /absolute/native-experiment
```

Both output directories must be new. The native workspace refers to the
reference catalog and local source files; keep that reference directory while
using this example. The copied vocabulary is checked against the resource pin
in the saved reference processing plan before processing preparation.

## Inject the components through Dagster

The example declares ordinary native resource dependencies. A workspace resource
receives the reference directory; a fetcher receives the workspace; a processor
receives its pinned reference data. The prepared resource receives those objects
and passes them to `prepare_local_experiment`.

```python
@dagster.resource(required_resource_keys={"workspace", "fetcher", "processor"})
def docspec_runtime(context):
    # Supply the catalog, producer acceptance, limits and other chosen settings
    # as in the complete example.
    with prepare_local_experiment(
        catalog_ref, context.resources.workspace,
        content_fetcher=context.resources.fetcher,
        processors=(context.resources.processor,),
        **settings,
    ) as prepared:
        yield prepared

definitions = build_dagster_definitions(resource_defs={
    "docspec_runtime": docspec_runtime,
    "workspace": workspace_resource,
    "fetcher": fetcher_resource,
    "processor": processor_resource,
})
```

The generator closes the prepared object's temporary task lookup when Dagster
tears down that worker's resources. Each process reconstructs its own services.
Only the existing bounded task and result messages cross native step boundaries.
The capture resource omits processor/extractor/segmenter dependencies entirely.

The driver also uses `dagster.build_resources` for preparation and reconciliation
outside an op. It puts the resulting exact handoff reference into ordinary native
resource configuration before dispatch. No separate DocSpec dependency container
or plugin registry is needed. See the native
[resource API](https://docs.dagster.io/api/dagster/resources) and
[execution API](https://docs.dagster.io/api/dagster/execution).

## Use native execution controls

`build_dagster_definitions` accepts native `executor_def` and `retry_policy`
arguments. The example uses Dagster's multiprocess executor, native concurrency
configuration, and a native retry policy for processing steps. Configure and
inspect these through Dagster. Local Python worker settings do not constrain
Dagster's worker pool.

DocSpec's saved execution profile describes its prepared worker, enforced task
lookup bound, deadline, and cache references. Native events link that evidence
to the actual Dagster run. The handoff identity stays independent of the native
run ID, allowing native re-execution to use the same document checkpoints.

A document-processing failure and a failed Dagster step answer different
questions. A permitted missing-document outcome can be retained by DocSpec while
the step succeeds. Worker failures and interruption remain native execution
events. Re-execution must still pass DocSpec's configuration, task membership,
checkpoint, and reconciliation checks.

## Inspect the result and qualification

`dagster-example.json` reports native run IDs, existing DocSpec handoff/release
references, and inspection summaries. It is an example report, not another run
database. Native history and step outputs live under the example's `dagster`
directory; retained document work lives in its dataset workspace.

The installed qualification in
[`test_dagster_experiment.py`](../tests/test_dagster_experiment.py) exercises the
actual copied example and a separate test-only interruption probe. The probe
records a native cancellation request and signals the parent while a child is
active after a capture checkpoint,
then uses native re-execution and stored step outputs. It checks that the
completed sibling stays complete, captured bytes are reused, resources close,
and accepted source failures keep their meaning. Execution results are recorded
in the checklist and review after the gate runs.

The example collects two native result messages in memory. It is not a
large-dataset collector, a qualification of a launcher's cancellation transport,
a hosted Dagster deployment, or proof of cleanup after
an uncatchable process kill. Native deployments choose their own executors,
storage and run controls; they can keep using the same prepared DocSpec services.
