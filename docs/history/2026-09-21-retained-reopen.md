# Reopen retained states without repeating semantic validation

`CoreWorkspace.open_state` now requires a successfully retained local state and
representation. It freshly checks the root and all pinned Iceberg recovery-file
hashes, then reuses publication's validation of the logical records and complete
membership. It performs no whole-record Python decoding or membership audit on
reopen, including after process restart. New data and external exports retain
full admission; explicit `records.verify` and `records.admit` remain full audits.

The [focused regression receipt](probes/2026-09-21-retained-reopen.json) records
the failing old-path check, passing fresh-process check, retention guards,
damaged-byte refusal, explicit audit coverage, and publication/import regressions.
Independent static review approved the owner boundary and changed path.

The [installed full-input receipt](probes/2026-09-21-retained-reopen-full.json)
records one fresh process opening the existing Federal Register and Regulations.gov
snapshots with the ordinary DocSpec 0.7.1 wheel. Both pins and counts matched;
known original values from every retained disposition matched the independent
source examples. The probe made full logical audit entry points fail immediately,
so its success establishes that reopen did not call them. Both layers of each
snapshot still passed fresh physical verification.

This reuses the local publisher's durable record; it does not introduce a cache
or trust arbitrary imported ledger files. Retained files must remain immutable
while readers are open. Hashing still reads the file bytes. Timings in the full
receipt are observations from a concurrent local run, not an isolated benchmark.
The search index build continued independently under its unchanged prior runtime.
