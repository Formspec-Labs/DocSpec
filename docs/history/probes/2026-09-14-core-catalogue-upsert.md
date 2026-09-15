# Incremental catalogue update qualification

The new `CoreWorkspace.upsert` API passed incremental checks on both existing
imports. No catalogue was reimported. Each test branch added 1,024 synthetic
records, replaced 16 existing records, then added one more record and replaced
one of the new records. The original `catalogue` states remain unchanged.

| Operation | Federal Register (1,007,639 base rows) | Regulations.gov (2,221,713 base rows) |
| --- | ---: | ---: |
| Publish 1,024 additions and 16 replacements, including pointer advance | 1.388 s | 1.008 s |
| Compare complete membership with the base | 0.399 s | 0.693 s |
| Publish a following batch: one addition and one replacement | 0.466 s | 0.608 s |
| Recover the first batch after reopening | 0.042 s | 0.042 s |

Both checks verified exact supplied values, expected population, unchanged base
values, and reuse of every original data file. File paths, sizes, row counts and
modification times matched before and after; this check did not rehash all old
payloads. The preceding full reimport qualification already checked those bytes.
The second batch survived reopening. Retrying the first batch returned its exact
state without another execution or moving the current pointer backward.
Changing the retry input was rejected.

Test data is explicitly labelled and confined to retained test branches, with
the separate dataset pointer `qualification-upsert-20260914-v1`. Source artifacts
and any real catalogue current pointers were not changed. The receipt records
the resulting state IDs and workspace paths.

These are single local runs with the existing one-thread engine setting and
6 GiB engine memory budget. Added values are small synthetic records;
replacements retain sampled catalogue values with a test field added. The times
establish that this update path works on the retained populations, not a general
throughput guarantee or an end-to-end publisher ingestion benchmark. Discovery,
network fetching, and extraction were outside this check.

The [harness](2026-09-14-core-catalogue-upsert.py) reused inventory helpers from
the previous reimport probe and completed within its 180-second bound.
[Raw results](2026-09-14-core-catalogue-upsert.json) preserve checks and timings.
Focused regressions additionally cover duplicate keys, invalid values, batches
larger than 2,048 rows, interruptions before and after journaling, concurrent
requests, stale current pointers, and the CLI.

Final local verification: **98 tests passed** across ingestion, runtime/CLI,
revision, execution, ledger and maintenance tests. Ruff and `git diff --check`
passed. This was a focused change check, not a rerun of the entire conformance suite.
