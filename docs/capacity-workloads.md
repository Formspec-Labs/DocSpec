# Reproduce a local capacity workload

The [Core C01 map](core-model-implementation-map.md#capacity-targets-fixed-before-tuning)
fixes the next implementation's workload inputs and time, memory and scan targets.
C03 qualifies its bulk operations and C25 measures the assembled runtime. The
observations below retain their original revisions and scopes.

The [workload recipe](../tests/support/capacity_experiment.py) exercises catalog
construction, capture, later processing, processor changes, retained-prefix
recovery and clean comparison through `CoreWorkspace` and its document pipeline.
Run each operation in a fresh process; the operating system measures it.

Install the measured wheel in an isolated environment outside the checkout.
Copy the recipe, [dataset helpers](../examples/dataset_example_support.py) and
[phrase processor](../examples/phrase_match_processor.py) there, preserving the
two helpers' `examples/` directory. Record the wheel, dependencies and interpreter
actually installed. Use a new output directory for each workload. Set `TMPDIR`
before starting Python when measuring temporary files.

Generation records the recipe's whole-file digest in `arguments.json` as
reproduction evidence. Source producer identity uses the DocSpec wheel digest;
processing definitions pin the stage configuration and vocabulary resource.
Later operations refuse changed wheel or phrase-processor bytes. Record the
recipe and both helpers alongside each operation's native measurement output,
for example with `shasum -a 256`. Keep the original copied files and saved
arguments with completed trials. A revised recipe needs its own recorded digest.

For example, from that copied directory with its environment active:

```sh
python capacity_experiment.py generate /absolute/path/to/text-trial \
  --workload text4096 --wheel /absolute/path/to/docspec.whl
python capacity_experiment.py build /absolute/path/to/text-trial
python capacity_experiment.py verify /absolute/path/to/text-trial
python capacity_experiment.py capture /absolute/path/to/text-trial
python capacity_experiment.py check /absolute/path/to/text-trial --phase capture
python capacity_experiment.py prefix /absolute/path/to/text-trial --prefix-tasks 16
python capacity_experiment.py resume /absolute/path/to/text-trial
python capacity_experiment.py inspect /absolute/path/to/text-trial
python capacity_experiment.py check /absolute/path/to/text-trial
python capacity_experiment.py changed /absolute/path/to/text-trial
python capacity_experiment.py clean /absolute/path/to/text-trial
python capacity_experiment.py compare /absolute/path/to/text-trial
python capacity_experiment.py behavior /absolute/path/to/text-trial
```

Use `--workload markup256` and a separate root for the markup candidate. Core
attempts record their real execution times. Use native process limits to enforce the
[C01 allowances](core-model-implementation-map.md#capacity-targets-fixed-before-tuning).
Alternatively, use `process` instead of `prefix` and `resume`.

`prefix` injects an exception before the next document processor operation after
N operations complete. The shared Core owner retains the completed choices and
the interrupted attempt. `resume` opens the same workspace in a fresh process
and reuses those exact choices. The count is document processor operations, one
per captured file; phrase calls are counted independently for every segment.
This checks retained-prefix recovery, not an operating-system crash or recovery
from an in-flight Core checkpoint. The generic Core capacity probe covers the
separate checkpoint-resume path.

The clean workspace shares original source files and catalog input, with separate
Core storage. Checks independently compare every selected source, captured byte
sequence, representation, segment and phrase value, including exact quote
positions and source spans. Excluded catalog inputs must have no stage results.
The shared implementations' observed calls prove that later processing avoids
fetches and vocabulary changes avoid extraction and segmentation. The comparison
checks both complete streams against the fixture oracle, then compares their
logical digests while excluding attempt identities and acquisition timestamps.
Fresh `inspect` uses the ordinary Core state inspector. Each `run.json` retains
the actual Core state and observed calls.

The separate `behavior` operation uses the same complete frozen population and
excluded inputs in a new `behavior/` workspace. It fails one identified extraction,
reopens and repairs it while selecting the exact retained capture results, then
changes only source titles and requires every stage result to remain unchanged.
It changes vocabulary A to B and back to A; the final selection must return the
original A results with zero algorithm calls. Each completed state passes the
same complete value and source-span oracle. Its `behavior-report.json` records
failed result identities, exact reuse, observed calls and checked digests.
A native measurement covers several stages together; that total is not a
single-run latency measurement. Count this additional workspace in the trial's storage receipt. These are actual
document operations; they do not substitute generic Core fixtures for document
coverage. The workspace reopens between stages within this one command.

| Workload | Selected documents | Captured files | Captured bytes | Segments |
| --- | ---: | ---: | ---: | ---: |
| `text16` smoke | 16 | 16 | 327,680 | 80 |
| `markup16` smoke | 16 | 32 | 6,422,528 | 288 |
| `text512` control | 512 | 512 | 10,485,760 | 2,560 |
| `text4096` candidate | 4,096 | 4,096 | 83,886,080 | 20,480 |
| `markup256` candidate | 256 | 288 | 99,090,432 | 3,712 |

Each workload also includes explicitly excluded catalog inputs. The two smoke
cases check the recipe's branches and are not capacity claims. Use a prefix of
four processor operations for `text16`; the default would complete all its files.

On September 14, the migrated Core recipe passed both smoke workloads using an
isolated Python 3.12.13 and wheel SHA-256
`afcf7489c828632dfb8c5508c6faaa56b7ff61f23328fc447912d68eb73efcbe`.
Each completed catalog build/verification, capture, an injected interrupted
processor attempt, fresh-process prefix recovery, inspection, changed vocabulary,
and clean comparison. Both also passed the separate failed-input repair,
title-only revision, and A→B→A document cases. Complete oracles checked 80 text
and 288 markup segments per result. Changed-resource processing made exactly
those phrase calls and no upstream calls; title changes and return to A made none.
Native receipts and copied inputs remain under `/tmp/docspec-core-capacity-smoke`.
These smoke observations validate the recipe; they do not qualify either full
capacity candidate. The dated observations below retain their original scope.

On September 12, 2026, both smoke cases completed through isolated Python 3.12.13
and the core-only wheel built from `930ad06` (SHA-256
`787b9de5bdf4b0a0d27f745522f97e630638420893342fe2e8ef44a81160a934`).
Text also completed the four-task prefix and fresh-process resume. Both complete
changed-resource results matched their clean controls. These observations predate
the subsequent validation-cost cleanups; they establish recipe execution, not
current release capacity. Later pinned trials are recorded below.

Before making a capacity claim, follow the [qualification guide](qualification.md):
declare the intended workload and resource budgets, collect native time and peak
memory per operation, account for workspace and temporary storage, and retain
the actual results. A fresh process does not imply a cold operating-system cache.

## Core writer/readers and cleanup races

The [contention probe](../tests/support/core_contention_experiment.py) uses the
existing built Core fixture (`base.parquet` and `workspace/`). It runs one writer
and four readers in separate spawned processes. Every writer batch transforms
1,024 existing occurrence values and publishes a membership revision through the
shared Core owners. Readers repeatedly inspect the selected state and consume
named title/URL fields, checking each result against the fixture. The probe
records reader latency, observed versions, batch backlog, real SQLite activity,
per-process memory and durable acknowledgements. After reopening, it reconciles
every acknowledged revision, replacement identity and changed value.

Run the two independent cases after the ordinary Core build:

```sh
python -m tests.support.core_contention_experiment contention /absolute/path/to/core-trial
python -m tests.support.core_contention_experiment cleanup /absolute/path/to/core-trial
```

Defaults are the C01 workload: 100 batches, 1,024 changed members and four readers.
For a small already-built eight-member fixture, use `--batches 2 --changed-members 2`.
Each reader checks those named members plus one absent key. The 600-second
contention target includes acknowledgement reconciliation; the live workers also
have a default 600-second deadline. Reports retain failed targets. Summing each
worker's and the parent process's peak resident memory gives a conservative
upper bound, not a simultaneous memory sample.

The cleanup case uses its own `cleanup-race-workspace/`. Two processes attempt
cleanup during an in-flight publication and publication during actual deletion.
The retained shared bytes and newly published bytes must remain intact. An
injected interruption after deleting an unreferenced file must leave a durable
unfinished removal; reopening and resuming must finish it correctly.

For an isolated wheel run, copy this probe and its existing helpers
`core_runtime_experiment.py`, `core_bulk_experiment.py`, `core_workload.py` and
`core_reference.py` under `tests/support/`, with empty package `__init__.py` files.
Keep the build recipe's `uv.lock` when invoking its command-line entry point.
No checkout imports or optional providers are needed. The probe pins its copied
sources in the receipt and records installed versions. Preserve the wheel and
native time/memory evidence separately. Small smoke success does not qualify the
full contention workload.

## Generic Core state workloads

The [runtime recipe](../tests/support/core_runtime_experiment.py) uses the frozen
million-member fixture and the actual Core publication, revision, selection and
recovery owners. Run it from the isolated copied package described above, with
the wheel's matching source snapshot and lockfile retained for its source pins.
Each command starts a fresh process. For the default full-size history:

```sh
python -m tests.support.core_runtime_experiment generate /absolute/path/to/core-trial
core_cache_description='Filesystem cache not cleared; fresh process'
for stage in build open fields named ordered-fields whole recover checkpoint edits membership history history-suffix audit; do
  python -m tests.support.core_runtime_experiment "$stage" /absolute/path/to/core-trial --cache-description "$core_cache_description"
done
python -m tests.support.core_runtime_experiment clean /absolute/path/to/core-trial --state history:1015 --cache-description "$core_cache_description"
python -m tests.support.core_runtime_experiment compare /absolute/path/to/core-trial --state history:1015 --cache-description "$core_cache_description"
```

`ordered-fields` retains an explicit canonical-JSON position ordering rule and
checks member-key tie breaks independently, including equal-position values.
`membership` removes and restores 1,024 keys, verifies the complete address
population, and requires restoration to select the original URL-dependent result.
`history-suffix` checks the 1,000-revision prefix before and after checkpointing,
then appends 16 revisions and reopens both histories. `clean` regenerates expected
values in a separate sibling directory named `core-trial-clean`, preserving the
actual occurrence identities. `compare` reopens both workspaces and checks every
key, identity and value. The clean directory has its own 80 GiB allowance and
appears separately in sampled storage measurements.

The recipe refuses an existing stage receipt. Use fresh trial directories for
repeated qualification, keeping the inputs, stage order, package and settings
fixed. Record cache conditions for each trial; a fresh process does not establish
a cold filesystem cache. Receipts distinguish timed work, independent oracle
work, sampled storage, native settings and whole-process memory. A passing small
test validates a recipe path only. Full repeated trials, scan accounting and the
[larger-than-memory case](core-larger-than-memory.md) remain required.

Receipts count calls and bytes at the shared canonical encoding/decoding functions,
blob reads, selected-row conversions, SQLite statements and transactions, Python
fsync calls, and newly retained file paths. Main and clean scratch/retained storage
are sampled separately. Query receipts keep compact identifiers; raw profiles
retain the full SQL. Native SQLite fsync calls and gross allocation counts remain
outside these observers. Use `--trace-allocations` for a separate diagnostic to
record Python peak allocation bytes and the change in live allocation count;
it adds overhead and does not measure native allocations.

The [boundary recipe](../tests/support/core_boundary_experiment.py) supplies the
separate `nested`, `schema` and `boundary` stages. These exercise nested JSON
changes, supplied-schema validation, and the differences between record, commit
and content-value byte limits through the production owners.

## Local comparison after removing duplicate row storage

On September 12, the `text512` recipe ran against isolated wheels from `f686437`
and `a4a0e05`, before and after removing mirrored local record members. Both used
Python 3.12.13 and the same locked core dependencies, without optional providers
or Dagster. The host ran macOS 26.6 on arm64 with 48 GiB RAM and 14 logical CPUs.
Each operation ran in a separate process under native `/usr/bin/time -l`.
Operating-system cache and unrelated host activity were uncontrolled.

All 544 generated source files and supplied rows were byte-identical between
trials: 512 selected documents plus 32 exclusions. Capture retained 10,485,760
bytes. Both later processing runs observed 512 extractions, 512 segmentations,
2,560 processor calls and zero fetches. Full independent fixture checks passed
for every captured file, representation, segment, derived value and source
association in each result. Release and execution identities differ as expected.

| Observed operation | Before | After |
| --- | ---: | ---: |
| Capture and retain, seconds | 20.28 | 19.36 |
| Process retained captures and retain, seconds | 94.13 | 87.90 |
| Fresh retained open and summary, seconds | 12.72 | 12.73 |
| Processing peak resident memory, bytes | 123,027,456 | 123,043,840 |
| Mirrored catalog-row files / bytes | 15 / 23,433,600 | 0 / 0 |

The definite saving is the eliminated row copy. Processing elapsed time was
lower in this pair; reopening time and processing memory were essentially
unchanged. These single observations establish no repeatable percentage gain,
maximum capacity, temporary-storage peak, or hard resource limit. They do not
include changed-resource, clean-control or recovery runs at this population.
That comparison does not qualify the larger text or markup candidates.

The before wheel's SHA-256 is
`3a8cf9f290b7ac7c68c68103a0bf7b4e7b882d873c26c97c9e736b166082be2a`;
the after wheel's is
`f1474c58ed82980c4f26ee99266e1ac0a33abce6d80c1bb0b9424d080429b43f`.
Local raw evidence remains under `/tmp/docspec-validation-cost-baseline` and
`/tmp/docspec-validation-cost-current`: archived source, wheels, installation
records, inputs, native result references, complete check outputs and `.time`
files. The shared recipe and commands above reproduce the operations; build a
fresh directory when code or input pins change.

## Pinned larger workload measurements

The [fresh Parquet observations](history/2026-09-12-parquet-capacity-observations.md)
record a passing markup256 trial on `c898512`. It completed capture, saved-prefix
recovery, changed-resource reuse and complete clean comparison within the
original allowances. Text4096 passed changed-resource processing and the clean
run, then exceeded the five-minute complete-comparison limit; it remains
unqualified. The report pins the installed wheel and records native operation
time, memory, sampled storage and complete fixture evidence.

The [September 12 observations](history/2026-09-12-local-capacity-observations.md)
record a passing 256-document markup trial against the frozen `a4a0e05` wheel
and original recipe. Capture, completed-prefix recovery, changed-resource reuse,
and complete clean comparison passed within the declared local time, memory,
and sampled-storage allowances. The 4,096-document text trial passed capture,
recovery, inspection and its complete processing check, then exceeded its
30-minute changed-resource limit. It returned no completed changed result;
clean and comparison qualification did not run. The failure remains recorded
against the original code and allowances.

These observations precede the later metadata/audit split, PDF and checkpoint
cleanup, and revised recipe. They do not establish those newer revisions'
performance. Exact inputs, commands, native measurements and qualification limits
remain with the frozen trials.

## Metadata opening and full audit are separate operations

The isolated core wheel from `3eced2f` opened the already retained markup256 and
text4096 processing results from those frozen trials. Each operation ran in a
fresh Python 3.12.13 process under `/usr/bin/time -l`, including imports and local
adapter setup. It called the public catalog API directly, without an inspection
summary scan. The reader explicitly accepted the original results' recorded
producer identity; their bytes and references remained unchanged.

| Retained result | Metadata open, seconds | Open peak resident bytes | Full audit, seconds | Audit peak resident bytes |
| --- | ---: | ---: | ---: | ---: |
| Markup256: 16,512 logical records | 0.23 | 43,040,768 | 8.29 | 78,200,832 |
| Text4096: 106,496 logical records | 0.11 | 35,389,440 | 53.34 | 94,175,232 |

All four commands succeeded. For each result, opening and auditing returned
identical release references, logical-state digests and declared counts.
[Opening checks metadata and linked controls](retained-catalog.md); auditing also
checks the complete retained data. These timings measure different requested
work, not a faster implementation of the same full audit. Ordinary reads check
the records and blobs they consume. Retention, export and maintenance still
require complete checks.

The wheel SHA-256 is
`95de0784cfda0c3e51a9e7fbe5ddc92459757a7b2ed2d7e8f653eca22f7983d5`.
Exact source, installed dependencies, input references, diagnostic script and
native measurements remain under
`/Users/mikewolfd/Work/corpora/docspec-metadata-audit-2026-09-12-3eced2f`.
These observations used the same shared host, overlapped the frozen text trial's
changed-resource run and had uncontrolled OS cache. They do not qualify a
complete workload on the newer revision or establish repeatable latency.

## Rerun diagnostic and remaining read costs

The same isolated `3eced2f` wheel completed a fresh `text512` experiment: capture,
later processing, changed-resource processing, and complete comparison with a
separate clean run. The changed run made exactly 2,560 processor calls and no
fetch, extraction, or segmentation calls. Every selected source, file,
representation, segment, derived value and source association matched the clean
fixture output. Native elapsed times were 18.41 seconds for capture, 90.11 for
later processing, 71.79 for clean, and 57.43 for complete comparison.

Only the changed-resource operation used `cProfile`; its 266.55-second elapsed
time includes profiling overhead. It recorded 3,072 redundant layer-descriptor
reads from partition scans, taking 4.25 cumulative instrumented seconds. Commit
`8331028` removes those adjacent reads while preserving fresh verification in
each new lookup. The profile also shows repeated parsing of other sources in the
same record partition; it does not establish an uninstrumented speedup for a fix.
The recipe, exact commands, results and profile remain under
`/Users/mikewolfd/Work/corpora/docspec-rerun-profile-2026-09-12-3eced2f`.

A separate parser diagnostic compared the source at `8331028` with direct shared
encoding of plain decoded JSON before freezing. Four passes over 4,096 saved
record rows took 0.975 and 0.817 seconds respectively; every returned row's
complete logical value agreed. That loop includes parsing and conversion to
mutable rows, excluding input loading and output comparison. Both source files,
input bytes, commands and observations remain under
`/Users/mikewolfd/Work/corpora/docspec-parser-copy-2026-09-12`. This small source-level
comparison is not an installed-workload or repeatable-throughput claim. These
diagnostics and checkout tests overlapped the frozen text trial on the shared
host; its activity log records those conditions.

## Direct Parquet query prototype

After the frozen text4096 changed-resource operation exceeded 1,800 seconds, a
disposable prototype converted its eight already-retained layers to Parquet.
It used Python 3.12.13, DuckDB 1.5.5 and PyArrow 25.0.1 outside the checkout.
All 106,496 complete logical records matched their originals. Input member
digests, sizes and counts were checked during conversion. JSONL member bytes
totaled 153,821,952; the prototype's Parquet files totaled 23,571,669 bytes.

The prototype stored four typed delivery fields and a canonical JSON payload,
sorted by source and record identity, with one file per layer and 2,048-row
groups. Conversion used Arrow batches capped at 512 rows and 4 MiB of input
bytes. Conversion plus complete parity checking took 12.22 seconds and peaked
at 351,191,040 resident bytes. The independent parity checker kept per-layer
record hashes in memory; that checker is not the production bounded reader.

Direct queries of files, segments and receipts returned the same 73,728 complete
selected values in both query patterns:

| Query pattern across 4,096 sources | Measured query-loop seconds |
| --- | ---: |
| One source per query | 13.217 |
| 128 sources per query | 1.523 |

Each loop decoded and hashed returned payloads; no whole-file hash ran per
query. Each process owned one DuckDB connection, with one native thread, a
512 MB memory setting and a 2 GB temporary-storage setting. These settings are
not process-memory guarantees. The host and operating-system cache were shared
and uncontrolled. These are single observations, not repeatable speed claims.

The selected [record backend](record-storage.md) instead stores generic routing
columns and complete JSON row bytes, with existing bucket and shard boundaries.
The prototype's layout and timings therefore do not measure that implementation.
They support direct Parquet queries and batching as a direction; complete
installed lifecycle, concurrency, corruption and capacity checks remain required.
The original failed trial stays unchanged.

Exact scripts, inputs, versions, file pins and native measurements remain in
`/Users/mikewolfd/Work/corpora/docspec-parquet-duckdb-prototype-2026-09-12`.
DuckDB documents [direct Parquet filtering and projection](https://duckdb.org/docs/current/data/parquet/overview)
and [the benefit of larger queries](https://duckdb.org/docs/current/guides/performance/how_to_tune_workloads).
Neither documentation nor this prototype establishes DocSpec's capacity.
