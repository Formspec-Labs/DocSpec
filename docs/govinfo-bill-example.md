# Build and revise one bill experiment

This example turns one bill ID into a catalog, captures one **explicitly chosen
XML version**, and compares two phrase lists against the same retained bytes.
SpicyDocs owns publisher URLs and source validation. DocSpec owns the catalog,
document capture, processing, evidence, and reuse.

## Run the offline example

From this checkout, use the pinned provider wheel and a new absolute output path:

```sh
uv run --frozen \
  --find-links ./vendor --with './vendor/spicy_docs-0.6.0-py3-none-any.whl[acquisition]' \
  python -m examples.govinfo_bills \
  --package-id BILLS-119hr6028ih --output /tmp/my-bill-experiment
```

The authored [fixtures](../examples/bill_fixtures/README.md) use invented content
and real URL shapes. They offer two versions and five format URLs, with the newer
version first. The explicit `ih` choice captures the introduced version. Only
BILLSTATUS XML and the chosen bill XML are read; the HTML/PDF links stay metadata.

The first processor finds two occurrences of “public access.” The second adds
“machine readable” and finds three total occurrences. The second experiment runs
after the source client closes and creates zero new captures, representations,
or segments. Changing the phrase resource produces a distinct processor identity.

## Inspect the result

| File or directory | What it establishes |
| --- | --- |
| `source-evidence/bill-status.xml` | Exact BILLSTATUS response bytes, which describe the bill and offered versions. |
| `source-evidence/*acquisition.json`, `bill-text-*.json` | Observed URLs, HTTP status, media type, time, digest, bytes, request count, and effective limits. |
| `catalog-preview.json` | All parsed status fields and format URLs, plus the one chosen XML candidate. |
| `processed.json`, `reprocessed.json` | Normal DocSpec plans, run references, retained result references, and inspection summaries. |
| `bill-example-summary.json` | Selected package, captured digest, lexical matches, and verified upstream reuse. |
| `source-evidence/*refusal*`, `processed-failures.json` | Available bounded refused bytes and acquisition/run failures when a request fails. |

The workspace also retains its ordinary content-addressed blobs and records.
The bill XML stays byte-for-byte intact. The normalized text is a separate
representation; each segment points back to an enclosing span of original XML.
Phrase offsets refer to segment bytes, not exact offsets within XML markup.
The source version is the native BILLS package ID. Capture hashes remain evidence
pins; the fetcher leaves transport version absent instead of inventing one.

## Change the experiment

The three seams are small and explicit:

1. [Build supplied records](../examples/govinfo_bills.py) from `acquire_status`.
   Review every `text_versions` entry, then pass an offered package ID. Missing XML
   stops with an explanation and preserved status evidence.
2. [Inject `BillContentFetcher`](../examples/govinfo_bill_fetcher.py) through
   `content_fetcher`. Its configured identity includes the installed provider
   files, actual acquisition budget, status digest, and selected package.
3. Pass an injected processor to `prepare_local_experiment`. To process retained
   bytes again, provide the earlier `base_release` and changed processor. The
   example saves normal references; [Python runs](python-runs.md) describes the API.

`VisibleTextExtractor` is qualified here for a small classic bill XML fixture,
including inline markup, entities, Unicode, and source spans. It includes XML
metadata as text and applies the explicit `header → level 2` mapping. These are
document-wide lexical matches. It does not identify legal requirements, decide
applicability, validate a legislative DTD, or reconstruct legislative hierarchy.

## Make a bounded live observation

Add `--live` to use public GovInfo responses. Supply `--congress`, `--bill-type`,
`--number`, and the exact `--package-id` for another bill. Start with a small bill
whose BILLSTATUS explicitly offers that XML package. Each source call allows at
most two requests and 2 MiB; live request starts are at least one second apart.
Transport timeouts bound individual waits. DocSpec also applies the example's
document, memory, segment, duration, and processor limits.

Live output records only what was observed for that bill and version. A status
response is metadata, not proof of downloaded bill text or collection coverage.
Acquisition observation times are separate from caller-supplied run timestamps,
sampled before each run; those timestamps are not measured finish times.

The [qualification test](../tests/test_govinfo_bill_installed_wheel.py) installs
both packages outside their checkouts and forbids network connections during the
example. Its [manifest](../vendor/spicy_docs.json)
pins the provider wheel. The [shared example helper](../examples/provider_identity.py)
separately hashes installed `spicy_docs`
files; this is not a digest of every Python dependency. A missing installer wheel
hash remains `null`. SpicyDocs is optional for DocSpec; only this example needs
its `acquisition` extra. Source reading and bill acquisition use the same wheel.
