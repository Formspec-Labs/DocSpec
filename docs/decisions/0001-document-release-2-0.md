# Decision 0001: DocumentRelease 2.0 — reconciliation and the text-member extension

- Date: 2026-08-30
- Status: accepted for local implementation; nothing minted, nothing published
- Supersedes: the `docspec-document-release` `1.1` root shape
  (`src/docspec/domain/release.py:21-22`) as DocSpec's publication surface, and
  the nineteen-field `release.json` shape at
  `docs/superpowers/specs/2026-08-05-docspec-standalone-platform-implementation-spec.md:1439-1444`.
  It does not supersede §7.5 of that spec; it implements it.
- Decides the open question left by
  `docs/superpowers/specs/2026-08-30-document-release-2.0-adoption.md` §9
  ("Reconciling the live record with this contract … is the next decision").

This is DocSpec's first decision record. The conventions it establishes: a
one-paragraph **Decision** that stands alone; every obligation stated as a
checkable rule against a named file and line; a **Sealed identities** section
that fixes schema `$id`s and digest domains so a later reader can tell a rename
from a redefinition; and an explicit **What this does not decide** so an absent
rule is a recorded absence rather than an oversight.

## Decision

DocSpec adopts the portable `DocumentRelease` 2.0 bundle as its one publication
surface, replaces `1.1` rather than migrating it, and closes all thirteen
deviations in one builder rather than staging them, because none is already
satisfied and the four cheap ones are cheap only when the other nine have
already reshaped the record. Identity is settled with two names over one
content: the artifact keeps its `rulespec_artifacts` derivation-envelope
`releaseId` (`urn:spicy:artifact:derivation:<64hex>`, `release.py:24,48`) — the
container mints names, DocSpec never does — and gains a
`documentStateDigest` over the canonical `{format, formatVersion, content}`
preimage, which the envelope's `spec` then carries, so that a name which today
is a function of the processing plan alone becomes a function of the plan *and*
the corpus content. The first mint pins the 10k checkpoint's own sealed catalog,
one release to one `SourceCatalog`, and carries attachments and comments inside
that same release as `textBody` members that reuse the document body's capture,
representation, structural-node, segment, excluded-range, and evidence rules
unchanged — one text pipeline with three kinds, not three pipelines. Extraction
stops counting and starts refusing: every parser declares a retention floor
measured on a named population before it may run, and an undeclared floor or an
unmeasurable source fails closed. Nothing is minted in 2.0 yet, so the twenty
sealed conformance bundles restamp under DocSpec's schema `$id`s and this
identity block as the builder's first obligation, before it touches real corpus.

## Given: two owner-delegated decisions

**D1 — the first mint pins the 10k checkpoint's own sealed catalog.** The
catalogs ship inside
`~/Work/corpora/_salvage-2026-08-28/docspec-qualification/fr-mirrulations-10k-v1/source-catalogs/`
and are digest-pinned by that checkpoint's `catalog-set.json`
(`campaignId: fr-mirrulations-10k-v1`, 10,000 documents from 13,592 candidates:
6,408 Federal Register `text/xml` and 3,592 Mirrulations `text/html` +
`application/json`, each parent draw pinned by digest). Rationale: a
`DocumentRelease` pins the exact catalog its documents were drawn from. That is
already structural, not conventional — `DocumentRelease.source_catalog` is one
`SourceCatalogRef`, not a tuple (`src/docspec/domain/release.py:33`), and the
derivation envelope builds exactly one input with role `source` from
`plan.source_catalog` (`src/docspec/adapters/platform_artifact.py:253-259`).
Pinning any other catalog would mint a release whose disposition projection over
`U` describes a universe its documents did not come from.

**D2 — `rulespec_artifacts.canonical_json_bytes` mints identity; product code
never does.** The two canonicalisers were measured disagreeing on four of five
inputs, and both are already imported into one module —
`rulespec_artifacts.canonical_json_bytes as artifact_json_bytes`
(`platform_artifact.py:32`) beside DocSpec's own `canonical_json_bytes`
(`platform_artifact.py:41`, defined at `src/docspec/domain/identity.py:112`).
Rationale: the container owns identity. Two canonicalisers that disagree are two
identity functions, and a record that can be named by either has no name.

## The format

### Root fields

The 2.0 root is a closed object of exactly six keys:

```text
format               "docspec-document-release"
formatVersion        "2.0"
releaseId            urn:spicy:artifact:derivation:<64 hex>   (container-minted)
documentReleaseId    urn:docspec:document-release:v2:<64 hex> (bundle-portable)
documentStateDigest  sha256:<64 hex>
content              the closed content object
annotations          publishedAt, releaseStatus, buildRunId
```

`annotations` is outside every preimage. Two builds of identical corpus content
share one `documentStateDigest` and one `documentReleaseId`.

This reconciles the nineteen-field shape at spec L1439-1444 against the
seventeen-key `to_dict` at `release.py:183-201`. The two missing keys were
`documentStateDigest` and `physicalShardPolicy`. The spec already named
`documentStateDigest` and the code never implemented it; 2.0 gives it a
definition and makes it load-bearing. `physicalShardPolicy` and `partitionPolicy`
are not carried: v2 does not partition (adoption spec §2), so a shard policy
would be a field with one legal value. Both return with the partitioning
decision, not before. The remaining 1.1 pointer-record fields — `activeLayers`,
`blobRoots`, `storeReceiptSetDigest`, `runReceipt`, `catalogCommitReceipt` — are
internal store addressing and do not cross the bundle boundary; the bundle
carries members, and a member manifest with per-member digests is what replaces
them.

### Member set

```text
release.json                                 root manifest
manifests/global.json                        the one member manifest
schemas/*.schema.json                        the eight schemas, digest-pinned
data/source-dispositions.json                one row per member of U
data/documents.json                          one row per document/version
data/attachments.json                        NEW — one row per enumerated attachment
data/comments.json                           NEW — one row per selected comment
data/structural-nodes.json                   source-derived structure, all kinds
data/search-segments.json                    bounded segments, all kinds
blobs/<textBodyId>.<ext>                     exact captured rendition bytes
text/<textBodyId>.txt                        the selected visible-text representation
```

`blobs/` and `text/` are keyed by `textBodyId` rather than `documentId`
(adoption spec §2), because a document body, an attachment, and a comment all
produce one captured rendition and one representation and must not collide.

Member manifest roles gain `attachments` and `comments`. Every member path is a
checked relative POSIX `objectKey` and every member carries `byteSize` and
`sha256`.

### Identity minting (D2)

Three names, one content, one minter:

1. `documentStateDigest` = `sha256:` over
   `rulespec_artifacts.canonical_json_bytes({"format", "formatVersion",
   "content"})`. This is the adoption spec §3 preimage verbatim, including both
   tokens, so a future reshape of the same fields cannot mint a colliding name.
2. `documentReleaseId` = `stable_urn("document-release", …, version=2)`
   (`identity.py:205`) over that same digest —
   `urn:docspec:document-release:v2:<hex>`. This is the *portable* name: a
   consumer holding only the bundle recomputes it from the bundle bytes with no
   container and no Rulespec checkout, which is what deviation row 13 is for.
3. `releaseId` stays the derivation logical ID minted by
   `expected_logical_id` over `{format, formatVersion, inputs, kind, spec}`
   (`platform_artifact.py:262-270`), and the `release.py:48`
   `urn:spicy:artifact:derivation:[0-9a-f]{64}` regex is preserved unchanged.
   This is the *artifact* name.

**The one change that closes row 3.** `derivation_spec(plan, partition_policy)`
must include `documentStateDigest` in the `spec` it returns. Today the live name
is a function of the plan alone: two builds of identical corpus content under
different plans get different names, and content that changes under one plan
gets the same name. Adding the content digest to `spec` makes the envelope id
move whenever the corpus moves, while leaving it plan-sensitive — which is
correct for an artifact name and wrong for a content-equality test. That is why
there are two: `releaseId` answers "is this the same artifact", and
`documentStateDigest` answers "is this the same corpus".

This is the reconciliation the porting task asked for, and it is cleaner than
carrying a content-derived `releaseId`: it keeps D2's principle exact (the
container mints, product code never), keeps the envelope discipline that
`release.py:48` already enforces, and still delivers the candidate's portable
content-derived name as a field a bundle-only consumer can check.

DocSpec's own `canonical_json_bytes` (`identity.py:112`) is retired from every
identity-bearing path in this format. Where 2.0 needs canonical bytes — root,
members, set digests, state digest — it calls `artifact_json_bytes`. A test must
prove no 2.0 code path reaches `docspec.domain.identity.canonical_json_bytes`.

### Canonical bytes (row 4)

Root and member bytes are exactly `artifact_json_bytes(value)` with **no**
trailing newline. `canonical_json_file_bytes` (`identity.py:119-122`) and the
`DocumentRelease.file_bytes` property (`release.py:225-226`) are removed from
the release path. This is not a tidy-up: the `invalid/noncanonical-root` fixture's
entire single-rule mutation is the addition of that newline, so today's
`file_bytes` output is byte-identical to a bundle the validator must refuse.

## The deviation resolution

All thirteen rows are live at `main@e8ee58f`. One line each for what changes.

| # | What changes in code |
| --- | --- |
| 1 | `DocumentRelease` stops being a pointer-record: the five store-addressing fields leave the root and are replaced by `manifests/global.json` with a complete digest-pinned member list and relative `objectKey`s only. |
| 2 | `counts`, `coverage`, `failures` become closed generated objects with named required recomputed fields; `freeze_json`/`thaw_json` passthrough at `release.py:57-68` is deleted, including the three fields that today get no value validation at all. |
| 3 | `derivation_spec` gains `documentStateDigest`; the root gains `documentStateDigest` and `documentReleaseId`; `releaseId` and its `release.py:48` regex are unchanged. Content-derived identity is *introduced*, not adjusted. |
| 4 | Release and member serialization calls `artifact_json_bytes` with no trailing newline; `file_bytes` and `canonical_json_file_bytes` leave the path. |
| 5 | A new `data/source-dispositions.json` member carries one row per member of `U` with the catalog disposition projected verbatim; the release stops implying membership from what is present. |
| 6 | `accepted-failure` and `rejected-run` stay in `AcquisitionDisposition`/`ProcessorDisposition` (`content.py:29,38`) but the 2.0 build gate refuses any release that emits either for a selected item — conformance is a producer that never emits them, not a narrower type. |
| 7 | A new `structuralNode` record type with `structuralParentId`, `depth`, dense sibling `ordinal`, and containment; `Segment` (`content.py:652-665`) gains `structuralParentId` and `headingPath`. |
| 8 | `Representation.warnings: tuple[str, ...]` (`content.py:492`) is replaced by `excludedRanges` with byte range, machine-legible `reasonCode`, and presentable prose `reason`. |
| 9 | The release carries `selectedSourceSetDigest` (the catalog's, not recomputed), `documentVersionSetDigest`, `segmentSetDigest`, `sourceDocumentMappingDigest` and the sealed `joinReceipt`; the existing catalog-side `selected_source_set_digest` (`adapters/source_catalog_artifact.py:375`) is projected rather than duplicated. |
| 10 | `CapturedFile.acquired_at` (`content.py:236`) leaves the capture record; publication time lives once, in `annotations`. A clock inside a content-derived identity would make two byte-identical captures two releases. |
| 11 | `require_relative_path` (`identity.py:42-49`) — which DocSpec already owns and simply does not apply — is applied to every `objectKey`; `ArtifactRef.locator`/`BlobRef.locator` (`references.py:21,57`) stop being bare `require_text`. |
| 12 | `EvidenceCoordinate` (`content.py:384-390`) makes `start` and `end` required and renames `source_digest` to `renditionSha256`; see *Coordinates* below for the third coordinate system attachments need. |
| 13 | `to_dict` gains `schemaSet`, and the eight schemas ride inside the bundle digest-pinned, so a consumer verifies with no Rulespec checkout. |

Sequencing note: the adoption spec's 9-breaking / 4-satisfiable split is real but
is not a staging plan. Rows 2, 6, 12, and 13 are cheap only after rows 1, 5, 7,
8, and 11 have reshaped the records they tighten. One builder closes all
thirteen.

## The extension: attachments and comments

Attachments and comments have zero schema surface anywhere in DocSpec today —
no domain type, no schema, no enum member. This section designs them.

### The shape: one text body, three kinds

Rule M7 puts them in the **same** per-catalog `DocumentRelease`, 1:1 with its
`SourceCatalog` (D1). They are therefore not a second release, not a second
identity, and not a second pipeline.

A **text body** is anything that has captured bytes, exactly one selected
Unicode representation, source-derived structure, bounded segments, and an
excluded-range ledger. A document body is one. So is an attachment. So is a
comment. Every text body carries:

```text
textBodyId      unique across the release
textKind        document-body | attachment | comment
capture         the existing captureRecord $def, unchanged
representation  the existing representation $def, unchanged
excludedRanges  the existing excludedRange $def, unchanged
```

`textKind` is exactly `TEXT_SOURCE_KINDS` minus `metadata-field`
(`spicysearch/src/spicysearch/text_units.py:24`), which never comes from a
release. The consumer's vocabulary is already correct; nothing needs to move.

`structural-nodes` and `search-segments` change their foreign key from
`documentVersionId` to `textBodyId`, and gain `textKind`. This is a schema change
to a sealed 2.0 schema, and this is the only moment it is free: nothing has been
minted in 2.0, so there is no reader to break and no artifact to migrate. Making
attachments a bolt-on later would mean either a second segment record type or a
nullable key, and both make the consumer branch on kind for coordinates it should
never branch on.

### New members, roles, and ownership

`data/attachments.json` (role `attachments`) and `data/comments.json` (role
`comments`). Both are text bodies plus their own owning facts:

- **Attachment** adds `ownerTextBodyId`, `ownerKind` (`document-body` or
  `comment`), `renditionOrdinal` (source order, dense, zero-based),
  `attachmentTitle` (nullable), and `attachmentDisposition`.
- **Comment** adds `documentId` (the document it comments on),
  `sourceItemId`, `sourceIssuedVersion`, and `commentSelection` (below).

An attachment's owner is exactly one text body and never another attachment.
A comment's owner is exactly one document, and a comment may itself own
attachments; the ownership graph is therefore two levels deep and acyclic, and
the validator proves it.

### Per-kind rules

The live pipeline already proves the two kinds are packed differently at source
(`spicy-regs@main src/spicy_regs/enrich_pdf.py:16-19`): documents carry a **flat**
`[{url, format, size}, …]` and comments carry a **nested**
`[{title, formats: [{url, format, size}]}, …]`. That is a source-shape fact, and
the release must not inherit it. The two shapes are unpacked once, at ingest,
into one attachment record; `attachmentTitle` is non-null only where the nested
form supplied one, and `ownerKind` records which unpacking rule produced the row
so it stays reversible to its source JSON. Two packings in, one record out.

### Comment selection and tie refusal

DocSpec does not select comments. The sealed upstream policy already does
(`spicy-docs@2b643ef src/spicy_docs/regulations_gov_source_native.py:1509-1521`):

```text
groupBy         /data/id
orderBy         /data/attributes/modifyDate DESC NULLS LAST
tieDisposition  refuse-repeated-normalized-instant
```

Each comment row carries `commentSelection` — that policy's `groupBy`, `orderBy`,
`tieDisposition`, and its digest — projected verbatim, and the selected
observation's normalized `modifyDate`. The inheritance is the refusal, not the
ordering: **if the release build is handed two observations of one comment id, it
fails.** It does not re-order, tie-break, or pick the first. This is the same
rule as "a capture result never feeds a fact back into the catalog" (adoption
spec §4) — resolving a tie here would be DocSpec deciding a selection the source
owner refused to decide. New diagnostic `invalid.comment-selection`, ordered
immediately after `invalid.disposition`, because a comment whose selection is
ambiguous has no capture worth judging.

### Coordinates

Every segment already carries two coordinate systems and keeps both: a
**representation** range (`representationStart`/`representationEnd`, half-open
over UTF-8 bytes) for the consumer, and **rendition** evidence naming the
captured bytes by digest for reversibility. The consumer seam needs no
translation — `spicysearch/schemas/output/2.0/text-unit.schema.json` takes
`releaseId` ← the release's `releaseId`, `sourceRecordId` ← `textBodyId`,
`sourcePath` ← `text/<textBodyId>.txt`, `sourceStartUtf8Byte` ←
`representationStart`, `coordinateSystem` ← `representation-utf8-byte`, which is
already in its `COORDINATE_SYSTEMS` (`text_units.py:26`).

Rendition evidence needs a third system, because attachments are usually PDFs and
a byte offset into a PDF is not reversible to anything a reader can see:

| `coordinateSystem` | Required | Reverses to |
| --- | --- | --- |
| `rendition-utf8-byte` | `start`, `end` | a byte slice of UTF-8 captured text (HTML, XML, TXT) |
| `rendition-byte` | `start`, `end` | a byte slice of opaque captured bytes |
| `rendition-page-region` | `start`, `end`, `page`, `region` | a rectangle on a page of a paginated rendition |

`page` is zero-based. `region` is a closed object `{x0, y0, x1, y1}` of
**integers in permille of the page box**, not floats — DocSpec's canonical JSON
admits no floating point (`identity.py:112`), so a fractional rectangle inside an
identity preimage would be unencodable. `start`/`end` remain required under
`rendition-page-region` and address the extractor's own page-text stream, so the
rectangle and the text slice check each other.

### The excluded ledger keeps its text

The reader honesty rule (`spicysearch/docs/design/spicysearch-doc-reader.html`,
which renders an excluded region as its own bytes plus "not searched · bytes
31,120–31,466" plus a presentable sentence) requires that an excluded region stay
readable. So:

- An excluded range is a **search** exclusion, never a redaction. Its bytes stay
  in the representation member, `representationByteTotal` counts them, and a
  reader slices them out of `text/<textBodyId>.txt` by offset. No separate
  retained-text field is needed and none is added — a second copy could drift
  from the first.
- `reason` is prose fit to show a reader, not an internal token.
  `reasonCode` is machine-legible and drawn from a closed vocabulary the release
  carries, so a reader can style a class of exclusion without parsing prose.
- Both are required. The `invalid/missing-projection-reason` fixture already
  gates the disposition analogue; the excluded-range analogue joins it.
- The coverage identity `segmentedByteTotal + excludedByteTotal ==
  representationByteTotal` holds **per text body** and in aggregate, for all
  three kinds.

### Policy digests (avoid-lesson A7)

Today's extractor identifiers carry no digest — `TEXT_EXTRACTOR_ID`,
`HTML_EXTRACTOR_ID`, `XML_EXTRACTOR_ID`, `JSON_EXTRACTOR_ID`,
`IMAGE_EXTRACTOR_ID` are bare strings (`src/docspec/processing/extraction.py:35-39`),
and the conformance fixture's `processingPolicy` digests the enclosing policy but
not the `extractorId` or `segmenterId` inside it. Policy content can therefore
drift under an unchanged id.

`processingPolicy` becomes `processingPolicies`: a sorted array of closed records,
one per `(textKind, mediaType)`, each carrying `extractorId` **and**
`extractorDigest`, `segmenterId` **and** `segmenterDigest`, `maxSegmentBytes`,
and the retention floor that governed it. Per-kind is not optional — a
`document-body` HTML extractor and a `comment` PDF extractor are different code
with different floors, and one policy record for both would digest neither
honestly.

### Set digests and counts

New: `attachmentSetDigest`, `commentSetDigest`, `textBodySetDigest`. Existing:
`selectedSourceSetDigest` (the catalog's, projected, never recomputed under
another name — spec §7.5), `documentVersionSetDigest`, `segmentSetDigest`,
`sourceDocumentMappingDigest` and the `joinReceipt`. `counts` and `coverage` gain
per-kind breakdowns; a zero is written, never omitted.

## Acceptance gates

### Extraction floors (Q12): DocSpec refuses

DocSpec today **counts and never refuses** — `HtmlExtractor` records
`elementCount` and `visibleUnicodeCodepointCount`
(`src/docspec/processing/extraction.py:237-238`) and no threshold reads them. The
predecessor refused, in three separate ways, at the extraction boundary rather
than inside any one adapter
(`spicy-regs@integrate/payload-prereqs src/spicy_regs/docpipeline/source.py:1324-1372`).
2.0 adopts that posture:

A parse is refused when it is **below the declared floor**, when there is **no
declared floor for that parser and format**, or when the source's **visible text
could not be measured**. Undeclared is not "inherit a default": a new extractor
states the population its floor came from before it may run. On success the
measurement is written into the receipt, so a passing build says how far it
passed by.

A floor is `RetentionFloor(value, unit, observed_minimum, population)`
(`source.py:975-1000`) with two invariants that carry over unchanged:
`0 < value < 1` (a floor outside that either gates nothing or refuses
everything), and `observed_minimum > value` (a floor with no margin under the
lowest legitimate document is a future false refusal). Units are the
visible-text fraction for markup and text density for binary renditions; they
are different measurements and never share a floor. Media types collapse onto
one format key before lookup (`text/xml`, `application/xml`, `*+xml` are one
population), so the same document is not gated differently by which header the
publisher sent.

Floors are **calibrated per parser and format on the preserved body corpora**,
not carried over as numbers:

- `~/Work/corpora/_preserved-2026-08-10/body-retrieval-corpus-2026-08-02` —
  5,971 files: 995 `distribution-html`, 995 `distribution-xml`, and 1,988 each of
  `cache`/`cache-xml`, with the draw manifest and both measurement records
  alongside. This is the HTML/XML population.
- `~/Work/corpora/_salvage-2026-08-28/spicysearch-output/rule-bodies-pre2000-2026-08-22`
  — 39,786 bodies with `manifest.json` (per-body carrier, bytes, sha256) and a
  one-line `failures.jsonl` whose single entry is a 404, i.e. an acquisition
  failure, not an extraction one. This is the pre-2000 HTML population and the
  hardest case for a visible-text floor.
  *(Path correction: the directory is `rule-bodies-pre2000-2026-08-22`.)*

The predecessor's four floors — `native:text/html` 0.75 against observed minimum
0.9453, `native:application/xml` 0.85 against 0.9930, `pypdf:application/pdf`
0.005 against 0.015584, `pymupdf:application/pdf` 0.005 against 0.015571 — are
**evidence of the method, not adopted values**. They were measured through the
predecessor's extractors; DocSpec runs different ones. Every floor is re-derived
against DocSpec's own extractor on the named corpus, and the derivation receipt
records parser, format, population, distribution, observed minimum, chosen value,
and margin. There is deliberately no floor for a format nobody measured: that
parser has no floor and is refused, which is the intended fail-closed shape.

### Zero accepted failures

§7.5's "no accepted processing failure" binds the **catalog universe `U`**. Its
members are source items, and its guarantee is the structural bijection: a
`selected` row carries a `documentVersionId`, any other disposition carries
`null`, and a selected item that cannot be made searchable **blocks the build**
rather than becoming a silent downstream exclusion. That is unchanged for
document bodies, and it extends unchanged to comments where the pinned catalog
selected them as members of `U`.

Attachments are not members of `U`. They are renditions of a member, and one
unparseable PDF among thirty on one comment must not fail a 10,000-document
mint. They get accounting instead of a floor: **every attachment enumerated by
its owner's source record gets a row**, with `attachmentDisposition` ∈
`captured | excluded | unavailable | failed`, and anything but `captured`
carries `reasonCode` + presentable `reason` and a null `textBodyId`. The build
fails when an enumerated attachment has no row — never when a row honestly says
the attachment could not be captured. This is the attachment-level analogue of
the disposition projection: loss stays visible instead of being accepted
silently, and it stays out of the bijection that guarantees corpus membership.

`COMPLETE-SEARCH-CORPUS` proves, over all three kinds: the set and mapping
digests, gapless visible-text accounting, exact evidence round trips,
expected-digest refusal, zero accepted failure for `U`, complete attachment
accounting, and verification from a clean installed package with no
source-producer checkout.

## Migration

There is none to perform. Nothing has been minted in 2.0, and 1.1 is replaced
rather than migrated — no compatibility reader, no dual-write, no conversion
tool. `RELEASE_FORMAT_VERSION` stays `"1.1"` until the builder lands and then
becomes `"2.0"` in one commit.

**The builder's first obligation, before it processes any real corpus, is to
restamp the twenty sealed conformance bundles.** They are sealed evidence: every
bundle's `manifests/global.json` and `release.json` `schemaSet` carry the
`https://rulespec.org/...` `schemaId` strings, and those strings sit inside the
digest preimages that `corpus.json` seals with `treeSha256`. Rewriting them by
hand would invalidate every member digest, the manifest digest, the
`schemaSetId`, the member `byteSize` counts, the `releaseId`, and the tree digest
of all twenty. They restamp by a builder or not at all (adoption spec §8).

The restamp is **one** operation covering four changes, because each of them
moves the same digests:

1. the six schema `$id`s move to `urn:docspec:schema:document-release-*:2.0`
   (adoption spec §8 table);
2. two schemas are added — `attachments` and `comments` — and
   `structural-nodes`/`search-segments` change their foreign key to `textBodyId`;
3. the root gains `documentStateDigest` and `documentReleaseId`, and the bundle
   bytes lose nothing (they already carry no trailing newline — the valid
   fixture parses under `parse_canonical_json(..., file_form=False)`);
4. the six schemas' `description` prose saying Rulespec owns them, and the root's
   citation of REF-024, are corrected to DocSpec ownership under REF-048. That
   prose is stale, and it was left byte-faithful in the port only so the diff
   showed one changed line per file.

The invalid-bundle corpus grows by the new diagnostics
(`invalid.comment-selection`, `invalid.attachment-accounting`,
`invalid.retention-floor`), each still a single-rule mutation of the valid bundle
with every downstream digest, count, coverage figure, and identity restamped, and
every byte offset derived from the fixture's own bytes.

The Rulespec candidate record
`release-records/document-release-v2-candidate.json` is not ported and is not
recreated. Its DocSpec equivalent — a record pinning the eight schemas, the
validator modules, and every sealed bundle — is part of the builder's work.

## What this decision does not decide

- **Serving policy.** Whether an attachment or comment segment may generate
  candidates, affect ranking, or appear in a response is SpicySearch's, under its
  Decision 0007 rule that processing permission is not serving permission. This
  release makes the records; it authorizes no lane.
- **Scale beyond the 10k checkpoint.** v2 does not partition. `partitionPolicy`
  and `physicalShardPolicy` return with a measured partitioning decision, and the
  first mint's feasibility numbers are that decision's input, not its conclusion.
- **Whether 1.1 artifacts exist anywhere that must be read.** This decision
  replaces the format; it makes no claim about historical bytes on disk.
- **Which extractor DocSpec runs for PDF.** The floor mechanism is decided; the
  parser choice and its measured floor are the calibration receipt's output.
- **The concrete `reasonCode` vocabularies.** Their closure is required here;
  their members are sealed with the builder against real corpus, because a
  vocabulary invented before the corpus is a vocabulary that will be wrong.
- **Retention of non-selected renditions.** A capture keeps one representation
  per text body; whether other candidate renditions are retained as blobs is a
  storage decision.

## Sealed identities

Schema `$id`s, packaged at `src/docspec/schemas/document_release/2.0/` — version
in the directory, no version suffix in the filename, `$id` a
`urn:docspec:schema:<name>:<version>` URN:

| Member | `$id` |
| --- | --- |
| `document-release.schema.json` | `urn:docspec:schema:document-release:2.0` |
| `member-manifest.schema.json` | `urn:docspec:schema:document-release-member-manifest:2.0` |
| `source-dispositions.schema.json` | `urn:docspec:schema:document-release-source-dispositions:2.0` |
| `documents.schema.json` | `urn:docspec:schema:document-release-documents:2.0` |
| `structural-nodes.schema.json` | `urn:docspec:schema:document-release-structural-nodes:2.0` |
| `search-segments.schema.json` | `urn:docspec:schema:document-release-search-segments:2.0` |
| `attachments.schema.json` *(new)* | `urn:docspec:schema:document-release-attachments:2.0` |
| `comments.schema.json` *(new)* | `urn:docspec:schema:document-release-comments:2.0` |

The first six are already packaged at `main@a5f21eb` and carry these `$id`s; the
fixtures still carry the `https://rulespec.org/...` strings until the restamp.

Identity URNs:

| Name | Form | Minted by |
| --- | --- | --- |
| `releaseId` | `urn:spicy:artifact:derivation:<64 hex>` | `rulespec_artifacts.expected_logical_id` |
| `documentReleaseId` | `urn:docspec:document-release:v2:<64 hex>` | `stable_urn` over `documentStateDigest` |
| `documentStateDigest` | `sha256:<64 hex>` | `rulespec_artifacts.canonical_json_bytes` over `{format, formatVersion, content}` |

Digest domains. 2.0's projections changed — the structure and segment members
are keyed by `textBodyId`, not `documentVersionId` — so 2.0 declares its own
domains at `/2` and the spec §7.5 `/1` domains are superseded for this format:

```text
docspec-document-set/2          {documentId}                          by documentId
docspec-document-version-set/2  {documentVersionId}                   by documentVersionId
docspec-text-body-set/2         {textBodyId, textKind}                by textBodyId
docspec-attachment-set/2        {attachmentId}                        by attachmentId
docspec-comment-set/2           {commentId}                           by commentId
docspec-representation-set/2    {representationId}                    by representationId
docspec-segment-set/2           {segmentId}                           by segmentId
docspec-source-to-document/2    {sourceItemId, documentId,
                                 documentVersionId}                   by sourceItemId
```

Each calls the installed Rulespec `framedSectionDigest` with one `members`
section. Every key is unique; the declared count must equal the streamed count;
no profile defines another set-digest algorithm.
`sourceDocumentMappingDigest` remains a **list** digest over the sorted
`[sourceItemId, documentVersionId]` pairs — the pairing is the fact, so a
repeated pair moves the digest rather than being folded away — and
`selectedSourceSetDigest` is the pinned catalog's own value, projected and never
recomputed under another name.
