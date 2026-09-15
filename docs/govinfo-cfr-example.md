# Build and revise one annual CFR experiment

This example turns annual CFR metadata into a catalog for **one explicitly chosen
section**, fetches its offered XML, and compares two phrase lists against the
same retained bytes. SpicyDocs maps publisher metadata and validates acquisition;
DocSpec selects the catalog item, retains the work, and processes it again.

## Run the offline example

Use Docker for the local storage catalog and a new output directory inside this
checkout. Publisher responses come from retained fixtures.

```sh
uv run --frozen \
  --find-links ./vendor --with './vendor/spicy_docs-0.15.0-py3-none-any.whl[acquisition]' \
  python tools/with_iceberg.py python -m examples.govinfo_cfr \
  --year 2025 --title 1 --volume 1 --section 18.1 --output "$PWD/my-cfr-experiment"
```

The [authored fixtures](../examples/cfr_fixtures/README.md) offer two sections
and three format links. Only the selected section becomes a catalog item and
document candidate. Its metadata retains the **complete mapped MODS package**,
including both constituents, PDF links, unknown fields, attributes, and original
element positions. The catalog does not claim whole-title or collection coverage.

The first processor finds two occurrences of “public access.” The second adds
“machine readable” and finds three total occurrences. The second run starts
after the source client closes. It creates zero new captures, representations,
or segments; only processor work changes.

## Inspect the evidence

| Output | What it establishes |
| --- | --- |
| `source-evidence/cfr-mods.xml` | Exact metadata response bytes. |
| `source-evidence/cfr-mods-acquisition.json` | Metadata URL, status, media type, observation time, byte/hash pins, and request limits. |
| `catalog-preview.json` | Complete SpicyDocs MODS mapping, typed edition facts, explicit selection, and paths to the chosen constituent and URL. |
| `source-evidence/cfr-text-*.json` | Annual selection, body identity, metadata pin, capture facts, and effective limits. |
| `processed.json`, `reprocessed.json` | Core selected state and document result records. |
| `cfr-example-summary.json` | Captured digest, phrase results, and checked upstream reuse. |
| `source-evidence/*refusal*`, `processed-failures.json` | Selection/capture failures and available refused response bytes. |

The workspace also keeps ordinary content-addressed blobs and records. Metadata
links establish offered locations; only the successful selected capture
establishes document bytes. MODS element paths count original element children
from one. Text segments point to enclosing byte spans in the original body XML.
Phrase offsets refer to normalized segment bytes, not exact XML byte offsets.

The source version is the publisher's annual-section `accessId`. Capture hashes
stay in acquisition evidence; no transport version is inferred from those hashes.

## Keep selection and processing explicit

[The fetcher](../examples/govinfo_cfr_fetcher.py) requires one matching constituent
`accessId` and one publisher-stated XML URL for the selected annual section.
Missing, duplicate, differently dated, or eCFR-only offers refuse selection.
SpicyDocs then checks the requested annual URL and native title, volume, and
section in the response. HTML and another edition are not fallbacks.

Edition dates and body dates remain separate. The authored 2025 cover-only
edition deliberately points to body XML stating 2023. Its native `isCoverOnly`
flag supplies the edition type; the date difference does not imply an incomplete
body. The example retains both observations without inferring amendments.

`VisibleTextExtractor` uses `SUBJECT → level 2` headings for annual XML.
It includes XML metadata as text, so phrase results are **document-wide lexical
matches**. Tests cover entities, inline markup, Unicode, and replayable source
spans in both the authored fixture and a retained real annual CFR granule.
It does not interpret law, assess applicability, validate a DTD, or reconstruct
CFR hierarchy. RefSpec's eCFR reader is not used for annual XML.

## Make a bounded live observation

Add `--live` and explicitly choose `--year`, `--title`, `--volume`, and `--section`.
The example requests volume MODS and one offered annual section XML. Each call
allows one request and 2 MiB; request starts are at least one second apart.
The supplied record is bounded to 16 MiB and catalog scratch space to 64 MiB.
DocSpec separately limits captured bytes, processing work, and elapsed runtime.
Larger metadata or sections require an explicit code/configuration change.

Run timestamps are sampled before each run; they are not measured finish times.
Acquisition observation times remain separate. A live success covers only the
requested volume metadata and section at their observation times.

The [installed-wheel test](../tests/test_provider_examples_installed_wheel.py) runs the
[same cases](../tests/test_govinfo_cfr_example.py) outside both checkouts and forbids
network connections during them. The [provider manifest](../vendor/spicy_docs.json)
pins the wheel; [provider identity](../examples/provider_identity.py) also hashes
installed package files. SpicyDocs remains optional for DocSpec; this example
selects its acquisition extra explicitly.
