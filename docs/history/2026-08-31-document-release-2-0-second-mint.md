# The second real DocumentRelease 2.0, minted 2026-08-31

The first mint (`2026-08-30-document-release-2-0-first-mint.md`) failed a
two-reviewer blind gate on six findings. Decision 0001's **amendment B1–B7**
ruled on all six; this is the mint those rulings produced. It is not a patch of
the first — the format's set digests moved to `/3`, the gate gained four
diagnostics and lost its ability to crash, the builder learned to enumerate
attachments and to derive from the pin, and the floors were re-measured under a
different metric on a different population. A new corpus name follows from all
of that, and does.

Nothing is published. The bundle is data and lives outside the repository; what
is committed is the pin, the floor calibration, the builder, its tests, and the
receipt beside this file.

## Identity

```text
releaseId            urn:docspec:document-release:v2:28a276a36046f3fa60a838b765cb99d817121a14a7c17596bb98e753e8c2637b
documentStateDigest  sha256:28a276a36046f3fa60a838b765cb99d817121a14a7c17596bb98e753e8c2637b
buildRunId           fr-mirrulations-10k-v1-full
releaseStatus        candidate
generation           docspec
```

The release id is derived from the state digest by string form, not minted a
second time (Decision 0001, identity rule 2). The URN prefix is unchanged at
`v2` even though `RELEASE_FORMAT_VERSION` flipped to `2.0` in this round: the
format version says which contract the bytes obey, the prefix says which
identity namespace the name lives in, and B7 is explicit that flipping one must
not move the other.

**The name now covers every logical row in the bundle.** Under the superseded
`/2` domains a set digest framed each row's id fields alone, so a same-length
mutation of a body's bytes with the physical digests restamped left
`documentStateDigest` where it was. The `/3` domains frame each record's full
logical row minus its physical locators and its acquisition clock, and two
digests were added — `sourceDispositionSetDigest` and `structuralNodeSetDigest`
— because without them 10,000 disposition rows and 225,917 structural nodes sat
outside the name. Rerun against this format, the attack moves the name; a
repack, and a re-clock, still do not.

## What it read

| | |
| --- | --- |
| Corpus pin | `urn:docspec:qualification-corpus-pin:v1:403cc652297d31f2ae35de495edee8745c823a8fc5027cde4a3d4fb96cfe44fd` |
| Pin file | `fixtures/fr-mirrulations-10k-v1/pins.json` |
| Campaign / tier | `fr-mirrulations-10k-v1` / `full` (D1) |
| Catalog id | `urn:docspec:source-catalog:v1:973aaa197206821869294deb09b3cb6281d9bd55ab265214026a48c71fc7d094` |
| Catalog digest | `sha256:ded6649aab3f04faa6a48f867de0854648ec10c04fcdad8f6527e075d97c45d6` |
| FR draw | `sha256:c8b42519358d14df5f26a50330f4fc5afaae7780e9cf3bd959d69c9a99a99957` |
| Mirrulations draw | `sha256:48b2eb86bcd363401fa3f4615dcb1be16f7c7c1a0b6f9a5d91ca1162dbd2350c` |
| Checkpoint | `~/Work/corpora/_salvage-2026-08-28/docspec-qualification/fr-mirrulations-10k-v1` |

The pin is identical to the first mint's. Nothing about the input changed; every
number below that differs from the first mint's differs because a rule changed.

Zero requests were made. 7,875 preserved copies came from the full tier's own
run and 216 from the intermediate run, which had preserved documents the full
run never captured.

## What it wrote

```text
requestedUniverseCount   10,000
selectedCount             8,091      (was 8,082 — the nine false refusals returned)
unavailableCount          1,909
failedCount                   0      (was 9)
excludedCount / deleted       0
documentVersionCount      8,091
structuralNodeCount     225,917
searchSegmentCount      219,212
attachmentRows            1,683      (was 0 — the member was hardcoded empty)
renditionRows             3,366
memberCount                 207
totalMemberByteSize     817,360,735
representationByteTotal 173,226,163
segmentedByteTotal      166,215,960
excludedByteTotal         7,010,203
unaccountedCount              0
```

`segmentedByteTotal + excludedByteTotal == representationByteTotal` holds in
aggregate and for `document-body`; `attachment` and `comment` carry measured
zeros, because no attachment rendition of this corpus became a text body and no
pinned catalog selects a comment into `U`.

Set digests, nine now rather than seven:

```text
selectedSourceSetDigest      sha256:7357b47d3f10766ad5a8bff7184c4ba57f8986c5bd051f6bfb5a94b96b252cab
sourceDispositionSetDigest   sha256:25314f9e434dc733bbcfcc496cb69f262aa911863bb2852ac5670892cb679d75   NEW
documentVersionSetDigest     sha256:466d770710bb7acc4862a40545be3699ef1903fce6eb004ebe57326cc7711868
textBodySetDigest            sha256:5708fe59e7994cbe69d46c1b6ec1bb15bde7849f9094a455453805a8dfd5d322
attachmentSetDigest          sha256:dd5e6f3f53feb39af72fe1818bee417ce7f5353f23380b64fa48aeb558201751
commentSetDigest             sha256:49fb8b432cc0c28459e68656fc1f0aca2b10aed521a0dd015bb208553cab60d2
structuralNodeSetDigest      sha256:667a21082d6fead607e065f2a5c4ba5d5e085528829946aa432fa6ecbdc8800d   NEW
segmentSetDigest             sha256:07ba86d5e0c0b5e5b3da6552527695dd4161f91afd4b4ef98f1808f950307d24
sourceDocumentMappingDigest  sha256:ca74f521cc760aa2db282782b81af7bac13fba96d3d3aa0de54963b7108b6f4d
```

`commentSetDigest` is the empty set under its own domain: written, never
omitted. `selectedSourceSetDigest` is the one value here a bundle-only reader
cannot recompute — B6 derives it over the **pinned catalog's** active items
through `selected_source_set_digest_from_pin`, so a consumer holding the pinned
bytes reproduces it by running that function over them, and the gate no longer
pretends to check it. Re-running that function over the pin reproduces
`sha256:7357b47d…` exactly.

## Attachments, enumerated for the first time

```text
attachment rows          1,683   one per selected Mirrulations document
rendition sub-rows       3,366   two per row, in source order
  text-excluded          1,683   text/html, owner-body-rendition
  source-unavailable     1,683   application/pdf, no-preserved-copy
  text-captured              0
  extraction-failed          0
rows carrying text           0
```

Every selected Mirrulations document enumerates a `pdf` and an `htm` in its
preserved source record's `fileFormats`, which is Decision 0001's flat DOCUMENT
packing — so it is a list of **renditions of one attachment**, not a list of
attachments, and one row groups both under one `attachmentId`.

The `htm` rendition IS the document body's own rendition — its declared size
matches the captured bytes exactly, all 1,683 times — so B4 resolves the
ambiguity Decision 0001 left: it is enumerated with an honest row and the
disposition `text-excluded`, never extracted a second time. Extracting it would
put the same text in the corpus twice; omitting it would break the enumeration
rule. The `pdf` has no preserved copy in the checkpoint and is
`source-unavailable` with a null capture and a reason. Neither can block the
build — attachments are renditions of a member of `U`, not members of it — and
`counts.attachmentAccounting` declares the tally so a reader recomputes it.

## What it refused, and why

```text
1,909  capture.no-preserved-copy   unavailable
    0  floor refusals
```

**The nine floor refusals of the first mint were false and are gone.** Those
documents extract completely; their ratio was low only because 48–60% of a
Federal Register XML rendition is pretty-print indentation, which the old
denominator counted as content the parser had failed to keep. Under the
normalized metric the lowest of them measures 0.4558 against a floor of 0.33.
No document in the corpus falls below either floor.

**The 1,909 are unchanged and remain the honest headline.** The checkpoint
preserved bytes for 8,091 of the pinned universe's 10,000 documents; 1,909
Mirrulations documents have no preserved copy anywhere in it, and this producer
does not fetch. They are carried as `unavailable` rows with a reason, inside the
universe, counted.

## The floors, and where they were measured

`fixtures/retention-floors/calibration.json` (format `2.0`, sealed by
`urn:docspec:schema:retention-floor-calibration:2.0`), method
`tools/calibrate_retention_floors.py`.

| kind / format | floor | observed minimum | population | n |
| --- | --- | --- | --- | --- |
| `document-body` `application/xml` | `0.33` | `0.453` | `fr-body-retrieval-2026-08-02:distribution-xml` | 993, whole |
| `document-body` `text/html` | `0.17` | `0.2382` | `fr-body-retrieval-2026-08-02:distribution-html` | 993, whole |

Four things changed, and each was a defect in the first calibration:

**The metric.** Retention is now
`normalized-visible-text-fraction`: whitespace-normalized representation bytes
over whitespace-normalized rendition bytes, every maximal run of ASCII
whitespace collapsed to one space on **both** sides. Both sides, because the
HTML extractor lays out verbatim and the XML extractor lays out normalized — an
un-normalized numerator makes them incommensurable and, measured, produces
retention above 1.0 on 993 real `distribution-html` documents, which the floor's
own arithmetic cannot represent.

**The population.** Each floor is measured on a population disjoint from the
corpus it gates, and the disjointness is measured rather than asserted: **0**
shared rendition digests and **0** shared source document numbers, both
recorded in the receipt. The id check is not redundant — the same Federal
Register document retrieved twice has two digests and one document number, and
only the id check would catch it. The pinned corpus contributes no stratum.
`distribution-html` is the population Decision 0001 named and the first
calibration never read.

**The coverage.** Every document in each population is measured, not 200 of
them, and the receipt has no way to say otherwise: `population.coverage` is the
constant `full-population`, `measuredCount` must equal `documentCount`, there is
one row per document, and `observedMinimum` must equal the distribution minimum
beside it. There is no field named `sample` anywhere in the format.

**Both ratios per document.** Raw and normalized, for all 1,986 documents.
Normalization hides exactly one thin-parse mode — content whose meaning is
carried BY its whitespace, destroyed by a parse that keeps the characters and
drops the layout — and a cratered raw ratio under a healthy normalized one is
its only signature.

`rule-bodies-pre2000-2026-08-22` is measured and deliberately **not** pooled:
over its full 39,785 bodies the minimum is 0.0589 against a next-lowest of
0.2481, so pooling it would set an HTML floor of 0.044 chosen by one outlier.

Measured retention across the mint, under the new metric:

| format | n | min | median | max |
| --- | --- | --- | --- | --- |
| `application/xml` | 6,408 | 0.4558 | 0.8839 | 0.9578 |
| `text/html` | 1,683 | 0.7933 | 0.9733 | 0.9981 |

## Processing policies, per (kind, format)

```text
document-body application/xml
  extractor  docspec.xml-visible-text/v1   a72106c4ae301b9d60876d26ab5fc87f18842bf106012cf6d8d9fc2b1a2c3a9e
  segmenter  docspec.bounded-structure-overlap/v1  6cb903aa417b3fb1f1616c9e6630f31907edd2a9609899662c667559fad07f08
  floor      0.33 under 0.453, normalized-visible-text-fraction
document-body text/html
  extractor  docspec.html-visible-text/v1  738399ff619a123b86016560e994d72c8efe5afeda0d9d116a7d3967f4fd6d7d
  segmenter  docspec.bounded-structure-overlap/v1  6cb903aa417b3fb1f1616c9e6630f31907edd2a9609899662c667559fad07f08
  floor      0.17 under 0.2382, normalized-visible-text-fraction
  maxSegmentBytes 65536 (declared and enforced; largest observed 13,018)
```

The gate now checks these rather than carrying them: `invalid.retention-floor`
refuses a floor that violates its own invariants, and refuses a text body whose
`(textKind, mediaType)` no declared policy governs — the checkable half of "an
undeclared floor fails closed".

## Verification

`src/docspec/adapters/document_release_verify.py`, whole bundle, generation
`docspec`: **`valid`, 0 diagnostics**. Build 95 s, verify 91 s, 186 s total.

The gate cannot be crashed. `_read_slice` refuses a negative offset or length
before it seeks, the index reader reports `invalid.member-digest` for such a
row, and `verify_document_release` catches anything escaping any rule and
reports it as `invalid.root-syntax` — no input can make the gate fail to produce
a verdict.

The twenty sealed predecessor bundles verify unchanged and byte-untouched,
20/20. The docspec conformance corpus is 24 bundles now, each sealing its whole
diagnostic set rather than only its primary code and path.

**Remint-idempotence, measured.** Two independent full runs, in separate
processes and separate trees, with **different** `publishedAt` annotations
(`2026-08-31T00:00:00Z` and `2026-09-01T12:34:56Z`), reach the identical
`documentStateDigest`, `releaseId`, nine set digests, `counts`, `coverage`, and
`attachmentAccounting`. The annotation moved and the name did not, which is the
property B1's exclusion set exists to keep: a process fact inside a
content-derived identity would make an honest remint of identical content a
different release.

## Where things are

```text
output/document-release-10k-v2/                     the bundle (817 MB, not tracked)
output/document-release-10k-v2-mint-receipt.json    the receipt as minted
docs/history/2026-08-31-document-release-10k-v2-mint-receipt.json   its committed copy
fixtures/fr-mirrulations-10k-v1/pins.json           the corpus pin
fixtures/retention-floors/calibration.json          the floor calibration (format 2.0)
src/docspec/schemas/retention_floor_calibration/2.0/   the receipt's own schema
tools/build_document_release.py                     the builder
tools/calibrate_retention_floors.py                 the calibration
src/docspec/processing/retention_floors.py          the refusal and the metric
tests/test_document_release_builder.py              19 tests
```

Rebuild it with:

```sh
uv run python -m tools.build_document_release \
  --output output/document-release-10k-v2 \
  --receipt output/document-release-10k-v2-mint-receipt.json
```

## Recorded departures and open items

| | |
| --- | --- |
| **The disposition mapping is still the builder's.** | Unchanged from the first mint, and unchanged in kind: the pinned snapshot's item vocabulary is `active` / `deleted` / `excluded` and carries no `selected`, so there is nothing to project. The mapping is written into the builder and checked before a byte is written. |
| **`selectedSourceSetDigest` is not gate-checkable.** | B6's recorded cost. A portable verifier reads one bundle and the pinned catalog is not in it, so the gate checks the value's form and not its value. The release's own selection is bound by `sourceDispositionSetDigest`, by `counts`, by the join receipt, and by the bijection; what this digest names is the CATALOG's selected set, and only a holder of the pinned bytes can confirm it. |
| **`invalid.comment-selection` has no corpus fixture.** | The rule is implemented and tested on a grown bundle; the fixture is not mintable. A comment is a member of `U`, and `source-dispositions.schema.json` requires a selected member of `U` to carry a non-null `documentVersionId` — "A selected item is a document." Neither pinned catalog selects a comment, and Decision 0001 deferred the `U` shape for comments deliberately. Minting the fixture would mean inventing a universe member no catalog carries. Recorded at `UNMINTABLE_CODES` in the test suite, not papered over. |
| **`attachmentAccounting.extractionFailed` is zero, and untested end to end.** | No rendition of this corpus was fetched and then refused, because the checkpoint preserved no attachment renditions at all beyond the bodies' own. The token is declared and the tally writes its zero; the path that populates it waits on a checkpoint that preserves attachment bytes. |
| **1,909 documents still have no bytes.** | Not a defect of this release; a gap in the checkpoint. Recovering them needs an acquisition run, which is a separate decision. |
| **`agencyName` still repeats `agencyId`.** | Neither source supplies a display name, so none is invented. |
| **Migration-manifest rows 6/7/8** | Marked ported at the first mint; the release that carries them still exists. |
