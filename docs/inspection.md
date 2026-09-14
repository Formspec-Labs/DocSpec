# Inspect recorded work and compare states

`CoreWorkspace.inspect(kind, identity, progress_limit=20)` reads recorded Core
meaning without invoking producers. CLI `docspec inspect` uses the same owner.

```sh
uv run --frozen docspec inspect --workspace /absolute/dataset --kind selection --id selected-result
uv run --frozen docspec compare older-state newer-state --workspace /absolute/dataset --limit 20
```

Inspection reports identity, retention, availability and evidence version. A
selection includes its requested operation and original selected result. A
result includes its actual execution, executed request, output availability and
a bounded tail of progress. Output status reads metadata without decoding every
bulk output. Missing or unavailable data is reported according to recorded
availability; inspection cannot manufacture omitted dependency evidence.

`workspace.compare(older, newer, sample_limit=20)` counts state additions,
removals and changes with a bounded sample. `workspace.rows(state_id)` streams
admitted occurrences in deterministic key order; close the iterator if you stop
early. These methods inspect existing meaning, while opening a writable
`CoreWorkspace` can initialize workspace resources. For portable read-only
consumption, use the admitted [result export](result-exports.md).

[The inspection owner](../src/docspec/application/core_inspection.py) and
[shared Python/CLI tests](../tests/test_core_runtime_cli.py) define these reports.
Source-catalog inspection remains independent: see [catalog evidence](catalog-evidence.md).
