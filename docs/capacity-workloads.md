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
current release capacity. The capacity candidates remain unqualified.

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
The 4,096-document text and 256-document markup candidates remain unmeasured.

The before wheel's SHA-256 is
`3a8cf9f290b7ac7c68c68103a0bf7b4e7b882d873c26c97c9e736b166082be2a`;
the after wheel's is
`f1474c58ed82980c4f26ee99266e1ac0a33abce6d80c1bb0b9424d080429b43f`.
Local raw evidence remains under `/tmp/docspec-validation-cost-baseline` and
`/tmp/docspec-validation-cost-current`: archived source, wheels, installation
records, inputs, native result references, complete check outputs and `.time`
files. The shared recipe and commands above reproduce the operations; build a
fresh directory when code or input pins change.
