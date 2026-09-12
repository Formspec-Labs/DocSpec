# Capture, repair, and compare a small document experiment

This walkthrough builds a catalog of four synthetic documents, excludes one
from the run, retains an initial capture with a visible missing-file failure,
repairs that failure, and processes the captures with a pinned phrase vocabulary.
It then compares two processor alternatives without fetching or extracting the
documents again. It uses public Python APIs, local files, and no network.

After [checkout setup](../CONTRIBUTING.md#set-up-a-checkout), run:

```sh
uv run --frozen python -m examples.offline_demo --output /absolute/new-experiment
```

`--output` must name a new directory. Repeating it refuses before changing the
previous experiment. The final JSON reports four catalog items, three selected
documents, one exclusion, one initial failure, no remaining failure after repair,
and six processed segments. The original, case-sensitive, and updated-resource
attempts produce four, three, and five phrase matches respectively.

## What the experiment demonstrates

The [walkthrough code](../examples/offline_demo.py) uses
`SuppliedRecordSource` and `SuppliedRecordCatalogPolicy` to build its catalog through
`build_local_catalog`. Raw caller fields, source-qualified item IDs, candidate
byte sizes and hashes remain in the source evidence. All four catalog rows are
selected by the catalog policy; the processing plan explicitly excludes the
`out-of-scope` item. `catalog-preview.json` makes that distinction visible.

The first `prepare_local_experiment(..., stop_after="capture")` uses the real
`LocalFileContentFetcher`. Two files exist; the selected `late-arrival.txt` does
not. With one acquisition attempt and explicit acceptance of transient external
failures, the result retains two captures and a failed item. The example then
creates the missing file from its pinned fixture bytes and requests a successor
with `retryFailures: "transient"`. That run captures only the failed item. The
original result still contains its failure and remains independently inspectable.

Next, the example supplies `TextExtractor`, `ParagraphSegmenter`, and the
[example phrase matcher](../examples/phrase_match_processor.py). It processes
the three retained captures without fetching again. Reopening the saved handoff
reuses the completed run. Two later attempts start from the same processed base:
one changes case sensitivity, and the other changes the vocabulary bytes and
resource pin. Both keep the exact captured-file, representation and segment
identities. The updated vocabulary's output values also agree with a clean run.

`retain` keeps each immutable result available; these calls do not choose a
current catalog result. The example introduces no scheduler, run-control service,
or additional saved ledger. Dagster supplies scheduling and execution management
when needed; this script exercises DocSpec's catalog, evidence, reuse and result
interfaces through the existing direct local helper.

## Inspect the output

| File or directory | What to inspect |
| --- | --- |
| `experiment-summary.json` | Small outcome summary, match counts and verified comparisons |
| `catalog-preview.json` | All catalog rows and the explicit run exclusion |
| `initial-capture.json`, `repaired-capture.json` | Existing plan/run/release/handoff references and inspection results before and after repair |
| `processed.json`, `case-sensitive.json`, `resource-v2.json` | Retained processor alternatives, their exact settings and recorded work |
| `matches.json` | Literal quotes, segment byte offsets, resource pins and enclosing source evidence for each alternative |
| `comparisons.json` | Existing inspection comparisons showing configuration and result differences |
| `reference-inputs/` | The exact two vocabulary files used by the processor |
| `sourceContent/` | Three materialized local source files; the excluded file is never fetched |
| `sourceCatalog/`, `documentCatalog/`, `controlRepository/` | Existing catalog, retained results and their verification dependencies |
| `clean-comparison/` | Independent clean processing result used to check the updated vocabulary |
| `implementation.json` | Example and processor code digests plus the installed DocSpec version; it does not claim to hash the entire installed runtime |

The stage JSON files contain an ordinary serialized `ProcessingPlan`. For example,
reopen a result without constructing a processor or fetching anything:

```python
import json
from pathlib import Path
from rulespec_artifacts import Producer
from docspec.domain.plans import ProcessingPlan
from docspec.domain.references import DocumentReleaseRef
from docspec.runtime import open_local_inspection
from docspec.workspace import LocalWorkspace

root = Path("/absolute/new-experiment")
stage = json.loads((root / "resource-v2.json").read_text())
view = open_local_inspection(
    ProcessingPlan.from_dict(stage["plan"]), LocalWorkspace(root),
    document_release_producer=accepted_document_producer,
    release_ref=DocumentReleaseRef.from_dict(stage["release"]),
)
print(view.summary())
```

`accepted_document_producer` is your explicit accepted `Producer`; the inspection
factory does not infer acceptance from the result being opened. The walkthrough
declares its producer in its source code from `implementation.json` and the
ordinary document-release verifier identity. See [inspection](inspection.md) for
bounded source/record reads and comparisons, and [Python runs](python-runs.md) for
preparation and recovery without caller-written plan/request files.

## Meaning and evidence limits

The [phrase processor guide](phrase-matching-example.md) defines exact literal
matching, case and overlap rules. A match helps a reviewer find a passage. It is
not semantic classification, a conclusion that a requirement applies, or proof
that a legal reference was resolved. No matches means the requested literals
were absent under the chosen matching rules, not that the concept was absent.

Each quote has byte offsets within its segment. Its enclosing source evidence
links back to the captured file. The example checks the quote against stored
segment bytes and, for its plain-text inputs, checks that the segment equals
the captured source slice. It does not invent exact raw-source offsets for
transformed HTML or PDF text. The separate
[representation example](representations.md) demonstrates those choices.

The automated test forbids network access and observes actual fetch/stage calls.
The package test also runs the copied walkthrough against an isolated installed
wheel while preserving the detailed typed-runtime probe. These are local fixture
checks; they do not establish live provider coverage, a qualified external
reference resource, scale, or published-package status.

## Unfamiliar-contributor exercise

The manual contributor exercise remains separate from passing an automated
example. Follow the setup and walkthrough without implementation context, choose
a small documented contribution, and record the files consulted, outside help,
time spent, confusing steps and focused verification. A maintainer should resolve
the observed friction before closing that exercise.
