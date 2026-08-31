# The third real DocumentRelease 2.0, minted 2026-08-31

The second mint (`2026-08-31-document-release-2-0-second-mint.md`) **passed** its
blind re-gate. This one exists because passing a gate is not the same as being
right: two defects were found after the verdict — one of them by the release's
first real consumer — and the PASS verdict itself carried five refinements. All
seven are ruled on in Decision 0001's **amendment C1–C7**, and this is the mint
those rulings produced.

The change that matters most is the smallest: the release now declares its
processing policy under the media type its rows actually carry. SpicySearch's
mapper refused the second bundle with *"document release declares no processing
policy for ('document-body', 'text/xml')"* — correctly, because all 6,408
Federal Register rows say `text/xml` and the policy table said `application/xml`,
which is the retention *format key* those collapse onto. The gate had a check for
exactly this and it collapsed both sides before comparing, so it was written to
be blind to the thing it was for. **A wire contract is what a consumer can join,
and this one could not be joined.**

The change that shows most is the second: **193 documents the first two mints
refused as unavailable are in this release.** They were never absent. The
salvage checkpoint held their bytes all along, byte-identically; what it lacked
was the record-layer rows that point at them, so the builder asked the only index
it had, was told nothing, and refused honestly. It had been refusing an index gap
and calling it an absent document.

Nothing is published. The bundle is data and lives outside the repository; what
is committed is the pin, the floor calibration, the builder, its tests, and the
receipt beside this file.

## Identity

```text
releaseId            urn:docspec:document-release:v2:5f2ae16efda2d582b0131e8eb06583e19d04693eeb64f8b16dadae98b4a7708b
documentStateDigest  sha256:5f2ae16efda2d582b0131e8eb06583e19d04693eeb64f8b16dadae98b4a7708b
buildRunId           fr-mirrulations-10k-v1-full
releaseStatus        candidate
generation           docspec
```

Derived from the state digest by string form, not minted a second time. The URN
prefix stays `v2`; `RELEASE_FORMAT_VERSION` stays `2.0`. Nothing in this round
touched either, and B7's rule that flipping one must not move the other is
unexercised and unbroken.

The name moved from the second mint's `28a276a3…` because the corpus did: 193
documents entered it, 220,582 segments replaced 219,212, and the disposition
rows of 193 items changed from `unavailable` to `selected`. Every one of those is
a logical fact the `/3` domains frame, so the name had to move and did.

## What it read

```text
corpus pin      urn:docspec:qualification-corpus-pin:v1:88dcaeb696358b23810a6c927e0437c603d95aaecb3f6a92ffb829bdedb88011
catalog         urn:docspec:source-catalog:v1:973aaa197206821869294deb09b3cb6281d9bd55ab265214026a48c71fc7d094
catalog digest  sha256:ded6649aab3f04faa6a48f867de0854648ec10c04fcdad8f6527e075d97c45d6
rescue map      sha256:41fc5eb645e7e7d85b3cd138f23eca7c4a0045761065c6ead75ce2c46619c10e
                full-store-map-v2.jsonl.gz
tier            full, 10,000 items
draws           federalRegister sha256:c8b42519…, mirrulations sha256:48b2eb86…
```

The pin identity changed from the second mint's because the pin gained a fourth
digest-pinned input. The catalog, its digest, both draw digests, and the tier are
byte-for-byte the ones D1 fixed.

**The rescue map is a second POINTER, not a second source of bytes.** Three rules
make it a rescue rather than a fetch, and the builder enforces all three:

1. it is consulted **only** where the checkpoint's own record layer has no
   pointer for that `(sourceItemId, candidateId)`, so it can never override a
   checkpoint record;
2. the blob must **already be in the pinned checkpoint** — a map row naming bytes
   the checkpoint does not hold supplies nothing and is dropped. This builder
   makes no request, and a rescue that had to reach for bytes would be a fetch
   wearing another name;
3. the returned copies go through the **same mandatory size-and-digest check**
   every preserved copy does. A mismatch is `capture.preserved-copy-unverifiable`
   — a capture failure with a disposition and a reason, never a reason to fetch.

Measured against the real checkpoint, the campaign's complete store map reduces
under rules 1 and 2 to **exactly the index gap**: 194 items, 387 blobs, and no
second pointer for anything already captured. It is the file that is pinned
rather than the curated 194-item rescue list because that list carries no
acquisition clock, and `captureRecord.acquiredAt` is required and non-nullable —
a rescue from it would have to invent 387 wall clocks. The store map carries the
record layer's own `acquiredAt`, `2026-08-06T12:00:00Z`, which is the same value
all 9,774 of the checkpoint's own records carry; it records no start instant, so
`acquisitionStartedAt` is **null**, which is what a nullable field is for.

```text
adopted from the checkpoint's record layer   8,091
adopted from the rescue map                    193
refusals from v1 and v2 thereby corrected      193
```

## What it wrote

```text
requestedUniverseCount   10,000
selectedCount             8,284      (v2: 8,091)
unavailableCount          1,716      (v2: 1,909)
excludedCount / failedCount / deletedCount   0 / 0 / 0
documentVersionCount      8,284
structuralNodeCount     226,110
searchSegmentCount      220,582
memberCount                 207
totalMemberByteSize   839,551,148
```

```text
coverage   representationByteTotal  183,193,476
           segmentedByteTotal       176,176,907
           excludedByteTotal          7,016,569
           accountedCount                10,000   unaccountedCount 0
           documentsWithSegmentCount      8,284
```

`segmentedByteTotal + excludedByteTotal == representationByteTotal` holds in
aggregate and per kind. Set digests:

```text
sourceDispositionSetDigest    sha256:4d26f52497e45bd326b42a20d64ced51d10f979b73fa9b86cd103b6eff6d6cd6
documentVersionSetDigest      sha256:87bfaed7b6ad326d17efa756aaf8fc7549f7a82c8a2a6e2ca396c1aad9bcbc3b
structuralNodeSetDigest       sha256:658d2478c099caa02b60870aaabcae4bbd8e688393656ed831615aa8e7a2508f
segmentSetDigest              sha256:9ec388145a9fb1d5e34f0299039f43d879c3801c6e2628e88843be61f72d7796
attachmentSetDigest           sha256:fa2bc9775dc9f0e49b3fb5bc3e3966181bfb1e1159360db9977053b2a8c904c1
commentSetDigest              sha256:49fb8b432cc0c28459e68656fc1f0aca2b10aed521a0dd015bb208553cab60d2
textBodySetDigest             sha256:ea7be60f2c9619072b9d717e8800a37fc2cf2af73036bcaf6d3db9cf4ac745f7
sourceDocumentMappingDigest   sha256:b1d746f4143aeb0e4e3d8adbf94e23976b0b5b6c7914b97e7fb1207421e3c4e0
selectedSourceSetDigest       sha256:7357b47d3f10766ad5a8bff7184c4ba57f8986c5bd051f6bfb5a94b96b252cab
```

**`selectedSourceSetDigest` is unchanged from the second mint, and that is the
point.** It names what the pinned *catalog* selected — all 10,000 active items —
and the rescue changed what this producer could carry, not what the catalog
chose. Under amendment C3 the gate now recomputes it from the release's own
disposition rows and requires the two to agree; it does, and it did for the
second mint too, which is how B6's claim that the value was not gate-checkable
was found to be false.

## Processing policies, keyed the way the rows are

```text
document-body  text/xml    docspec.xml-visible-text/v1   floor 0.33 under observed 0.453
                           population fr-body-retrieval-2026-08-02:distribution-xml
document-body  text/html   docspec.html-visible-text/v1  floor 0.17 under observed 0.2382
                           population fr-body-retrieval-2026-08-02:distribution-html
```

The second mint declared these under `application/xml` and `text/html`. The
floors are the same floors, calibrated on the same populations through the same
extractors, under the same collapsed format key — what changed is the label the
release publishes, which is now the media type its capture rows carry. The
collapse survives in exactly one place, `retention_floors.format_key`, and only
on the lookup side: a floor is a property of a parser and a document family, a
policy row is a property of the bytes a release carries. Where two spellings of
one family are both present the release will declare two rows with the same
extractor, segmenter, and floor; here only `text/xml` occurs, so there are two
rows for two families rather than three.

The gate now matches `(textKind, capture.mediaType)` **literally**. Every one of
this release's 8,284 document rows is governed by a declared policy under the
name it carries.

## The rescued 193, measured rather than assumed

All 193 have a preserved `rendition-html` and a preserved `metadata-json` in the
checkpoint, and all 193 are `selected`. **The floors applied to them exactly as
to everyone else — no tuning, no exemption.** They cleared comfortably: the
lowest of the 193 measures 0.8444 against a `text/html` floor of 0.17, and the
whole HTML population's minimum is 0.7933, which is a document that was already
in the second mint. None was close.

```text
retention  text/xml    6,408 documents   min 0.4558  median 0.8839  max 0.9578
           text/html   1,876 documents   min 0.7933  median 0.9734  max 0.9981
```

The 194th rescued item, `SEC-2020-1944-0001`, captured `metadata-json` only. It
has no body rendition anywhere in the checkpoint and stays `unavailable` with
`capture.no-preserved-copy` — the honest answer, and the one the rescue does not
change.

## Attachments, recomputed

```text
attachmentRows       1,876   (v2: 1,683)
renditionRows        3,752   (v2: 3,366)
textCaptured             0
textExcluded         1,876   the owning body's own `htm` rendition (B4)
sourceUnavailable    1,876   the `pdf` sibling, never preserved
extractionFailed         0
```

The growth is exactly the 193: every one is a Mirrulations document whose
preserved `metadata-json` enumerates a `pdf` and an `htm` in `fileFormats`, which
is the flat document packing Decision 0001 names — one attachment, two
renditions. The `htm` is matched to the owning body by media type **and** by the
size the source declared, and all 193 matched, so all 193 are `text-excluded`
rather than mistaken for an unpreserved file. `counts.attachmentAccounting` is
recomputed from the rows by the builder and again by the gate; nothing here is
asserted.

## What it refused, and why

```text
capture.no-preserved-copy   1,716
```

One reason code, and 1,716 is 1,909 minus the 193. Every other refusal the
vocabulary declares was reachable and none fired: no item fell below its floor,
none failed to parse, none failed to segment, none had incomplete metadata, and
none lacked a markup rendition. The remaining 1,716 have no bytes anywhere in the
checkpoint and no row in the rescue map that names bytes it holds; recovering
them needs an acquisition run, which is a separate decision.

## Verification

`src/docspec/adapters/document_release_verify.py`, whole bundle, generation
`docspec`: **`valid`, 0 diagnostics**. Build 98 s, verify 92 s, 190 s total.

**Remint-idempotence, measured again.** Two independent full runs, in separate
processes and separate trees, with different `publishedAt` annotations
(`2026-08-31T00:00:00Z` and `2027-04-04T04:04:04Z`), reach the identical
`documentStateDigest`, `releaseId`, nine set digests, `counts`, `coverage`,
`attachmentAccounting`, `processingPolicies`, and rescue-map record — and, member
by member, the identical 207 `sha256`/`byteSize`/`recordCount` triples. The
annotations differ and the name does not.

The twenty sealed predecessor bundles verify unchanged and byte-untouched,
20/20. The docspec conformance corpus is **29** bundles now, up from 24: five
rules that were prose in the decision are fixtures in the corpus.

## What the gate learned this round

| | |
| --- | --- |
| **Policy governance is literal.** | `(textKind, capture.mediaType)` must appear in `processingPolicies` as written. `invalid/ungoverned-media-type` is the fixture the rule never had — B4's second arm shipped untested, and shipped broken. |
| **`selectedSourceSetDigest` is recomputed.** | From the disposition rows, excluding the two `catalog.state-*` codes that mean the catalog itself did not select the item. `invalid/selected-source-set-digest`. It caught something the first time it ran: `--universe-sample` declared a pin digest over 10,000 items while its bundle carried 200 rows, and the sample now attests to the universe it has. |
| **Both reason-code vocabularies are enforced.** | B7 closed them in prose and nothing read the lists — the proof being `unmapped-rendition-format`, a code the builder could emit that sat outside them and that nobody noticed. `invalid/unknown-disposition-reason-code` and `invalid/unknown-attachment-reason-code`. A test reads the decision's own fenced blocks back, so a transcription drift fails rather than sits. |
| **The corpus mints a comment.** | B4 required it; B4's own round landed the attachment and left `data/comments.jsonl` at zero bytes. `invalid.comment-selection` leaves `UNMINTABLE_CODES` with it — and the recorded reason it was there was wrong about its own premise: a comment reaches a release as a text body of a member, not as a member. |
| **The calibration receipt is recomputed.** | Every row's two ratios from its own byte counts, the whole distribution, the lowest document, and the floor the margin rule implies. A receipt agreeing with itself and with none of its 993 rows used to pass, which is the exact shape of what B5 found. |

## Where things are

```text
output/document-release-10k-v3/                     the bundle (886 MB, not tracked)
output/document-release-10k-v3-mint-receipt.json    the receipt as minted
docs/history/2026-08-31-document-release-10k-v3-mint-receipt.json   its committed copy
fixtures/fr-mirrulations-10k-v1/pins.json           the corpus pin, now with the rescue map
fixtures/retention-floors/calibration.json          the floor calibration (unchanged)
tools/fr_mirrulations_pin.py                        the pin, and `rescued_captures`
tools/build_document_release.py                     the builder
src/docspec/adapters/document_release_verify.py     the gate
tests/test_document_release_builder.py              26 tests
```

The rescue map is read from `DOCSPEC_QUALIFICATION_RESCUE`, defaulting to
`~/Work/corpora/_rescue-2026-08-31-qualification-store-map`. A pin that declares
a rescue map it cannot read **refuses** rather than continuing: a mint that
quietly dropped 193 documents under the same pin and the same procedure is the
silent difference the pin exists to prevent.

Rebuild it with:

```sh
uv run python -m tools.build_document_release \
  --output output/document-release-10k-v3 \
  --receipt output/document-release-10k-v3-mint-receipt.json
```

## Recorded departures and open items

| | |
| --- | --- |
| **The disposition mapping is still the builder's.** | Unchanged, and unchanged in kind: the pinned snapshot's item vocabulary is `active` / `deleted` / `excluded` and carries no `selected`, so there is nothing to project. |
| **The source reason-code vocabulary carries four codes no real mint emits.** | The conformance corpus's producer projects its pinned catalog's own `selection.reasonCode` verbatim, and that catalog's four are none of B7's. Both behaviours are right for their producer, and a closed list that cannot spell the only sealed corpus in existence is a list the gate cannot turn on. They are a named second group in amendment C4, not a widening of what the 10k builder may mint. |
| **`unmapped-rendition-format` is declared and unreached.** | Every `fileFormats` entry in this corpus is `pdf` or `htm`. The code is right and stays; C4 records why rather than deleting it to tidy a list. |
| **`attachmentAccounting.extractionFailed` is zero, and untested end to end.** | Unchanged from the second mint. The checkpoint preserves no attachment renditions beyond the bodies' own, so nothing was fetched and then refused. |
| **1,716 documents still have no bytes.** | Down from 1,909, and the remainder is not an index gap: the rescue map names no blob for them that the checkpoint holds. Recovering them needs an acquisition run. |
| **The rescue map is a local salvage artifact.** | It is pinned by path and digest like the checkpoint, and like the checkpoint it is not in this repository. A machine without it cannot mint this release, and says so at the pin rather than minting a smaller one. |
| **`agencyName` still repeats `agencyId`.** | Neither source supplies a display name, so none is invented. |
| **Floors sit outside the identity preimage.** | Recorded in amendment C7 rather than left to be assumed: `documentStateDigest` does not bind the governing floor, because `processingPolicies` is excluded from `logical_content` by the decision's own ruling. What binds a floor is the calibration receipt, sealed by its own schema and now recomputed from its rows, and the `processingPolicies` member itself, which is digest-pinned in the manifest and cross-checked row by row. |
