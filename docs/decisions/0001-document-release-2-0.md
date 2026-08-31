# Decision 0001: DocumentRelease 2.0 — reconciliation and the text-member extension

- Date: 2026-08-30
- Status: accepted for local implementation; nothing minted, nothing published
- Accepted-by: agent (delegated scope: owner 2026-08-30 "execute phase 1... you for human-less decision making"; D1/D2 choices were made by the lead agent and NOT individually shown to the owner)
- Supersedes: the `docspec-document-release` `1.1` root shape (`src/docspec/domain/release.py:21-22`) as DocSpec's publication surface; the nineteen-field `release.json` shape at `docs/superpowers/specs/2026-08-05-docspec-standalone-platform-implementation-spec.md:1439-1444`; and **that spec's `documentStateDigest` definition at `:1507-1533`** — the `framedSectionDigest` call with domain `docspec-document-state/2` over eight ordered sections, four of which (`processingPlan`, `activeLayers`, `retentionDispositions`, `partitionPolicy`) have no counterpart at all in a 2.0 bundle, and a fifth (`sourceCatalog`) survives only in part: its store-addressed half — `catalogStateDigest`, the catalog schema digest, the catalog-policy digest — does not exist here. Only `counts` and `coverage` come across whole; `failures` leaves with the rest. That block is dead for this format; the preimage below replaces it. It does not supersede §7.5; it implements it.
- Decides the question left open by `docs/superpowers/specs/2026-08-30-document-release-2.0-adoption.md` §9.

Conventions: a standalone **Decision**; every obligation a checkable rule against
a named file and line; **Sealed identities** fixing schema `$id`s and digest
domains; **What this does not decide**, so an absent rule is a recorded absence;
and an `Accepted-by:` line on every decision made inside.

## Decision

DocSpec adopts the portable `DocumentRelease` 2.0 bundle as its one publication
surface, replaces `1.1` rather than migrating it, and closes all thirteen
deviations in one builder, because none is already satisfied and the four cheap
ones are cheap only after the other nine have reshaped the record. Identity is
**two names over one content**: the derivation envelope's `releaseId`, and a
`documentStateDigest` over the bundle's *logical* content which the envelope's
`spec` then carries; the portable bundle name is *derived* from that digest by
string form, not minted a second time. The first mint pins the 10k checkpoint's
FULL-tier sealed catalog, one release to one `SourceCatalog`, and carries
attachments and comments in that release as `textBody` members reusing the
document body's capture, representation, structural-node, segment,
excluded-range, and evidence rules unchanged — one text pipeline, three kinds.
Extraction stops counting and starts refusing: every parser declares a retention
floor measured on a named population before it may run; an undeclared floor or an
unmeasurable source fails closed. Nothing is minted in 2.0, so restamping the
twenty sealed conformance bundles is the builder's first obligation.

## Given: two delegated decisions

**D1 — the first mint pins the 10k checkpoint's FULL-tier sealed catalog.**
`catalog-set.json` in
`~/Work/corpora/_salvage-2026-08-28/docspec-qualification/fr-mirrulations-10k-v1/`
carries three tiers (`smoke` 100, `intermediate` 1,000, `full` 10,000). The
`full` tier, verbatim:

```text
catalogId  urn:docspec:source-catalog:v1:973aaa197206821869294deb09b3cb6281d9bd55ab265214026a48c71fc7d094
digest     sha256:ded6649aab3f04faa6a48f867de0854648ec10c04fcdad8f6527e075d97c45d6
locator    source-catalogs/42/42890dab2fadf68b0a710e8e155293101830645ef0526d0404e6c76fb4f82176/catalog.json
```

10,000 documents from 13,592 candidates: 6,408 Federal Register `text/xml`; 3,592
Mirrulations documents carrying both `text/html` and `application/json`. Pinning
one catalog is structural, not conventional — `DocumentRelease.source_catalog` is
one `SourceCatalogRef`, not a tuple (`release.py:33`), and the envelope builds
exactly one input with role `source` (`platform_artifact.py:253-259`). Any other
catalog would mint a release whose disposition projection over `U` describes a
universe its documents did not come from.
*Accepted-by: agent (delegated scope, header).*

**D2 — the container mints the top-level release name.** The rationale first
recorded here — that the two canonicalisers "were measured disagreeing on four of
five inputs" — **did not reproduce.** Two independent re-runs since, and a third
made while writing this correction, found the two encoders agreeing byte for byte
on every value in this format's domain, including the sealed valid root itself; where they part company they *refuse* differently
rather than emit differently (an unsafe integer, a lone surrogate, a float each
raise a different exception type with a different message from the two
implementations), and the one byte-level divergence found — object keys are
ordered by UTF-16 code unit under `artifact_json_bytes` and by code point under
`canonical_json_bytes` — needs an object key outside the Basic Multilingual
Plane, which no member of this format carries. The landed support module asserts
the byte agreement directly
(`tests/test_document_release_verify.py::test_the_two_encoders_agree_byte_for_byte_on_this_formats_domain`,
which runs both encoders over the sealed root, its content, and the logical
payload),
modulo the safe-integer guard `document_release_support.py:75-90` adds on top.
The measurement is withdrawn rather than repeated.

What survives is enough to decide the same way, and is stated as what it is:
**single-minter discipline** — both are imported into one module,
`artifact_json_bytes` (`platform_artifact.py:32`) beside DocSpec's own
`canonical_json_bytes` (`platform_artifact.py:41`, defined at `identity.py:112`),
and one name minted by two encoders in one file is a coin flip waiting to be
called wrong by a later editor — and **divergent refusal surfaces**: a value one
encoder seals and the other refuses is a bundle whose validity depends on which
import the producer reached for. Because the two agree on the bytes, *which one
signed a digest is not observable from the digest*, so the choice has to be made
once, up front, and read back off the bundle rather than inferred — which is
exactly why the landed gate detects the minting generation instead of guessing
it. The principle is scoped to exactly that name — it is **not** "product code
never mints names": `stable_urn` has 85 legitimate call sites in `src/` (record
layers, catalogs, receipts), none retired here.
*Accepted-by: agent (delegated scope, header).*

## The format

The 2.0 root is a closed object of **exactly six keys**:

```text
format               "docspec-document-release"
formatVersion        "2.0"
releaseId            urn:docspec:document-release:v2:<64 hex>   (content-derived)
documentStateDigest  sha256:<64 hex>
content              the closed content object
annotations          publishedAt, releaseStatus, buildRunId
```

`annotations` is outside every preimage. This reconciles the nineteen-field spec
shape against the seventeen-key closed shape `from_dict` accepts
(`release.py:183-201`; `to_dict` itself is `release.py:173-179`): the missing two
were `documentStateDigest` (named, never implemented) and `physicalShardPolicy`
(not carried — v2 does not partition, adoption spec §2, so it would have one
legal value). The 1.1 pointer-record fields `activeLayers`, `blobRoots`,
`storeReceiptSetDigest`, `runReceipt`, `catalogCommitReceipt` are internal store
addressing; the digest-pinned member manifest replaces them.

**Identity — two minted names, one derived form.**

1. `documentStateDigest` = `sha256:` over `artifact_json_bytes` of
   `{format, formatVersion, logicalContent}`, where `logicalContent` is `content`
   minus every physical or packing fact: `globalManifest`, `counts.memberCount`,
   `counts.totalMemberByteSize`, `processingPolicies`. `coverage`'s
   representation and segment byte totals stay in — facts about the corpus text,
   not about how it was packed. **A physical-only repack therefore preserves
   `documentStateDigest`**, which `INCREMENTAL-EQUIVALENCE` requires (spec
   conformance table L2245) and a flat hash over the whole root would break.
   *Accepted-by: agent (delegated scope, header).*
2. `documentReleaseId` is **derived, not minted**:
   `"urn:docspec:document-release:v2:" + documentStateDigest-hex` — no second
   canonicalisation, no `stable_urn` over a re-serialised value. That is why the
   "retire `identity.py:112` from every 2.0 identity path" test is satisfiable:
   nothing on the 2.0 path needs DocSpec's canonicaliser. It is the value the
   root's `releaseId` carries.
3. The envelope `releaseId` stays the derivation logical ID from
   `expected_logical_id` over `{format, formatVersion, inputs, kind, spec}`
   (`platform_artifact.py:262-270`); `derivation_spec` gains
   `documentStateDigest` (row 3). The envelope name answers "is this the same
   artifact"; the state digest answers "is this the same corpus".

Root and member bytes are exactly `artifact_json_bytes(value)` with **no**
trailing newline; `canonical_json_file_bytes` (`identity.py:119-122`) and
`DocumentRelease.file_bytes` (`release.py:225-226`) leave the release path — the
`invalid/noncanonical-root` fixture's whole mutation is that newline, so today's
`file_bytes` output equals a bundle the validator must refuse.

**Member set.**

```text
release.json   manifests/global.json   schemas/*.schema.json (eight, digest-pinned)
data/source-dispositions.jsonl   one row per member of U
data/documents.jsonl             one row per document version
data/attachments.jsonl           NEW — one row per enumerated attachment
data/comments.jsonl              NEW — one row per selected comment
data/structural-nodes.jsonl      source-derived structure, all kinds
data/search-segments.jsonl       bounded segments, all kinds
blobs/<partition>                captured rendition bytes, partitioned
text/<partition>                 selected representations, partitioned
```

`data/*` members are **JSONL**, one canonical-JSON record per line, following
DocSpec's own `docspec-record-layer/1.1` (`adapters/storage.py:1220-1224`, profile
`urn:docspec:profile:record-storage:local-jsonl:1` at `:55`) and the catalog's own
`items.jsonl` members (`CATALOG_ITEMS_MEDIA_TYPE = "application/x-ndjson"`,
`source_catalog_artifact.py:74`). The ported fixtures carry them as single-line
JSON arrays, regressing a streaming record layer into a whole-file parse.

Text and blob members follow the **`SourceCatalog` multipart partition pattern**,
not one member per text body: rows bucket by digest of their `textBodyId`, each
bucket is written once as a `blobRef`-carrying member with a `recordCount`, and
an unchanged bucket is reused rather than recopied
(`source_catalog_artifact.py:79-85,1375-1395`; `CATALOG_PARTITION_BUCKET_COUNT =
64`). One member per text body would put 10k+ entries in `globalManifest` at the
first mint and cross the recorded ≈10,000-member threshold at which external
partition manifests must return (consolidation row 61,
`spicysearch@main:docs/history/2026-08-29-document-consolidation.md:128`);
partitioned, the first mint's manifest is tens of members. *Accepted-by: agent (delegated scope,
header).* Manifest roles gain `attachments` and `comments`; every member path is
a checked relative POSIX `objectKey` carrying `byteSize` and `sha256`.

## The deviation resolution

All thirteen rows are live at `main@e8ee58f`. One line each.

| # | What changes in code |
| --- | --- |
| 1 | `DocumentRelease` stops being a pointer-record: the five store-addressing fields leave the root, replaced by `manifests/global.json` with a complete digest-pinned member list and relative `objectKey`s only. |
| 2 | `counts`, `coverage`, `failures` become closed generated objects with named required recomputed fields; `freeze_json`/`thaw_json` passthrough (`release.py:57-68`) is deleted, including the three fields that get no value validation today. |
| 3 | `derivation_spec` gains `documentStateDigest`; the root gains `documentStateDigest` and takes the content-derived `releaseId`. Content-derived identity is *introduced*, not adjusted. |
| 4 | Release and member serialization calls `artifact_json_bytes` with no trailing newline; `file_bytes` and `canonical_json_file_bytes` leave the path. |
| 5 | A new `data/source-dispositions.jsonl` member carries one row per member of `U` with the catalog disposition projected verbatim; the release stops implying membership from what is present. |
| 6 | `accepted-failure` and `rejected-run` stay in `AcquisitionDisposition`/`ProcessorDisposition` (`content.py:29,38`), but the build gate refuses any release that emits either for a selected item — conformance is a producer that never emits them, not a narrower type. |
| 7 | A new `structuralNode` record type with `structuralParentId`, `depth`, dense sibling `ordinal`, and containment; `Segment` (`content.py:652-665`) gains `structuralParentId` and `headingPath`. |
| 8 | `Representation.warnings: tuple[str, ...]` (`content.py:502`) is replaced by `excludedRanges` with byte range, machine-legible `reasonCode`, and presentable prose `reason`. |
| 9 | The release carries `selectedSourceSetDigest`, `documentVersionSetDigest`, `segmentSetDigest`, `sourceDocumentMappingDigest`, and the sealed `joinReceipt`. `selectedSourceSetDigest` is **derived from the pin, not projected from it** — see *The catalog pin* below; the other three are the release's own, under the `/2` domains in *Sealed identities*. |
| 10 | `CapturedFile.acquired_at` (`content.py:243`) **stays in the capture record** and is **excluded from every preimage**. A9 governs preimages, not records: a clock inside a content-derived identity would make two byte-identical captures two releases, but erasing acquisition time would destroy provenance the reader needs. |
| 11 | `require_relative_path` (`identity.py:42-49`) — already owned, simply not applied — is applied to every `objectKey`; `ArtifactRef.locator`/`BlobRef.locator` (`references.py:21,57`) stop being bare `require_text`. |
| 12 | `EvidenceCoordinate` (`content.py:384-390`) makes `start` and `end` required and renames `source_digest` to `renditionSha256`; the coordinate enum stays the two systems under *Coordinates*. |
| 13 | `to_dict` gains `schemaSet`, and the eight schemas ride inside the bundle digest-pinned, so a consumer verifies with no Rulespec checkout. |

The adoption spec's 9-breaking / 4-satisfiable split is real but is not a staging
plan: rows 2, 6, 12, 13 are cheap only after 1, 5, 7, 8, 11 have reshaped the
records they tighten. One builder closes all thirteen.

## The extension: attachments and comments

A **text body** is anything with captured bytes, exactly one selected Unicode
representation, source-derived structure, bounded segments, and an excluded-range
ledger. A document body is one; so is an attachment; so is a comment. Rule M7 puts
all three in the **same** per-catalog release (D1). Each carries `textBodyId`,
`textKind` (`document-body | attachment | comment`), and the existing `capture`,
`representation`, `excludedRanges` `$def`s unchanged; `textKind` is exactly
`TEXT_SOURCE_KINDS` minus `metadata-field`
(`spicysearch@main:src/spicysearch/text_units.py:24`). `structural-nodes` and
`search-segments` re-key from `documentVersionId` to `textBodyId` and gain
`textKind` — free only now, since nothing is minted; a later bolt-on would need a
second segment record type or a nullable key, and both make the consumer branch on
kind for coordinates it must not branch on.

Mint rules. *All three Accepted-by: agent (delegated scope, header).*

| Id | Rule |
| --- | --- |
| `textBodyId` | `document-body`: **equals** the `documentVersionId` — one body per version, and a second name would be a second identity for one thing. `attachment`: equals its `attachmentId`. `comment`: equals its `commentId`. |
| `attachmentId` | `stable_urn("document-release-attachment", {ownerTextBodyId, sourceAttachmentId, renditionOrdinal}, version=2)`, where `sourceAttachmentId` is the source's own stable attachment identity when it supplies one and the owner-scoped ordinal when it does not — so re-enumerating an unchanged owner re-mints the same id. |
| `commentId` | **equals** the catalog's comment `sourceRecordId`, i.e. `/data/id` (`spicy-docs@2b643ef:src/spicy_docs/regulations_gov_source_native.py:702`) — the value the upstream selection policy groups by, so the release cannot disagree with the selection it inherits. |

Attachment rows add `ownerTextBodyId`, `ownerKind` (`document-body` or `comment`),
`renditionOrdinal` (source order, dense, zero-based), `attachmentTitle`
(nullable), `renditions`. Comment rows add `documentId`, `sourceItemId`,
`sourceIssuedVersion`, `commentSelection`. An attachment's owner is exactly one
text body and never another attachment; a comment's owner is exactly one document
and may own attachments. The graph is two levels deep and acyclic; the validator
proves it.

**Cardinality.** One source attachment may be published in several renditions.
**One attachment row** carries the attachment and groups its M renditions under
its `attachmentId`; **per-rendition sub-rows** carry `renditionOrdinal`, media
type, capture, and their own `attachmentDisposition`. The row is the attachment;
the sub-row is the rendition that succeeded or failed. *Accepted-by: agent
(delegated scope, header).*

**Per-kind packing.** The live pipeline packs the kinds differently at source
(`spicy-regs@main:src/spicy_regs/enrich_pdf.py:16-19`): documents flat
`[{url, format, size}, …]`, comments nested `[{title, formats: [...]}, …]`. The
release must not inherit that source-shape fact: both unpack once, at ingest, into
one attachment record; `attachmentTitle` is non-null only where the nested form
supplied one, and `ownerKind` records which rule produced the row so it stays
reversible. Two packings in, one record out.

**Comment selection and tie refusal.** DocSpec does not select comments; the
sealed upstream policy does
(`spicy-docs@2b643ef:src/spicy_docs/regulations_gov_source_native.py:1509-1521`):
`groupBy /data/id`, `orderBy /data/attributes/modifyDate DESC NULLS LAST`,
`tieDisposition refuse-repeated-normalized-instant`. **The comment interface
contract is post-selection rows** — DocSpec is handed the already-selected
observation per comment id, never a candidate set to reduce. Each row projects
that policy verbatim, plus its digest and the selected observation's normalized
`modifyDate`. The inheritance is the refusal, not the ordering: **handed two
observations of one comment id, the build fails.** It does not re-order,
tie-break, or pick the first; resolving a tie would be DocSpec deciding a
selection the source owner refused to decide (adoption spec §4). New diagnostic
`invalid.comment-selection`, ordered immediately after `invalid.disposition`.
**Under the D1 catalog there are no comment members** — the 10k sampling plan
excluded comments, dockets, and tombstones from the draw (`DocSpec@9f0edab^`, 10k
sampling plan L61) and the full tier's counts are documents only. The schema,
member role, and contract are sealed now because sealing them later is not free;
the first mint writes zero comment rows and a zero count.
*Accepted-by: agent (delegated scope, header).*

**Coordinates.** Every segment keeps two systems: a **representation** range
(`representationStart`/`representationEnd`, half-open over UTF-8 bytes) for the
consumer, and **rendition** evidence naming captured bytes by digest for
reversibility. The consumer seam needs no translation —
`spicysearch@main:src/spicysearch/schemas/output/2.0/text-unit.schema.json` takes
`releaseId` ← the release's `releaseId`, `sourceRecordId` ← `textBodyId`,
`sourcePath` ← the text partition member, `sourceStartUtf8Byte` ←
`representationStart`, `coordinateSystem` ← `representation-utf8-byte`, already in
`COORDINATE_SYSTEMS` (`text_units.py:26`). Rendition evidence keeps exactly two
systems, both requiring `start` and `end`: `rendition-utf8-byte` (a slice of UTF-8
captured text) and `rendition-byte` (a slice of opaque captured bytes). A third
`rendition-page-region` system with an integer-permille page rectangle was
designed and is **dropped** — no extractor DocSpec runs emits page geometry, so
the release would carry a coordinate nothing can produce or check; deferred until
a coordinate-emitting extractor is chosen, permille rule included.
*Accepted-by: agent (delegated scope, header).* Two constraints bind every span —
segments, structural nodes, excluded ranges alike: **`end > start`**, and every
bound falls on a **UTF-8 character boundary** of the bytes it addresses.

**The excluded ledger keeps its text.** The reader honesty rule
(`spicysearch@main:docs/design/spicysearch-doc-reader.html`,
`docs/ui-product-requirements.md:76`) requires an excluded region to stay readable,
so an excluded range is a **search** exclusion, never a redaction: its bytes stay
in the representation member, `representationByteTotal` counts them, and a reader
slices them out by offset — no separate retained-text field, since a second copy
could drift. `reason` is prose fit to show a reader, `reasonCode` machine-legible
from a closed vocabulary the release carries; both required, and the
`invalid/missing-projection-reason` fixture gains an excluded-range analogue. The
coverage identity `segmentedByteTotal + excludedByteTotal ==
representationByteTotal` holds **per text body** and in aggregate, every kind.

**Policy digests (avoid-lesson A7).** Extractor identifiers carry no digest today
(`processing/extraction.py:35-39` — five bare strings) and the fixture's
`processingPolicy` digests the enclosing policy but not the `extractorId` or
`segmenterId` inside it, so policy content can drift under an unchanged id.
`processingPolicy` becomes **`processingPolicies`**: a sorted array of closed
records, one per `(textKind, mediaType)`, each carrying `extractorId` **and**
`extractorDigest`, `segmenterId` **and** `segmenterDigest`, `maxSegmentBytes`, and
the retention floor that governed it — per-kind is not optional, since a
`document-body` HTML extractor and a `comment` PDF extractor are different code
with different floors. It sits **beside** the identity preimage, not inside it:
the plan is already bound into the envelope name through `derivation_spec`, and
folding policy digests into `documentStateDigest` would make "is this the same
corpus" answer "no" whenever a segmenter is rebuilt over unchanged text.
*Accepted-by: agent (delegated scope, header).*

New set digests `attachmentSetDigest`, `commentSetDigest`, `textBodySetDigest`
join `selectedSourceSetDigest` (derived from the pin — see *The catalog pin*),
`documentVersionSetDigest`, `segmentSetDigest`, `sourceDocumentMappingDigest`,
and the `joinReceipt`. `counts` and `coverage` gain per-kind breakdowns; a zero
is written, never omitted.

## The catalog pin

**`sourceCatalogPin` reshapes to `{catalogId, catalogDigest}`.** The ported pin
is four fields — `releaseId`, `releaseDigest`, `requestedUniverseSetDigest`,
`selectedSourceSetDigest` (`document-release.schema.json:139-164`) — and three of
them describe an artifact DocSpec no longer produces. What the D1 catalog
actually is, read at the locator D1 quotes
(`~/Work/corpora/_salvage-2026-08-28/docspec-qualification/fr-mirrulations-10k-v1/source-catalogs/source-catalogs/42/42890dab2fadf68b0a710e8e155293101830645ef0526d0404e6c76fb4f82176/catalog.json`),
is a `docspec-source-catalog` `1.0` snapshot whose root keys are exactly
`baseCatalog`, `catalogId`, `counts`, `coverage`, `format`, `formatVersion`,
`itemsMember`, `kind`, `partitions`. So:

- `catalogId` is the snapshot's own
  `urn:docspec:source-catalog:v1:973aaa19…` value, verbatim;
- `catalogDigest` is the **byte** sha256 of the pinned `catalog.json` —
  `ded6649aab3f04faa6a48f867de0854648ec10c04fcdad8f6527e075d97c45d6`, which is
  the digest `catalog-set.json` already records for the full tier — and is
  verified against those bytes, not against a re-serialisation of the parsed
  value;
- `requestedUniverseSetDigest` and `selectedSourceSetDigest` **leave the pin**.
  The snapshot carries neither at its root, so a pin declaring them would be
  pinning values it cannot read back.

**`selectedSourceSetDigest` is therefore not projected.** There is nothing to
project: the D1 snapshot has no such field. The builder **derives** it, over the
pinned catalog's items, under the catalog's own domain
`docspec-selected-source-set/1` with the catalog's own record shape
(`source_catalog_artifact.py:375-395`), and records it as **derived-from-pin**.
§7.5's rule is *no recompute under another name*; this recomputes under **the
same** name, in the same domain, with the same algorithm, so the rule permits it
— what §7.5 forbids is a second digest of the same fact wearing a different
domain, and there is none here. A consumer that wants the check re-run runs the
identical function over the identical pinned bytes and must get the identical
value; that is the whole point of deriving rather than copying.
*Accepted-by: agent (delegated scope, header).*

## Acceptance gates

**Extraction floors (Q12): DocSpec refuses.** DocSpec today counts and never
refuses — `HtmlExtractor` records `elementCount` and `visibleUnicodeCodepointCount`
(`extraction.py:237-238`) and no threshold reads them. The predecessor refused at
the extraction boundary rather than inside any one adapter
(`spicy-regs@integrate/payload-prereqs:src/spicy_regs/docpipeline/source.py:1324-1372`),
and 2.0 adopts that posture: a parse is refused when it is **below the declared
floor**, when there is **no declared floor for that parser and format**, or when
the source's **visible text could not be measured**. Undeclared is not "inherit a
default"; on success the measurement is written into the receipt.

A floor is `RetentionFloor(value, unit, observed_minimum, population)`
(`source.py:975-1000`) with two invariants carried over unchanged: **`0 < value <
1`** (outside that a floor either gates nothing or refuses everything) and
**`observed_minimum > value`** (no margin under the lowest legitimate document is
a future false refusal). Units are the visible-text fraction for markup and text
density for binary renditions; different measurements, never one floor. Media
types collapse onto one format key before lookup (`text/xml`, `application/xml`,
`*+xml` are one population), so the same document is not gated differently by
which header the publisher sent. Floors are **calibrated per parser and format on
the preserved body corpora**:

- `~/Work/corpora/_preserved-2026-08-10/body-retrieval-corpus-2026-08-02` — 5,971
  files (995 `distribution-html`, 995 `distribution-xml`, 1,988 each of
  `cache`/`cache-xml`), draw manifest and both measurement records alongside.
  The HTML/XML population.
- `~/Work/corpora/_salvage-2026-08-28/spicysearch-output/rule-bodies-pre2000-2026-08-22`
  — 39,786 bodies with `manifest.json` and a one-line `failures.jsonl` whose sole
  entry is a 404 (an acquisition failure, not an extraction one). The pre-2000
  HTML population, and the hardest case for a visible-text floor.

The predecessor's four floors — `native:text/html` 0.75 against observed minimum
0.9453, `native:application/xml` 0.85 against 0.9930, `pypdf:application/pdf`
0.005 against 0.015584, `pymupdf:application/pdf` 0.005 against 0.015571 — are
**evidence of the method, not adopted values**; they were measured through the
predecessor's extractors. Every floor is re-derived against DocSpec's own
extractor on the named corpus, and the receipt records parser, format,
population, distribution, observed minimum, chosen value, and margin. A format
nobody measured has no floor and is refused — the intended fail-closed shape.

**The `application/json` posture.** The Mirrulations half of the D1 catalog
supplies each of its 3,592 documents as both `text/html` and `application/json`.
**Rendition choice always selects the markup sibling**: where an item offers
`text/html` or `text/xml`, that rendition is the body and the JSON rendition
never is. No JSON floor is calibrated for this mint and none is needed. An item
whose **only** rendition is `application/json` has no declared floor and is
refused, like any other unmeasured format — not silently admitted. Should such
items appear at a later scale, the fix is to name a JSON calibration population,
not to relax the rule. *Accepted-by: agent (delegated scope, header).*

**Zero accepted failures, and attachment accounting.** §7.5's "no accepted
processing failure" binds the **catalog universe `U`**, whose members are source
items, and its guarantee is the structural bijection: a `selected` row carries a
`documentVersionId`, any other disposition carries `null`, and a selected item
that cannot be made searchable **blocks the build**. Unchanged for document
bodies, extending unchanged to comments wherever a pinned catalog selects them
into `U` (the D1 catalog does not). Attachments are **not** members of `U` — they
are renditions of a member, and one unparseable PDF among thirty on one comment
must not fail a 10,000-document mint. They get accounting instead of a floor:
**every attachment enumerated by its owner's source record gets a row**, and every
rendition of it a sub-row with an `attachmentDisposition`:

```text
text-captured        text was extracted and is carried
text-excluded        captured, deliberately not extracted (policy)
source-unavailable   the enumerated bytes could not be fetched
extraction-failed    fetched, but extraction refused or failed
```

These tokens deliberately avoid the catalog's `selected | excluded | deleted |
unavailable | failed` vocabulary: an attachment disposition is not a catalog
disposition, and reusing the words invites a reader to join them. Anything but
`text-captured` carries `reasonCode` + presentable `reason` and a null
`textBodyId`. **A below-floor attachment is `extraction-failed` and never blocks
the build.** The build fails when an enumerated attachment has *no row* — never
when a row honestly says it could not be captured. Loss stays visible instead of
accepted silently, and out of the bijection that guarantees corpus membership.
*Accepted-by: agent (delegated scope, header).*

`COMPLETE-SEARCH-CORPUS` proves, over every kind present: the set and mapping
digests, gapless visible-text accounting, exact evidence round trips,
expected-digest refusal, zero accepted failure for `U`, complete attachment
accounting, and verification from a clean installed package with no
source-producer checkout.

## Migration, and the builder's obligations

There is no migration to perform: nothing is minted in 2.0, and 1.1 is replaced,
not migrated — no compatibility reader, no dual-write, no conversion tool.
`RELEASE_FORMAT_VERSION` stays `"1.1"` until the builder lands, then becomes
`"2.0"` in one commit.

**Reuse, do not rewrite.** The conformance validator
(`rulespec@c584a1d:src/rulespec_conformance/document_release.py`, 1,524 lines)
and the fixture restamper (`tools/build_document_release_fixtures.py`, 889 lines)
**moved to DocSpec** as a port of working code, not a reimplementation: verifier
at `src/docspec/adapters/document_release_verify.py` with
`src/docspec/document_release_support.py` (`59616e7`), restamper at
`tools/restamp_document_release_fixtures.py` (`19ae99b`), every sealed bundle
run by `tests/test_document_release_verify.py` (`260152f`). The builder this
decision authorises implements the **produce** side only and calls the moved
verifier as its gate.

**The gate is generation-aware, because the corpus it gates is.** The identity
and set-digest rules above are this decision's; the twenty sealed bundles were
minted under the predecessor's — a full-content digest through
`docspec.domain.identity.canonical_json_bytes`, and plain sorted-set digests. A
verifier holding one rule would be wrong about half of what it reads: applying
the rules here to the sealed corpus renames all twenty, and applying the
predecessor's to a bundle minted under this decision would accept a
`documentStateDigest` that moved on a physical-only repack. So
`document_release_verify.py` reads the **minting generation off the bundle** —
from the same declared schema `$id`s `SCHEMA_ID_GENERATIONS` already resolves,
since the re-homing in restamp item 1 is exactly the boundary between the two
rule sets — and checks each bundle against the rules it was minted under.
Predecessor bundles keep verifying unchanged, 20/20; a bundle declaring the
re-homed `$id`s is checked against this decision. A bundle mixing the two
spellings is refused (`invalid.schema`) rather than resolved to a winner, and a
bundle that cannot say what it is falls to the predecessor rules, which are the
only rules any sealed bundle was ever minted under. This is a property of the
restamp, not a permanent compatibility layer: after the restamp lands, the
predecessor branch has no bundle left to serve and the decision to retire it is
the restamp's to record. *Accepted-by: agent (delegated scope, header).*

**Its first obligation, before any real corpus, is to restamp the twenty sealed
conformance bundles.** Each bundle's `manifests/global.json` and `release.json`
`schemaSet` carry `https://rulespec.org/...` `schemaId` strings inside digest
preimages that `corpus.json` seals with `treeSha256`, so hand-editing invalidates
every member digest, the manifest digest, the `schemaSetId`, the `byteSize`
counts, the `releaseId`, and the tree digest of all twenty. They restamp by a
builder or not at all (adoption spec §8).

**Precondition, unrecorded until now: the restamper's own input is not here.**
Every fixture in this corpus is built from the sealed `SourceCatalogRelease` v1
fixture, which it pins by identity and digest (adoption spec §7), and that
fixture **did not come across with the restamper**:
`tools/restamp_document_release_fixtures.py:76` names
`tests/fixtures/source_catalog_release_v1/valid`, and no such directory exists
in this repository, so both modes stop at `_require_inputs` (`:79-96`) rather
than half-building a corpus. Importing that fixture is therefore step zero of
the first obligation, before item 1 below.

**One operation, this exhaustive list:**

1. the six schema `$id`s move to `urn:docspec:schema:document-release-*:2.0`
   (adoption spec §8 table);
2. two schemas are added — `attachments` and `comments`;
3. `schemaSet.schemas` `minItems`/`maxItems` go **6 → 8**; `schemaSetId` recomputed;
4. `documents.schema.json` gains `textBodyId` and `textKind`;
5. `structural-nodes` and `search-segments` re-key from `documentVersionId` to
   `textBodyId` and gain `textKind`;
6. `processingPolicy` → `processingPolicies` (per-`(textKind, mediaType)` array
   with extractor and segmenter digests);
7. three new set digests — `attachmentSetDigest`, `commentSetDigest`,
   `textBodySetDigest` — plus per-kind `counts` and `coverage` breakdowns;
8. the root gains `documentStateDigest`; the `releaseId` pattern stays
   `^urn:docspec:document-release:v2:[0-9a-f]{64}$`, documented as the
   content-derived form (see *Sealed identities*);
9. the catalog pin reshapes to **`{catalogId, catalogDigest}`** (*The catalog
   pin*): `sourceCatalogPin` (`document-release.schema.json:139-164`) drops
   `requestedUniverseSetDigest` and `selectedSourceSetDigest`, which the D1
   snapshot does not carry, renames `releaseId`/`releaseDigest` to
   `catalogId`/`catalogDigest`, and takes
   **`^urn:docspec:source-catalog:v1:[0-9a-f]{64}$`** for the id — the ported
   pattern names a producer that no longer owns catalogs and does not match the
   D1 catalog's own `catalogId`. The **same pattern appears twice**, and both
   sites move together: `sourceCatalogPin.releaseId`
   (`document-release.schema.json:151-152`) and
   `captureRecord.catalogReleaseId` (`documents.schema.json:116`), the
   per-document back-reference the root pin is checked against. Fixing one and
   not the other would make every capture row disagree with the root;
10. the evidence `coordinateSystem` enum stays the two systems above, documented
    as closed;
11. member re-keying: `data/*` members become **JSONL**, and `blobs/`/`text/`
    become partitioned members rather than one file per document;
12. **ownership and consumer prose, at the two sites that actually carry it.**
    Only **one** schema claims Rulespec ownership — `document-release.schema.json:5`,
    "Rulespec Core owns this schema; DocSpec owns the records it carries
    (REF-024)" — and it is corrected to DocSpec ownership under REF-048. The
    other five carry no ownership sentence at all. The **second-consumer**
    correction belongs to a different file: `search-segments.schema.json:5`
    says "SpicySearch consumes these; it must not run a second segmentation
    pipeline", and that is the single-consumer sentence the C27 disposition
    contradicts, so it is the one that must also name the Rulespec
    Extrapolator and carry the no-re-segmentation obligation onto it;
13. `member-manifest.schema.json:94-104` — the member `role` enum gains
    **`attachments`** and **`comments`**, the two new tabular roles from
    restamp item 2, so the manifest can describe the members the schema set
    now declares;
14. `documents.schema.json:100-101` — `captureRecord` **gains the acquisition
    wall clock**: `acquiredAt` required and `acquisitionStartedAt` nullable,
    mirroring `CapturedFile` (`content.py:243,247`), under deviation row 10's
    ruling that the clock stays in the record and out of every preimage. **Two
    descriptions must be corrected, not one**, because both currently assert
    the opposite: `documents.schema.json:101` ("Carries no wall clock: a
    capture time would put a clock inside a content-derived identity…") and
    `document-release.schema.json:56` ("Per-record wall clocks are absent from
    this contract entirely — see the spec"). Both sentences die; what replaces
    them says where the clock lives and which preimages exclude it;
15. `search-segments.schema.json` — the evidence coordinate reserves
    **`evidenceGrade`**, unpopulated in 2.0 (*Recorded dispositions*), beside
    the closed `coordinateSystem` enum at `:74-80`;
16. `member-manifest.schema.json:116-117` — `recordCount` semantics widen for
    the partitioned members item 11 introduces. Today the description reads
    "Row count for a tabular member; null for a schema, rendition, or
    representation member, which has no rows", and the verifier enforces
    exactly that (`document_release_verify.py`, `_validate_member_descriptor`).
    A partitioned `text/` or `blobs/` member **is** a bucket of rows and
    carries its own `recordCount`, exactly as the catalog's partitions do
    (`source_catalog_artifact.py:79-85,1375-1395`), so the null-for-rendition
    rule must be restated per role rather than per "has rows", and the
    verifier's member-descriptor rule (`_validate_member_descriptor`'s `elif`; `OPAQUE_ROLES` only feeds `ALLOWED_MEMBER_ROLES`) restated with it.

The invalid-bundle corpus grows by `invalid.comment-selection`,
`invalid.attachment-accounting`, and `invalid.retention-floor`, each still a
single-rule mutation of the valid bundle with every downstream digest, count,
coverage figure, and identity restamped from the fixture's own bytes. The Rulespec
candidate record `release-records/document-release-v2-candidate.json` is not
ported and not recreated; its DocSpec equivalent — a record pinning the eight
schemas, the validator modules, and every sealed bundle — is builder work.

## Recorded dispositions

One line each, so an absent rule is a recorded absence. *All Accepted-by: agent
(delegated scope, header).*

| Item | Disposition |
| --- | --- |
| Second consumer (C27) | The **Rulespec Extrapolator** is consumer #2 and is contractually forbidden from re-segmenting; the segment records are a two-consumer contract, and the sentence that says otherwise — `search-segments.schema.json:5`, "SpicySearch consumes these" — is corrected to say so (restamp item 12). |
| Active-set diff | Deferred, named: the builder's `diff` verb (`src/docspec/cli.py`, `document-release diff`) owns it; 2.0 defines no cross-release active-set delta. |
| Compaction trigger | Moot in v2's self-contained shape — a bundle that carries its members has nothing to compact — recorded as moot rather than dropped, so the partitioning decision can reopen it. |
| Media-type conflict refusal | Adopted as a builder diagnostic: a rendition whose declared media type contradicts its sniffed type is refused at capture, never silently coerced. |
| K14 invariants | Both adopted as builder diagnostics: an HTTP-200 challenge page is **quarantined, never sealed as body text**; a missing content-type **must not erase a rendition already identified by path** (consolidation K14, `spicysearch@main:docs/history/2026-08-29-document-consolidation.md:214`). |
| Evidence grading | Deferred, named: the field is `evidenceGrade` on the evidence coordinate, reserved in the restamp (item 15) and unpopulated in 2.0. |
| Multi-representation (C5) | Representation stays **singular** in v2 — one selected Unicode representation per text body. OCR and other alternate representations are a recorded **v2.1 extension point**, not a v2 field. |
| Migration-manifest rows 6/7/8 | Exact source capture, processing segments, and extraction task protocol are **marked ported at the first mint**, when the release that carries them exists — not before. |

## What this decision does not decide

| Open | Why it stays open |
| --- | --- |
| Serving policy | Whether an attachment or comment segment may generate candidates, affect ranking, or appear in a response is SpicySearch's, under its Decision 0007 rule that processing permission is not serving permission. This release makes the records; it authorizes no lane. |
| Scale beyond 10k | v2 does not partition the *release*; `partitionPolicy` and `physicalShardPolicy` return with a measured partitioning decision whose input is the first mint's numbers. |
| Legacy 1.1 bytes | This decision replaces the format; it makes no claim about historical artifacts on disk. |
| The PDF extractor | The floor mechanism is decided; the parser and its measured floor are the calibration receipt's output. |
| `reasonCode` vocabularies | Closure is required here; members are sealed with the builder against real corpus. |
| Non-selected renditions | Whether candidate renditions other than the selected one are kept as blobs is a storage decision. |


## Amendment 2026-08-31: the three stopped restamp items

The restamp executed thirteen of sixteen items and stopped on three rather
than invent — correctly, because item 2 as written was self-contradictory.
Resolutions, each *Accepted-by: agent (delegated scope, header)*:

**A1 — the `attachmentId` preimage drops the ordinal.** The contradiction:
`renditionOrdinal` sat in the id preimage while the cardinality rule says one
attachment row groups its M renditions — an id that changes per rendition
cannot name the row that groups them. Resolution: `attachmentId` =
`stable_urn` over `{ownerTextBodyId, ownerKind, attachmentIdentity}` where
`attachmentIdentity` is the source's own stable identifier for the attached
file; `renditionOrdinal` lives only on per-rendition sub-rows. The id names
the attachment; the ordinal names its renditions.

**A2 — per-kind counts and coverage get names.** `counts.perKind` is a closed
object keyed `document-body` / `attachment` / `comment`, each carrying exactly
`{textBodies, segments, representationByteTotal, segmentedByteTotal,
excludedByteTotal}`; the coverage identity
`segmentedByteTotal + excludedByteTotal == representationByteTotal` holds per
kind and in aggregate. `coverage` itself stays aggregate; the per-kind
breakdown lives under `counts`.

**A3 — `reasonCode` seals as a bounded string now, closes at the real mint.**
The attachment/comment schemas seal with the four disposition tokens closed
and `reasonCode` a non-empty string (max 64 chars, kebab-case pattern);
closing the enum requires the real corpus, as *What this does not decide*
already records. The enum closure is a first-real-mint obligation.

**A4 — the bucket framing gap gets a member role.** Item 11's digest-bucketed
text/blob members had no way to recover one body's bytes from a shared
bucket; the builder refused multi-body buckets, which cannot survive real
scale. Resolution: one new member role `text-body-index` — JSONL rows
`{family: "text"|"blob", textBodyId, member, startByte, byteLength, sha256}`
— making every body byte-slice recoverable and digest-verifiable from bundle
bytes alone. The multi-body refusal lifts where the index covers the bucket;
an indexed body whose slice hash mismatches is `invalid.member-digest`.

**Sequencing note, recorded honestly:** the restamp ran before item 2 was
resolvable — the contradiction was discoverable only by attempting the
implementation. The 2026-08-31 docspec corpus is therefore an intermediate
state of the single restamp operation, not a second mint: completing items
2, 3, and 7 under this amendment regenerates it as part of finishing that
one operation. Nothing real was minted against the intermediate corpus.

## Sealed identities

Schema `$id`s, packaged at `src/docspec/schemas/document_release/2.0/` — version
in the directory, no version suffix in the filename, `$id` a
`urn:docspec:schema:<name>:<version>` URN. The first six are packaged at
`main@a5f21eb` with these `$id`s; the fixtures still carry the
`https://rulespec.org/...` strings until the restamp.

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

| Name | Form | Minted by |
| --- | --- | --- |
| `releaseId` (2.0 bundle) | `urn:docspec:document-release:v2:<64 hex>` | derived: URN prefix + `documentStateDigest` hex |
| `releaseId` (1.1 / envelope) | `urn:spicy:artifact:derivation:<64 hex>` | `rulespec_artifacts.expected_logical_id` |
| `documentStateDigest` | `sha256:<64 hex>` | `artifact_json_bytes` over `{format, formatVersion, logicalContent}` |
| catalog pin `catalogId` | `urn:docspec:source-catalog:v1:<64 hex>` | the pinned `SourceCatalog` snapshot's own value, verbatim |
| catalog pin `catalogDigest` | `<64 hex>` | byte sha256 of the pinned `catalog.json`, verified against those bytes |
| `selectedSourceSetDigest` | `sha256:<64 hex>` | derived from the pin under `docspec-selected-source-set/1` (*The catalog pin*) |

**The `releaseId` wire key changes meaning between 1.1-wire and 2.0-wire, and
that is recorded rather than glossed.** In 1.1 and in the derivation envelope,
`releaseId` is the plan-derived artifact name matching
`^urn:spicy:artifact:derivation:[0-9a-f]{64}$` (`release.py:48`). The ported 2.0
schema's `releaseId` pattern is `^urn:docspec:document-release:v2:[0-9a-f]{64}$`
— the *content-derived* form — and 2.0 keeps it. So the bundle's `releaseId` is
the portable content-derived name while the envelope carrying the bundle keeps
its own `urn:spicy:artifact:derivation:` id unchanged in the derivation record.
Same key, two meanings, one boundary between them; a reader that assumes 1.1's
meaning against a 2.0 bundle is reading the wrong name.
*Accepted-by: agent (delegated scope, header).*

Digest domains. 2.0's projections changed — structure and segment members are
keyed by `textBodyId`, not `documentVersionId` — so 2.0 declares its own domains
at `/2`, superseding the spec §7.5 `/1` domains for this format:

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

`sourceDocumentMappingDigest` is **`docspec-source-to-document/2` in that table
and nothing else**: a framed SET digest over unique `sourceItemId` keys, carrying
`documentId` and `documentVersionId` in each record. The candidate's list digest
over repeated `[sourceItemId, documentVersionId]` pairs does not survive into
2.0. It cannot: `U`'s bijection already makes `sourceItemId` unique across the
selected rows, so a repeated pair is not a fact the digest has to preserve — it
is `invalid.duplicate-identity`, and a digest that quietly absorbed it would be
a second, weaker report of a defect that already has a diagnostic. The
predecessor generation's list digest stays exactly where it is, in the twenty
sealed bundles and in the verifier's predecessor branch
(`adapters/document_release_verify.py`, `mapping_digest`), and nowhere else.

`selectedSourceSetDigest` is **derived from the pin**, under
`docspec-selected-source-set/1` — see *The catalog pin*.

## Amendment 2026-08-31 (second): the first-mint blind-gate findings

The first real mint under this decision — `output/document-release-10k-v1`,
receipt `docs/history/2026-08-30-document-release-10k-v1-mint-receipt.json`,
builder `96c16f2` — went through a two-reviewer blind gate and **failed**. Six
findings, verified by construction rather than by reading; six rulings. The
mint is superseded, not patched: the format, the gate, the builder, and the
floors all move, so the release that comes out is a new one.

*All rulings below: Accepted-by: agent (delegated scope: owner 2026-08-30
"execute phase 1... you for human-less decision making"; findings from the
first-mint blind gate, 2026-08-31).*

### B1 — identity did not name content; the set digests move to `/3` and frame full logical rows

**Finding, by construction.** The `/2` domains project each row onto id fields
only (`FRAMED_SET_DOMAINS`, `adapters/document_release_verify.py:527-541`), so a
same-length mutation of a representation's bytes with the physical digests
restamped verifies clean under an **unchanged** `documentStateDigest`. The same
hole swallows `sourceMetadata.sourceUrl`, `sourceMetadata.title`, and a narrowed
`segment.evidence.end`. "Two names over one content" was true of the physical
half and false of the logical one.

**Ruling.** Every set digest this format carries moves to a `/3` domain, and a
`/3` domain frames each record's **full logical row**: the canonical row exactly
as the member carries it, minus an enumerated per-record-type exclusion set. The
row's own content digests — `capture.sha256`, `representation.sha256`,
`byteSize`, `expectedSha256` — are **logical** and stay in. **The line, stated once and then enumerated: a
source-issued fact is content; a DocSpec-process fact is packing.** A
`modifyDate`, a source id, a content digest came from the publisher and belongs
in the name. A fetch time, a run id, a tool version came from this producer's
act of building, and putting one inside a content-derived identity would break
**remint-idempotence** -- an honest remint of identical content would mint a
different name. Two kinds of field come out, and both are that line:

* **physical locator facts**, which say where a byte landed rather than what it
  is. Row-level, that is exactly the `objectKey` fields; the bucket offsets
  (`startByte`, `byteLength`, `member`) live in the `text-body-index` member,
  which no set digest reads. This is the same physical/logical line the root
  already draws at `logical_content`, extended one level down.
* **process-provenance facts**, of which this format's rows carry exactly two:
  `capture.acquiredAt` and `capture.acquisitionStartedAt`, which deviation row
  10 already excludes from *every* preimage -- "a clock inside a content-derived
  identity would make two byte-identical captures two releases". The other
  process facts this release carries -- `annotations.buildRunId`,
  `annotations.publishedAt`, and `processingPolicies` with its tool digests --
  are already outside the identity preimage at the root, and stay there. The
  enumeration below is exhaustive per record type, so a field added to any row
  later is content until this decision says otherwise.

The enumerated exclusion set, per record type, complete:

```text
source-dispositions   (none)
documents             capture.objectKey
                      capture.acquiredAt, capture.acquisitionStartedAt
attachments           representation.objectKey
                      renditions[].capture.objectKey
                      renditions[].capture.acquiredAt
                      renditions[].capture.acquisitionStartedAt
comments              capture.objectKey, representation.objectKey
                      capture.acquiredAt, capture.acquisitionStartedAt
structural-nodes      (none)
search-segments       (none)
```

`documents` also excludes `representation.objectKey`; it is listed once above
with its sibling to keep the two `objectKey`s of one row together.

**The name now covers every logical row the bundle carries**, which needs two
digests that did not exist. `content` gains `structuralNodeSetDigest` and
`sourceDispositionSetDigest`, under `docspec-structural-node-set/3` and
`docspec-source-disposition-set/3`. Without them, 10,000 disposition rows and
every structural node would stay outside the name, and B1 would be half-fixed:
"any logical fact moves the name" has to be true of all of them or it is a
slogan. `docspec-document-set/2` and `docspec-representation-set/2` are
**withdrawn** rather than renumbered — they were declared in *Sealed identities*
and no `content` field ever carried them, so a `/3` spelling would seal a name
nothing mints.

Two of the `/3` domains stay **projections** rather than full rows, because the
fact each names is a projection and its rows are already covered whole
elsewhere: `docspec-text-body-set/3` streams `{textBodyId, textKind}` across all
three kinds, and `docspec-source-to-document/3` streams
`{sourceItemId, documentId, documentVersionId}`. Both are declared as
projections in the table below, so a reader never has to guess which a domain is.

**Repack invariance survives, and is the reason the exclusion set is exactly
these fields.** A repack rewrites member paths, bucket offsets, member digests,
the global manifest, `counts.memberCount`, and `counts.totalMemberByteSize`.
Every one of those is excluded — the `objectKey`s row-level, the rest at
`logical_content` — so `documentStateDigest` is unmoved, and
`INCREMENTAL-EQUIVALENCE` still holds.

The `/2` domains are **not** retired from the code: the twenty sealed
predecessor bundles are not minted under them either (they carry plain sorted-set
digests), but the docspec-generation fixture corpus was, and it is restamped
under `/3` as part of the same single restamp operation the first amendment's
sequencing note describes.

### B2 — the version binding is `documentId@sourceIssuedVersion`, and the gate checks it

**Finding.** `documentVersionId` embeds `@sha256:<hex>` and nothing checked what
that hex names; the `documentVersionId` ↔ `capture.sha256` embedding was a
producer convention the verifier never read.

**Ruling, with the finding's own remedy corrected against the corpus.** The gate
was to bind the embedded digest to `capture.sha256` "where ids embed
`@sha256:`". Measured against the D1 corpus, that binding is **false for 1,683 of
8,082 rows**: the Federal Register half embeds the rendition digest (its
`sourceIssuedVersion` *is* the candidate's `expectedDigest`), while the
Mirrulations half embeds the catalog's own source-issued version digest, and no
`expectedDigest` exists there at all. Enforcing the literal rule would refuse a
fifth of a correct corpus. The convention that is actually true of every row is
the one the gate now enforces, under a new diagnostic `invalid.version-binding`,
ordered immediately after `invalid.identity`:

1. `documentVersionId` **equals** `documentId + "@" + sourceIssuedVersion`;
2. `textBodyId` **equals** `documentVersionId` for a `document-body`, which this
   decision's mint-rule table already required and nothing checked;
3. where `sourceIssuedVersion` is a `sha256:` digest **and** the capture declares
   a non-null `expectedSha256`, the embedded hex equals `capture.sha256` — the
   finding's rule, scoped to the rows for which its premise holds. An id that
   claims to name captured bytes must name the digest of the bytes carried.

Under B1 the body bytes already move the name through `capture.sha256` and
`representation.sha256`; this rule is the second lock, on the id itself.

### B3 — the gate cannot crash, and the corpus test asserts whole diagnostic sets

**Finding.** `_read_slice` (`:882-887`) seeks to a caller-supplied offset; a
negative `startByte` in a `text-body-index` row raises an uncaught `OSError`,
because the guard at `:1332` checks only overflow. An untrusted bundle could
therefore take the gate down instead of being refused by it — a regression
against the ported validator, which reported diagnostics. Separately, the corpus
test asserted only each case's primary `code` and `path`, so a bundle emitting
its expected diagnostic **plus five others** passed.

**Ruling.** Three obligations. `_read_slice` refuses a negative offset or length
before it seeks. The index reader reports `invalid.member-digest` for a slice
whose offset or length is negative, exactly as it does for one that overruns.
And `verify_document_release` is wrapped: any exception escaping any rule is
caught and reported as `invalid.root-syntax` naming the bundle, so **no input can
make the gate fail to produce a verdict**. A gate that can crash is a gate that
can be skipped. The conformance corpus test asserts the **full** diagnostic set —
every code and every path, in order — per bundle.

### B4 — attachments are enumerated, and the three named diagnostics get fixtures

**Finding.** This decision's L413 says "every attachment enumerated by its
owner's source record gets a row". All 1,683 selected Mirrulations documents
enumerate `fileFormats` (a `pdf` and an `htm`) in their preserved source
records; the builder hardcoded `attachments: []`
(`tools/build_document_release.py:665-666,778-783`) and the mint manifest
asserted compliance anyway. The three diagnostics this decision names —
`invalid.attachment-accounting`, `invalid.comment-selection`,
`invalid.retention-floor` — existed nowhere: not in `DIAGNOSTIC_CODES`, not in
the verifier, not in the invalid corpus.

**Ruling.** The builder enumerates. `fileFormats` is this decision's flat
document packing (`enrich_pdf.py:16-19`), and the flat list is a list of
**renditions of one attachment**, not a list of attachments: one row, `M`
sub-rows, exactly as *Cardinality* requires. `attachmentIdentity` is the
owner-scoped ordinal, because regulations.gov supplies no per-attachment id for a
document's own content file; `attachmentTitle` is null, because the flat packing
carries none.

**The `htm` rendition is the owning body's own rendition, and is
`text-excluded`.** This decision is genuinely ambiguous here — it says
attachments are "renditions of a member" and that every enumerated attachment
gets a row, and it never says whether the rendition the release already carries
as the document body counts twice. Resolved: **a rendition whose captured bytes
are already carried as its owner's body is enumerated with an honest row and the
disposition `text-excluded`, reason code `owner-body-rendition`, never extracted
a second time** — extracting it would put the same text in the corpus twice, and
omitting it would break the enumeration rule. The `pdf` rendition has no
preserved copy in the pinned checkpoint and is `source-unavailable` with reason
code `no-preserved-copy`; the ADR's own rule holds — the build fails when an
enumerated attachment has no row, never when a row honestly says it could not be
captured.

Because a rendition may name bytes that belong to another text body's slice of a
shared bucket, the gate resolves a capture against the `text-body-index` **by
digest** when the capture's own body is not indexed: the index already records
which byte range of which member digests to what, and a capture naming one of
those digests is verified against that range rather than against the whole
bucket. The body-keyed lookup keeps priority, so nothing about a document body's
verification changes.

`counts` gains `attachmentAccounting`, a closed object recomputed from the rows
— `{attachmentRows, renditionRows, textCaptured, textExcluded, sourceUnavailable,
extractionFailed}`. "Complete attachment accounting" is a claim
`COMPLETE-SEARCH-CORPUS` requires proving, and a declared tally a reader
recomputes is how it is proved from bundle bytes alone.

The three diagnostics land with one invalid-corpus fixture each, which means the
conformance corpus's valid bundle must finally **carry** an attachment and
comments — it carried neither, so two of the three record types this decision
sealed had never been minted by anything:

* `invalid.attachment-accounting` — non-dense `renditionOrdinal`s, more than one
  `text-captured` rendition, a row whose `textBodyId` and captured rendition
  disagree, a rendition that is not `text-captured` and carries no
  `reasonCode`/`reason`, or a declared `counts.attachmentAccounting` that
  disagrees with the rows. These leave `invalid.disposition` and
  `invalid.duplicate-identity`, where they never belonged: this decision is
  explicit that an attachment disposition is not a catalog disposition and that
  reusing the words invites a reader to join them.
* `invalid.comment-selection` — comment rows that do not all project **one**
  sealed policy. One release inherits one selection; two `policyDigest`s in one
  release is the release claiming a selection nobody sealed. Ordered immediately
  after `invalid.disposition`, as this decision already required.
* `invalid.retention-floor` — a declared floor that violates its own invariants
  (`0 < value < 1`, `observedMinimum > value`, a declared unit), or a text body
  whose `(textKind, mediaType)` has **no** governing `processingPolicies` entry.
  The second arm is the checkable half of "an undeclared floor fails closed": a
  body extracted under no declared floor is visible in the bundle without
  re-running any extractor.

### B5 — the retention floor measures parse quality, on a population it does not gate

**Finding, four defects in one.** The sealed `observedMinimum: 0.4777` was a
**400-document stride-sample statistic labelled as the population minimum** —
the true minimum over the gated corpus was 0.1936. The nine refusals the mint
recorded were **false**: those documents extract completely, and their ratio was
low only because 48–60% of their raw XML is pretty-print indentation, which the
denominator counted as content the parser had failed to keep. The floors were
**pooled with the corpus they gate**, so the calibration could not refuse
anything it had not already admitted. And the `distribution-html` population
this decision names went **unused**.

**Ruling, in four parts.**

1. **The metric changes.** Retention is
   `whitespace-normalized representation bytes / whitespace-normalized rendition
   bytes`, where normalization replaces every maximal run of ASCII whitespace
   bytes with one space and strips the ends. Both sides are normalized by the
   same rule, and that is not decoration: the HTML extractor lays out verbatim
   and the XML extractor lays out normalized, so an un-normalized numerator makes
   the two extractors' outputs incommensurable — measured, the raw-numerator form
   produces retention **above 1.0** on 993 `distribution-html` documents, which
   the floor's own arithmetic cannot represent. Normalized on both sides the
   ratio measures what it claims to: the share of the source's non-whitespace
   substance the parse kept. Publisher indentation cancels; a thin parse still
   craters it, because the numerator is what survived. The unit is renamed to
   `normalized-visible-text-fraction`; `visible-text-fraction` stays in the
   schema's vocabulary because the sealed predecessor corpus carries it, and
   nothing in the docspec generation may mint it again.
2. **The calibration population is disjoint from the gated corpus.** Each floor
   is measured on this decision's own preserved body corpus
   (`_preserved-2026-08-10/body-retrieval-corpus-2026-08-02`): `distribution-xml`
   for `application/xml`, `distribution-html` for `text/html` — the population
   this decision named and the first calibration skipped. Disjointness is
   **measured two ways, not asserted**: zero of the 1,986 rendition digests
   appears among the pinned corpus's captured digests, and -- because the same
   Federal Register document can be retrieved twice into different bytes -- zero
   of their 1,986 source document numbers appears among the pinned corpus's
   6,408. A digest check alone would have missed a redraw of the same document;
   the id check is the one that settles it. Both counts ride in the receipt, and
   a non-zero one drops those documents from the calibration. The pinned corpus contributes **no** stratum. A floor calibrated on
   the corpus it gates cannot refuse anything in it, which is what made
   `observedMinimum: 0.4777` both wrong and unfalsifiable.
   The `rule-bodies-pre2000-2026-08-22` population is measured and **not pooled**,
   recorded here so the choice can be re-argued: over its full 39,785 bodies its
   minimum is 0.0589 against a next-lowest of 0.2481, so pooling it would set an
   HTML floor of 0.044 — a floor that gates nothing, chosen by one outlier.
3. **The receipt carries both ratios for every document it measured**, raw
   (representation bytes over rendition bytes) and normalized, one row per
   document over the whole population. The gate keys on the normalized one only.
   The raw column is kept because normalization hides exactly one thin-parse
   mode: content whose meaning is carried BY its whitespace -- a positional
   table, an indented hierarchy -- is destroyed by a parse that keeps the
   characters and drops the layout, and the signature of that is a cratered raw
   ratio under a healthy normalized one. Two cheap columns, one signal nothing
   else in this format reports.
4. **`observedMinimum` is what its name says.** It is the minimum over the
   **full** named population, never a sample statistic. The calibration receipt
   is re-formatted so the confusion is not expressible: it has no field named
   `sample` anywhere, `population.coverage` is the constant `full-population`,
   `population.measuredCount` must equal `population.documentCount`, and
   `observedMinimum` must equal `distribution.minimum` over that same count. A
   receipt that measured a sample cannot be written in this format. The receipt
   is sealed by its own JSON Schema, `urn:docspec:schema:retention-floor-calibration:2.0`,
   and the builder refuses a receipt that does not validate against it.
5. **The margin rule is unchanged and stated:** the floor is three quarters of
   the population's observed minimum, truncated to two significant digits. It is
   not a low quantile — a p1 floor would refuse the bottom percent of the very
   population that defined it.

Measured under this ruling: `application/xml` floor **0.33** under an observed
minimum of **0.453** over 993 `distribution-xml` renditions; `text/html` floor
**0.17** under an observed minimum of **0.2382** over 993 `distribution-html`
renditions. Against the gated corpus these refuse nothing: its lowest XML
document measures 0.4558 and its lowest HTML document 0.7934. The nine false
refusals are readmitted, and the mint's selected count becomes 8,091.

### B6 — `selectedSourceSetDigest` is derived from the pin, and the gate stops recomputing it

**Finding.** The builder derived it from the release's **own** document rows
(`tools/build_document_release.py:754-762`), so a consumer holding the pinned
catalog bytes could not reproduce it — which is the one thing *The catalog pin*
says it exists to allow.

**Ruling.** The builder derives it over the **pinned catalog's** items, under the
catalog's own `docspec-selected-source-set/1` domain and record shape
(`adapters/source_catalog_artifact.py:375-395`), from a single exported function
so "the identical function over the identical pinned bytes" names something. The
domain covers **every item the pinned snapshot carries whose `state` is
`active`** — the catalog's own vocabulary for what it selected; `deleted` and
`excluded` items are, by that vocabulary, not selected. Each member is
`{sourceItemId, documentId}` where `documentId` is derived from the item exactly
as the builder derives it for a document row, so the pairing is a function of the
pinned bytes and nothing else. Under the D1 pin all 10,000 items are `active`.

**Consequence, recorded rather than glossed: the gate no longer recomputes this
digest.** A portable verifier reads one bundle and the pinned catalog is not in
it, so `selectedSourceSetDigest` becomes a pin-derived **attestation** whose form
the gate checks and whose value only a holder of the pinned bytes can. That is
the cost of deriving from the pin rather than projecting from the release, and it
is the cost this decision already chose when it said a consumer "runs the
identical function over the identical pinned bytes". The release's own selection
is not thereby unchecked: it is carried by `data/source-dispositions.jsonl`,
bound by `counts`, by the join receipt, by the bijection, and — under B1 — by
`sourceDispositionSetDigest`.

**Correction, 2026-08-31: that consequence is false for this format, and C3
below withdraws it.** The premise — "the pinned catalog is not in the bundle" —
is true of the catalog *bytes* and false of the *fact the digest names*.
`data/source-dispositions.jsonl` carries one row per member of `U` with both
fields the domain's record shape takes, so a bundle-only reader can reconstruct
the members and reproduce the value. Measured against
`output/document-release-10k-v2`, framing the 10,000 disposition rows as
`{sourceItemId, documentId}` under `docspec-selected-source-set/1` reproduces the
declared `sha256:7357b47d…b252cab` byte for byte. The derivation rule above is
unchanged and still right; what was wrong was the claim that it left the value
uncheckable. See **C3**.

### B7 — the standing obligations come due

**`RELEASE_FORMAT_VERSION` becomes `"2.0"`** (`src/docspec/domain/release.py:22`).
*Migration, and the builder's obligations* held it at `1.1` "until the builder
lands, then becomes `2.0` in one commit". The builder landed.

**The release URN prefix does not move with it.**
`urn:docspec:document-release:v2:` is a string downstream consumers pin, and
`RELEASE_FORMAT_VERSION` is a different fact from the identity namespace: the
format version says which contract the bytes obey, the URN prefix says which
identity namespace the name lives in, and *Sealed identities* fixed the latter
at `v2` for the 2.0 format deliberately. Flipping one must not move the other,
and anything that would move it stops and reports instead.

**The `reasonCode` vocabularies close** (amendment A3's first-real-mint
obligation, and *What this decision does not decide*'s `reasonCode` row). Two
vocabularies, closed separately because they are separately spelled:

*Source-disposition reason codes* — dotted, on `data/source-dispositions.jsonl`:

```text
catalog.state-deleted            the pinned catalog records the item as deleted
catalog.state-excluded           the pinned catalog records the item as excluded
selection.no-markup-rendition    no markup rendition, and JSON has no floor
capture.no-preserved-copy        the checkpoint preserved no copy
capture.preserved-copy-unverifiable   the preserved copy is not what was recorded
capture.expected-digest-differs  the catalog's expected digest names other bytes
extraction.no-extractor          no visible-text extractor for that format
extraction.unparseable-source    the captured bytes could not be parsed
extraction.no-visible-text       the parse produced no visible text
extraction.retention-floor-undeclared   no floor is declared for that parser
extraction.below-retention-floor        the parse fell below its declared floor
extraction.retention-unmeasurable       retention could not be measured
segmentation.refused             the bounded segmenter refused the text
segmentation.no-searchable-segment      the text produced no segment
segmentation.segment-over-declared-bound  a segment exceeded the declared bound
structure.heading-path-disagrees the section tree and the segmenter disagree
metadata.incomplete              the catalog item carries no required metadata
```

*Attachment rendition reason codes* — kebab-case, on the sub-rows of
`data/attachments.jsonl`:

```text
owner-body-rendition   these bytes are the owning body's own rendition (B4)
no-preserved-copy      the checkpoint preserved no copy of this rendition
```

Both close **in this decision**, not in the schemas: the schemas keep the
bounded-string patterns as the **outer** bound, which is what makes a producer's
new code a decision to record here rather than a schema migration. A code outside
these lists is a code somebody has to add to this amendment first.

### Sealed identities: the `/3` domains

Superseding the `/2` table. `F` marks a domain that frames the **full logical
row** minus B1's exclusion set; `P` marks a **projection** with the record shape
shown.

```text
F  docspec-source-disposition-set/3   by sourceItemId
F  docspec-document-version-set/3     by documentVersionId
F  docspec-attachment-set/3           by attachmentId
F  docspec-comment-set/3              by commentId
F  docspec-structural-node-set/3      by structuralNodeId
F  docspec-segment-set/3              by segmentId
P  docspec-text-body-set/3            {textBodyId, textKind}          by textBodyId
P  docspec-source-to-document/3       {sourceItemId, documentId,
                                       documentVersionId}             by sourceItemId
```

Each still calls the installed Rulespec `framedSectionDigest` with one `members`
section, keys still unique, declared count still equal to the streamed count,
rows still ordered by the key under the UTF-16 rule.

**A `/3` digest refuses a repeated key; it never absorbs one.** The guarantee
`invalid.duplicate-identity` already gave `sourceItemId` now holds for every one
of these domains, and it has to: with the physical locators excluded, two
identical attachments enumerated on one document would frame to one identical
record, and a set digest that folded them would leave a multiplicity change
invisible to the release's name -- the same shape of hole B1 exists to close.
Every row type carries a unique identity field with its own duplicate
diagnostic (`sourceItemId`, `documentVersionId`, `attachmentId`, `commentId`,
`structuralNodeId`, `segmentId`), the digester raises rather than dedupes, and
the invalid corpus proves it for the attachment set.

`selectedSourceSetDigest` stays at `docspec-selected-source-set/1` (B6) — it is
the catalog's fact under the catalog's name, and it is the one digest in this
format a bundle-only reader cannot recompute.

| Name | `$id` / form |
| --- | --- |
| retention-floor calibration receipt | `urn:docspec:schema:retention-floor-calibration:2.0` |

## Amendment 2026-08-31 (third): the post-gate defects and the re-gate refinements

The second mint — `output/document-release-10k-v2`, releaseId
`urn:docspec:document-release:v2:28a276a3…`, receipt
`docs/history/2026-08-31-document-release-10k-v2-mint-receipt.json` — **passed**
its blind re-gate. Two defects were found after it, one by its first real
consumer, and the re-gate's PASS verdict carried five refinements. Seven
rulings; the mint that comes out of them is a third one, and the second is
superseded rather than patched, for the same reason the first was: the record
changes, so the name does.

*All rulings below: Accepted-by: agent (delegated scope: owner 2026-08-30
"execute phase 1... you for human-less decision making"; post-gate findings and
re-gate refinements, 2026-08-31).*

### C1 — a processing policy is keyed by the media type the rows carry, and the gate stops collapsing

**Finding, by a consumer.** SpicySearch's document-release mapper refused the v2
bundle: *"document release declares no processing policy for
('document-body', 'text/xml')"*. It is right. All 6,408 Federal Register rows
carry `capture.mediaType: "text/xml"`, and the release declares its XML policy
under `mediaType: "application/xml"` — the *format key*
(`processing/retention_floors.py:100`), which is what a floor is looked up by, not
what a row says. The HTML half matched only because `text/html` happens to be its
own format key. The gate missed it because it collapsed both sides before
comparing (`_validate_processing_policies`'s `govern`), so the one check that
could have caught the mismatch was written to be blind to it.

**Ruling, two halves.**

1. **`processingPolicies[].mediaType` is the media type the capture record
   carries, verbatim.** Not the format key. The array is a per-`(textKind,
   mediaType)` table a consumer joins its rows against, and a table keyed on a
   value no row holds is a table nobody can join. Where two spellings of one
   family are both present — `text/xml` and `application/xml` — the release
   declares **two rows** with the same extractor, the same segmenter, and the
   same floor. That is not duplication: it is the table saying, of each media
   type it carries, which policy governed it.
2. **The gate matches literally.** `(textKind, capture.mediaType)` must appear in
   the declared set as written. No collapse on either side.

**One mapping, stated in one place.** A *floor* is a property of a parser and a
document family, so it is calibrated and looked up under the collapsed **format
key**; a *policy row* is a property of the bytes a release carries, so it is
declared under the **media type**. The one function that relates them is
`retention_floors.format_key`, and it is called on the lookup side only — by
`RetentionFloorRegistry.floor_for`, and by the builder resolving which calibrated
extractor governs a media type. The calibration receipt keeps its `formatKey`
label and its `population` ids unchanged; a policy row simply names its own media
type beside them. Nothing else in this format collapses a media type.

**No new diagnostic code.** Amendment B4 already assigns this rule to
`invalid.retention-floor` — "a text body whose `(textKind, mediaType)` has **no**
governing `processingPolicies` entry" — and the code is right; what was wrong was
that the check answered a question about format keys while the rule is about
media types. The invalid corpus gains a fixture for that arm
(`invalid/ungoverned-media-type`), which it never had: the sealed
`invalid/retention-floor` case exercised the floor-invariant arm alone, so the
governance arm shipped untested and shipped broken.

### C2 — a preserved copy the checkpoint's record layer lost is still rung one

**Finding.** Both mints refused 1,909 items with `capture.no-preserved-copy`. For
**194** of them that refusal is false: the campaign's own capture stores record a
successful capture, and for **193** of those the refused `rendition-html`
candidate's bytes exist **byte-identically** in the pinned checkpoint's blob
store already. What the checkpoint lacks is not the bytes; it is the *record
layer row* that points at them — `preserved_captures` reads
`runs/*/records/record-layers/*.json` (`tools/fr_mirrulations_pin.py:321-368`),
and for these items no such row came across in the salvage. The builder asked the
only index it had, was told nothing, and refused honestly. It was refusing an
index gap and calling it an absent document.

**Ruling: the supply ladder gains a rung *inside* rung one, not below it.**
"Adopt and verify: preserved-copy is rung one" is unchanged and is not weakened.
A **rescue map** is a second *pointer* into the same pinned blob store — a
record-layer supplement, not a second source of bytes — and the builder consults
it **only where the checkpoint's own records lack a pointer for that
`(sourceItemId, candidateId)`**. Three rules make it a rescue rather than a
fetch, and all three are mandatory:

1. **The bytes must already be in the pinned checkpoint.** A map row naming a
   blob the checkpoint does not hold supplies nothing and is ignored. The builder
   makes no request, and a rescue that had to reach for bytes would be a fetch
   wearing another name.
2. **The digest check is mandatory and identical.** A rescued blob is read
   through the same `PreservedCapture.read` every preserved copy is: size against
   the record, sha256 against the record, and then the catalog's own
   `expectedDigest` where it declared one. **A mismatch is a capture failure**
   — disposition `unavailable`, reason `capture.preserved-copy-unverifiable` —
   **never a reason to fetch.**
3. **The map is pinned by path and digest** in the same in-repo pin machinery the
   corpus is (`fixtures/fr-mirrulations-10k-v1/pins.json`), re-read and
   re-digested on every load, so a mint says which map it read and a map edited
   after the fact fails at the pin rather than changing a release downstream.

**Which file is the map, and why not the one the finding names.** The finding's
`detail-194.json` is the curated list — 194 ids, their candidates, digests, byte
sizes, media types, and the verified `missing_in_checkpoint: []`. It carries **no
acquisition clock**, and `captureRecord.acquiredAt` is required and non-nullable
(restamp item 14, deviation row 10: the clock stays in the record). A rescue from
that file would have to **invent** a wall clock for 387 captures, which is
precisely the kind of manufactured provenance this decision refuses everywhere
else. The pinned map is therefore
`_rescue-2026-08-31-qualification-store-map/full-store-map-v2.jsonl.gz`, the
complete campaign capture-disposition map extracted from the same stores, whose
rows carry the **record layer's own shape** — `sourceItemId`, `candidateId`,
`disposition`, `acquiredAt`, `mediaType`, and a `blob` of
`{digest, byteSize, locator}`. Its `acquiredAt` is the campaign's recorded
`2026-08-06T12:00:00Z`, the same value every one of the checkpoint's own 9,774
records carries; it records no start instant, so `acquisitionStartedAt` is
**null**, which is what a nullable field is for. Measured, the complete map under
rules 1 and 2 reduces to **exactly** the finding's 194 items and 387 blobs and to
nothing else — no new pointer for any already-captured item — so the broader file
buys the clock and costs no scope.

**Expected effect, recorded before the mint so the mint can be checked against
it:** 193 items move from `unavailable` to `selected` (the 194th,
`SEC-2020-1944-0001`, captured `metadata-json` only and stays honestly
unavailable); selected 8,091 → **8,284**, unavailable 1,909 → **1,716**, failed
**0**. **Floors apply to the rescued 193 exactly as to everyone else** — no
tuning, no exemption; a rescued document that fails its floor is refused with its
reason like any other. The 193 also carry preserved `metadata-json`, so their
attachments are enumerated like every other Mirrulations document's and
`counts.attachmentAccounting` grows accordingly; the accounting is **recomputed
from the rows**, never asserted. The mint receipt records the correction — how
many refusals from v1 and v2 it reverses — and the pinned map's digest.

### C3 — the gate recomputes `selectedSourceSetDigest` from the dispositions

**Finding.** B6's recorded consequence — "the gate no longer recomputes this
digest", because "a portable verifier reads one bundle and the pinned catalog is
not in it" — is **empirically false for this format**. The catalog's *bytes* are
not in the bundle; the *members the domain digests* are.
`data/source-dispositions.jsonl` carries one row per member of `U` with both
fields `docspec-selected-source-set/1` takes, and framing those rows reproduces
`output/document-release-10k-v2`'s declared value byte for byte. The rationale is
corrected in place above.

**Ruling.** The gate cross-checks it, under `invalid.set-digest` with the other
set digests. The dispositions-derived member set is
`{sourceItemId, documentId}` over **every disposition row whose `reasonCode` is
neither `catalog.state-deleted` nor `catalog.state-excluded`** — which is exactly
B6's "every item the pinned snapshot carries whose `state` is `active`", read off
the two codes that mean the catalog itself did not select the item. Every other
refusal in the vocabulary is *this producer's*, made about an item the catalog
did select, so it stays in the set. That keying is only sound because C4 closes
the vocabulary in the gate: an invented code could otherwise move an item in or
out of this set silently.

**What this does and does not change.** It does not change how the value is
*derived* — B6's rule stands, the builder still derives it over the pinned
catalog's items through one exported function, and a consumer holding the pinned
bytes still reproduces it that way. It adds a **second, independent** route to
the same value from bundle bytes alone, and requires the two to agree. A release
whose declared attestation disagrees with its own membership rows is now refused
rather than believed.

### C4 — the reason-code vocabularies are enforced, not merely written down

**Finding.** B7 closed two vocabularies "in this decision, not in the schemas",
with the schemas keeping their bounded patterns as the outer bound — and nothing
read the lists. They were a convention. The proof is
`unmapped-rendition-format` (`tools/build_document_release.py:202`), a code the
builder can emit that sits outside B7's two-item attachment list and that nobody
noticed, because nothing was looking.

**Ruling, three parts.**

1. **`unmapped-rendition-format` joins the closed attachment list.** It is a
   legitimate refusal — a source enumerating a rendition in a format this
   producer maps to no media type — and it is unreachable in this corpus only
   because every `fileFormats` entry in it is `pdf` or `htm`. A code that is
   right and unreached is kept and declared, not deleted to make a list tidy.
2. **The gate enforces both lists**, under the codes that already own the rows: a
   source-disposition `reasonCode` outside its list is `invalid.disposition`, an
   attachment rendition `reasonCode` outside its list is
   `invalid.attachment-accounting`. The schemas' kebab-case and dotted patterns
   stay exactly where they are, as the **outer** bound; what the gate adds is the
   inner one. Enforcement is the **docspec generation's alone** — the twenty
   sealed predecessor bundles were minted under no such list and are not
   retroactively judged by it. Each list gets an invalid-corpus fixture, so "the
   vocabulary is closed" is a thing that fails a test rather than a thing that is
   said.
3. **The source-disposition list gains the four codes the sealed conformance
   corpus projects.** The 10k builder *mints* its codes and every one is B7's;
   the conformance corpus's producer *projects* its pinned catalog's own
   `selection.reasonCode` verbatim, and that catalog's four codes —
   `policy.document-type-out-of-scope`, `source.withdrawn-after-publication`,
   `source.rendition-forbidden`, `source.metadata-unparsable` — are none of
   B7's. Both behaviours are correct for their producer, and a closed list that
   cannot spell the only sealed corpus in existence is a list the gate cannot
   turn on. They join as a named second group, and no real mint emits them:

```text
codes a pinned catalog projects verbatim (the conformance corpus's)
policy.document-type-out-of-scope   the catalog's selection policy excluded it
source.withdrawn-after-publication  the publisher withdrew it and serves a tombstone
source.rendition-forbidden          every candidate rendition was refused by the source
source.metadata-unparsable          the source metadata record could not be parsed
```

```text
attachment rendition reason codes, complete (superseding B7's two)
owner-body-rendition       these bytes are the owning body's own rendition (B4)
no-preserved-copy          the checkpoint preserved no copy of this rendition
unmapped-rendition-format  the source named a format this producer maps to no media type
```

B7's closing sentence is unchanged and now has teeth: **a code outside these
lists is a code somebody has to add to this amendment first.**

### C5 — the calibration receipt recomputes its distribution from its own rows

**Finding.** `validate_receipt` enforced `observedMinimum ==
distribution.minimum` and three count agreements, and never recomputed the
minimum — or the median, the quantiles, or either raw statistic — from the
document rows sitting beside them. A receipt whose `distribution` and
`observedMinimum` agreed with each other and with **neither** of its 993 rows was
expressible, and B5's whole point was that a self-consistent statistic is exactly
what went wrong the first time.

**Ruling.** `validate_receipt` recomputes, from `measurement.documents` alone:
every row's own `retention` and `rawRetention` from its own four byte counts, the
whole `distribution` object, `lowestDocument`, and the floor `value` the margin
rule implies. Each must equal what the receipt declares. The receipt keeps its
declared fields — a reader should not have to recompute to read one — but nothing
in it is believed.

### C6 — the conformance corpus finally mints a comment

Amendment B4 required the sealed valid bundle to "finally **carry** an attachment
and comments"; it landed the attachment and left `data/comments.jsonl` at zero
bytes, so one of the three record types this decision sealed had still never been
minted by anything. The valid bundle now carries **one comment row** with its own
capture, representation, structural node, segment, index slices, and per-kind
policy, and the whole corpus is resealed around it — every tree digest, every
count, every declared diagnostic set. Nothing structural prevented it: the
grown-bundle test had already proved a comment can live in a bundle without being
a selected member of `U`, and what was missing was the sealing, not the
possibility.

### C7 — floors sit outside the identity preimage, and what binds them instead

For the record, since nothing above says it and a reader could reasonably assume
otherwise: **`documentStateDigest` does not bind the governing retention floor.**
The floors ride in `processingPolicies`, and *Policy digests (avoid-lesson A7)*
put that member "**beside** the identity preimage, not inside it" — B1's
`logical_content` excludes it by name — so re-calibrating a floor over unchanged
text does not rename the corpus, which is the behaviour that section chose
deliberately. What binds a floor is therefore not the release's name: it is the
**calibration receipt**, sealed by
`urn:docspec:schema:retention-floor-calibration:2.0` and validated before the
builder may read a floor from it, and the **`processingPolicies` member itself**,
which is inside `content`, inside the global manifest, digest-pinned like every
other member, and cross-checked row by row by the gate. A reader asking "which
floor governed this text?" reads the member; a reader asking "is this the same
corpus?" reads the name; the two questions are deliberately not the same
question.
