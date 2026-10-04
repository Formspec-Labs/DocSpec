# Direct Core execution versus Dagster

## Experiment declared before measurement

- Decision: choose a default execution route for the existing small local
  document-statistics example; identify whether a larger benchmark is needed.
- Hypothesis: direct Core execution has lower small-job latency; Dagster worker
  startup and event storage dominate such short operations. Similar timings
  would weaken this explanation.
- Arms: direct serial `CoreOperations.resolve`, native Dagster in-process, and
  native Dagster with at most two concurrent worker processes.
- Cases: the existing `examples.dagster_experiment.prepare` fixture, with two
  captured documents and two content-statistics operations. Measure initial
  processing and exact selection recovery separately.
- Held constant: source text, serialized tasks, processor, pinned dependencies,
  local storage and output verification. Each arm gets an isolated copy of one prepared workspace.
  Run three repetitions in rotating arm order, serially. Stop at ten minutes
  or a failed correctness check; retain failures rather than discard them.
- Timing: imports and input preparation are outside the measured intervals.
  Report scheduler setup separately; processing/recovery wall time includes
  opening Core resources, scheduling and result publication. Time producer calls
  separately; their sum is work time, not elapsed time when workers overlap.
  These are fresh workspaces with ordinary warm OS caches, not cold-disk trials.
- Outcome checks: canonical output values agree across arms and repetitions;
  recovery keeps exact result IDs and invokes no producers; multiprocess execution
  must actually use child workers.
- Decision rule: use the lower median initial-processing latency for this local
  example only if every correctness check passes. Report the full range and setup
  cost. Do not change production defaults. A bulk-throughput recommendation needs
  representative larger tasks; this small fixture cannot establish one.

Run from the repository root with the Dagster extra installed:

```sh
.venv/bin/python docs/history/probes/2026-09-30-dagster-benchmark/benchmark.py --output /tmp/docspec-dagster-benchmark
```

The output directory must be new. It retains each workspace, task file, worker
measurement, process log and case result. `receipt.json` records versions, source
revision, harness digest, raw measurements and summaries.

## Harness correction before the completed comparison

The first attempt prepared inputs independently for each arm. Its equality check
rejected the task files because preparation creates distinct Core identities,
even for equal document text. The attempt stopped after the direct and in-process
cases; its receipt and logs remain at
`/tmp/docspec-dagster-benchmark-20260930`. No timing conclusion uses that attempt.
The corrected harness prepares once and copies the closed workspace and task file
for each arm, preserving identical input identities. Outcome criteria are unchanged.

## Results

All completed trials passed: serialized tasks and canonical output values matched;
exact recovery preserved selected result IDs and called no producers. The
multiprocess cases used child workers. Three repetitions per arm ran in rotating
order on the same host, with DocSpec 0.12.2, SpicyDocs 0.53.0 and Dagster 1.13.16.

Seconds below are medians, with observed minimum–maximum in parentheses where
shown. Initial total is the median of each trial's setup plus fresh execution,
not the sum of separately computed medians.

| Arm | Setup | Fresh execution (range) | Recovery (range) | Initial total | Producer work sum |
| --- | ---: | ---: | ---: | ---: | ---: |
| `direct` | 0.000 | 0.465 (0.444–0.467) | 0.036 (0.035–0.037) | 0.465 | 0.337 |
| `dagster_in_process` | 0.339 | 0.526 (0.505–0.562) | 0.150 (0.145–0.162) | 0.857 | 0.266 |
| `dagster_multiprocess` | 0.338 | 2.452 (2.424–2.594) | 2.198 (2.177–2.272) | 2.792 | 0.406 |

**Decision: direct execution for this small local workload.** In-process Dagster
adds modest execution overhead plus instance/definition setup. Worker Dagster
adds about two seconds of elapsed execution time on this fixture, including on
recovery where producers do no work. This supports startup and scheduler overhead
as the explanation for the small-job cost; it does not isolate process startup
from event storage, resource reconstruction or interprocess communication.

Producer work is the sum of measured calls, including Core work performed inside
the producer. It is not a pure algorithm benchmark and cannot be subtracted from
parallel wall time to obtain scheduler overhead. Imports, preparation, workspace
copying, output verification and final instance disposal are outside these timing
intervals. There was no cold-cache control or host load isolation. No retries or
failures were injected in this timing run; the earlier Dagster regression tests
exercise those behaviors separately.

This fixture does not measure the throughput benefit of parallel long-running
operations, large tables, remote acquisition, or a persistent Dagster deployment.
Keep Dagster when scheduling, retries and worker management are needed; benchmark
representative larger operations before deciding how finely to split that work.
No production execution default changed.

[Receipt](receipt.json) contains raw timings, output values and their digests,
producer process IDs, versions and the harness digest.
[Failed attempt](failed-attempt.json) preserves the rejected initial comparison;
[process logs](process-logs.txt.gz) retain both attempts' console evidence.
Full workspaces remain at `/tmp/docspec-dagster-benchmark-20260930-v2`.
