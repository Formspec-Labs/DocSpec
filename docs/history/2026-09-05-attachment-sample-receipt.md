# The 2,000-document attachment sample

Run 2026-09-05T16:47:40Z to 2026-09-05T19:32:31Z. Selection `a13e74aad2c350fc…`, key `API_GOV`, 3.7s API spacing, direct arm on.

**The run crashed after the main pass** on an unhandled `RemoteDisconnected` (fixed in `d8436c9`). All 2,000 documents completed first; the negative re-probe was finished separately with `--reprobe-only`. Nothing was refetched.

User-Agent, on which the whole direct arm depends: `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36`

## 1. Recoverable fraction

| cell | drawn | with files | rate | 95% interval | design weight |
| --- | ---: | ---: | ---: | --- | ---: |
| (none) | Other | 1050 | 963 | 91.7% | 89.9%–93.2% | 661.82 |
| (none) | Supporting & Related Material | 450 | 294 | 65.3% | 60.8%–69.6% | 207.06 |
| Copyrighted | any | 120 | 2 | 1.7% | 0.5%–5.9% | 446.50 |
| Other | any | 120 | 6 | 5.0% | 2.3%–10.5% | 166.38 |
| (none) | minor | 150 | 54 | 36.0% | 28.8%–43.9% | 14.25 |
| Confidential Business Information | any | 60 | 1 | 1.7% | 0.3%–8.9% | 19.65 |
| Personally Identifiable Information | any | 50 | 4 | 8.0% | 3.2%–18.8% | 5.04 |
| **weighted corpus estimate** | 2,000 | 1324 raw | **81.0%** | | |

Raw sample rate is 66.2%, and it is **not** the corpus rate: the allocation over-samples the withheld cells by design. The weighted estimator `sum(w·indicator)/sum(w)` gives **81.0%**.

The standing corpus figure from the 306-document probe is 82% (657,099–750,366 of 865,082). This sample, drawn independently on a different frame with a different instrument, lands at 81.0%.

## 2. Bytes

- documents with at least one file: **1,324** of 2,000
- declared bytes, all formats: **4.48 GB**, median 266,752, mean 3,383,714
- actually downloaded: **4.47 GB** across 1,324 documents
- declared equals actual: **1,324**; differs: **0**
- download errors: **0**
- magic bytes contradicted the extension: **3** of 2,736 files

  All three are named `.xlsx` and begin `d0cf11e0` — the OLE2 signature of a legacy `.xls`, not the ZIP of an `.xlsx`. The bytes are real and the extension is wrong, which is what a magic check is for: a consumer trusting the name would fail to open them. `BIS-2018-0006-168736`, `BIS-2018-0006-49077`, `BIS-2018-0006-21836`.

Weighted mean bytes per unavailable document: **1,614,737**. Over the 790,230 unrestricted unavailable documents that projects to **1.28 TB** of declared content.

## 3. Rate limit and throughput

- API arm: 2,000 requests in 2.75 h = **728/hour** at concurrency 1
- declared limit: {'1000': 2000}; `X-RateLimit-Remaining` fell to a minimum of **258**
- **429s: 0**; non-200: 4 (all 404, documents the publisher no longer serves)
- direct arm: **34,410 requests**, 3.48/s sustained, **0 client-rejected**, 1 unreadable

The direct arm's rate is bounded by the API arm's pacing, not by the host: it rides inside the 3.7 s gap. It cost nothing from the api.data.gov budget.

## 4. Are any withheld strata empty of attachments?

| reason code | drawn | with files | 95% upper bound |
| --- | ---: | ---: | ---: |
| Confidential Business Information | 60 | 1 | 8.9% |
| Copyrighted | 120 | 2 | 5.9% |
| Other | 120 | 6 | 10.5% |
| Personally Identifiable Information | 50 | 4 | 18.8% |

## Declared versus served, both directions

- API declared a file the host would not serve: **1** documents
- host served a file the API never declared: **0** documents
- disagreed on the URL set: **1**

That is what licenses sizing the campaign's byte cost from declared metadata instead of downloading it: the two sources of truth agree on what exists.

## Shapes

- attachments with no file at all: **113** across 74 documents
- documents whose attachment ships in several formats: **311**
- total attachments 2,509, renditions 2,745 (1.094 per attachment)
- documents carrying inline comment text: **680**

## The negative re-probe

Every one of the **676** negatives was probed a second time after a pause, and **0** changed. A transient failure and a genuine absence look identical in one observation; they rarely agree twice. Zero flips means the negatives are absences, not failures.
