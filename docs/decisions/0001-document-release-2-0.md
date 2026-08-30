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
`spicysearch@main:docs/superpowers/plans/2026-08-29-document-consolidation.md:128`);
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
| K14 invariants | Both adopted as builder diagnostics: an HTTP-200 challenge page is **quarantined, never sealed as body text**; a missing content-type **must not erase a rendition already identified by path** (consolidation K14, `spicysearch@main:docs/superpowers/plans/2026-08-29-document-consolidation.md:214`). |
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
