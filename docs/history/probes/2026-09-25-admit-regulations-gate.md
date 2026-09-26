# C27 on Regulations.gov: admit the live dockets and documents families over HTTPS

**Passed.** `CoreWorkspace.admit_generation` admitted the live spicy-regs
`dockets` generation (279,380 rows) in **3.9 s at 0.46 GiB** and `documents`
(2,002,831 rows) in **19.7 s at 1.09 GiB**, each over HTTPS from the public
publication into its own dataset. Both equal the retained 2026-09-14 catalogue
on every key it holds, beyond rows the publisher modified since: each of the
1,785 docket and 1,059 document exceptions carries a later `modify_date`. The
Python oracle agrees on all 2,282,211 rows, and re-admitting both pins returns
the same states without a byte or ledger row more.

The [harness](2026-09-25-admit-regulations-gate.py) and
[receipt](2026-09-25-admit-regulations-gate.json) record every number below;
per-step receipts, logs and RSS samples are in
`~/Work/corpora/c27-gate-20260925/regulations-gov/receipts/`. Code: DocSpec
`0068bf7` (main, vendoring spicy-docs 0.34.1), DuckDB 1.5.5, PyArrow 25.0.1,
Python 3.12.9, one DuckDB thread and a 6 GiB engine limit, on a 14-CPU arm64 Mac
at load 7–10. Each step ran once, in its own process, under the PM01 watch
wrapper with a 12 GiB cap.

## Fixture

| Input | What | Rows |
| --- | --- | ---: |
| `dockets` | publication run 36207823917 at spicy-regs `1d283d5`: `sha256:69afafdc…`, member `308b35c6…`, 11.6 MB | 279,380 |
| `documents` | the same run: `sha256:f0796ed9…`, member `d7487819…`, 76.9 MB | 2,002,831 |
| retained `catalogue` (pin `sha256:2200b5e6…`) | reimported on 2026-09-14 from the 2026-09-02 supply; each item's own raw record decoded through DocSpec's JSON decoder and projected with spicy-docs' public-table profiles | 1,943,106 documents, 278,607 dockets |

Neither family declares identity fields. spicy-docs 0.34.1's table contracts key
`dockets` on `docket_id` and `documents` on `document_id` (spelling `value/1`);
the previously vendored 0.26.6 has neither contract, and both admissions refused
with `IntegrityError: table has no declared identity fields` after downloading
and admitting the family (`receipts/blocked-0.26.6/`). Neither table carries an
R5 observation-time column.

## Admission

| Generation | Seconds | Peak RSS | Downloaded | Member | Membership | Index | Ledger records |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| dockets | 3.86 | 0.46 GiB | 11,744,952 | 11,632,733 | 12,285,643 | 19,712,955 | +9 (114,688 B, new file) |
| documents | 19.70 | 1.09 GiB | 77,049,404 | 76,940,085 | 86,984,016 | 143,774,937 | +9 (+40,960 B) |
| FR prior, for comparison | 14.75 | 0.95 GiB | local | 155,941,669 | 46,197,560 | 73,210,953 | +9 |

Seconds are the `admit_generation` call, including the download; each process
took 4.9 and 20.6 s. Downloaded bytes are the pointer, root, member manifest and
member, whose sizes staging checks against their descriptors. Each admission
adds 3 units, 9 records and 9 links. Per row, documents cost 9.8 µs against
FR's 14.6 µs, with a narrower row; membership is 43.4 B and the index 71.8 B per
row, as FR's 72.6 B.

## Checks, in a fresh process

| Check | dockets | documents |
| --- | --- | --- |
| Admitted count equals `recordCount` | 279,380 | 2,002,831 |
| Rules equal those built from the member's pyarrow schema | yes | yes |
| Python oracle, key and occurrence of every row | 0 mismatches | 0 mismatches |
| Catalogue keys compared (all of them) | 278,607 | 1,943,106 |
| Catalogue-only keys | 0 | 0 |
| Generation-only keys | 773 | 59,725 |
| Two-way `EXCEPT` over the shared contract columns | 1,785 each way | 1,059 each way |
| Exceptions whose generation `modify_date` is later | 1,785 | 1,059 |

The comparison read each state through `CoreStateReader.table()`, took 71 s and
peaked at 3.4 GiB. The shared columns are the public-table profile's: 7 for
dockets and 15 for documents. `pdf_extraction_results_json` is generation-only,
and `text_content` and `text_extraction_status` are left out: spicy-regs fills
them from its own PDF extraction, never from the API record the catalogue holds.
They are NULL on every generation row, so including them changes nothing.

## Adjudications

- **Every exception is a later publisher update.** By key, each differing row's
  `modify_date` in the generation is later than the catalogue's; 0 rows differ
  otherwise. Beside `modify_date`, dockets differ in `title` (290), `abstract`
  (77), `rin` (2) and `docket_type` (1); documents in `attachments_json` (112),
  `file_url` (93), `title` (62), `fr_doc_num` (23), the comment dates (20–21),
  `posted_date` (13), `withdrawn` (9), `reason_withdrawn` (8), `agency_code`
  (2), `docket_id` (1) and `document_type` (1). No difference is a type or JSON
  key-order artifact.
- **Generation-only keys.** All 773 dockets were modified on or after the
  2026-09-02 supply. Of 59,725 documents, 5,019 were posted or modified since
  then; the other 54,706 are older records, mostly OSHA (30,821) and EPA
  (17,554), that the catalogue's supply did not hold, 54,667 of them without a
  `withdrawn` value. They are the producer's wider coverage, not admission's.

## Retry and the next generation

Re-admitting both pins returned the same states and reports in 1.1 and 4.8 s
(each still downloads and admits the family before finding its state); no record
file, ledger row or ledger byte changed. `publication.json` named the same pins
at 02:04 UTC, before the 06:25 UTC sweep, so no next generation was admitted and
`changes` against a direct comparison waits for one (`changes` in the harness).

## Reproduce

From `~/Work/corpora/c27-gate-20260925/regulations-gov`, each under
`pm01-gate-2026-09-23/tools/watch.sh LOG 12 --`:

```sh
WT=~/Work/spicy-stack-worktrees/docspec-admission; G=$WT/docs/history/probes/2026-09-25-admit-regulations-gate.py
R="uv run --frozen --project $WT --extra dagster --extra s3 --extra http python $G"
$R reference; $R pointer first; $R admit first dockets; $R admit first documents
$R compare first; $R retry first
# after a sweep publishes new pins: $R pointer next; $R admit next dockets; ...; $R changes first next documents
```
