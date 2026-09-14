# What passing checks establish

DocSpec's regression checks establish behavior for the inputs and environments
they exercise. Capacity, live source reliability, and package publication each
need their own evidence. A passing test suite does not establish those claims.

The [2026-09-14 implementation acceptance](core-model-implementation-map.md#implementation-acceptance--2026-09-14)
records the completed implementation scope and the limits of its evidence.
Unrun or unmet performance targets remain unqualified claims; completing the
expanded capacity matrix is not a prerequisite for accepting the implementation.

## Run the regression checks

Use the same command as [CI](../.github/workflows/ci.yml):

```sh
uv run --frozen --extra dagster --extra s3 pytest --require-regression-map \
  --junitxml=/tmp/docspec-pytest.xml
```

The [regression map](../conformance/test-matrix.json) connects requirement IDs to
exact test functions. It contains no hand-maintained pass status. The optional
[pytest hooks](../tests/conftest.py) require every mapped function and all its
parameter cases to be selected and to complete setup, execution, and teardown
successfully. Missing tests, skipped modules, deselection, skips, expected
failures, and incomplete execution prevent a successful strict run. Strict mode
accepts whole files and directories; ordinary focused pytest remains available
without the flag.

Pytest runs the suite once and emits its
[native JUnit report](https://docs.pytest.org/en/stable/how-to/output.html#creating-junitxml-format-files).
CI checks that the report exists, then retains it with the console log, lockfile,
and built wheels under the checkout's commit. The existence check covers exits
before pytest starts its session, when session-finish hooks cannot run. Download
the files from the exact workflow run when reviewing its evidence; local output
and another commit's CI run are separate observations.

The retired conformance command reran pytest and wrapped its outcomes in a
DocSpec report. Its dataset artifact lists were empty, byte counts were zero,
and peak memory was unmeasured. Native test results now carry regression
evidence; there is no replacement DocSpec report format or runner.

## Current Core regression scope

The regression map follows the implemented Core owners: typed records and exact
value identity; batch storage; SQLite admission and provenance; actual attempts
and recovery; value and membership revisions; selected values; checkpoints;
dependency adequacy and exact reuse; current selection and policy-authorized
removal. Document, source, scheduler and independent-export checks exercise
those owners through their public interfaces.

Profile registries, document stores, release manifests and saved handoffs were
retired with their runtime. Their format-only tests are retired too. Useful
behavior now belongs to Core checks: immutable publication and guarded pointers,
complete retained states, actual failed attempts, repeatable source evidence,
recovery, bounded streams, and independent export admission. The
[implementation map](core-model-implementation-map.md) records the dispositions.
Git history retains the earlier regression map and test implementations.

A generic state export retains the explicitly selected state and its evidence.
A document export supplies the document pipeline's explicit retained roots to
include source and stage selections. Tests check complete requested populations,
original execution provenance, exact bytes, unavailable content, corruption and
reopening without the original workspace. They establish neither search-corpus
completeness nor unspecified application semantics inside arbitrary JSON values.

The [earlier review](history/2026-09-12-regression-qualification-architecture.md)
explains the retained pytest hook boundaries. The current map contains selectors,
not stored pass statuses; only an actual strict run establishes its result.

## Qualify a capacity claim

Choose a workload that answers an actual dataset or deployment decision.
The old mandatory 100k/1m/5m ladder is no longer a prerequisite for every change.
Choose sizes and distributions that cover the intended bytes, documents, pages,
segments, formats, and processing costs; a convenient prefix alone cannot
establish representativeness.

Before running, pin the source revision and installed wheels, inputs or fixture
generator, selected stages and components, storage settings, resource limits,
machine, cache state, operations, recovery scenario, and acceptance thresholds.
Use the existing local runner or native Dagster job. The
[local capacity recipe](capacity-workloads.md) supplies reproducible fixture
workloads and links exact measured revisions and remaining gaps. Retain actual
input and result
artifacts, elapsed time, peak memory, storage and scratch use, and the results of
independent admission and comparison with a clean run.
Retain Core state, request, execution, result and selection identities alongside
the measurement tool's output and reproduction commands. The retired `ScaleProfile`
and `ScaleResult` formats only validated supplied declarations; no runtime
measured or executed through them. Document invocation limits count new source bytes and generated rows; common
operation limits bound control records and journals. Native schedulers and
provider clients own their retry limits. There is no replacement DocSpec capacity report format.

Capacity results apply to their exact installed wheels and workloads. The old
release verifier and its dataset-sized Python identity set are retired; Core
performance claims still require measurement against the
[C01 targets](core-model-implementation-map.md#capacity-targets-fixed-before-tuning).
[D37](dataset-experiments-todo.md#d37) retains the earlier qualification history: the
original text workload exceeded its changed-resource time limit. The
[fresh Parquet trial](history/2026-09-12-parquet-capacity-observations.md) qualifies
markup256; text4096 passed processing but exceeded its complete-comparison time
limit. Synthetic local tests also do not establish
live provider reliability or processor semantic quality.

## Qualify publication

An installed-wheel test proves the tested artifact installs and supports its
exercised behavior. To claim a release was published, retain the exact source
revision, tested wheel digest, destination version and registry result. To claim
a dataset was published, retain its exact admitted bytes and destination
reference. CI completion, a local commit, and publication are distinct events.
