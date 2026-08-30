# DocumentRelease 2.0

## Portable wire contract — adoption specification

Editor's Draft — 30 August 2026

> **Partly superseded by Decision 0001 (2026-08-30),**
> `docs/decisions/0001-document-release-2-0.md`. This document records the
> *adoption* — what DocSpec took over from the Rulespec candidate, byte-faithful,
> and what the delta to the live 1.1 record was. It left §9's reconciliation
> question open, and Decision 0001 answered it, in places by deciding against
> what is written here. The four sections that no longer state the rule are
> annotated in place: **§2** (members and partitioning), **§3** (identity),
> **§6 row 10** (the capture wall clock), and **§8** (which schema carries the
> ownership prose). **§9** is annotated separately: it was overtaken by the
> verifier and restamper landing, not by a decision. Where this document and the
> decision disagree, the decision governs; this one is kept because the delta
> table and the provenance it records are still the record of where the format
> came from.

## Provenance

This specification was authored in the `rulespec` repository as
`spec/rulespec-document-release.md`, a candidate for a portable
`DocumentRelease` wire format. Rulespec deleted that work in `768ca58` under the
one-container rule: Rulespec owns portable schemas, generated types, identity
functions, validators, and conformance fixtures, but it owns no document
meaning, so a record format describing captures, representations, structure, and
segments cannot live there. The deletion removed the work without re-homing it.
The content survives at `rulespec@main` (`c584a1d`).

DocSpec adopts it here, as the owner. RefSpec REF-048 moved capture, document
processing, and `DocumentRelease` to DocSpec; SpicyRegs retains source
acquisition and faithful source-native publication. Ownership of the record
carries the obligation to validate it, so every sentence that read "Rulespec
validates" in the candidate now reads "DocSpec validates" here. The schemas and
fixtures move with the prose: `src/docspec/schemas/document_release/2.0/` and
`tests/fixtures/document_release_v2/`.

This document records intent. It does not prove implementation. No production
code changes with it — `src/docspec/domain/release.py` still writes
`docspec-document-release` at `formatVersion` `1.1`, and §6.1 records exactly how
far that is from what follows. Reconciling the two is the next decision, not
this one.

## 1. Why version 2.0

DocSpec's live root writes `format: "docspec-document-release"` at
`formatVersion: "1.1"` (`src/docspec/domain/release.py:21,22`). That is a
different artifact: an internal pointer-record of active layers, blob roots,
store receipts, and a partition policy, whose members live in stores this
release format does not describe.

This is the portable wire contract — a self-contained bundle carrying the
dispositions, captures, representations, structure, and segments themselves.
Publishing it as `1.0` under the same token would place the portable shape
*below* the internal one on one version line, so a reader would take 1.1 for a
newer superset of it. `2.0` says what is true: same product, same logical
artifact, not compatible with 1.1.

The token stays in DocSpec's namespace because DocSpec owns the records. The
identity URN follows DocSpec's own `stable_urn` convention
(`urn:docspec:<kind>:v<n>:<digest>`, `src/docspec/domain/identity.py:205`), so
this release is `urn:docspec:document-release:v2:<digest>`.

The `v2` in that URN was chosen in the candidate to avoid colliding with live
releases the candidate believed already minted `urn:docspec:document-release:v1:`
over a different identity preimage. That premise is false at DocSpec HEAD: a
live `DocumentRelease` is named by a derivation logical ID matching
`urn:spicy:artifact:derivation:[0-9a-f]{64}` (`release.py:24,48`), minted by
`derivation_logical_id` over the processing plan and partition policy
(`src/docspec/adapters/platform_artifact.py:262`), not by `stable_urn` over the
release content. There is therefore no live `v1` under this kind to collide
with. `v2` is kept anyway: it matches the `2.0` format version, and the name
should not need a second explanation.

## 2. Bundle shape

```text
release.json                                       root manifest
manifests/global.json                              the one member manifest
schemas/document-release-v2.schema.json            release root schema
schemas/member-manifest-v1.schema.json             member manifest schema
schemas/source-dispositions-v1.schema.json         disposition projection schema
schemas/documents-v1.schema.json                   document/capture/representation schema
schemas/structural-nodes-v1.schema.json            structural node schema
schemas/search-segments-v1.schema.json             search segment schema
data/source-dispositions.json                      one row per member of U
data/documents.json                                one row per document/version
data/structural-nodes.json                         source-derived structure
data/search-segments.json                          bounded, deterministic segments
blobs/<documentId>.<ext>                           exact captured rendition bytes
text/<documentId>.txt                              the selected visible-text representation
```

Only relative POSIX member paths. No absolute path, no parent traversal, no
symlink. Every member carries its exact `byteSize` and `sha256`.

> **Superseded — Decision 0001, "The format", *Member set*.** The two sentences
> this section rests on are both reversed there. `data/*` members are **JSONL**,
> one canonical-JSON record per line, not the single-line JSON arrays the sealed
> fixtures carry; and `blobs/`/`text/` are **partitioned** members following the
> `SourceCatalog` multipart pattern — rows bucket by digest of their
> `textBodyId`, each bucket written once as a `blobRef`-carrying member with a
> `recordCount` — not one member per document. "v2 does not partition" is
> therefore false as written: what v2 does not partition is the *release*, which
> stays one bundle (Decision 0001, *What this decision does not decide*, "Scale
> beyond 10k"); its members do partition. One member per text body would put
> 10k+ entries in `globalManifest` at the first mint. The member list also grows
> by `data/attachments.jsonl` and `data/comments.jsonl`, and the schema list from
> six to eight (Decision 0001 restamp items 2, 11).

The member paths above are the paths *inside a bundle*, and the sealed fixtures
use them verbatim. The same six schemas are also packaged for DocSpec's own use
at `src/docspec/schemas/document_release/2.0/`, under DocSpec's packaged-schema
naming and URN `$id` scheme; §8 records that mapping.

## 3. Identity

There are **two** minting generations under this format version, and the rule
below is only the first one's. Both are live, because the sealed conformance
corpus was minted under one and everything DocSpec will ever produce is minted
under the other. `src/docspec/adapters/document_release_verify.py` reads which
generation a bundle declares — from its schema `$id`s — and checks it against
the rules it was minted under.

**The predecessor generation** — the twenty sealed bundles in
`tests/fixtures/document_release_v2/`, and nothing else, ever again:

```text
urn:docspec:document-release:v2:<sha256 over canonical {format, formatVersion, content}>
```

Canonical JSON is DocSpec's `canonical_json_bytes`
(`src/docspec/domain/identity.py:112`) — UTF-8, sorted keys, no insignificant
whitespace, no floating point, no unsafe integer — which equals RFC 8785 on that
value domain. The digest is over the **whole** `content`, member manifest and
packing counts included, so a physical-only repack of one of these bundles
renames it.

**The docspec generation** — every bundle minted under Decision 0001, and the
shape the restamp moves the sealed corpus to. Two names over one content, and
the second is derived rather than minted:

```text
documentStateDigest  sha256:<sha256 over artifact-canonical {format, formatVersion, logicalContent}>
releaseId            "urn:docspec:document-release:v2:" + documentStateDigest hex
```

`logicalContent` is `content` minus every physical or packing fact —
`globalManifest`, `counts.memberCount`, `counts.totalMemberByteSize`,
`processingPolicies` — so a physical-only repack **preserves** the identity,
which `INCREMENTAL-EQUIVALENCE` requires and the flat digest above breaks.
`coverage`'s byte totals stay in: they are facts about the corpus text, not about
how it was packed. The canonicaliser is the container's,
`rulespec_artifacts.canonical_json_bytes`, not DocSpec's (Decision 0001, D2), and
the root gains `documentStateDigest` as a key (restamp item 8). Decision 0001,
*Identity — two minted names, one derived form*, is the governing text.

Both generations agree on the rest. Root and member bytes are exactly canonical
bytes with **no** trailing newline; `canonical_json_file_bytes` is the file form
and is not used here. `annotations` is excluded from every preimage, and that is
where `publishedAt`, `releaseStatus`, and `buildRunId` live, so two builds of
identical corpus content share one identity. The format token and version are
**inside** the preimage: a content-only digest lets a future reshape of the same
fields mint a colliding name, and binding the token closes that.

## 4. What the release contains

**Disposition projection.** One row per member of the requested universe `U`,
carrying the catalog's disposition verbatim. A consumer obtains corpus
membership and exclusion coverage from this release alone, without reading the
source catalog. A capture result never feeds a fact back into the catalog.

**The bijection is structural.** A row whose `catalogDisposition` is `selected`
MUST carry a `documentVersionId`; any other disposition MUST carry `null`. There
is no corpus-side disposition that discards a selected item, so a processing
failure cannot become a silent downstream exclusion. A selected item that cannot
be made searchable blocks the build. `processingFailures` records attempts; it is
never load-bearing for membership.

**Capture.** Each document names the exact catalog release, `sourceItemId`,
`documentId`, `sourceIssuedVersion`, and `candidateRenditionId` its bytes came
from, plus the digest-pinned member holding those bytes. When the catalog
declared an `expectedSha256`, it MUST equal the captured digest.

**Representation.** Exactly one human-readable Unicode representation per
document, `text/plain; charset=utf-8`. Markup is not search text: HTML and XML
are extracted to visible text before segmentation.

**Structure and segments.** Source-derived `structuralNode` records form a tree;
a child's range lies inside its parent's, sibling ordinals are dense and
zero-based, and a section node spans its whole section. Each `searchSegment`
carries a `structuralParentId`, a dense document-wide `ordinal`, the
`headingPath` from the root down to its parent, its representation range, and
reversible `evidence` coordinates naming the captured rendition **by digest**.

**Offsets are bytes.** Every range is half-open `[start, end)` over UTF-8 bytes.
`spicy-regs/PLAN.md` §1b decides byte offsets for the ecosystem, and DocSpec's
own `EvidenceMapping` already counts a half-open representation byte interval
(`src/docspec/domain/content.py:429`).

**Coverage.** Every byte of the selected representation is covered by at least
one search segment or by exactly one excluded range, and the two never overlap.
Segments may overlap each other; `segmentedByteTotal` is the size of their
union, so `segmentedByteTotal + excludedByteTotal == representationByteTotal`
holds regardless.

**Set digests and the join receipt.** `selectedSourceSetDigest`,
`documentVersionSetDigest`, and `segmentSetDigest` are canonical set digests over
deduplicated sorted identifier lists. `sourceDocumentMappingDigest` is a **list**
digest over the sorted `[sourceItemId, documentVersionId]` pairs — the pairing is
the fact, so a repeated pair must move the digest rather than be folded away. The
`joinReceipt` seals that digest with both counts; equality plus distinctness is
the proof of the bijection.

## 5. Diagnostics and first-failure order

```text
invalid.root-syntax          invalid.source-catalog-pin
invalid.format               invalid.disposition
invalid.identity             invalid.capture
invalid.path                 invalid.representation
invalid.membership-missing   invalid.structure
invalid.membership-extra     invalid.segment
invalid.member-digest        invalid.coverage
invalid.schema               invalid.join
invalid.duplicate-identity   invalid.set-digest
                             invalid.counts
```

Bundle integrity first: nothing can be judged until the bytes are trusted, and
`invalid.path` outranks the membership codes — an unsafe member path must be
refused before anything tries to resolve it, or the refusal happens after the
read it was meant to prevent. The domain half runs in dependency order — a
segment cannot be judged against a structural parent whose own range is already
known wrong, structure cannot be judged against an untrusted representation, and
a representation cannot be judged before the capture it was extracted from.

DocSpec validates this. Ownership of the record carries the obligation; the
diagnostic set above is DocSpec's to emit, in this order, from DocSpec's own
validator.

## 6. Deviations from DocSpec's live `docspec-document-release` 1.1

This is DocSpec's migration work. Recorded so the delta is a list rather than a
discovery. The table below was reproduced verbatim from the candidate; §6.1
records what each row is worth against the code as it stands. **One row is no
longer verbatim**: row 10's proposal was decided the other way in Decision 0001
and is annotated in place, because a delta table that still proposes what was
decided against is not a record of the delta, it is a second claimant. Every
other row stands as the candidate wrote it.

| # | Live 1.1 | Portable 2.0 |
| --- | --- | --- |
| 1 | Root is a pointer-record: `activeLayers`, `blobRoots`, `storeReceiptSetDigest`, `runReceipt`, `catalogCommitReceipt`, `partitionPolicy` | Root describes a self-contained bundle: one member manifest, complete member digests, relative paths only |
| 2 | `counts`, `coverage`, `failures`, `partitionPolicy` are open `dict[str, Any]`, validated only as non-negative integers | All closed objects with named, required, recomputed fields |
| 3 | Identity digests `_content` alone (`release.py:64,156`); `format`/`formatVersion` sit outside the preimage | Identity digests `{format, formatVersion, content}` |
| 4 | `canonical_json_file_bytes` appends a trailing newline (`identity.py:98`) | Member and root bytes are exact canonical JSON with **no** trailing newline, matching `ExtrapolationRelease` v2 |
| 5 | No disposition projection over `U`; membership is implied by what is present | One required row per member of `U`, with the catalog disposition projected verbatim |
| 6 | `AcquisitionDisposition` and `ProcessorDisposition` both admit `accepted-failure` and `rejected-run` (`content.py:29,38`) | No corpus-side disposition exists; a selected item is a document or the build fails |
| 7 | No structural-node record. `Segment` carries `kind` and `derivation` but no parent, depth, or heading path (`content.py:652`) | `structuralNode` records with `structuralParentId`, `depth`, dense sibling `ordinal`, and containment; segments carry `headingPath` |
| 8 | `Representation.warnings` is a free string tuple (`content.py:492`) | Explicit `excludedRanges` with byte ranges, machine-legible `reasonCode`, and prose `reason` |
| 9 | No selected-source, document/version, segment, or source-document mapping digest; only `store_receipt_set_digest` and `logical_state_digest` | Four canonical digests plus a sealed one-to-one join receipt |
| 10 | `CapturedFile.acquired_at` is required, and `acquisition_started_at` optional (`content.py:243,247`) | **[Decided otherwise — Decision 0001 row 10.]** The candidate proposed removing the wall clock. It is **kept in the capture record** and **excluded from every preimage** instead: the identity argument governs preimages, not records, and erasing acquisition time would destroy provenance the reader needs. `captureRecord` therefore *gains* `acquiredAt` and a nullable `acquisitionStartedAt` in the restamp (item 14), and the two schema descriptions that assert the opposite are corrected with it |
| 11 | `ArtifactRef.locator`/`BlobRef.locator` are free strings and may be absolute | Every member path is a checked relative POSIX `objectKey` |
| 12 | Evidence names its source by `EvidenceCoordinate.source_digest` with optional `start`/`end`/`page`/`region` | Evidence requires `coordinateSystem`, `renditionSha256`, `start`, `end`, and must resolve inside the named rendition |
| 13 | No schema set inside the release | The six schemas ride inside the bundle, digest-pinned, so a consumer verifies with no Rulespec checkout |

Items 1, 3, 4, 5, 7, 8, 9, 10, and 11 are breaking for a 1.1 producer. Items 2,
6, 12, and 13 are tightenings a 1.1 producer can satisfy without reshaping its
own records.

### 6.1 Each row against the code as it stands

Every row was re-checked against `main` at `e8ee58f`. The headline: **no row is
already satisfied.** All thirteen are live deviations. The candidate's 9-breaking
/ 4-satisfiable split holds, but three rows describe the code inaccurately and
are corrected below — the corrections make row 3 *worse*, not better.

| # | Verdict | Evidence at HEAD | Note |
| --- | --- | --- | --- |
| 1 | Breaks | `release.py:31-45` fields, `to_dict` keys at `release.py:183-201` | Confirmed as written. No member manifest, no member digests, no bundle. |
| 2 | Breaks (satisfiable) | `release.py:42-45,57-68` | Confirmed, and the row understates it. Only `counts` gets the non-negative-integer check (`release.py:57`). `coverage`, `failures`, and `partitionPolicy` are passed through `freeze_json`/`thaw_json` with **no value validation at all** — any JSON shape is admitted. |
| 3 | Breaks — **row is materially wrong** | `release.py:24,48`; `application/commit.py:459`; `adapters/storage.py:1680`; `adapters/platform_artifact.py:262-272` | The live identity does not digest `_content`. `release_id` is supplied by `DocumentCatalog.release_id(plan, partition_policy)` and is a `derivation_logical_id` over `{format, formatVersion, inputs, kind, spec}` where those tokens are the **`rulespec_artifacts` derivation-envelope** constants, not `docspec-document-release`/`1.1`. `release.py:48` requires the result to match `urn:spicy:artifact:derivation:[0-9a-f]{64}`. `identity_content()` feeds only `to_dict()`/`file_bytes`, never a name. So the live name is a function of the *plan*, not the *content*: two builds of identical corpus content under different plans get different names, and the same content republished under one plan gets the same name regardless of what changed downstream. The cited line numbers `release.py:64,156` are stale. |
| 4 | Breaks | `release.py:225-226`; `identity.py:119-122` | Confirmed; the cited `identity.py:98` is stale. Empirically: `tests/fixtures/document_release_v2/valid/release.json` carries **no** trailing newline and parses under `parse_canonical_json(..., file_form=False)`; the `invalid/noncanonical-root` fixture's entire single-rule mutation is the addition of that newline — i.e. the invalid fixture is byte-for-byte what DocSpec's `file_bytes` emits today. |
| 5 | Breaks | no projection record in `src/`; `requestedUniverseSetDigest`/`selectedSourceSetDigest` appear only on the source-catalog side (`adapters/source_catalog_artifact.py:115-116,375`) | Confirmed. The DocumentRelease root carries neither. |
| 6 | Breaks (satisfiable) | `content.py:29,34-35,38,42-43` | Confirmed; cited line numbers accurate. Satisfiable only as a **build gate** — the enum members stay in the domain, so conformance means a producer that never emits them, not a narrower type. |
| 7 | Breaks | `content.py:652-665` | Confirmed; cited line accurate. `Segment` has `segment_id`, `source_item_id`, `file_id`, `representation_id`, `representation_start`/`_end`, `ordinal`, `kind`, `content`, `evidence`, `segmenter_id`, `policy_digest`, `derivation`. No structural-node type exists anywhere in `src/`. |
| 8 | Breaks | `content.py:492,502` | Confirmed; `warnings: tuple[str, ...] = ()`. No excluded-range record exists. |
| 9 | Breaks | `release.py:39,161-171` | Confirmed. `store_receipt_set_digest` and the `logical_state_digest` property, nothing else. Nuance: DocSpec *does* implement `selected_source_set_digest` (`adapters/source_catalog_artifact.py:375`), but on the catalog builder — it is never carried on the release. |
| 10 | Breaks | `content.py:236,243,247` | Confirmed; cited line accurate. `acquired_at` is a required positional validated by `require_text`; `acquisition_started_at` defaults to `None`. |
| 11 | Breaks | `references.py:21,57` | Confirmed. Both are `require_text` only. DocSpec already owns the right check — `require_relative_path` (`identity.py:42-49`) — and simply does not apply it to locators. |
| 12 | Breaks (satisfiable) | `content.py:384-390` | Confirmed. `coordinate_system` and `source_digest` are required; `start`, `end`, `page`, `region` all default to `None`. Satisfiable: a producer can always populate `start`/`end`, and `source_digest` → `renditionSha256` is a wire-name change, not a shape change. |
| 13 | Breaks (satisfiable) | `release.py:173-179` | Confirmed; `to_dict` has no `schemaSet`. Additive. The packaging habit already exists — `src/docspec/schemas/{source_catalog/1.0,scale_profile/2.0,scale_result/1.0}` — and this adoption adds `document_release/2.0` to it. What is new is carrying the schemas *inside the bundle*. |

Two consequences for the reconciliation decision that follows this document:

1. **Row 3 is the deepest cut.** The candidate framed it as "move two tokens
   inside the preimage". The real gap is that the live release has no
   content-derived name at all. Adopting v2 identity is not an adjustment to the
   existing digest, it is the introduction of one.
2. **Nothing is free.** No row can be checked off before work starts, so a
   staged migration has to sequence all thirteen. The four marked satisfiable
   are cheap, not done.

## 7. Conformance fixtures and the candidate bundle digest

`tests/fixtures/document_release_v2/` holds one valid bundle and one invalid
bundle per diagnostic code — twenty in all — each a single-rule mutation of the
valid one with every downstream digest, count, coverage figure, and identity
restamped. Every byte offset in the corpus is derived from the fixture's own
bytes; hand-written offsets in a corpus about offsets would test the author's
arithmetic instead of the validator.

The valid bundle is built from the sealed `SourceCatalogRelease` v1 fixture, and
pins it by identity and digest — the two candidates are joined, not merely
adjacent.

`corpus.json` names each bundle, its expected diagnostic code, its expected
failure path, and its `treeSha256`. Those tree digests, the `schemaSetId` in each
`release.json`, the per-member `sha256` values in each `manifests/global.json`,
and each bundle's `releaseId` are all sealed over the bundle bytes **as
authored**. The bundles are therefore copied here byte-for-byte, including the
`https://rulespec.org/...` `schemaId` strings they carry internally; see §8.

The candidate record `release-records/document-release-v2-candidate.json` — a
`RulespecCoreRelease` pinning the schemas, the validator modules, and every
sealed bundle — is **not** ported. It is a Rulespec release record naming
Rulespec module paths, and it has no meaning in this repository. The equivalent
DocSpec pinning is part of the reconciliation work, not this adoption.

## 8. Schema identifiers

The six schemas are packaged at `src/docspec/schemas/document_release/2.0/`
under DocSpec's existing packaged-schema conventions: the version is the
directory, so the filename carries no version suffix, and the `$id` is a
`urn:docspec:schema:<name>:<version>` URN, mirroring
`src/docspec/schemas/source_catalog/1.0/`. Each schema's `$id` was the only byte
changed; every `$ref` in all six is an internal `#/$defs/...` pointer, so no
cross-reference needed adjusting.

| Candidate path | DocSpec path | Candidate `$id` | DocSpec `$id` |
| --- | --- | --- | --- |
| `schemas/document-release-v2.schema.json` | `document-release.schema.json` | `https://rulespec.org/schemas/releases/document-release-v2.schema.json` | `urn:docspec:schema:document-release:2.0` |
| `schemas/document-release-v2/member-manifest-v1.schema.json` | `member-manifest.schema.json` | `https://rulespec.org/schemas/releases/document-release-v2/member-manifest-v1.schema.json` | `urn:docspec:schema:document-release-member-manifest:2.0` |
| `schemas/document-release-v2/source-dispositions-v1.schema.json` | `source-dispositions.schema.json` | `https://rulespec.org/schemas/releases/document-release-v2/source-dispositions-v1.schema.json` | `urn:docspec:schema:document-release-source-dispositions:2.0` |
| `schemas/document-release-v2/documents-v1.schema.json` | `documents.schema.json` | `https://rulespec.org/schemas/releases/document-release-v2/documents-v1.schema.json` | `urn:docspec:schema:document-release-documents:2.0` |
| `schemas/document-release-v2/structural-nodes-v1.schema.json` | `structural-nodes.schema.json` | `https://rulespec.org/schemas/releases/document-release-v2/structural-nodes-v1.schema.json` | `urn:docspec:schema:document-release-structural-nodes:2.0` |
| `schemas/document-release-v2/search-segments-v1.schema.json` | `search-segments.schema.json` | `https://rulespec.org/schemas/releases/document-release-v2/search-segments-v1.schema.json` | `urn:docspec:schema:document-release-search-segments:2.0` |

Two open items follow from this and belong to the reconciliation decision, not
here:

- **The fixtures still name the old `$id` values.** Every bundle's
  `manifests/global.json` and `release.json` `schemaSet` carry the
  `https://rulespec.org/...` strings, and those strings are inside the digest
  preimages that `corpus.json` seals with `treeSha256`. Rewriting them by hand
  would invalidate the member digests, the manifest digest, the `schemaSetId`,
  the member `byteSize` counts, the `releaseId`, and the tree digest of every one
  of the twenty bundles. The fixtures are sealed evidence; they are restamped by
  a builder or not at all. Until a DocSpec builder exists, the packaged schemas
  and the fixture-embedded schemas are byte-identical apart from the `$id` line.
- **The root schema's `description` still says Rulespec owns it.** **One** of
  the six carries that prose, not each: `document-release.schema.json:5`,
  "Rulespec Core owns this schema; DocSpec owns the records it carries
  (REF-024)". The other five describe their records and claim no owner. That one
  sentence is stale under REF-048. It was left byte-faithful in this port so the
  diff shows exactly one changed line per file; correcting it is a deliberate
  edit, and it should happen alongside whatever restamps the fixtures
  (Decision 0001 restamp item 12, which also redirects the *second-consumer*
  correction to the sentence that actually needs it —
  `search-segments.schema.json:5`, "SpicySearch consumes these").

## 9. What this adoption does not do

> **Overtaken by events.** This section described the state on the day of
> adoption. Since then the conformance validator and the fixture restamper moved
> in (`src/docspec/adapters/document_release_verify.py`,
> `src/docspec/document_release_support.py`,
> `tools/restamp_document_release_fixtures.py`), so the packaged 2.0 schemas
> **are** read by production code and every sealed bundle is run by
> `tests/test_document_release_verify.py`. What is still true is the sentence
> that matters: `src/docspec/domain/release.py` is untouched and
> `RELEASE_FORMAT_VERSION` remains `"1.1"`, so nothing is minted in 2.0 yet.
> The next decision this section asks for is Decision 0001, which is written.

No production code changes. `src/docspec/domain/release.py` is untouched, and
`RELEASE_FORMAT_VERSION` remains `"1.1"`. Nothing in DocSpec reads the 2.0
schemas at runtime; `tests/test_document_release_schema_bundle.py` is the only
consumer, and it asserts that the bundle loads, that all six schemas are valid
JSON Schema, that the valid fixture satisfies the root schema, and that four
clearly invalid fixtures are refused.

Reconciling the live record with this contract — deciding whether 1.1 migrates,
is replaced, or coexists, and in what order the thirteen rows are closed — is
the next decision.
