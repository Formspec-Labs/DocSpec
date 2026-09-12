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

The example selects the six installed local profiles through
`ProfileRegistry.builtin().local_profiles()`, creates a processing plan, and calls
`docspec.runtime.prepare_local_run(...).run()`. This uses the same planning,
execution, checkpoint, delivery, and reconciliation code as the run commands,
with the local content fetcher explicitly injected and recorded. The plan runs
extraction and segmentation without an additional processor.

The example also writes plan and run-request files for its later CLI commit
step and for inspection. The Python execution API does not require those files;
see [Python runs](python-runs.md) for direct preparation and recovery.

Finally, the existing `document-release commit` and `document-catalog open`
commands publish and verify the application release. Opening the catalog checks
the release and its dependencies; it does more than parse the reference JSON.
This demonstrates the [application release lifecycle](architecture.md#what-comes-out).
The separate portable-bundle builder has its own sealed fixture checks.

## One workspace for local execution

The example uses a `docspec-local-run-request` at version `2.0`. Its `workspace`
is the output directory; storage defaults to named children such as
`blobStorage`, `documentStores`, and `controlRepository`. Only the catalog and
read-only example input locations need `roots` overrides. An omitted
`profileDirectory` uses the descriptions shipped in the installed wheel.

Python callers can inspect the same paths without creating them:

```python
from pathlib import Path
from docspec.workspace import LocalWorkspace

workspace = LocalWorkspace(Path("/absolute/path/to/experiment"))
print(workspace.roots["documentStores"])
```

`roots` accepts any subset of the eight known storage names with absolute paths;
unknown names and relative paths refuse before planning. `profileDirectory`
remains an explicit override for a custom profile set. The plan still pins and
checks complete profile descriptions, including their resource limits.
`docspec profile list` shows installed profiles without an extra path argument.

Local execution defaults to one worker and the same number of in-flight tasks;
`maxWorkers` and `maxInFlight` can be overridden. The deadline, processing plan,
retry/failure policy, and producer/verifier settings remain explicit. Defaults
do not infer acceptance from supplied artifact metadata. Worker and execution
records retain the resolved roots, profile pins, and settings for inspection.
Version `1.0` run requests are no longer accepted; update current callers directly.

An injected fetcher used by local runs must declare a nonempty `downloader_id`
and a SHA-256 `configuration_digest`. Its acquisition metadata should describe
that same implementation and configuration. Preparation and saved-task recovery
use one worker description containing the effective roots, fetcher, policies,
accepted producers, sink, partition settings, and fixed evidence timestamp.
Recovery refuses changed settings before fetching. Saved worker descriptions
now use version `2.0`; older prepared workers must be prepared again.
The shared runtime also pins `docspec.runtime.local-worker/v1` as its worker
implementation. Reprepare handoffs created by the former CLI worker; retained
plans and results keep their existing references.

A workspace supplies locations only. Runs use the existing plan and ledger
identities. The Python runtime also supports [capture-first runs](python-runs.md#capture-first-and-process-later);
this walkthrough runs all configured stages together. Broader lifecycle
inspection and simpler plan construction remain checklist D04.

## Inspect the output

| File or directory inside `run/` | Meaning |
| --- | --- |
| `implementation.json` | Digests of the local code, schemas, profiles, example, and lockfile, including uncommitted edits; the example producer ID pins this manifest |
| `source-catalog/`, `source-catalog-reference.json` | Sealed catalog and its small immutable reference |
| `plan.json`, `run-request.json` | Selected profile pins, processing limits, workspace/overrides, and execution settings |
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

## Unfamiliar-contributor exercise

Checklist E5 tests whether these instructions work for someone who has not read
the implementation or the refactor conversation. The exercise is still pending.

1. Record the checkout commit and start time, then follow the setup and walkthrough
   using the repository documentation. Record any outside help you need.
2. Choose one small behavior change from the contributor task map. Find its code,
   governing rule, and focused tests; explain the intended result before editing.
3. Make the change, run the focused checks, and review the diff. Record the commands
   and outcomes, including any failure that required another file or instruction.
4. Report elapsed time, files consulted, confusing steps, and whether the change
   worked without private context. Include the diff or commit so a reviewer can
   verify the result.

A maintainer records the participant's evidence and resolves the observed
friction before closing E5. Passing the automated example alone does not finish
this exercise.

The timestamps and source record are synthetic. The result proves this local
walkthrough worked; it is not upstream acquisition evidence, a scale campaign,
or a published package.
