# What passing checks establish

DocSpec's regression checks establish behavior for the inputs and environments
they exercise. Capacity, live source reliability, and package publication each
need their own evidence. A passing test suite does not establish those claims.

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

## Disposition of the nine previously partial requirements

These decisions revise scope. They do not turn unrun checks into passes.

| Previous requirement | Current proof and remaining boundary |
| --- | --- |
| `BOUNDARY-IMPORT` | Keep actual imports and isolated dependency checks. The shared canonical encoder is an intentional dependency. |
| `BOUNDARY-CODE` | Retire historical symbol/archive absence checks. Current import and installed-package checks establish ownership. Git retains old code. |
| `SOURCE-CATALOG-CONTRACT` | Keep public catalog, provider, coverage and refusal checks. Capacity and unsupported physical formats remain separate. |
| `RELEASE-MANIFEST` | Check current retained releases and result-container admission. The superseded campaign export format stays retired. |
| `COMPLETE-SEARCH-CORPUS` | Rename to `RETAINED-RESULT-EVIDENCE`: check retained population, bytes and relationships. Search products own search completeness. |
| `SCHEDULER-PORTABILITY` | Keep real local and native Dagster process, retry and recovery checks. Other executors and deployment capacity remain unqualified. |
| `DOCUMENT-RELEASE-INTEGRITY` | Keep lineage, blob and malformed-reference checks. Fixture coverage does not establish corpus capacity. |
| `SCALE` | Retire the unused scale profile/result formats and their `SCALE-FORMAT` parsing checks. Capacity claims require the measurements described below. |
| `PACKAGE-RELEASE` | Rename to `INSTALLED-PACKAGE`: build and run isolated wheels. Publishing a release requires separate registry and exact-wheel evidence. |

The [independent architecture review](history/2026-09-12-regression-qualification-architecture.md)
records the reasoning and the pytest hook boundaries.

## Qualify a capacity claim

Choose a workload that answers an actual dataset or deployment decision.
The old mandatory 100k/1m/5m ladder is no longer a prerequisite for every change.
Choose sizes and distributions that cover the intended bytes, documents, pages,
segments, formats, and processing costs; a convenient prefix alone cannot
establish representativeness.

Before running, pin the source revision and installed wheels, inputs or fixture
generator, selected stages and components, storage profiles, resource limits,
machine, cache state, operations, recovery scenario, and acceptance thresholds.
Use the existing local runner or native Dagster job. The
[local capacity recipe](capacity-workloads.md) supplies reproducible fixture
workloads; its larger cases remain unmeasured. Retain actual input and result
artifacts, elapsed time, peak memory, storage and scratch use, and the results of
independent admission and comparison with a clean run.
Use saved DocSpec plan, handoff, run and release references alongside the
measurement tool's output and reproduction commands. The retired `ScaleProfile`
and `ScaleResult` formats only validated supplied declarations; no runtime
measured or executed through them. Existing work and execution limits continue
to govern execution. There is no replacement DocSpec capacity report format.

No corpus-capacity result is established by this cleanup. In particular,
`DocumentReleaseVerifier.verify` keeps a set of distinct blob identities that
grows with the result. [D37](dataset-experiments-todo.md#d37) remains open for
representative measurement and any bounded implementation that measurement
justifies. Synthetic local tests also do not establish live provider reliability
or processor semantic quality.

## Qualify publication

An installed-wheel test proves the tested artifact installs and supports its
exercised behavior. To claim a release was published, retain the exact source
revision, tested wheel digest, destination version and registry result. To claim
a dataset was published, retain its exact admitted bytes and destination
reference. CI completion, a local commit, and publication are distinct events.
