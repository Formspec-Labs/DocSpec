# The first real DocumentRelease 2.0, minted 2026-08-30

The format sealed by `docs/decisions/0001-document-release-2-0.md` now has a
real corpus in it. This names everything the mint read, everything it wrote,
and everything it refused, so the release can be re-derived from the pin rather
than trusted.

Nothing is published. The bundle is data and lives outside the repository; what
is committed is the pin, the floor calibration, the builder, its tests, and the
receipt beside this file.

## Identity

```text
releaseId            urn:docspec:document-release:v2:eb3d0e7e86010d9ac51d94daf5fb04e51e9a6eb934caae342f1c973f4ca93a8b
documentStateDigest  sha256:eb3d0e7e86010d9ac51d94daf5fb04e51e9a6eb934caae342f1c973f4ca93a8b
buildRunId           fr-mirrulations-10k-v1-full
releaseStatus        candidate
generation           docspec
```

The release id is derived from the state digest by string form, not minted a
second time (Decision 0001, identity rule 2). The state digest is taken over
logical content, so `publishedAt` and `buildRunId` move without renaming the
corpus — two mints of these bytes an hour apart carry one identity, and the
builder's own tests prove it.

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

Zero requests were made. Every rendition carried is a preserved copy in the
checkpoint's blob stores, re-digested against the acquisition record that wrote
it and, for the 6,408 Federal Register items whose catalog entry declares one,
against the catalog's own expected digest. 7,866 copies came from the full
tier's own run and 216 from the intermediate run, which had preserved documents
the full run never captured; the run each copy came from is recorded rather
than flattened.

## What it wrote

```text
requestedUniverseCount   10,000
selectedCount             8,082
unavailableCount          1,909
failedCount                   9
excludedCount / deleted       0
documentVersionCount      8,082
structuralNodeCount     225,608
searchSegmentCount      218,891
memberCount                 207
totalMemberByteSize     813,352,062
representationByteTotal 172,975,777
segmentedByteTotal      165,975,979
excludedByteTotal         6,999,798
unaccountedCount              0
```

`segmentedByteTotal + excludedByteTotal == representationByteTotal` holds in
aggregate and for `document-body`; `attachment` and `comment` are present with
measured zeros, as Decision 0001 requires of a release that carries none of
them. Attachments and comments have no rows because the 10k sampling plan
excluded them from the draw.

Set digests:

```text
selectedSourceSetDigest      sha256:9d46bec3569083ee65f7f1486b34697360b0286c790f08daf2baf232a86115af
documentVersionSetDigest     sha256:976ceb385d38855551b9d27bf79763bf06ff1722d3336bda9a7a35e3ffae7770
textBodySetDigest            sha256:2b7d3cb81a4413da7a4d750040032829f598bf3a09374e78871ef3430f61d1a7
segmentSetDigest             sha256:3023ca9f54683273332fcc1a8634cb23a129709d94146cb0b7470377616b50e7
sourceDocumentMappingDigest  sha256:b869122c135ad98d5fe4dca1166453ecadcaef662fa801b3dfefe87b198ab395
attachmentSetDigest          sha256:958e47ad2d329fe88d5572b1caf66074081b693d88b75fae49146abc43aa99e8
commentSetDigest             sha256:1990965877bd73781541d269617103403ce8f26d28596d9768912fdd8bd92fc7
```

The last two are the empty set under their own domains: written, never omitted.

## What it refused, and why

```text
1,909  capture.no-preserved-copy          unavailable
    9  extraction.below-retention-floor   failed
```

**The 1,909 are the honest headline.** The pinned catalog's universe is 10,000
documents, and the checkpoint preserved bytes for 8,091 of them. The full tier's
acquisition run captured all 6,408 Federal Register items and 1,467 of the 3,592
Mirrulations items before it stopped; the intermediate run supplies 216 more.
1,909 Mirrulations documents have no preserved copy anywhere in the checkpoint,
and this producer does not fetch. They are carried as `unavailable` rows with a
reason, inside the universe, counted — loss stays visible rather than being
dropped from the denominator.

The 9 are the floor working on real data: Federal Register XML documents whose
visible text fell below 0.35 of their captured bytes, against a pooled sample
whose lowest document was 0.4777. The lowest retention among the 6,399 XML
documents that passed is 0.3615, so the floor sits between the population it was
calibrated on and the documents it refused rather than gating nothing.

No row is `selected` without a document version and a document row, and no row
carries a processing failure. The builder checks that before it writes a byte.

## The floors, and where they were measured

`fixtures/retention-floors/calibration.json`, method
`tools/calibrate_retention_floors.py`.

| kind / format | floor | pooled observed minimum | population |
| --- | --- | --- | --- |
| `document-body` `application/xml` | `0.35` | `0.4777` | `fr-body-retrieval-distribution-xml + fr-mirrulations-10k-v1-xml` |
| `document-body` `text/html` | `0.59` | `0.7976` | `fr-rule-bodies-pre2000 + fr-mirrulations-10k-v1-html` |

Unit is the visible-text fraction: retained representation bytes over captured
rendition bytes, both UTF-8, as decimal strings. Each floor is three quarters of
the lowest document in a 400-document pooled sample — 200 from the preserved
corpus Decision 0001 names and 200 from a deterministic stride through the
pinned corpus — truncated to two significant digits. Pooling is deliberate: the
preserved XML draw holds no document under 37 KB, and a short notice spends far
more of its bytes on preamble markup than a long rule does, so a floor measured
on the preserved draw alone (0.46) would have refused documents the mint corpus
legitimately contains. Every sampled document is pinned by digest inside the
receipt.

Measured retention across the mint:

| format | n | min | median | max |
| --- | --- | --- | --- | --- |
| `application/xml` | 6,399 | 0.3615 | 0.7182 | 0.8906 |
| `text/html` | 1,683 | 0.7976 | 0.9741 | 0.9983 |

## Processing policies, per (kind, format)

```text
document-body application/xml
  extractor  docspec.xml-visible-text/v1   a72106c4ae301b9d60876d26ab5fc87f18842bf106012cf6d8d9fc2b1a2c3a9e
  segmenter  docspec.bounded-structure-overlap/v1  6cb903aa417b3fb1f1616c9e6630f31907edd2a9609899662c667559fad07f08
document-body text/html
  extractor  docspec.html-visible-text/v1  738399ff619a123b86016560e994d72c8efe5afeda0d9d116a7d3967f4fd6d7d
  segmenter  docspec.bounded-structure-overlap/v1  6cb903aa417b3fb1f1616c9e6630f31907edd2a9609899662c667559fad07f08
  maxSegmentBytes 65536 (declared and enforced; largest observed 13,018)
```

The segmenter digest is the bounded policy's own, which binds the tokenizer that
counted: `tiktoken` 0.14.0 at `o200k_base`, under the `tokens` extra. A machine
without the extra falls back to a deterministic codepoint counter, and the
fallback's name and version ride in the same digest, so a release says which
tokenizer measured its boundaries rather than leaving a reader to assume.

## Verification

`src/docspec/adapters/document_release_verify.py`, whole bundle, generation
`docspec`: **`valid`, 0 diagnostics**. Build 65 s, verify 73 s, 138 s total,
peak resident 2.4 GB.

The twenty sealed predecessor bundles and the twenty docspec fixture bundles
verify unchanged, byte-untouched, to the same codes and paths.

## Where things are

```text
output/document-release-10k-v1/                     the bundle (813 MB, not tracked)
output/document-release-10k-v1-mint-receipt.json    the receipt as minted
docs/history/2026-08-30-document-release-10k-v1-mint-receipt.json   its committed copy
fixtures/fr-mirrulations-10k-v1/pins.json           the corpus pin
fixtures/retention-floors/calibration.json          the floor calibration
tools/build_document_release.py                     the builder
tools/calibrate_retention_floors.py                 the calibration
tools/fr_mirrulations_pin.py                        the pin loader
src/docspec/processing/visible_text.py              the extractors
src/docspec/processing/retention_floors.py          the refusal
tests/test_document_release_builder.py              18 tests
```

Rebuild it with:

```sh
uv run python -m tools.build_document_release \
  --output output/document-release-10k-v1 \
  --receipt output/document-release-10k-v1-mint-receipt.json
```

## Recorded departures and open items

| | |
| --- | --- |
| **The disposition mapping is the builder's.** | Decision 0001 says the release projects "the catalog disposition ... verbatim". The pinned snapshot's item vocabulary is `active` / `deleted` / `excluded` and carries no `selected`, so there is nothing to project. The mapping is written into `tools/build_document_release.py` and checked: `selected` iff a document version and a document row, `null` otherwise, a reason on every non-selected row, no accepted processing failure. |
| **1,909 documents have no bytes.** | Not a defect of this release; a gap in the checkpoint. Recovering them needs an acquisition run, which is a separate decision. |
| **`agencyName` repeats `agencyId`.** | The Federal Register draw supplies agency slugs and the regulations.gov metadata supplies an agency code; neither supplies a display name, so none is invented. The Federal Register XML does carry `<AGENCY>`/`<SUBAGY>` prose, and matching it to the slugs positionally would be a guess. |
| **`acquisitionStartedAt` is truncated to whole seconds.** | The acquisition records carry microseconds; `$defs/instant` admits whole seconds. Truncated, never rounded, so an acquisition is reported no later than it happened. |
| **`RELEASE_FORMAT_VERSION` stays `1.1`.** | Decision 0001 says it becomes `2.0` when the builder lands. It has not been flipped: `src/docspec/domain/release.py` is still the 1.1 pointer-record, and stamping `2.0` on a root that is not the 2.0 shape would make the constant lie. Retiring the pointer-record is its own operation. |
| **`reasonCode` enums stay open.** | Decision 0001 A3 makes closing them a first-real-mint obligation. This mint's vocabulary is now known — `capture.no-preserved-copy`, `capture.preserved-copy-unverifiable`, `capture.expected-digest-differs`, `selection.no-markup-rendition`, `extraction.below-retention-floor`, `extraction.retention-floor-undeclared`, `extraction.retention-unmeasurable`, `extraction.unparseable-source`, `extraction.no-visible-text`, `segmentation.region-empty`, `segmentation.region-not-evidence-eligible`, `segmentation.refused`, `segmentation.no-searchable-segment`, `segmentation.segment-over-declared-bound`, `metadata.incomplete`, `structure.heading-path-disagrees`, `catalog.state-deleted`, `catalog.state-excluded` — but closing the enum changes a sealed schema and renames every fixture bundle, so it is recorded here and left to the operation that restamps them. |
| **Migration-manifest rows 6/7/8** | Decision 0001 marks exact source capture, processing segments, and the extraction task protocol "ported at the first mint". The release that carries them now exists. |
