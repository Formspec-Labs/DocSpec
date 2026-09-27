# Regulations.gov attribute tables: a contract proposal

- Date: 2026-09-26. **Proposal** to spicy-docs, which owns the contracts, and
  to spicy-regs, which will publish the two families.
- Produced by: DocSpec `a93ecd4` (`lane/regulations-attributes`),
  [`tools/export_regulations_attributes.py`](../../tools/export_regulations_attributes.py).
- Evidence: `~/Work/corpora/regulations-attributes-20260926/` (`run.sh`, its
  `receipts/`, `logs/` and `members/`).

The thin spicy-regs `documents` (18 columns) and `dockets` (7) tables carry a
few of the attributes the Regulations.gov API returns. DocSpec's capture holds
all of them. Two tables carry the attributes the thin tables lack, one row per record:

| Table | Key | References | Rows | Columns |
| --- | --- | --- | ---: | ---: |
| `document_attributes` | `document_id`, spelled `value/1` | `documents.document_id` | 1,943,106 | key + 37 |
| `docket_attributes` | `docket_id`, spelled `value/1` | `dockets.docket_id` | 278,607 | key + 15 |

## Source

DocSpec's row-copied Regulations.gov catalogue: state `catalogue`, pin
`sha256:2200b5e68601decb8ac664d824d025c4e2031663c10bae900dcfcd20cbd74643`,
supplied 2026-09-02 and imported 2026-09-14 (workspace
`docspec-iceberg-reimport-2173b92-20260914`). Each member holds its API
record, and a document also holds its joined docket and Federal Register
records. A row reads the member's **own** record: the `sourceNativeFacts`
element whose `fields.data.id` is the member key. All 2,221,713 members have
exactly one, and its `data.type` and scope agree: 1,943,106 documents and
278,607 dockets.

## Column rules

- **Name:** the API attribute in snake_case (`docAbstract` → `doc_abstract`,
  `subType2` → `sub_type2`), with `_json` after a list. The rule is mechanical,
  so a new attribute has a predictable name.
- **Type:** VARCHAR throughout, following the published `federal_register`
  generation, so both families register as contract tables on the v1 path.
- **Scalars:** spelled by spicy-docs' `schemas.tables.text`. Strings stay as
  stated, including `""`, and booleans are `true` or `false`. Integers are
  decimal. Instants stay as the publisher's ISO 8601 UTC spelling.
- **Lists:** spelled by `json_column`: compact, keys sorted, non-ASCII escaped.
  NULL means the publisher stated null. `[]` means it stated an empty list.
- **Irregular or agency-specific data:** only `displayProperties`, kept whole
  as JSON. Each element's `name` is the attribute it relabels for that
  agency.

The exporter spells both natively in DuckDB. A value whose native spelling
could differ from the helpers' is refused rather than approximated: a double,
a nested list, or an object key outside `[A-Za-z0-9_]`. The capture holds
none of these.

### Columns I would type natively later

These carry the same information and would let consumers filter and sort
without parsing every query. DocSpec's typed layers already hold these types
(decision 0007).

| Proposed type | Columns | Evidence |
| --- | --- | --- |
| BOOLEAN | `allow_late_comments`, `open_for_comment`, `within_comment_period` | Every stated value is `true` or `false` |
| INTEGER | `page_count` | Always stated, 0–999,999 |
| TIMESTAMP (UTC) | documents' `author_date`, `effective_date`, `implementation_date`, `postmark_date`, `receive_date`; dockets' `effective_date` | All 2,935,804 stated values match `YYYY-MM-DDTHH:MM:SSZ`. Some years are publisher errors (`receive_date` up to 3019, `author_date` 4014), but they are still valid instants. |
| LIST<VARCHAR> | `authors_json`, `topics_json`, `keywords_json` | Each is an array of strings wherever stated |

`display_properties_json` stays JSON: it is a list of objects, and DocSpec's
table profile has no struct type.

## `document_attributes`

Counts are rows with a non-null value, and non-empty rows where some values are
`""` or `[]`. They come from the exported member and agree with the census of
the capture (`receipts/census.json`, `receipts/member-columns.json`).

| Column | Non-null | What the publisher states |
| --- | ---: | --- |
| `document_id` | 1,943,106 | The document's id; its API record is `https://api.regulations.gov/v4/documents/{document_id}` |
| `allow_late_comments` | 1,943,106 | Whether late comments are accepted; `true` on 432,301 |
| `author_date` | 674,860 | When the document was written (labelled “Author/ Document Date”) |
| `authors_json` | 217,308 | Its authors, people or organizations; almost all on Supporting & Related Material |
| `category` | 154,651 | The submitter's sector category; 38 values |
| `cfr_part` | 118,737 | The CFR parts affected, free text as stated |
| `comment` | 410,329 | The record's comment text, markup included; up to 900,294 bytes |
| `display_properties_json` | 1,461,445 | The agency's labels for this record's fields: an array of `{label, name, tooltip}` |
| `doc_abstract` | 511,572 | The abstract or summary |
| `effective_date` | 89,352 | When the action takes effect |
| `exhibit_location` | 2,393 | Where a physical exhibit is held |
| `exhibit_type` | 36,570 | The exhibit's type |
| `field1` | 5,797 | An agency-defined field (“Answer Date”, “XRIN”, “RTID”, …) |
| `field2` | 135,720 | An agency-defined field (“File Date” on all but one) |
| `fr_vol_num` | 120,435 | The Federal Register volume or citation, free text |
| `gov_agency` | 98,453 | The government body that submitted the document |
| `gov_agency_type` | 98,475 | Its level: Federal, State, Local, Tribal, Regional, U.S. House of Representatives or U.S. Senate |
| `implementation_date` | 199,148 | The implementation or service date |
| `legacy_id` | 134,235 | The id in a predecessor system (“Document Legacy ID”, “Legacy Exhibit Number”, …) |
| `media` | 383,076 | How the document arrived: `Electronic`, `Paper`, … (spellings vary) |
| `object_id` | 1,943,106 | The publisher's internal object handle |
| `omb_approval` | 8,067 | An OMB control number, as stated |
| `open_for_comment` | 1,943,106 | Whether the document was open for comment at capture (`true` on 461,158); a captured observation, not a live one |
| `organization` | 283,063 | The organization the author or submitter names |
| `original_document_id` | 533,772 (165,397 non-empty) | The id of the document this one derives from; `""` on 368,375 |
| `page_count` | 1,943,106 | Pages in the content file |
| `postmark_date` | 29,599 | The postmark date (often relabelled “Answer Date”) |
| `receive_date` | 1,928,713 | When the agency received it |
| `reg_writer_instruction` | 66,795 | Agency notes (labelled “Old Submitter” on 61,052) |
| `restrict_reason` | 20,248 | Why access is restricted, free text |
| `restrict_reason_type` | 75,033 | Copyrighted 53,580, Other 20,022, Confidential Business Information 1,179, Personally Identifiable Information 252 |
| `source_citation` | 71,150 | The source publication's citation |
| `start_end_page` | 287,972 | The Federal Register start and end pages, as stated |
| `subject` | 129,254 | The subject line |
| `subtype` | 1,330,483 | The agency's subtype: Correspondence, Report, Decision, … (823 values) |
| `topics_json` | 85,110 | The publisher's topics |
| `tracking_nbr` | 1,380,562 | The portal's tracking number |
| `within_comment_period` | 155,468 | Whether it arrived within the comment period (`true` on 10,842) |

## `docket_attributes`

| Column | Non-null | What the publisher states |
| --- | ---: | --- |
| `docket_id` | 278,607 | The docket's id; its API record is `https://api.regulations.gov/v4/dockets/{docket_id}` |
| `category` | 92,596 | The docket's status (“Disposition”): Pending 77,431, Closed 6,736, … |
| `display_properties_json` | 278,607 (213,748 non-empty) | The agency's labels for this docket's fields; `[]` on 64,859 |
| `effective_date` | 14,132 | Mostly the docket close date (“Docket Close Date” on 13,111) |
| `field1` | 199 | An agency-defined field (“Related Docket's RIN”, “Related To”) |
| `field2` | 60,527 | An agency-defined field (“Docket Status” on 59,872) |
| `generic` | 88,468 | An agency program code (“Docket Item Code”, “Location”, “Program Area”) |
| `keywords_json` | 86,576 | The docket's keywords |
| `legacy_id` | 7,931 | The id in a predecessor system |
| `object_id` | 278,607 | The publisher's internal object handle |
| `organization` | 7,789 | Labelled “Pre-EDOCKET ID” on 3,980 and “Organization” on 3,766 |
| `petition_nbr` | 132 | A petition number |
| `program` | 71,867 | The program office (“Center” on 64,910) |
| `short_title` | 42,081 | A short title (“Action Office” on 13,262) |
| `sub_type` | 111,474 | The agency's subtype; 411 values |
| `sub_type2` | 21,010 | A second subtype level |

Labels come from each record's `displayProperties`. They show that the same
attribute means different things at different agencies. A consumer should show
the record's label, not the column name, and should not build a global facet
from `field1`, `field2`, `generic`, `organization` or `program`.

## Left out on purpose

| Reason | Documents | Dockets |
| --- | --- | --- |
| The thin table carries it | `docketId`, `agencyId`, `title`, `documentType`, `postedDate`, `modifyDate`, `commentStartDate`, `commentEndDate`, `fileFormats`, `frDocNum`, `withdrawn`, `reasonWithdrawn`, `additionalRins` | `agencyId`, `title`, `docketType`, `modifyDate`, `dkAbstract`, `rin` |
| Never stated (0 non-null) | `email`, `phone`, `submitterRepAddress`, `submitterRepCityState` | — |
| Constant | `paperLength`, `paperWidth` (0 on every record) | — |
| Spells the key | `links.self` and `relationships.attachments` (the key in a fixed URL on every record) | `links.self` |
| Submitter contact details | `firstName` 313,819; `lastName` 307,131; `address1` 40,341; `address2` 10,730; `city` 133,496; `stateProvinceRegion` 142,556; `zip` 116,880; `country` 126,985; `fax` 54; `submitterRep` 101,418 | — |

The contact details are personal data about individual submitters, and no
consumer has asked for them. 299,605 of the records naming a first name are
typed Other. The capture keeps them. If a consumer needs them,
spicy-docs could add them as the `comments` contract adds `first_name` and
`last_name`.

Two attributes named in the search coverage notes, `numPages` and `citation`,
appear in no captured record. `page_count` and `fr_vol_num` /
`start_end_page` / `source_citation` are what the API states instead.

## The bootstrap members

`members/receipt.json` records the source above and each member's columns,
types, sha256, byte size and rows.

| Member | Rows | Bytes | sha256 | Row groups | Largest group, uncompressed |
| --- | ---: | ---: | --- | ---: | ---: |
| `document_attributes.parquet` | 1,943,106 | 72,439,721 | `7fc4b65da6f94e4f418166b4ea0a8851ec5429d3f249b48ac922dcb370bc8bc7` | 30 | 57.1 MiB |
| `docket_attributes.parquet` | 278,607 | 4,660,147 | `2c51004acd75de07e6489650d8238bcb50395f10920696b5c05008f99f4ffa57` | 4 | 8.0 MiB |

Each member is sorted by its key, zstd-compressed and carries no field IDs.
No row group exceeds 64 MiB uncompressed. Both are far below 1 GiB, so neither
needs an `agency_code` split. Document rows are skewed: `comment` reaches
900 KB. The exporter therefore sizes groups from the mean row width, then
checks the largest (57.1 of 64 MiB here) and halves the group size until it
fits. As spicy-regs measured, DuckDB ignores `ROW_GROUP_SIZE_BYTES` for
sorted output.

## Measurements

Each step ran in its own process, one at a time, under the PM01 watch wrapper
with a 12 GiB cap. The machine was a shared 14-CPU arm64 Mac at load 4–6,
running DuckDB 1.5.5. The workspace was a fresh APFS clone of the capture.

| Step | Wall | Peak RSS (tree) | Output |
| --- | ---: | ---: | --- |
| extract: own record per member, via `value_relation` | 86.3 s | 6.3 GiB | 376 MB Parquet |
| census: every attribute of every record | 30.4 s | 6.3 GiB | `receipts/census.json` |
| derive `docket_attributes` (`derive_table` 15.4 s) | 16.8 s | 1.1 GiB | +48.7 MB workspace |
| derive `document_attributes` (`derive_table` 74.3 s, 0.038 ms/row) | 75.5 s | 1.6 GiB | +417.8 MB workspace |
| export both members, with their verification | 6.8 s | 2.6 GiB | 77.1 MB |
| check: every row against the catalogue, in Python | 158.3 s | 2.7 GiB | `receipts/check.json` |

Each derive is one metadata unit. It is bound to the `catalogue` state, so
`generating_request` names that state as the layer's input. Its occurrences
follow the C29 rules: `value/1` over `member_key`, scoped by the definition.

## Checks

- **Member against layer:** equal row counts and equal multiset digests
  (count plus the sum of per-row hashes), for both tables.
- **Every row against the catalogue:** each record is decoded by Python's
  `json` through DocSpec's reader and spelled by spicy-docs' `text` and
  `json_column`. It is compared with the member row, both streamed in key
  order. No values differ: 0 of 73,838,028 for documents and 0 of 4,457,712
  for dockets.
- **Samples:** 21 keys per table: 10 by md5, 10 holding escaped JSON
  characters, and the widest row. 0 of 798 and 0 of 336 values differ.
- **References:** every attribute row has a parent in the published thin
  generations. Those are documents `7c98ef8c…` (member
  `sha256:1f70cc76…`, 2,002,887 rows) and dockets `e601dbf6…` (member
  `sha256:69ceb2d0…`, 279,429 rows). In the other direction, 59,781
  documents and 822 dockets have no attributes row. They were posted after the
  capture or are absent from it, and the delta job's first run fills them.
- **Determinism:** two runs from separate fresh clones gave byte-identical
  members and the same derived state IDs.
- **Tests:** `tests/test_export_regulations_attributes.py` compares the
  native spellings with spicy-docs' helpers. Its cases cover every control
  character, DEL, non-ASCII text, surrogate pairs and a literal `\u001F`. It
  checks the refusals, and it round-trips five real records. One of those
  carries `authors` holding control characters.

What a clean result could hide:

- **The column list:** both sides of every comparison share it. They cannot
  show a wrongly chosen or wrongly named attribute; review of this document
  does that.
- **Staleness:** the comparison is against the 2026-09-02 capture, not the live
  API.
- **Determinism beyond one setup:** it is shown on one machine with one DuckDB
  version.
- **DEL:** a draft that left DEL unescaped matched everywhere on real data. The
  unit test caught it, because Python's `ensure_ascii` escapes DEL
  (`\u007f`).

## For the delta job

Applying the same helpers (`text` per scalar, `json_column` per list) to
`data.attributes` of an API response reproduces these cells exactly. This
was measured over the whole capture. An unchanged record therefore keeps its
row digest, and so its DocSpec occurrence, when a later generation is admitted
(decision 0007, item 4). If a record states an attribute that this contract
does not name, the job should report it rather than drop it silently.

## Questions for spicy-docs

1. Should submitter contact details stay out, as proposed?
2. `object_id` is kept under the inclusion rule, but no consumer has named
   it. Keep it or drop it?
3. The API URL spells the key. This proposal states the rule in the key's
   description instead of adding a column.
4. When should the native types above replace VARCHAR, and should the thin
   tables' dates follow at the same time?
