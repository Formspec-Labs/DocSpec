# Retained catalogue reimport check

**Both retained catalogues passed: 3,229,352 records, zero missing, additional or
changed values.** Each source member matched its recorded digest. Reopened states
matched every original ID and canonical value hash, with no duplicate IDs. Every
base Iceberg data file remained byte-identical through the test revisions. The
original catalogues were preserved; no publisher downloads were needed.

The [Federal Register receipt](2026-09-14-iceberg-federal-register-reimport.json)
and [Regulations.gov receipt](2026-09-14-iceberg-regulations-gov-reimport.json)
contain the exact input pins, counts, versions and measurements. These are
catalogue records, not downloaded document bodies. Both runs used production
Core APIs at commit `dafe6af`, DuckDB 1.5.5, PyIceberg 0.12.0 and PyArrow 25.0.1.

| Check | Federal Register | Regulations.gov |
| --- | ---: | ---: |
| Distinct records | 1,007,639 | 2,221,713 |
| Original source-item JSON | 7.626 GB | 22.984 GB |
| Import and durable publication | 16 min 49 s | 50 min 42 s |
| Reopen, file verification and complete value comparison | 4 min 09 s | 11 min 47 s |
| Base Parquet files | 6 | 13 |
| Base Parquet bytes | 0.613 GB | 1.665 GB |
| Core workspace, including ledger and revisions | 1.310 GB | 3.203 GB |
| Peak process memory | 8.80 GiB | 9.85 GiB |

## Small revisions

Each revision starts from the imported base. The value-edit timing includes its
transformation and provenance publication. Timings below are seconds.

| Operation | Federal Register publication | Regulations.gov publication | Federal Register full comparison | Regulations.gov full comparison |
| --- | ---: | ---: | ---: | ---: |
| Remove one key | 0.353 | 0.362 | 30.874 | 101.922 |
| Remove 1,024 keys | 0.547 | 0.390 | 23.939 | 112.713 |
| Edit one value | 0.579 | 1.377 | 21.482 | 112.003 |

The removals wrote no new data rows. Each 1,024-key removal wrote exactly 1,024
positional deletions in one file: 5,852 bytes for Federal Register and 5,837 bytes
for Regulations.gov. Editing one value appended exactly one occurrence row and
one membership row, plus one positional deletion. All original data files were
shared unchanged. Every revision's added, removed and changed counts matched
its intended edit.

The result supports cheap physical updates on these real catalogues. Initial
import and full-state comparison remain slow: publishing the tested revisions
took 0.35–1.38 seconds, while their full comparisons took 21–113 seconds. This
check does not isolate JSON processing, native sorting, ledger admission or
storage costs. It does not compare equivalent workloads on the former backend
or another engine.

One run per catalogue used one DuckDB thread and a 6 GiB managed-memory allowance;
process memory includes allocations outside that allowance. Runs partly
overlapped on the same machine. The workspace sizes exclude the separate expected
and recovered digest files used by the test. The temporary REST fixtures were
removed after completion. Production source and the Core spec were unchanged.

The [harness](2026-09-14-iceberg-catalog-reimport.py), raw receipts, and
[time-bound amendment](2026-09-14-iceberg-catalog-reimport-time-amendment.json)
make the check repeatable. The final harness adds an optional time-limit argument;
its import and verification logic is unchanged from the data runs. Fresh workspaces
and full logs remain under
`/Users/mikewolfd/Work/corpora/docspec-iceberg-reimport-20260914/`, in
`federal-register-2/` and `regulations-gov/`. Each base state is named `catalogue`.

The [initial harness failure](2026-09-14-iceberg-catalog-reimport-setup-failure.json)
occurred before any import: the harness assumed the two small metadata members
used blob addresses, but they are artifact-local files. The corrected harness
handles both descriptor forms. The failed receipt and log remain retained;
no data result from that attempt is counted as a completed run.

## Protocol and recorded execution adjustments

Decision: Can the current Core Iceberg writer import the two retained catalogues faithfully and publish small revisions without rewriting their original data files?

Hypothesis: Every original source-item ID and canonical value survives reopening. Removing 1 or 1,024 keys writes deletion records proportional to the change and shares the original membership data files. Editing one value appends one occurrence and shares the original occurrence files.

Arms: The original pinned Federal Register and Regulations.gov catalogues supply the expected values; the current Core implementation supplies the imported values. This is an equality and capacity check, not a timed comparison against the previous backend.

Cases: All 1,007,639 Federal Register records, then all 2,221,713 Regulations.gov records, including every disposition. Verify source manifest and member hashes, compare all IDs and canonical value hashes after reopening, then exercise two removals and one value edit. Original catalogue files are read only.

Held constant: One run per catalogue, one DuckDB thread, 6 GiB managed engine memory, local retained inputs, the pinned REST fixture in `tools/with_iceberg.py`. Import timing includes source decoding, hashing and a compact expected-value digest file. No second full-size copy of the inputs. Stop a dataset after 45 minutes or if free disk falls below 10 GiB; preserve failed receipts.

Decision rule: Any missing, additional or altered value fails the import check. Unexpected original data-file replacement fails the small-revision check. Report absolute timings and output sizes without claiming a framework speedup. Stop after these cases answer the question; no benchmark campaign.

The companion script takes an original artifact directory and a **new** output directory. Run it through `tools/with_iceberg.py` from the output directory's parent so the local REST fixture can access the new workspace. The receipt records source pins, software versions, checks and stage timings. Digest comparison uses stable IDs rather than row order.

Execution adjustment: Federal Register began first. After its inputs were read,
the ledger publication phase remained active with roughly 3 GiB process memory.
Before starting Regulations.gov, the machine had 48 GiB memory, 14 logical CPUs
and about 90 GiB free disk. Start that independent import concurrently to shorten
the total wait. Keep separate workspaces, one native thread and 6 GiB per engine.
The timings consequently include overlap and uncontrolled shared filesystem load;
they are descriptive timings, not an isolated performance comparison. The equality
and file-sharing criteria remain unchanged.

Time-bound adjustment: Regulations.gov's measured publication rate made the
original 45-minute bound too short for a healthy full run. At about 21 minutes
elapsed, its process alarm was extended by one hour, allowing about 81 minutes
total. The process continued without restarting the import. Two brief debugger
attachments changed only that timer; elapsed measurements include those pauses.
The original receipt still records its initial 45-minute setting. The retained
`timer-extension.log` beside the workspace records the adjustment. The final
harness accepts an optional third argument for the time limit in minutes, so a
repeat can set this allowance at startup. No correctness criterion changed.
