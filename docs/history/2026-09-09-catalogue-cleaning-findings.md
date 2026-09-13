# Catalogue cleaning findings, 2026-09-09

The agency dropdown exposed an upstream data problem: unidentified Federal
Register headings were promoted into agency identities during catalogue
normalization. The native index preserved those values. Atlas-backed choices
can keep them out of the selector, but do not repair the catalogue or remove
their contribution to text search.

This is a bounded forensic inspection of named examples, not a corpus audit or
quality-rate estimate. It used native term dictionaries, exact indexed record
lookups, and the named catalogue members below. It did not scan the combined
export or change any catalogue, source data, policy, or index. No timing or
population claim follows from these examples.

## Inputs and trace

The inspected active configuration was SpicyEngine's `state/catalogue.json`:

- Database: `catalogue_c7d5f51cf06769c4_29e6bc34`.
- Export SHA-256: `c7d5f51cf06769c4c0da7c21bcdc10afeeafbfb18dd1536c86b7c37a370efd27`.
- Export receipt: `/Users/mikewolfd/Work/corpora/_spicyengine-native-20260909-planned/export-receipt.json`.
- Federal Register catalogue artifact: `sha256:0872a608f6f21655887d346a50ae307c071358960ab93a363f0a8227f31f0394`.
- Regulations.gov catalogue artifact: `sha256:1ac55e0c874c6692c5c61791353cf4b5fbbc755d56de05a0bbb960a278617604`.

Both catalogue artifacts name DocSpec producer commit
`38808185ebe44d14d3d9d812a001d9917876f7a6`. Its normalization functions were
read with `git -C ../DocSpec show <commit>:<path>`, alongside current code.
Every catalogue member opened below matched its filename SHA-256; each
selected JSON line, excluding its final LF, matched the snapshot's stored
`source_item_sha256`. This establishes the examples in the actual catalogue,
not merely a similar value in a current checkout. This inspection did not
re-admit every member of either complete distribution or compare the held
source-native observations against a fresh publisher response.

The observed path is:

1. The catalogue retains publisher fields in `sourceNativeFacts`.
2. DocSpec creates `normalizedMetadata` and records its normalization decision.
3. Search's [`_facets`](../../../spicysearch-query-models-20260909/src/spicysearch/source_catalog_metadata.py)
   copies normalized agency IDs/names and `documentType` into facets.
4. Engine's [`export_sql`](../../../spicyengine/tools/native_export.py) copies those facets
   into `agency_ids`, `agencies`, and `document_types`; topic labels come from
   `sourceObservedTopics`. It also retains the original snapshot `record`.
5. SereneDB indexes these fields. The old dropdown exposed their distinct values.

## Confirmed defect: raw headings become agency identities

| Engine record ID | Preserved raw value | Where it appears |
| --- | --- | --- |
| `83e47af1f78c:96-1584@1996-01-30` | `Food Additives Permitted for Direct Addition to Food for Human` | `/sourceNativeFacts/0/fields/agencies/0/raw_name`; then `/normalizedMetadata/agencies/0/agencyId` and `agencyName`; then both native agency arrays |
| `83e47af1f78c:95-15887@1995-06-28` | `Collecting Food Stamp Recipient Claims From Federal Income Tax Refunds` | The same source and normalized paths as above |

The first record's title is “Food Additives Permitted for Direct Addition to
Food for Human Consumption; Olestra.” The second is “Food Stamp Program:
Collecting Food Stamp Recipient Claims From Federal Income Tax Refunds and
Federal Salaries.” These are title fragments in agency fields. Each source
agency array also contains proper publisher agency objects with numeric IDs,
slugs and names. The fragment objects contain only `raw_name`.

The producer mechanism is explicit in DocSpec's
[`federal_register_catalog._agencies`](../../src/docspec/application/federal_register_catalog.py):

```python
name = raw.get("name") or raw.get("raw_name")
identity = raw.get("slug") or name
```

The normalization receipt reports `sourcePaths: ["record.agencies"]`,
`outcome: "normalized"`, and no `unparseableValues` for these records. The
failure is therefore more specific than “dirty labels”: an unidentified
publisher heading becomes an apparently usable agency identifier. Search's
agency-name text unit also permits lexical matching, so hiding the choice
alone does not remove this text from ranking inputs.

**Owner and next correction:** DocSpec should distinguish identified agency
objects from unrecognized `raw_name` observations. Preserve the latter and
their source paths, but do not manufacture an agency ID from their text.
Use the publisher's explicit ID/slug and an exact Atlas mapping when available;
otherwise retain an unresolved observation. Keep the document available even
when its agency cannot be resolved. Add these exact examples and a legitimate
unresolved agency as controls before changing the producer's normalization
and selection behavior. Republish a new catalogue when that change is approved.

## Confirmed encoding damage; original decoding failure not localized

Record `83e47af1f78c:C3-12266@2003-05-23` has these exact values:

```json
{"sourceNativeFacts":[{"fields":{"agencies":[
  {"raw_name":"DEPARTMENT OF COMMERCE\ufffd09"},
  {"raw_name":"Bureau of Industry and Security\ufffd09"}
]}}]}
```

Both survive as normalized agency IDs and names. These are the literal Unicode
replacement character U+FFFD followed by `09`; the inspected strings do not
contain a tab character. A mistaken tab/escape conversion is a plausible
explanation, not an established diagnosis. The held source-native observation
already contains the damage, so SereneDB did not introduce it.

**Owner and next correction:** trace this document through SpicyRegs' captured
publisher bytes and decoding before deciding whether the publisher or a local
adapter introduced the replacement character. DocSpec should keep this value
unresolved rather than treating it as an agency ID. Do not globally delete
U+FFFD or replace `\ufffd09` with whitespace: that could hide loss or change
identifiers without recovering the original value.

## Legitimate publisher spelling differences: subject capitalization

`publisher_topics` contains both `Environmental Protection` and
`Environmental protection`. The distinction is present in source observations:

- `bd8b7c278ac6:AFRH-2009-0002-0001` carries Regulations.gov
  `/sourceNativeFacts/0/fields/data/attributes/topics/1` =
  `Environmental Protection`. Its `sourceObservedTopics` identifies the
  `regulations.gov` scheme.
- The same catalogue record's joined Federal Register observation carries
  `/sourceNativeFacts/1/fields/topics/1` = `Environmental protection`.
- `83e47af1f78c:2024-30506@2024-12-30` carries the lowercase-p variant as its
  own Federal Register topic and preserves the `federalregister.gov` scheme.

This is confirmed display duplication across publishers, not confirmed source
corruption. Keep the raw labels and `(scheme, observedTopicId)` identities.
Engine may group presentation/search spellings while retaining every executed
exact source value. RefSpec owns any assertion that separately identified
publisher topics mean the same concept; lowercasing alone does not establish
that assertion. No upstream rewrite of these source labels is warranted here.

## Legitimate docket categories exposed as document categories

| Engine record ID | Source item kind | Native field | Normalized and indexed result |
| --- | --- | --- | --- |
| `bd8b7c278ac6:ATBCB-2022-0002` | Regulations.gov `data.type = "dockets"` | `data.attributes.docketType = "Rulemaking"` | `normalizedMetadata.documentType`, `document_type`, and `docket_type` all contain `Rulemaking` |
| `bd8b7c278ac6:AMS-CN-10-0027` | Regulations.gov `data.type = "dockets"` | `data.attributes.docketType = "Nonrulemaking"` | The same fields all contain `Nonrulemaking` |

These are valid docket classifications, not alternative spellings of `Rule`.
The producer's
[`_docket_item_from_row`](../../src/docspec/application/regulations_gov_catalog.py)
explicitly reads `docketType` into `documentType`; its sealed normalization
receipt names that source path. Search then creates a `document_type` facet
from the normalized field, while its source-native path also preserves the
separate docket facet. Engine copies both; it did not merge a related docket's
classification into a document in these examples.

**Owner and next correction:** use a clear record-kind distinction and keep
document classification separate from docket classification when evolving the
DocSpec/Search data preparation. In the current UI, “Record type” honestly
describes a list spanning documents and dockets. Keep `Rulemaking` distinct
from `Rule`; do not silently remap one into the other or delete docket records.
Whether related-document context contributes other type values was not audited.

## Additional text preparation issue found in the same bounded sample

Record `83e47af1f78c:2024-30506@2024-12-30` preserves `SO<INF>2</INF>` in its
source abstract and the native `abstracts` array. Its final text is
`received no comments.December 30, 2024`.

The markup's presence in a text search/display field is confirmed. Whether the
trailing date came from publisher formatting or local field-boundary extraction
remains unresolved. Keep the source string, and have Search's text preparation
use a source-format-aware plain-text conversion with reversible evidence
coordinates. Do not use a broad tag-stripping regex or remove the trailing date
without checking the held publisher bytes. No ranking-effect claim was tested.

## Reproduction and exact catalogue coordinates

Commands ran from `/Users/mikewolfd/Work/spicyengine` through `uv run python`.
Dictionary discovery used this form with an explicit field and predicate:

```sql
SELECT value
FROM (SELECT unnest(ts_dict_agg(agency_ids)) AS value FROM comparison_docs_idx) terms
WHERE contains(value, 'Food') OR contains(value, '�') OR contains(value, chr(9))
ORDER BY value LIMIT 12;
```

The other dictionary checks used `agencies` with `contains(value, 'Food')`,
`publisher_topics` with `lower(value) = 'environmental protection'`, and
`document_types`/`docket_types` with `true`. The bounded dictionary output does
not prove the absence of other values.

Exact record inspection used keyword analysis, without natural-language query
rewriting. All record IDs above can be reopened this way:

```python
import engine, json
config = engine.active()
record_id = "83e47af1f78c:96-1584@1996-01-30"
statement = (
    f"SELECT id,title,record FROM {engine.INDEX} "
    f"WHERE id @@ {engine.text_query(record_id, keyword=True)} LIMIT 1"
)
rows = engine.sql(statement, config["database"])
record = json.loads(rows[0]["record"])
print(json.loads(record["subject"]["display_metadata_json"]))
print(record["facets"])
print(record["units"])
```

Below, `A` means
`/Users/mikewolfd/Work/corpora/supply-2026-09-02/catalogs/catalog-A/catalog-composite-2/.blobs/sha256/`;
`B` means
`/Users/mikewolfd/Work/corpora/supply-2026-09-02/catalogs/catalog-B-composite/catalog/.blobs/sha256/`.
Offsets are zero-based byte positions; lengths include the final LF. The row
hash excludes that LF and equals the snapshot's `source_item_sha256`.

| Source item | Root | Member filename / SHA-256 | Byte offset | Line length | Row SHA-256 |
| --- | --- | --- | ---: | ---: | --- |
| `96-1584@1996-01-30` | B | `68118a0b029a58084da6ac0240da97e1f2326ef3fb501bbed5bb6e6652c80334` | 89788013 | 8139 | `9458211abe65ecfbeef15703ade9c67b3760824ca4416fc45b03f02d47a102e7` |
| `95-15887@1995-06-28` | B | `8998e24aa62e3be5430e570a9d66ce7fec2791d858cf53a16930795a08f76324` | 85518516 | 8868 | `130b3e6a34a28e2e06c9d764a4e5a13e532a12693a2b999a4a60ea5891eb3224` |
| `C3-12266@2003-05-23` | B | `7468c55ec4bed9f989f9cd5913a8fe439c34ffdacc495aa200bfb9ea49d53ac0` | 104679074 | 6974 | `906dc65795da9bc4c01860a1c03f21014839e0f4b09f4cc0a73b0d0e5dc8f869` |
| `AFRH-2009-0002-0001` | A | `05a85396e6351fdfbac60ec2d41be6f13ebb5a59e4702bc45a749011919ecbe8` | 177748 | 10962 | `ff3670ce9fadd3b31a154aa9c89d843c6d40b260b9cf830356b0cbce793dcd9e` |
| `2024-30506@2024-12-30` | B | `80c4982f1cf6bfc52955511811ac6012b53e1eb7cd6ab9bbd9f6a17726b0bf28` | 77070409 | 8954 | `32369794e5b4f074eb2c00b9fbca54462c3ce0d3630484b0ed12c61391dd4304` |
| `ATBCB-2022-0002` | A | `177eac712e9f203668dad1a1bb6a117fa7bc87aded2907f88b28a61c21729f86` | 3470220 | 7764 | `1fe8a99096cd2225a0bd50e07d0964ac2d46d733d1c958bc1fd47e6613d832cf` |
| `AMS-CN-10-0027` | A | `e65d421135ba124884312503c943132922f1971ebcfccef10a259d1caf669d31` | 730931 | 6797 | `487e313533c59ee62211825df0f4892ae70cb6bce86ec73a537673666d506fb4` |

For a targeted recheck, hash the named member, seek to its recorded offset,
read the recorded length, verify the row hash, then inspect the JSON paths
above. This avoids another corpus-wide investigation. Preserve these held
bytes when producing corrected releases.
