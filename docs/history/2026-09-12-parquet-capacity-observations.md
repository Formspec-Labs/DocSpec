# Parquet local capacity observations

**The markup256 trial passed. Text4096 failed its comparison time limit.** These fresh
trials measure the Parquet/DuckDB implementation at `c898512`, with the same
allowances as the [earlier trial](2026-09-12-local-capacity-observations.md).
That earlier text changed-resource timeout remains a failed observation.

## Installed implementation and scope

The tested DocSpec wheel SHA-256 is
`b8d1187e6383ceb72442781b205480a9280abfc623f89dd1c20d508b53cde75b`.
The recipe SHA-256 is
`3d081d2b07b7963d3435d648e41d8326ec593ea1c5407b52206fc8de0033c816`;
the phrase processor is
`dfd6057bc548ed70611aec29d0555611c8c32dab9a49c70d11bef143c44df103`.
Python 3.12.13 ran with assertions enabled in an isolated core installation,
including DuckDB 1.5.5, PyArrow 25.0.1 and Rulespec Artifacts 1.0.12.

The predeclared plan uses one local worker and one task in flight per trial.
The two trials may overlap on a shared macOS 26.6 arm64 host with 48 GiB RAM,
14 logical CPUs and APFS storage. Workspaces and temporary directories are
separate; operating-system cache and unrelated activity are uncontrolled.

Native `/usr/bin/time -l` measures each operation's elapsed time and peak resident
memory. Every operation must fit 1 GiB. Build, source verification, capture,
changed and clean each have 30 minutes; processing has 30 minutes for the sum
of prefix and resume active time. Fresh inspection and complete comparison each
have five minutes. Generation and independent fixture checks are timed and must
succeed, with no qualification latency threshold.

Native `du -sk` samples before, after and about every ten seconds. Main workspace
excluding clean plus temporary storage has an 8 GiB sampled allowance; clean has
its own 8 GiB allowance. These observations cannot establish an exact peak or
hard storage quota. Native DuckDB memory settings likewise do not impose a hard
process memory limit; the reported resident memory covers the Python process.

## Markup256 result

The source catalog contains 272 documents; the run selects 256 and excludes 16.
Captured content comprises 288 files, 99,090,432 bytes and 3,712 segments,
including HTML files up to 4 MiB and documents with multiple files.

| Operation | Elapsed seconds | Peak resident bytes |
| --- | ---: | ---: |
| Generate fixtures | 0.27 | 99,418,112 |
| Build catalog | 0.59 | 91,537,408 |
| Verify source catalog | 0.30 | 89,800,704 |
| Capture and retain | 8.42 | 147,095,552 |
| Complete capture check | 2.29 | 136,036,352 |
| Completed prefix of 16 tasks | 6.20 | 165,462,016 |
| Resume and retain | 65.16 | 341,098,496 |
| Fresh inspection and summary | 13.23 | 182,517,760 |
| Complete processing check | 17.79 | 207,454,208 |
| Changed phrase resource and retain | 79.99 | 339,705,856 |
| Clean v2 run and retain | 76.07 | 293,011,456 |
| Complete checks and native comparison | 56.35 | 226,721,792 |

All operations succeeded within their allowances. Prefix and fresh-process
resume took 71.36 seconds of active time, with no intervening downtime at the
wrapper's one-second timestamp resolution. Their combined calls were exactly
288 extractions, 288 segmentations and 3,712 processor invocations, with no
fetches. Changed-resource processing invoked only the processor, 3,712 times.
The clean control fetched, extracted and segmented all 288 files and processed
all 3,712 segments using separate result storage and cache.

The complete fixture checks covered every selected source association, captured
byte sequence, representation/map, segment/coordinate and derived value.
Changed and clean logical streams agreed, with digest
`sha256:5c1d583970dc0f500bba8ccae507ab24c21cc456434185af45501d146bbe0e5c`.
Native comparison records the expected differences in execution and input
provenance; the fixture oracle establishes the intended value equality.

The largest sampled main-plus-temporary allocation was 750,328 KiB; clean was
1,002,540 KiB. Final allocation was 734,296 KiB for the main workspace excluding
clean, 1,002,540 KiB for clean and zero for the temporary directory. There were
no storage sampling errors. Peak resident memory across operations was
341,098,496 bytes, below the declared 1 GiB allowance.

## Text4096 comparison failure

The trial selects 4,096 documents and excludes 256. Its 4,096 captured files
contain 83,886,080 bytes and produce 20,480 segments. Catalog construction,
source verification, capture, both initial fixture checks, completed-prefix
recovery, fresh inspection, changed-resource processing and the clean run passed.

| Completed operation | Elapsed seconds | Peak resident bytes |
| --- | ---: | ---: |
| Generate fixtures | 0.81 | 89,063,424 |
| Build catalog | 5.06 | 101,482,496 |
| Verify source catalog | 1.71 | 96,124,928 |
| Capture and retain | 127.06 | 380,715,008 |
| Complete capture check | 30.69 | 228,229,120 |
| Completed prefix of 16 tasks | 38.75 | 195,723,264 |
| Resume and retain | 570.37 | 996,540,416 |
| Fresh inspection and summary | 97.37 | 359,989,248 |
| Complete initial processing check | 123.97 | 371,654,656 |
| Changed phrase resource and retain | 684.99 | 985,972,736 |
| Clean v2 run and retain | 549.63 | 632,242,176 |

Prefix plus resume took 609.12 seconds of active time, with no intervening gap
at the wrapper's one-second timestamp resolution. Their combined calls were
4,096 extractions, 4,096 segmentations and 20,480 processor invocations, with no
fetches. Changed-resource processing invoked only the processor, 20,480 times.
The clean run fetched, extracted and segmented all 4,096 files and processed all
20,480 segments. All completed operations fit their time and 1 GiB allowances.

Complete comparison started at 20:38:45 UTC with a 300-second limit and exited
with timeout status 124. The wrapper finished at 20:44:07 UTC after cleanup.
Comparison returned no completed output. Its partial native timing file contains
only a peak-memory-footprint value, a different metric from maximum resident
memory; no completed elapsed-time/RSS pair is available for this operation.
The earlier complete initial processing check does not establish changed/clean
equality. **Text4096 remains unqualified.**

The largest sampled main-plus-temporary allocation was 4,069,812 KiB; clean was
6,675,008 KiB, both below their separate 8 GiB allowances. A fresh native size
check after termination recorded 3,530,428 KiB for the main workspace excluding
clean, 6,169,008 KiB for clean and 7,868 KiB of remaining temporary files. No
storage sampling errors were recorded. The failed trial, original allowances,
inputs and code remain preserved; a revised implementation needs a fresh
qualification.

## Evidence and limits

Raw evidence remains at
`/Users/mikewolfd/Work/corpora/docspec-parquet-capacity-2026-09-12`: archived source,
exact wheels and locked dependencies, installed runtime inventory, recipe and
processor, predeclared plan, original inputs, native saved references, command
lines, exit statuses, complete check outputs, time/RSS measurements, storage
samples and machine/activity notes. `implementation.json` pins the exact source,
wheel, recipe, processor, dependency list and native measurement wrapper.

The same source passed 1,129 strict regression tests, with one live integration
test deselected, and 66 focused storage/runtime checks. Installed-package
examples ran in that suite; a separate installed text16 smoke also completed
recovery and full changed/clean comparison. Console logs, native JUnit and the
independent implementation review remain with the trial.

These results establish only the measured local workloads. They do not establish
maximum corpus size, larger populations/files, parallel-worker or deployment
capacity, exact scratch peaks, cold-cache behavior, provider reliability,
PDF/OCR or general processor quality. Recovery means a completed task prefix
followed by a fresh process, not an operating-system kill during a stage.
The revisions between the earlier trial and this one include several validation
and parser changes as well as Parquet; this comparison cannot attribute a
repeatable speedup to Parquet alone.
