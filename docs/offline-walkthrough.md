# Capture, repair, and compare a small document experiment

The [offline example](../examples/offline_demo.py) runs the current Core document
pipeline against local fixture bytes. It retains a missing-file failure, repairs
that input, processes captures, changes processor settings and reference data,
grows the selected state, and checks the result against a clean run.

```sh
uv run --frozen python tools/with_iceberg.py python -m examples.offline_demo --output ./experiment
```

The output directory must be new. The example uses local files and needs no
provider network access during execution. Writes use a local Iceberg REST
catalog, started by the helper through Docker; see [storage setup](record-storage.md#configure-writes). It opens `CoreWorkspace` directly, imports source
items, and calls the same document operations used elsewhere.

Initial capture records the missing input as an actual failure. Once its fixture
file is supplied, another run reuses completed successful work. Processing adds
extraction, segmentation and a pinned phrase matcher. Reopening the workspace
recovers the exact selections. Case-sensitivity and vocabulary alternatives
reuse their unchanged first three stages. A grown source state includes every
intended input, and a separate clean workspace verifies the resulting phrase
values.

Inspect `experiment-summary.json`, `initial-failures.json`, `matches.json`,
`comparisons.json`, and the per-run JSON records. `workspace/` contains the Core
ledger, native record files and blobs; `inputs/` contains the local source files.
The original failure remains inspectable after repair. See
[the installed example check](../tests/test_offline_example.py) and
[Python runs](python-runs.md) for the API.

This fixture proves its stated behavior and comparisons. Large-workload capacity,
remote CI and publication remain separate evidence.
