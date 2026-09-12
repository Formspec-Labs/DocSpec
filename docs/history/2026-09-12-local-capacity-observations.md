# Frozen local capacity observations

**The markup256 trial passed its declared local qualification. Text4096 remains
in progress.** These observations apply to DocSpec source `a4a0e05` and the
original recipe, before later metadata/audit and recipe simplifications.

The tested wheel SHA-256 is
`f1474c58ed82980c4f26ee99266e1ac0a33abce6d80c1bb0b9424d080429b43f`.
The recipe SHA-256 is
`b399026486db5944decc40a98cc0e2958826ddaec0630bf8ad2878361a6cc7ce`;
the phrase processor is
`dfd6057bc548ed70611aec29d0555611c8c32dab9a49c70d11bef143c44df103`.
Both files can be recovered from that source revision. Python 3.12.13 ran with
assertions enabled in an isolated, hash-locked core installation, including
Rulespec Artifacts 1.0.12. The imported package location and complete installed
distribution list are retained in `installed-runtime.json` beside the trial.

## Accepted scope and measurement

Before generation, `qualification-plan.md` fixed both populations, operations and
ceilings. Each trial uses one local worker and one task in flight. The independent
trials may overlap on the same macOS 26.6 arm64 host: 48 GiB RAM, 14 logical CPUs,
APFS storage, with uncontrolled OS cache and other activity. Dedicated workspaces
and temporary directories separate trial state. There is no isolated-latency,
aggregate memory or parallel-worker claim.

Native `/usr/bin/time -l` measures each Python operation. Its peak resident-memory
values on this host are bytes. Every operation must remain within 1 GiB. Build,
source verification, capture, processing, changed and clean each have 30 minutes;
processing sums prefix and resume active time. Inspect and complete compare each
have five minutes. Fixture generation and independent checks must succeed and
are timed separately without a latency threshold.

Native `du -sk` records allocation before/after and roughly every ten seconds.
The observed main workspace plus temporary directory must fit 8 GiB, with a
separate 8 GiB allowance for the clean control. Samples are not atomic, may
overlap at completion, and can miss brief peaks; they establish no exact storage
high-water mark or enforced quota. Native operation times and actual start/end
timestamps are retained separately from the fixed logical recipe timestamp.

## Markup256 result

The source catalog contains 256 selected documents and 16 explicit exclusions.
Selected content comprises 288 files, 99,090,432 bytes and 3,712 segments, with
HTML files up to 4 MiB and some documents containing two files.

| Operation | Elapsed seconds | Peak resident bytes |
| --- | ---: | ---: |
| Generate fixtures | 1.13 | 76,103,680 |
| Build catalog | 0.55 | 54,493,184 |
| Verify source catalog | 0.25 | 51,642,368 |
| Capture and retain | 10.85 | 69,533,696 |
| Complete capture check | 2.58 | 87,359,488 |
| Completed prefix of 16 tasks | 8.35 | 97,435,648 |
| Resume and retain | 90.41 | 129,662,976 |
| Fresh inspection and summary | 17.14 | 98,959,360 |
| Complete processing check | 22.99 | 141,344,768 |
| Changed phrase resource and retain | 133.12 | 127,107,072 |
| Clean v2 run and retain | 102.65 | 144,867,328 |
| Complete checks and native comparison | 82.68 | 146,456,576 |

Prefix plus resume took 98.76 seconds of active time, separated by 39 seconds.
Their combined calls were exactly 288 extractions, 288 segmentations and 3,712
processor invocations, with no fetches. The changed-resource run invoked only
the processor, 3,712 times. The clean control used separate result storage and
cache while sharing unchanged physical source files and source catalog.

Every command exited successfully. The fixture oracle checked every selected
identity, captured byte sequence, representation/map, segment/coordinate, derived
value and source association. Complete changed/clean oracle streams agreed;
their logical stream digest is
`sha256:0d7312a3a6159000862e3f5fcfccae11f61c45338eb67cd7cde57f74446022ec`.
The native comparison also reports expected differences in execution provenance.

The largest sampled main-plus-temporary allocation was 881,996 KiB; clean was
1,068,124 KiB. The largest temporary sample alone was 38,996 KiB. Final allocation
was 808,276 KiB for the main workspace and 1,031,272 KiB for clean, with zero
temporary allocation. No `du` errors were recorded. Workspace totals include
saved intermediate state, not just the final retained catalog.

## Text4096 progress and limits

The 4,096 selected documents plus 256 exclusions comprise 4,096 captured files,
83,886,080 bytes and 20,480 segments. Build, source verification, capture, its
complete byte check, a 16-task prefix, fresh-process recovery, inspection and
the complete processing oracle have passed. Prefix plus resume took 1,309.71
seconds of active time with a 16-second gap; the maximum of their resident
memory peaks was 143,884,288 bytes. Fresh inspection took 119.51 seconds.
Changed-resource, clean-control and complete-comparison qualification remain
pending. Do not infer their outcome from these earlier phases.

Checkout test activity overlapped part of this text trial; actual intervals are
recorded in `background-activity.txt`. It used a separate environment and did not
change the installed trial code or inputs. Timings describe that shared host.

Neither case establishes maximum corpus size, larger files/populations, exact
scratch peaks or quotas, cold-cache performance, deployment/parallel-worker
capacity, real-provider reliability, PDF/OCR quality, active-stage kill at these
sizes, or general processor quality. Recovery here means a completed task prefix
and a fresh process. A threshold failure remains a failure until corrected and
rerun, rather than being converted to a pass by changing the threshold.

## Retained evidence

Raw evidence remains at
`/Users/mikewolfd/Work/corpora/docspec-capacity-2026-09-12-a4a0e05`:
archived source, exact wheels and requirements, installed environment, original
recipe/processor, predeclared criteria, generated source files and supplied rows,
saved native plan/handoff/run/release references, command lines, exit statuses,
stdout, native time/RSS output, storage samples and machine/activity notes.
Use that original recipe to reproduce this revision. The current recipe reuses
comparison views and separates its code digest from dataset producer identity.
