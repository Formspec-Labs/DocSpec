# One document, from local input to verified release

Run this example after the [checkout setup](../CONTRIBUTING.md#set-up-a-checkout).
It needs the core and development dependencies, with no credentials, sibling
checkout, private corpus, optional PDF libraries, or network access during the run.

```sh
demo_root=$(mktemp -d)
uv run --frozen python -m examples.offline_demo --output "$demo_root/run"
```

The command finishes with this summary:

```json
{"recordCounts": {"dispositions": 1, "failures": 0, "files": 1, "receipts": 2, "representations": 1, "segments": 1, "source-items": 1}, "verdict": "pass"}
```

## Inputs and processing

[`examples/offline/source.json`](../examples/offline/source.json) is one synthetic
record shaped like Federal Register metadata. Its rendition is
[`notice.html`](../examples/offline/notice.html), including non-ASCII text to
exercise UTF-8 evidence coordinates. The URL uses `example.invalid`; an explicit
local fetcher maps only that URL to the pinned file. It cannot fall back to HTTP.

The small `ExampleSource` adapter stands at the source-record interface. It pins
these local inputs; it does not claim they came from an official source-native
release. The real `FederalRegisterCatalogPolicy`, `SourceCatalogBuilder`, and
catalog verifier turn them into a sealed, selected catalog item.

The example then selects the six existing local profiles, creates a processing
plan, and calls `docspec.cli.execution.run_local`. This uses the same planning,
execution, checkpoint, delivery, and reconciliation code as the run commands,
with the local content fetcher explicitly injected and recorded. The plan runs
extraction and segmentation without an additional processor.

Finally, the existing `document-release commit` and `document-catalog open`
commands publish and verify the application release. Opening the catalog checks
the release and its dependencies; it does more than parse the reference JSON.
This demonstrates the [application release lifecycle](architecture.md#what-comes-out).
The separate portable-bundle builder has its own sealed fixture checks.

## Inspect the output

| File or directory inside `run/` | Meaning |
| --- | --- |
| `implementation.json` | Digests of the local code, schemas, profiles, example, and lockfile, including uncommitted edits; the example producer ID pins this manifest |
| `source-catalog/`, `source-catalog-reference.json` | Sealed catalog and its small immutable reference |
| `plan.json`, `run-request.json` | Selected profiles, processing limits, local roots, and execution settings |
| `blobStorage/`, `documentStores/`, `recordStorage/` | Captured source/representation bytes, job revisions, and immutable record layers |
| `run-reference.json`, `controlRepository/` | Reconciliation receipt reference and the control artifacts it names |
| `commit-request.json`, `commit-receipt.json`, `commit-result.json` | Exact publication request and CLI result/evidence |
| `release-reference.json`, `documentCatalog/` | Published application release reference and catalog |
| `verification.json` | Complete result of opening and verifying the published release, including counts and layer references |

Run `uv run --frozen pytest tests/test_offline_example.py` to exercise the same
argument-parsing entry point with socket connections forbidden. CI's default
suite includes this check.

## Repeat or make a small change

`--output` must name a new directory. Repeating the command with the same path
refuses before modifying the first result. Use another child of `demo_root` for
a second run. Output paths are part of local storage configuration, so different
directories may yield different plan and release identities even for identical
source text; the example does not promise identity equality across locations.

To practice a contribution, change a sentence in `examples/offline/notice.html`,
run the example into a new directory, and inspect the `files`, `representations`,
and `segments` layers named in `verification.json`. The captured digest and
evidence should follow the changed bytes. Restore the fixture when finished,
or include an intentional example change and its validation in your contribution.

The timestamps and source record are synthetic. The result proves this local
walkthrough worked; it is not upstream acquisition evidence, a scale campaign,
or a published package.
