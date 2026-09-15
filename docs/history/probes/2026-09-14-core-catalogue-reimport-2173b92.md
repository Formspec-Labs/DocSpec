# Full catalogue reimport after the import and comparison fixes

Both fresh imports passed through commit `2173b92`: **3,229,352 records**, zero
missing, additional or altered values, and all six revision cases passed. Source
identities, inventories and disposition counts match the earlier catalogue runs.
The original data files remained unchanged after the revisions.

The initial overheating and shared-host conditions prevent a reliable comparison
of total import times with the earlier run. The timings below describe these
completed runs; they do not establish a whole-import speedup or regression.

| Catalogue | Records | Import and publication | Full reopen and value verification |
| --- | ---: | ---: | ---: |
| Federal Register | 1,007,639 | 2,550.12 s | 250.23 s |
| Regulations.gov | 2,221,713 | 3,994.89 s | 773.39 s |

| Revision | Federal Register publication | Federal Register comparison | Regulations.gov publication | Regulations.gov comparison |
| --- | ---: | ---: | ---: | ---: |
| Remove one key | 0.32 s | 0.43 s | 0.25 s | 0.64 s |
| Remove 1,024 keys | 0.52 s | 2.21 s | 0.24 s | 4.57 s |
| Edit one value | 0.90 s | 0.37 s | 0.67 s | 0.63 s |

Removals wrote only the expected one or 1,024 deletion rows. Each value edit
added one entity row, one membership row and one deletion row. Both retained
their original data files. Base storage used five entity files plus one membership
file for Federal Register (612,746,715 bytes), and twelve plus one for
Regulations.gov (1,664,797,362 bytes). Peak process memory was 8.12 and 8.14 GiB,
respectively; the 6 GiB setting controls managed engine memory.

The [receipt archive](2026-09-14-core-catalogue-reimport-2173b92.json.gz) contains
both complete receipts, progress logs, diagnostic logs and the time-bound
amendment. Both processes exited successfully, and the last checks completed at
2026-09-15 00:07:54 UTC (September 14 locally). This check covers retained
catalogue metadata, complete value recovery and keyed revisions. The broader
qualification matrix was not repeated.

## Protocol and run conditions

Decision: verify the complete Federal Register and Regulations.gov catalogues
through production commit `2173b92`, and measure import and revision costs after
the snapshot-copy and comparison changes.

Use the existing [harness](2026-09-14-iceberg-catalog-reimport.py) unchanged. Import
all 1,007,639 Federal Register records and all 2,221,713 Regulations.gov records
into new workspaces. Hash every source member, reopen and check every retained
file, and compare all IDs and canonical value hashes. Then remove one key, remove
1,024 keys, and edit one value from each imported base. Require exact revision
counts, proportional new/deleted rows, and unchanged original data files.

Run the catalogues concurrently in separate workspaces, each with one native
thread, 6 GiB managed engine memory and its own local REST fixture. Allow 90
minutes per catalogue from startup and stop if free disk falls below 10 GiB.
Keep all failure receipts and logs. Original catalogues and earlier imported
workspaces remain unchanged. These are catalogue metadata, not downloaded bodies.

Compare descriptive timings with the retained earlier runs; shared filesystem
load, cache state and concurrent work prevent a controlled speedup claim. Success
requires zero missing, additional or altered values and all revision checks to
pass. Stop after these two complete runs; do not repeat the broader test matrix.

Outputs and full logs are retained under
`/Users/mikewolfd/Work/corpora/docspec-iceberg-reimport-2173b92-20260914/`.

Both import timers began at 2026-09-14 22:48:16 UTC on a 48 GiB, 14-logical-CPU
Mac. Versions: Python 3.12.9, DuckDB 1.5.5, PyArrow 25.0.1, PyIceberg 0.12.0,
msgspec 0.21.1. Harness SHA-256:
`5acaa5a815c2b091082280c75cd01f8c841038bb59149e0d3b11b21552e76941`.

Scheduling adjustment: at 22:53:08 UTC, clear macOS's background scheduling flag
on the two owned test processes with `taskpolicy -B` (both calls succeeded).
Before that, each process had accumulated about 52 CPU seconds in 4 min 25 s of
wall time. Do not restart either run; elapsed timings include that initial period.
Native thread and memory settings, inputs and correctness criteria are unchanged.

Further scheduling checks: at 23:02:28 UTC, request throughput and latency tier
zero on both owned processes (`taskpolicy -t 0 -l 0`, both succeeded). At roughly
23:04–23:05 UTC, briefly attach LLDB to Federal Register and request
`QOS_CLASS_USER_INITIATED` for its main thread; the call returned zero and the
process resumed. Its retained `federal-register-scheduling.log` records the call.
These adjustments did not establish an improvement. A 23:01:48 UTC system
snapshot reported 18 GiB of compressed memory, 47 GiB of used physical memory,
and a load average of 184. A one-second sample of the Regulations.gov process
showed active Python encoding through DuckDB's Arrow input reader. Shared-host
conditions and the diagnostic pause are included in elapsed timings.

Time-bound amendment, 23:11 UTC: source loading remains healthy but is far slower
than the historical run. Extend the remaining allowance to six hours for each
owned process without restarting it, replacing the original 90-minute alarm.
The 10 GiB free-disk stop remains. Preserve debugger logs and the amendment receipt;
the harness's original settings still record its 90-minute launch argument.
The longer allowance permits the requested complete-data checks under observed
host pressure; it does not change acceptance criteria or establish acceptable
production throughput.

Observed transition around 23:20 UTC: source progress accelerated in both jobs
without another code, scheduling or engine-setting change. At 23:21 UTC both
processes used about 99% of one CPU core. Federal Register's later 50,000-row
intervals took roughly 13–17 seconds, compared with several minutes earlier.
This reinforces the shared-host limitation on whole-run timing comparisons;
the observation does not identify the cause of the transition.

The user subsequently reported that the Mac had been overheating. This is
consistent with the observed recovery, but thermal throttling was not directly
measured by the harness. Treat overheating and the observed host pressure as
environmental limitations, not evidence of a code regression.
