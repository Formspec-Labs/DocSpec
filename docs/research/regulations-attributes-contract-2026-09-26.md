# Regulations.gov attribute tables: a contract proposal

- Date: 2026-09-26. **Proposal** to spicy-docs, which owns the contracts, and
  to spicy-regs, which will publish the two families.
- Status: spicy-docs carries the contracts on `typed-contracts` (`7e5d19b`,
  unreleased), with these columns and types. Two owner rulings, spicy-regs
  decisions 66 and 67, replaced this proposal's first defaults: the submitter
  contact fields are published, and the columns are typed natively.
- Produced by: DocSpec `5435912` (`lane/regulations-attributes`),
  [`tools/export_regulations_attributes.py`](../../tools/export_regulations_attributes.py).
  `e3ceeea` then narrowed instants to whole seconds, which leaves every
  exported byte unchanged. The untyped first export (`a93ecd4`) is superseded.
- Evidence: `~/Work/corpora/regulations-attributes-20260926/`:
  - `run-v2.sh`, with its `receipts-v2a/` and `logs-v2a/`;
  - `members-v2/`, the bootstrap members;
  - `members-v2-rerun/` and `receipts-v2-whole-seconds/`, the determinism
    evidence;
  - `proof/`, the reference proof.

The thin spicy-regs `documents` (18 columns) and `dockets` (7) tables carry a
few of the attributes the Regulations.gov API returns. DocSpec's capture holds
all of them. Two tables carry the attributes the thin tables lack, one row per
record:

| Table | Key | References | Rows | Columns |
| --- | --- | --- | ---: | ---: |
| `document_attributes` | `document_id`, spelled `value/1` | `documents.document_id` | 1,943,106 | key + 47 |
| `docket_attributes` | `docket_id`, spelled `value/1` | `dockets.docket_id` | 278,607 | key + 15 |

A document's RIN, related documents and related text come from its joined
Federal Register record through `documents.fr_doc_num`, not from this table.

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

## Two producers, one spelling

spicy-regs will likely build both families from the Mirrulations mirror of full
API records, as their sole producer. These bootstrap members then become the
cross-check over the records both hold. Both producers must spell every cell
the same way. The reference is spicy-docs' `project_document_attributes` and
`project_docket_attributes`, which take a record's `data.id` and
`data.attributes` and return a checked typed row. The exporter's native typing
is proven against them on every row (see “Checks”).

## Column rules

- **Name:** the API attribute in snake_case (`docAbstract` → `doc_abstract`,
  `subType2` → `sub_type2`). `displayProperties` becomes
  `display_properties_json`. The rule is mechanical, so a new attribute has a
  predictable name.
- **VARCHAR:** a string as stated, including `""`. It is the default type.
- **BOOLEAN:** `allow_late_comments`, `open_for_comment` and
  `within_comment_period`, as stated.
- **INTEGER:** `page_count`, as stated. Its range is 0–999,999.
- **TIMESTAMPTZ:** UTC, stored as microseconds. It covers documents'
  `author_date`, `effective_date`, `implementation_date`, `postmark_date` and
  `receive_date`, and dockets' `effective_date`. As the reference requires, a
  stated instant must be spelled exactly `YYYY-MM-DDTHH:MM:SSZ`; any other
  spelling refuses, fractional seconds included. All 2,935,804 stated values
  are spelled that way. Some years are publisher errors
  (`receive_date` up to 3019, `author_date` 4014), but they are still valid
  instants.
- **VARCHAR[]:** `authors`, `topics` and `keywords`, in the publisher's order.
  Every element is a non-empty string: 275,520, 480,984 and 619,432 of them.
- **JSON text:** `display_properties_json` stays VARCHAR, spelled by spicy-docs'
  `json_column`. It is an array of `{label, name, tooltip}` whose `name` is
  the attribute it relabels for that agency. It is NULL when nothing is stated
  and `[]` when the publisher states an empty list.

The exporter types each record natively. It reads the attributes once into a
typed struct, and once more for their JSON types, which the struct's casts
would otherwise hide. It refuses any value its type does not describe exactly
rather than cast it:

- a non-string in a VARCHAR;
- a non-boolean flag;
- a `page_count` that is not a 32-bit integer;
- an instant not spelled exactly `YYYY-MM-DDTHH:MM:SSZ`;
- a list holding anything but strings;
- display properties outside the printable-ASCII `{label, name, tooltip}`
  shape. That shape is already `json_column`'s spelling.

The capture holds none of these. Where the reference refuses, the exporter
refuses too, and the exporter never spells a value differently. It is stricter
in two places, neither of which the capture exercises:

- a non-string in a VARCHAR, which the reference spells with `text()`;
- display properties outside the printable-ASCII `{label, name, tooltip}`
  shape, which the reference passes to `json_column`.

## `document_attributes`

Counts are rows with a non-null value (and non-empty rows, where some values
are `""`), from the exported member and the census of the capture
(`receipts-v2a/census.json`). Every column not typed here is VARCHAR.

| Column | Type | Non-null | What the publisher states |
| --- | --- | ---: | --- |
| `document_id` | VARCHAR | 1,943,106 | The document's id; its API record is `https://api.regulations.gov/v4/documents/{document_id}` |
| `address1` | | 40,341 | The submitter's street address, first line |
| `address2` | | 10,730 | The submitter's street address, second line |
| `allow_late_comments` | BOOLEAN | 1,943,106 | Whether late comments are accepted; true on 432,301 |
| `author_date` | TIMESTAMPTZ | 674,860 | When the document was written (“Author/ Document Date”) |
| `authors` | VARCHAR[] | 217,308 | Its authors, people or organizations; almost all on Supporting & Related Material |
| `category` | | 154,651 | The submitter's sector category; 38 values |
| `cfr_part` | | 118,737 | The CFR parts affected, free text as stated |
| `city` | | 133,496 | The submitter's city |
| `comment` | | 410,329 | The record's comment text, markup included; up to 900,294 bytes |
| `country` | | 126,985 | The submitter's country |
| `display_properties_json` | | 1,461,445 | The agency's labels for this record's fields, `json_column` text |
| `doc_abstract` | | 511,572 | The abstract or summary |
| `effective_date` | TIMESTAMPTZ | 89,352 | When the action takes effect |
| `exhibit_location` | | 2,393 | Where a physical exhibit is held |
| `exhibit_type` | | 36,570 | The exhibit's type |
| `fax` | | 54 | The submitter's fax number |
| `field1` | | 5,797 | An agency-defined field (“Answer Date”, “XRIN”, “RTID”, …) |
| `field2` | | 135,720 | An agency-defined field (“File Date” on all but one) |
| `first_name` | | 313,819 | The submitter's given name |
| `fr_vol_num` | | 120,435 | The Federal Register volume or citation, free text |
| `gov_agency` | | 98,453 | The government body that submitted the document |
| `gov_agency_type` | | 98,475 | Its level: Federal, State, Local, Tribal, Regional, U.S. House of Representatives or U.S. Senate |
| `implementation_date` | TIMESTAMPTZ | 199,148 | The implementation or service date |
| `last_name` | | 307,131 | The submitter's family name |
| `legacy_id` | | 134,235 | The id in a predecessor system (“Document Legacy ID”, “Legacy Exhibit Number”, …) |
| `media` | | 383,076 | How the document arrived: `Electronic`, `Paper`, … (spellings vary) |
| `object_id` | | 1,943,106 | The publisher's internal object handle |
| `omb_approval` | | 8,067 | An OMB control number, as stated |
| `open_for_comment` | BOOLEAN | 1,943,106 | Whether the document was open for comment at capture (true on 461,158); a captured observation, not a live one |
| `organization` | | 283,063 | The organization the author or submitter names |
| `original_document_id` | | 533,772 (165,397 non-empty) | The id of the document this one derives from; `""` on 368,375 |
| `page_count` | INTEGER | 1,943,106 | Pages in the content file |
| `postmark_date` | TIMESTAMPTZ | 29,599 | The postmark date (often labelled “Answer Date”) |
| `receive_date` | TIMESTAMPTZ | 1,928,713 | When the agency received it |
| `reg_writer_instruction` | | 66,795 | Agency notes (labelled “Old Submitter” on 61,052) |
| `restrict_reason` | | 20,248 | Why access is restricted, free text |
| `restrict_reason_type` | | 75,033 | Copyrighted 53,580, Other 20,022, Confidential Business Information 1,179, Personally Identifiable Information 252 |
| `source_citation` | | 71,150 | The source publication's citation |
| `start_end_page` | | 287,972 | The Federal Register start and end pages, as stated |
| `state_province_region` | | 142,556 | The submitter's state, province or region |
| `subject` | | 129,254 | The subject line |
| `submitter_rep` | | 101,418 | The name of the submitter's representative |
| `subtype` | | 1,330,483 | The agency's subtype: Correspondence, Report, Decision, … (823 values) |
| `topics` | VARCHAR[] | 85,110 | The publisher's topics |
| `tracking_nbr` | | 1,380,562 | The portal's tracking number |
| `within_comment_period` | BOOLEAN | 155,468 | Whether it arrived within the comment period (true on 10,842) |
| `zip` | | 116,880 | The submitter's postal code |

## `docket_attributes`

| Column | Type | Non-null | What the publisher states |
| --- | --- | ---: | --- |
| `docket_id` | VARCHAR | 278,607 | The docket's id; its API record is `https://api.regulations.gov/v4/dockets/{docket_id}` |
| `category` | | 92,596 | The docket's status (“Disposition”): Pending, Closed and others. |
| `display_properties_json` | | 278,607 (213,748 non-empty) | The agency's labels for this docket's fields; `[]` on 64,859 |
| `effective_date` | TIMESTAMPTZ | 14,132 | Mostly the docket close date (“Docket Close Date” on 13,111) |
| `field1` | | 199 | An agency-defined field (“Related Docket's RIN”, “Related To”) |
| `field2` | | 60,527 | An agency-defined field (“Docket Status” on 59,872) |
| `generic` | | 88,468 | An agency program code (“Docket Item Code”, “Location”, “Program Area”) |
| `keywords` | VARCHAR[] | 86,576 | The docket's keywords |
| `legacy_id` | | 7,931 | The id in a predecessor system |
| `object_id` | | 278,607 | The publisher's internal object handle |
| `organization` | | 7,789 | Labelled “Pre-EDOCKET ID” on 3,980 and “Organization” on 3,766 |
| `petition_nbr` | | 132 | A petition number |
| `program` | | 71,867 | The program office (“Center” on 64,910) |
| `short_title` | | 42,081 | A short title (“Action Office” on 13,262) |
| `sub_type` | | 111,474 | The agency's subtype; 411 values |
| `sub_type2` | | 21,010 | A second subtype level |

Labels come from each record's `displayProperties`. They show that the same
attribute means different things at different agencies. A consumer should show
the record's label, not the column name, and should not build a global facet
from `field1`, `field2`, `generic`, `organization` or `program`.

## Left out on purpose

| Reason | Documents | Dockets |
| --- | --- | --- |
| The thin table carries it | `docketId`, `agencyId`, `title`, `documentType`, `postedDate`, `modifyDate`, `commentStartDate`, `commentEndDate`, `fileFormats`, `frDocNum`, `withdrawn`, `reasonWithdrawn`, `additionalRins` | `agencyId`, `title`, `docketType`, `modifyDate`, `dkAbstract`, `rin` |
| Never stated: null on all 1,943,106 records | `email`, `phone`, `submitterRepAddress`, `submitterRepCityState` | — |
| Constant | `paperLength`, `paperWidth` (0 on every record) | — |
| Spells the key | `links.self` and `relationships.attachments` (the key in a fixed URL on every record) | `links.self` |

Two attributes named in the search coverage notes, `numPages` and `citation`,
appear in no captured record. `page_count` and `fr_vol_num` /
`start_end_page` / `source_citation` are what the API states instead.

## The bootstrap members

`members-v2/receipt.json` records the source above and, for each member, its
columns and footer types, sha256, byte size, rows and row groups.

| Member | Rows | Bytes | sha256 | Row groups (rows each) | Largest group, uncompressed |
| --- | ---: | ---: | --- | ---: | ---: |
| `document_attributes.parquet` | 1,943,106 | 77,515,299 | `fae54021e6324c3edd429d36a7ab2b6966fb01889fdc8d22b77588593fac9ef0` | 80 (24,576) | 32.5 MiB |
| `docket_attributes.parquet` | 278,607 | 4,780,846 | `c01a2796099e37b522e5618836d86e4533164ae24e2759ae380a4b51a4c65d8e` | 4 (79,872) | 8.9 MiB |

**Footer types:** BOOLEAN, INTEGER, `TIMESTAMP WITH TIME ZONE` and `VARCHAR[]`
where the contract types a column, and VARCHAR elsewhere.

**Physical layout:**

- Each member is sorted by its key, zstd-compressed and carries no field IDs.
- Both are far below 1 GiB, so neither needs an `agency_code` split.
- Document rows are skewed: `comment` reaches 900 KB. The exporter therefore
  estimates each row's bytes and fixes the group size before writing. The
  size is the most whole DuckDB vectors (2,048 rows each) for which every
  group, in key order, stays under 48 MiB of the estimate. After writing, it
  checks that no group exceeds 64 MiB.
- DuckDB ignores `ROW_GROUP_SIZE_BYTES` for sorted output, as spicy-regs
  measured.

## Measurements

Each step ran in its own process, one at a time, under the PM01 watch wrapper
with a 12 GiB cap. The machine was a shared 14-CPU arm64 Mac at load 5–7,
running DuckDB 1.5.5. The workspace was a fresh APFS clone of the capture (run
A of `run-v2.sh`).

| Step | Wall | Peak RSS (tree) | Output |
| --- | ---: | ---: | --- |
| extract: own record per member, one parse of each value | 91.2 s | 5.2 GiB | 376 MB Parquet |
| census: every attribute of every record | 28.8 s | 6.4 GiB | `receipts-v2a/census.json` |
| derive `docket_attributes` (`derive_table` 6.5 s) | 7.8 s | 0.6 GiB | +46.8 MiB records |
| derive `document_attributes` (`derive_table` 71.4 s, 0.037 ms/row) | 72.6 s | 1.4 GiB | +428.3 MiB records |
| export both members, with row-group sizing and verification | 10.8 s | 3.6 GiB | 82.3 MB |
| check: samples, parents, and every row against the exporter's Python reference | 198.3 s | 2.6 GiB | `receipts-v2a/check.json` |
| proof: every row against spicy-docs' projections | 197.3 s | 2.4 GiB | `proof/reference-proof.json` |

Each derive is one metadata unit. It is bound to the `catalogue` state, so
`generating_request` names that state as the layer's input. Its occurrences
follow the C29 rules: `value/1` over `member_key`, scoped by the definition.

## Checks

- **Member against layer:** equal row counts and equal multiset digests
  (count plus the sum of per-row hashes). The footer types equal the
  contract's.
- **Against spicy-docs' reference:** every catalogue record, read through
  DocSpec's reader, was projected by `project_document_attributes` or
  `project_docket_attributes`. This used spicy-docs `7e5d19b`, module sha256
  `5f27f985…`, run in a scratch environment built from this lane's lock. Each
  projection was compared with its member row value by value, type included,
  with instants in UTC and NULL equal only to NULL. No values differ: 0 of
  93,269,088 for documents (48 columns) and 0 of 4,457,712 for dockets (16).
- **Against the exporter's own Python reference** (`check --all`): 0
  differing values of the same totals.
- **Samples:** 21 keys per table: 10 by md5, 10 whose list columns hold text
  outside printable ASCII, and the widest row. 0 of 1,008 and 0 of 336
  values differ.
- **References:** every attribute row has a parent in the published thin
  generations. Those are documents `7c98ef8c…` (member `sha256:1f70cc76…`,
  2,002,887 rows) and dockets `e601dbf6…` (member `sha256:69ceb2d0…`, 279,429
  rows).
- **Rows without attributes:** 59,781 documents and 822 dockets in the thin
  generations have no attributes row. The capture has a posted-date floor of
  1990-01-01. Of the documents, 52,699 were posted before 1990 and 2,022 have
  no posted date, and 54,706 were last modified before 2026-09-02; so were 23
  of the dockets. A delta keyed on `lastModifiedDate` will never reach those,
  so filling them needs a full pass over the publisher's mirror. The other
  5,075 documents and 799 dockets changed on or after 2026-09-02.
- **Determinism:** two runs of `5435912`, each from its own fresh clone, gave
  byte-identical members and the same derived state IDs.
- **After `e3ceeea`:** re-deriving both layers retyped every row and returned
  the same published states, whose request binds the rows' digest. The export
  was byte-identical.
- **Tests:** `tests/test_export_regulations_attributes.py` covers:
  - native typing equal to the Python reference on control characters, DEL,
    non-ASCII text, surrogate pairs, and boundary integers and instants;
  - each type's refusals, fractional-second instants included;
  - every member-check bound (digest, footer types, order, codec, field IDs,
    64 MiB, 1 GiB);
  - the row-group sizing;
  - extract's single-own-fact refusal and publish's mixed-pin refusal;
  - a round trip of five real records, one with control characters in
    `authors`.

What a clean result could hide:

- **The column list:** both sides of the comparisons share it with the
  contract. A wrongly chosen or wrongly named attribute is for review of this
  document to find.
- **Staleness:** the comparison is against the 2026-09-02 capture, not the live
  API.
- **Values the capture never exercises:** fractional instants, non-string
  scalars and nulls inside lists are covered only by tests.
- **Determinism beyond one setup:** it is shown on one machine with one DuckDB
  version.

## For the delta job

Projecting an API response's `data.attributes` with spicy-docs' functions
reproduces these cells exactly. This was measured over the whole capture. An
unchanged record therefore keeps its row digest, and so its DocSpec occurrence,
when a later generation is admitted (decision 0007, item 4). If a record states
an attribute this contract does not name, the job should report it rather than
drop it silently.

## Questions for spicy-docs

1. `object_id` is kept under the inclusion rule, but no consumer has named
   it. Keep it or drop it?
2. The API URL spells the key. The proposal states the rule in the key's
   description instead of adding a column.
