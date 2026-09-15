# Core import and comparison follow-up

**Comparisons now read values only for sampled changes; admitted records no
longer retain and recursively copy a second Python tree.** Membership counts,
type distinctions, record defaults and original canonical bytes are preserved.

| Operation | Before | After | Evidence limit |
| --- | --- | --- | --- |
| Federal Register: exact comparison after one value edit | 21.48 s | 2.98 s | Same retained states; historical baseline, engine already open |
| Regulations.gov: same comparison | 112.00 s | 5.81 s | Same retained states; historical baseline, engine already open |
| Three detached reads of 256 real records | 0.92 / 1.05 s | 0.45 / 0.29 s | Isolated snapshot reads, ABBA order; admission excluded |
| Import and publish 4,096 real records | 75.84 / 51.42 s | 63.95 / 125.01 s | Variable elapsed time; no end-to-end speedup established |

Both catalogue comparisons returned the exact expected occurrence IDs, counts
and value-change flags. All six before/after imports reopened with the same
canonical value digest. Separate profiles recorded about 7.4 million recursive
copy calls before the change; the new snapshot reader uses compiled decoding.
Profiled totals of 199.49 and 54.40 seconds are diagnostic, not throughput claims.

The native comparison still scans complete membership. Initial imports still
perform canonical encoding, persisted-record admission and durable publication.
These changes remove specific redundant work; they do not establish a general
framework ranking or a full-catalogue import speedup.

The subsequent [full reimport check](2026-09-14-core-catalogue-reimport-2173b92.md)
verifies both complete catalogues after these fixes and records the new revision
timings and overheating limits on import-time comparisons.

The [harness](2026-09-14-core-import-compare.py) uses existing production APIs.
The [receipt archive](2026-09-14-core-import-compare.json.gz) preserves all seven
receipts under their original filenames, including the interrupted first harness
attempt and intermediate comparisons. Input slices, hashes, source changes and
settings are retained there. The original catalogue inputs and comparison states
were not modified.

The final state-storage, publication, ledger, provenance and CLI regression run
passed **57 tests** in 350.07 seconds. Earlier record/admission and focused runs
also passed (88 and 9 checks, with overlapping coverage). Cases include aliases,
null and type distinctions, empty and zero-sample comparisons, concurrent reads,
record defaults, mutation isolation, retries and immutable-identity conflicts.
An initial fixture accidentally shared an entity ID with a state ID; correcting
the fixture resolved that failure. Ruff and whitespace checks pass. These are
local checks; no CI or remote publication was run.

## Measurement protocol and corrections

Decision: remove avoidable payload work from comparisons and import publication.

Hypotheses: comparing compact membership addresses before fetching sample values
reduces full-state comparison time; repeated copies of admitted values account for
a meaningful part of import publication. A small import profile will distinguish
copying from decoding, native writes and ledger work before the import change.

Arms: current commit `0760e8b` versus the targeted fixes. Baseline measurements
are collected before source edits. Each comparison checks the same retained
Federal Register and Regulations.gov base and one-value revision. Imports use
the first 32 records of each source partition from both original catalogues,
through `CoreWorkspace.create`, into fresh temporary workspaces.

Held constant: one native thread, 6 GiB managed engine memory, identical source
records and settings. Two uninstrumented imports and one separate profiled import
per arm; one comparison per catalogue per arm. Cache and filesystem load are
uncontrolled. Stop each invocation at ten minutes. Original catalogues and
retained comparison states are read only.

Decision rule: adopt if exact comparison outputs and reopened imported values
match, relevant regressions pass, and the measured operation improves. The
profile explains cost; instrumented time is not a throughput measurement. Do not
extrapolate the small import measurement into a full-catalogue speed guarantee.

Harness correction before implementation: the first attempt retained every
decoded input and sorted all decoded output values in Python. One import took
110.93 seconds and verified, but a process sample found heavy garbage collection
while those object graphs remained live. The second repetition was interrupted;
its partial receipt is retained separately. The harness now retains source bytes
and streams decoded input and output, matching the catalogue import path. Source
records and acceptance criteria are unchanged; the interrupted run is excluded
from the before/after comparison.

To avoid repeating the expensive existing comparison, use its retained
one-value-edit measurements (21.48 seconds Federal Register, 112.00 seconds
Regulations.gov) as historical controls. That code is unchanged between the
measured implementation and `0760e8b`. New timings reuse those exact states;
they are not a controlled claim about warm versus cold cache performance.

The first comparison follow-up included engine startup (33.08 and 40.73 seconds).
The next excluded startup to match the historical timer (13.98 and 24.59 seconds).
Both receipts remain. The final implementation also compares canonical membership
bytes before decoding changed addresses, and its separate receipt owns the final
timings. Counts and sampled values agree in every completed comparison.

Whole-import timings varied too much to establish an end-to-end improvement:
75.84/51.42 seconds before, 63.95/125.01 seconds after. The separate profiled runs
took 199.49 and 54.40 seconds. The baseline profile identified 7.4 million
recursive copy calls, which the change removes. To isolate that improvement,
measure three detached reads of 256 admitted records (every sixteenth sample
record) using the actual baseline and current snapshot classes, ordered ABBA.
Keep input admission outside that timer and require returned values to agree.
This follow-up settles only the cost of snapshot reads, not total import time.
