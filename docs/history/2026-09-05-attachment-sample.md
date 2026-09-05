# The 2,000-document attachment sample, and what it is for

2026-09-05. Selection: `2026-09-05-attachment-sample-selection.json`,
`sha256:a13e74aad2c350fc290e34e3bffecb153ac008eb9199a4abec08570ac25d3306`.
Tools: `tools/select_attachment_sample.py`, `tools/fetch_attachment_sample.py`.

## The campaign is already sized. This does not re-ask that.

**712,350 of 865,082 recoverable (82%), 95% band 657,099–750,366.** From
`~/Work/corpora/supply-2026-09-02/receipts/regs-document-attachments-2026-09-04.md`:
306 documents probed at `?include=attachments` — the exact shape that populates
`included` — 306 of 306 returning cleanly, drawn from two `sha256 mod 64`
catalog partitions. That frame is a pseudorandom 1/64 sample, and it reproduced
the full-corpus rates on three independent figures (Other 96.2% vs 96.2%,
Supporting 25.1% vs 25%, overall 38.8% vs 39%) before being used. The estimate
was then re-derived under a different decomposition — splitting restricted from
unrestricted — and came back 712,318 against 712,350.

**A correction to my own record.** I wrote in commit `91b78a7` that the
campaign's "yield is unmeasured", and told the overseer session it was unsized,
citing `unavailable-coverage-2026-09-04.md`. That receipt does withdraw an
estimate — the *earlier* ~720,000 one, which rested on a 35-document web-search
sample and on the attachments *relationship*, a stub present on 100% of all
documents and therefore carrying no information. It does not touch the 306-probe
figure, which was recorded later the same day and answers the question the
coverage receipt said was open. Two estimates of similar magnitude, one
withdrawn and one standing; I applied the first's withdrawal to the second
without checking which it named. Caught by a blind review, confirmed here
against both receipts.

**Filed as a fourth instance of the pattern in decision 0006**, and a new shape
of it: not a check that agrees with itself, but a *retraction* aimed at the
wrong target. A withdrawal is as load-bearing as a claim and needs the same
question asked of it — of which measurement, exactly?

## What the sample adds

| | before | after this sample |
| --- | --- | --- |
| `Other` unrestricted, 694,914 docs | 93.3% on n=120, ±32,000 docs | n=1,050, roughly ±11,000 |
| `S&RM` unrestricted, 93,179 docs | 69.2% on n=65, ±10,000 docs | n=450, roughly ±4,000 |
| per-agency rates | none | 48 and 69 agencies in the two main cells |
| throughput at a registered key | **never measured** | measured at concurrency 1 |
| format and no-file shapes | unknown | already found, see below |

The throughput row is the one the 306-probe explicitly disclaims: it ran through
Zyte on `DEMO_KEY` with no rate limiting, and its own receipt says reading that
as a rate would be an error.

## Why the cells are what they are

A census of the frame, 2026-09-05, over all 865,206 unavailable rows:

| documentType | unrestricted | restricted | total |
| --- | --- | --- | --- |
| Other | 694,914 | 299 | 695,213 |
| Supporting & Related Material | 93,179 | 74,578 | 167,757 |
| Notice | 1,004 | 79 | 1,083 |
| Proposed Rule | 410 | 11 | 421 |
| Rule | 368 | 9 | 377 |
| Public Submission | 355 | 0 | 355 |
| **total** | **790,230** | **74,976** | **865,206** |

Two facts set the design. Nearly every restricted document is Supporting &
Related Material — 74,578 of 74,976 — so the withholding axis and the document
type axis are almost the same axis. And `Other`, two thirds of the population,
is 99.96% unrestricted, so the corpus estimate is dominated by one cell whose
rate rests on 120 probes.

**An earlier draw of this sample spent 1,000 of 2,000 rows on the restricted
strata.** Their file rate is already measured at 0 of 45 Copyrighted and 0 of 3
CBI; a thousand more rows there would have re-confirmed a zero and bought
roughly nothing. Corrected after review: 1,650 rows now sit in the unrestricted
cells where the band is loose, and 350 remain spread across the four reason
codes — enough to bound each one's zero at about 2–6%.

| cell | population | drawn | design weight |
| --- | --- | --- | --- |
| (none) · Other | 694,914 | 1,050 | 661.82 |
| (none) · Supporting & Related Material | 93,179 | 450 | 207.06 |
| (none) · minor types | 2,137 | 150 | 14.25 |
| Copyrighted | 53,580 | 120 | 446.50 |
| Other (reason) | 19,965 | 120 | 166.38 |
| Confidential Business Information | 1,179 | 60 | 19.65 |
| Personally Identifiable Information | 252 | 50 | 5.04 |

Allocation is deliberately not proportional, so **the raw sample fraction is not
a corpus rate**. Every row carries its `designWeight`; the estimator is
`sum(w · indicator) / sum(w)`.

## Already found, before the run

Ten documents were fetched live on 2026-09-05 to check the response shape before
2,000 rows were read through the parser. Three findings:

- **`REGULATIONS_GOV_API_KEY` in `RefSpec/.env` is rejected** — 41 characters,
  `API_KEY_INVALID`, 403 on all ten. `API_GOV` (40 characters, the api.data.gov
  length) works.
- **One attachment is often several formats.** `DOT-OST-1996-1116-0017` ships
  one attachment as `.tif` (449,805 B) and `.pdf` (156,456 B) of the same pages.
  Counting `fileFormats` entries called that two attachments and double-counted
  the content.
- **An attachment can carry no file at all.** `FDA-1987-N-0054-0051` has one in
  both the linkage and `included`, with `fileFormats` empty — withheld content,
  which is a different answer from no attachment.

Neither of the last two appears in the API documentation and neither raises an
error; both would have produced a clean, confident, wrong number.

## Operational

`api.data.gov` meters **govinfo and regulations.gov against one hourly quota**,
so this sample and any concurrent govinfo census draw from the same 1,000/hour.
They run one after the other, not together. At 3.7 s spacing the sample is about
2.1 hours and is resumable by document id.
