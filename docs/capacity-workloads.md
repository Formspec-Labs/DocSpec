# Reproduce a local capacity workload

The [workload recipe](../tests/support/capacity_experiment.py) exercises catalog
construction, capture, later processing, processor changes, saved-task recovery
and clean comparison through the existing local runtime. Run its operations as
ordinary processes; the operating system measures them. It supplies no scheduler,
capacity verdict or replacement for native run and result references.

Install the wheel being measured in an isolated environment outside the checkout.
Copy the recipe and [phrase processor](../examples/phrase_match_processor.py)
there, preserving the processor's `examples/` directory. Record the wheel,
dependencies and interpreter actually installed. Use a new output directory for
each workload. Set `TMPDIR` before starting Python when measuring temporary files.

Generation records the recipe's whole-file digest in `arguments.json` as
reproduction evidence. Source and retained-result producer identities use the
DocSpec wheel digest; processing identities remain in the existing pinned stage
and processor descriptions, configuration and vocabulary resources. Later
operations still refuse changed wheel or processor bytes. An edit to measurement
or checking code alone does not invalidate those dataset pins.

Record the recipe actually invoked alongside each operation's native measurement
output, for example with `shasum -a 256 capacity_experiment.py`. Keep the original
copied recipe and saved arguments with completed trials. A revised recipe's
results need their own recorded digest; the generation digest does not describe
later edits.

For example, from that copied directory with its environment active:

```sh
python capacity_experiment.py generate /absolute/path/to/text-trial \
  --workload text512 --wheel /absolute/path/to/docspec.whl \
  --completed-at 2026-09-12T12:00:00Z --deadline 4000000000
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
```

Choose the recorded timestamp and absolute deadline for the actual trial.
Alternatively, use `process` instead of `prefix` and `resume`. Recovery here means
a completed, nonempty task prefix followed by a fresh process; it does not mean
an operating-system kill during a stage. The clean workspace shares original
source files and catalog input, with separate result storage and processor cache.
Checks compare every selected source, captured byte sequence, representation,
segment and derived value, including quote positions and source associations.
Observed component calls also establish which upstream work was reused.
The compare operation opens one inspection view for each result, checks both
complete fixture outputs, then passes those same views to the native comparison.
Record and blob reads retain their checks; this avoids repeating full admission
solely to construct another view in the same operation.

| Workload | Selected documents | Captured files | Captured bytes | Segments |
| --- | ---: | ---: | ---: | ---: |
| `text16` smoke | 16 | 16 | 327,680 | 80 |
| `markup16` smoke | 16 | 32 | 6,422,528 | 288 |
| `text512` control | 512 | 512 | 10,485,760 | 2,560 |
| `text4096` candidate | 4,096 | 4,096 | 83,886,080 | 20,480 |
| `markup256` candidate | 256 | 288 | 99,090,432 | 3,712 |

Each workload also includes explicitly excluded catalog inputs. The two smoke
cases check the recipe's branches and are not capacity claims. Use a prefix of
four tasks for `text16`; the default would complete all its tasks.

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
original allowances. The fresh text4096 trial remains in progress; it has not
yet qualified. The report pins the installed wheel and records native operation
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
