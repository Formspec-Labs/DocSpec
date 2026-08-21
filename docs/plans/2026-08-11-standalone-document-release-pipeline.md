# Standalone Document Release Pipeline

**Date:** 2026-08-11

**Target:** One DocSpec pipeline that consumes a sealed `SourceCatalogRelease` and publishes a portable, immutable `DocumentRelease`, with no sibling-checkout dependency

**Boundary:** REF-024, in `RefSpec/docs/decisions.md`

**Source:** Section 6 of `spicysearch/docs/history/2026-08-11-cross-product-reconciliation-recommendations.md`

**Evidence commits:** DocSpec `7e3d0f2`, Rulespec `b64ca67`

## Decision

DocSpec runs the pipeline end to end from a sealed input release:

1. Consume a sealed `SourceCatalogRelease`.
2. Capture the selected source files.
3. Produce normalized representations, document nodes, structural segments, evidence coordinates, and coverage information.
4. Publish a portable, immutable `DocumentRelease`.

The seam is `SourceCatalogRelease -> DocumentRelease`. REF-024 states the ownership rows and the exchange rule; this plan does not restate them. The internal source-access port added below implements that seam. It does not replace it, move it, or introduce a second one.

DocSpec remains independently usable by SpicySearch and by other products. A consumer needs the published `DocumentRelease` and the installed DocSpec package. It does not need a SpicyRegs checkout, a SpicyRegs output directory, or any DocSpec tool that reaches outside this repository.

## Verified starting point

Checked against DocSpec `7e3d0f2` and Rulespec `b64ca67` on 2026-08-11.

### What the release side already has

- `src/docspec/domain/release.py:188` writes `"format": "docspec-document-release"`; `:215` requires `"formatVersion": "1.1"` on read.
- `src/docspec/domain/references.py:174-178` defines `SourceCatalogRef(catalog_id, locator, digest)`. The reference already carries a SHA-256 digest, so a digest-addressed handle for the input release needs no new identity type.
- `src/docspec/ports/source_catalog.py:41-50` defines the `SourceCatalog` Protocol with `open`, `verify`, `describe`, and `stream`, each keyed by `SourceCatalogRef`; `:53` aliases it as `SourceCatalogReader`.
- `src/docspec/adapters/source_catalog.py:64-72` defines `LocalJsonlSourceCatalog.write(items: Iterable[SourceItem], *, kind, base_catalog, partitions, coverage) -> SourceCatalogRef`. It already consumes exactly what a discovery-shaped port would emit.
- `src/docspec/domain/content.py:191-196` defines `SourceItem(item_id, version, candidates, state, metadata)`.

### What `src/docspec/ports/` holds

- 16 port modules, 21 `Protocol` classes, 29 exported names in `ports/__init__.py:20-50`.
- None of the 21 is discovery-shaped. `SourceCatalog` reads a fixed distribution; `ContentFetcher` acquires one candidate.
- `ports/content_fetcher.py:88` binds `AcquisitionSource = ContentFetcher`. The new port needs a different name. Verified at `:88` both in the working tree and at `7e3d0f2`.

### Where the sibling-checkout crossings were

Removed on 2026-08-11 in DocSpec `2df6e4f` (pinned input), `8af75cc` (deletions), and `bdca9d1` (gate). Every site the recording named is gone.

| Face | Former site | What replaced it |
| --- | --- | --- |
| Absolute sibling path | `:55` `DEFAULT_SPICYREGS` | `DEFAULT_INPUT_MANIFEST = REPO_ROOT / "fixtures/qualification/fr-mirrulations-10k-v1/input-manifest.json"` |
| Gitignored sibling output | `:56` `DEFAULT_FR_ROOT` | `--federal-register-root` has no default; `_inputs` refuses without it |
| Subprocess into the sibling checkout | `:142-160` | Nothing. The draw arrives pinned; no draw is built here |
| Private-symbol import through `python -c` | `:161-168` | `validate_mirrulations_draw` in `tools/fr_mirrulations_support.py:641`, which already recomputed the draw identity, schema, source boundary, selection, and every document's closed shape from the bytes |

Supporting sites, all deleted in the same change set:

- `:58` `BUILDER_RELATIVE`, `:630` `--spicyregs`, and `:241`, `:313-315` passing that root into the execution manifest.
- `:69-74` `_run_checked` and its three `cwd=spicyregs` call sites — `:95` `git rev-parse HEAD`, `:142`, `:168`.
- `:87-185` `freeze_producer_inputs`, including `:90`, `:96`, `:128`, `:174`, and the `producer/spicyregs-validation.json` receipt it wrote into gitignored output. `admit_producer_inputs` at `tools/fr_mirrulations_qualification.py:91` replaces it.
- `tools/fr_mirrulations_support.py:1019` and `:1024-1027`, the per-validation re-hash of each draw file and of the producer's builder source. `build_source_items` rereads the same draws immediately below and its recomputed digests are already compared against the manifest, so the repeat added work and no guarantee. The producer facts stay sealed by the manifest identity.
- `tools/fr_mirrulations_support.py:853-855`, `:908`, `:910`: `build_execution_manifest` took `spicyregs_repository`, `spicyregs_commit`, and `spicyregs_builder` and called `resolve(strict=True)` against the sibling checkout at manifest-write time. It takes one `mirrulations_producer` mapping now, validated by `require_producer_identity`, which resolves nothing.

### Where the draw pin lives

`fixtures/qualification/fr-mirrulations-10k-v1/` holds two tracked files:

- `mirrulations-draw.json`, a byte-identical copy of the draw the campaign produced: 1,979,300 bytes, `sha256:48b2eb86bcd363401fa3f4615dcb1be16f7c7c1a0b6f9a5d91ca1162dbd2350c`, identity `urn:spicyregs:mirrulations-document-draw:d82165252eb1a6d050ab80bd`, 3,592 documents.
- `input-manifest.json`, identity `urn:docspec:qualification-input-manifest:v1:c15a4c01d209edd348b17f4c8069f87095b69441348f3600fae16b988ab6def7`. It pins the draw by relative path, digest, and byte length, and carries the producer facts the deleted receipt used to read out of the checkout: name `SpicyRegs`, commit `adbd5a2b58391c5f0f623fad20674c7851251660`, builder path `src/spicy_regs/corpora/mirrulations_document_corpus.py` inside the producer's own repository, and builder digest `sha256:78e9c8bd8dad38c1afc6fb48a7c557a12015755a22e814fa92c0d8832d2f1e43`.

This follows the RefSpec ICPSR precedent: the campaign's own copy stays where it is under gitignored `output/`, and a byte-identical tracked copy with a recorded digest is what the harness reads.

`admit_producer_inputs` consumes it by (path, expected digest). It reads the manifest as canonical JSON, checks its closed shape and content-derived identity, resolves the draw through `require_relative_path` against the manifest's own directory, bounds the read at 64 MiB, compares the file size, recomputes `sha256_digest` over the bytes, and refuses a mismatch before any parser sees them — the shape `LocalSourceReleaseReader._reference` uses at `src/docspec/adapters/source_catalog.py:274`.

The captured Federal Register root is not pinned. It is 192 MB of cached source bytes plus a 3.5 MB draw manifest, so the operator names it with `--federal-register-root` and preparation refuses without it. No file names where it sits.

### Rulespec dependency

- The `SourceCatalogRelease` and `DocumentRelease` schema sets arrive as one digest-addressed bundle inside the Rulespec Core wheel, alongside generated types, identity and digest helpers, validators, diagnostics, and valid and invalid conformance fixtures.
- Neither schema root exists in Rulespec at `b64ca67`. `SourceCatalogRelease` appears in `rulespec/TODO.md` only, and nowhere in code, schemas, or fixtures. The installed wheel currently carries 40 compiled rkaf kernel schemas under `rulespec_conformance/_data/compiled/json-schema/core/`. As of rulespec `20de071` (2026-08-11) both roots exist as sealed candidates — `SourceCatalogRelease` v1 (`urn:rulespec:core:2de89ad8…`) and `DocumentRelease` v2 (`urn:rulespec:core:ff444f84…`) — carried in the wheel as digest-addressed bundles beside the kernel schemas; the v2 spec's §6 deviation table is this repository's migration delta from its live 1.1 format.
- `SourceCatalogRelease` appears nowhere in DocSpec at `7e3d0f2`.

This is a fact about the input, recorded so the pipeline is written against the bundle rather than against a locally invented shape. Authoring and packaging the two schema sets is Rulespec work.

### Consumer-side facts (recorded 2026-08-21)

Two facts about the repositories on the other side of the seam, recorded here
so the swap is planned against what their code pins today. Both are
observations of sibling checkouts; changing either is work in that repository.

- SpicySearch admits document releases through `_ReleaseProfile` pins in
  `spicysearch/src/spicysearch/canonical.py:80-150`: `format_version` markers
  `spicyregs-document-release/v1` and `spicyregs-document-release/v2`, id
  prefix `urn:spicyregs:document-release` (plus the test-fixture marker
  `urn:spicyregs:schema:document-release:1`). The v2 profile requires an exact
  top-level key set (`source_input`, `release_status`, `passage_coverage`,
  `policies`, `link_verification_receipts`, the capture and rendition lists,
  and the v1 keys). DocSpec's live surface is `docspec-document-release/1.1`
  with a disjoint key set, so DocSpec-as-producer needs either a translation
  emitting `spicyregs-document-release/v2` or a new SpicySearch profile pinned
  to `docspec-document-release/1.1`.
- Rulespec's closed `#CoordinateSystem` enum
  (`rulespec/constraints/core/source-fragment.cue:19`, mirrored as
  `COORDINATE_SYSTEM` in `rulespec_conformance/contract/enums.py:279`) has
  `rkaf:utf8-byte` and no `utf8-byte-range` member; fragments carry
  `oa:start`/`oa:end` half-open offsets plus `rkaf:coordinateSystem`.
  DocSpec's `utf8-byte-range` coordinates (`processing/extraction.py`,
  `processing/segmentation.py`) are the same semantics -- half-open byte
  offsets into the UTF-8 representation bytes -- so the crossing needs only
  the label mapping `utf8-byte-range` to `rkaf:utf8-byte`, not an enum change.

## The source-access port

Add one Protocol to `src/docspec/ports/` and one adapter method. Nothing more.

- The port accepts the source release by digest. It takes a digest-addressed reference — `SourceCatalogRef` already carries `catalog_id`, `locator`, and `digest` — verifies it, and yields `SourceItem` values.
- The name is not `AcquisitionSource`; that name is taken at `ports/content_fetcher.py:88`. It is also not `SourceCatalog` or `SourceCatalogReader`; both are taken at `ports/source_catalog.py:41,53` for reading a fixed local distribution.
- The emitted `SourceItem` stream is the same stream `LocalJsonlSourceCatalog.write(items: Iterable[SourceItem])` already accepts at `src/docspec/adapters/source_catalog.py:64`, so the adapter method is a translation from verified release records to `SourceItem`, not a new record model.
- Composition is explicit constructor injection, matching the existing ports. No registry, no plugin discovery, no service locator, no runtime lookup.
- The port is an internal interface behind the `SourceCatalogRelease -> DocumentRelease` seam. Verification of the release itself stays where it is: one complete verification operation per boundary crossing, at admission and at open, covering the root digest, every declared member, closed membership, relative paths, containment, symlinks, schemas, and member digests.

### What the port is, in code

`src/docspec/ports/source_release.py` holds the Protocol and the three records it needs:

| Name | Site | Shape |
| --- | --- | --- |
| `SourceReleasePin` | `:16` | `(root, digest)` — one release root locator, checked by `require_relative_path`, and the `sha256:` digest those bytes must have |
| `SourceReleaseAdmission` | `:28` | `(pin, reference, summary)` — refuses construction unless `reference.locator` and `reference.digest` equal the pin's and `summary.catalog_id` equals `reference.catalog_id` |
| `SourceReleaseRead` | `:43` | `(admission, items)` — one admission and one `Iterator[SourceItem]`, mirroring `SourceCatalogRead` at `ports/source_catalog.py:34` |
| `SourceReleaseReader` | `:50` | `admit(pin) -> SourceReleaseAdmission` at `:53`, `open(pin) -> SourceReleaseRead` at `:55` |

The pin carries no `catalog_id`. Identity is read back out of the digest-verified bytes and returned in `admission.reference` as a `SourceCatalogRef`, so a caller addresses the release by where it sits and which bytes it is, and learns what it is from the release itself. `SourceCatalogSummary` from `ports/source_catalog.py:15` is reused for counts, partitions, coverage, and the parent `base_catalog` pin; no second summary record exists.

`LocalSourceReleaseReader` at `src/docspec/adapters/source_catalog.py:268` implements it against the `docspec-source-catalog` 1.0 distribution the repository already reads. Its constructor takes one `LocalJsonlSourceCatalog` and nothing else — no default root, no discovery. `_reference` at `:274` bounds the read by that catalog's `max_root_bytes`, recomputes `sha256_digest` over the root bytes and refuses a mismatch before parsing, then builds `SourceCatalogRef(catalogId, pin.root, pin.digest)`. `admit` at `:290` returns `LocalJsonlSourceCatalog.verify`'s summary; `open` at `:294` returns `LocalJsonlSourceCatalog.open`'s stream. Every other check — closed root shape, identity-to-content binding, identity-to-locator binding, closed membership, symlinks, containment, member size and digest, item order, item validity, and counts — stays in `_open_root` at `:145` and `_validated_items` at `:183`, unduplicated.

Exports: `ports/__init__.py` (33 names, from 29; 36 once the schema gate lands below); `adapters/__init__.py:16,41`. `src/docspec/ports/` now holds 17 port modules. `ownership/modules.json` is regenerated from `tools/generate_ownership_manifest.py`, which names the new module under `SOURCE-CATALOG-CONTRACT` and `DOCUMENT-RELEASE-INTEGRITY`.

Tests are in `tests/test_source_catalog.py`, over a fixture release written by `LocalJsonlSourceCatalog.write`:

- `:87` admits a pin, matches the recovered reference and summary against the written ones, opens twice for the same items, and republishes the emitted stream through `LocalJsonlSourceCatalog.write` to the same content-derived reference.
- `:109` refuses a root that is not a contained relative path, a pin whose digest is not the root's, and the original pin after the root bytes are rewritten.
- `:129` refuses a tampered `items.jsonl` at both `admit` and `open`.

`uv run pytest -q`: 268 passed, 1 deselected. Recorded in DocSpec `5cdcf35` (port and adapter) and `ff604e4` (tests) on 2026-08-11.

`LocalSourceReleaseReader` continues to read the local `docspec-source-catalog`
1.0 distribution. `LocalWireSourceReleaseReader`, recorded under Phase 1 below,
implements the same port for the Rulespec-published wire format. The two
adapters share the port and domain records without adding another release
format.

### Where the wire schema pin lives

`fixtures/wire/source-catalog-release-v1/` holds 28 tracked files: `pins.json`, three schema files, and four sealed conformance bundles. Each is a byte-identical copy of what Rulespec Core publishes, verified against the digests the `urn:rulespec:core:2de89ad867a3794cc1006ef4cd0301248d48a719b5cbab1946f62c2c30ac0ec5` candidate names, and copied under the same ICPSR precedent the Mirrulations draw follows. They are pinned rather than read out of the `rulespec-conformance` wheel because `tests/test_package_boundary.py:111` forbids resolving a path outside this repository and the wheel is not a DocSpec dependency; a pin is what makes the crossing a data crossing.

- `schemas/source-catalog-release-v1.schema.json`, `sha256:1b7f0ccdefe52973db97fb145e3893a43bf4b12dcf00630a13af87a3486f4bbf`, 8,764 bytes, role `release-root`.
- `schemas/member-manifest-v1.schema.json`, `sha256:c22acd4d8d397d2bd790ee42fc7a44f93fccce3b8ae39e5e378967035b075d4c`, 3,038 bytes, role `member-manifest`.
- `schemas/source-items-v1.schema.json`, `sha256:94c3953f0a615a94d3a6f6489d9095ab8882f82d677c1661a9b79d3642e90702`, 8,818 bytes, role `source-items`.
- `bundles/valid/`, the sealed valid bundle, `releaseId` `urn:spicy-regs:source-catalog-release:v1:2bce80ff4f54251a54930ee86a1697d5e946f6f07f6b6b269ecefd0a8bafc8bc`: 6 source items, 4 declared members, complete with the three schema members its own manifest declares, which are the same bytes as `schemas/`.
- `bundles/invalid/unknown-version/`, `bundles/invalid/missing-disposition/`, and `bundles/invalid/unknown-disposition/`, the three published invalid bundles a JSON Schema decides on its own. Their first diagnostic codes in the publisher's validator are `invalid.format` at `release.json`, `invalid.schema` at `data/source-items.json/2/selection`, and `invalid.schema` at `data/source-items.json/2/selection/disposition`.
- `pins.json`, identity `urn:docspec:wire-release-pins:v1:43132bbb767f5de626c204d7462d674288d91ff4bec411a662d207e51a508779`, which records the candidate id and status, the schema roles, ids, and origin paths, each bundle's release id, expected structural verdict, and upstream code and path, and all 27 pinned files by relative path, byte size, media type, and digest.

The other thirteen published invalid bundles turn on identity, membership, digest, count, coverage, path, and duplicate rules that no JSON Schema decides. They are not pinned, because this gate would report them as conforming.

### What the schema gate is, in code

`src/docspec/ports/source_release.py` holds three more names:

| Name | Site | Shape |
| --- | --- | --- |
| `SourceReleaseViolation` | `:59` | `(member, pointer, message)` — one refusal located by release member and JSON pointer |
| `SourceReleaseConformance` | `:74` | `(violations,)`; `conforms` at `:80` is derived, so a verdict cannot disagree with its own evidence |
| `SourceReleaseSchemaGate` | `:84` | `check(root, manifest, items) -> SourceReleaseConformance` at `:91` |

`src/docspec/adapters/wire_source_release.py` implements it against Draft 2020-12, in `adapters/` because `jsonschema` is not the standard library. `load_wire_release_pins` at `:138` names one pins file and resolves everything else below it as a contained relative path, checks each declared size, recomputes each digest before a parser sees the bytes, and refuses a tree with a missing or extra file or a symlink. `read_wire_release_bundle` at `:217` reads `release.json`, `manifests/global.json`, and `data/source-items.json` bounded at 64 MiB and duplicate-safe. `JsonSchemaWireSourceReleaseGate` at `:245` reports every root and member-manifest violation and the first violation of each source item, in a stable order.

The docstring at `:246` records what the gate is: the consumer-side structural gate. Contract authority is Rulespec's own validator at the cross-product verdict-agreement step, SpicySearch PLAN step 7. This gate claims none of its diagnostic codes and decides none of the identity, membership, digest, count, coverage, path, duplicate, or canonical-encoding rules that validator also decides. A bundle it reports as conforming has passed structure and nothing else.

`jsonschema>=4.23,<5` and `ijson>=3.3,<4` are the `docspec[wire]` extra. Both are imported inside the wire adapter, so the core install stays dependency-free and `import docspec` still needs nothing. `tests/test_package_boundary.py` names both dependencies.

`LocalSourceReleaseReader` at `src/docspec/adapters/source_catalog.py:277` takes the gate as an optional `wire_gate` keyword. `_screen` at `:291` runs on any digest-verified root that its own `docspec-source-catalog` 1.0 distribution does not describe. That format is not the wire format, so a local release never reaches the gate and keeps exactly the verification it had; a release published in the wire format refuses with the violation that decided it, or, when it is structurally clean, with the fact that this reader does not admit that format. Without the keyword the reader behaves as before. Migrating the local format to the wire format is separate work and is not started here.

Tests are in `tests/test_wire_source_release.py`, nine of them, none resolving a path outside this repository:

- `:76` recomputes the three candidate digests from the tracked bytes and confirms every bundle's own schema members are the same bytes.
- `:91` reads the sealed valid bundle back and confirms it conforms with no violations.
- `:113` refuses each pinned invalid bundle and locates the refusal. The publisher's first diagnostic code sits beside each row as a comment and is not asserted.
- `:133` holds each bundle's recorded verdict to what the gate returns.
- `:147` and `:170` refuse a changed byte, a changed size, a missing file, an undeclared extra file, and a pins file whose identity no longer derives from its content.
- `:185` proves the gate is on the reader's path: a local distribution admits identically with and without it, and a wire root refuses differently with it than without it, after the pinned digest is checked first.

`uv run pytest -q`: 280 passed, 1 deselected. `uv run ruff check src tests tools` passes. Recorded in DocSpec `761cc8c` (pins), `07ff6e0` (gate), and `7705b51` (tests) on 2026-08-12.

## Removing the four crossings

The four faces were one boundary and were removed in one change set. The repeated hashing went with them; the verification did not.

`validate_execution_manifest` still recomputes both draw digests and compares them against the manifest, through `build_source_items` at `tools/fr_mirrulations_support.py:1065`. What it no longer does is hash the same two files a second time in the same call, or hash a file in the producer's checkout at all. `admit_producer_inputs` verifies the pinned draw once at admission, before parsing.

## Implementation phases

### Phase 1: Consume the sealed input release

- The source-access Protocol remains `SourceReleaseReader` in
  `src/docspec/ports/source_release.py:50`. No second port or wire-specific
  domain record was added.
- `LocalWireSourceReleaseReader` at
  `src/docspec/adapters/wire_source_release.py:496` implements that port for a
  configured local wire release root. The caller supplies only a relative
  `release.json` locator and its SHA-256 digest through `SourceReleasePin`.
- Preparation at `:525` verifies the root pin, the root's global-manifest
  reference, closed membership, contained relative paths, regular files,
  symlink refusal, non-item member sizes and digests, the item member's declared
  size, and the root and manifest schemas.
- The item stream at `:634` uses `ijson` to hold one source record at a time. It
  hashes the exact member bytes while parsing them, applies the pinned item
  schema, translates the record, enforces strict `sourceItemId` order, and
  reconciles the byte size, digest, record count, and all five source
  dispositions at end of stream.
- `JsonSchemaWireSourceReleaseGate.check_header` and `check_item` at `:465` and
  `:482` expose the existing pinned Draft 2020-12 gate incrementally; the
  bounded six-record fixture helper remains available for conformance tests.

Translation uses the current domain records:

- `selected` becomes `SourceItemState.ACTIVE`;
- `deleted` becomes `SourceItemState.DELETED`;
- `excluded`, `unavailable`, and `failed` become
  `SourceItemState.EXCLUDED`, with the original disposition, reason code, and
  reason retained in metadata; and
- each `candidateRenditions` entry becomes a `CandidateFile`, preserving its
  rendition ID, locator, media type, expected SHA-256, and expected byte size.

The reader retains the wire record's document identifier, normalized metadata,
source-native metadata, observations, and observed topics. It verifies the
declared member bytes and translates them with bounded memory; it does not run
a second whole-corpus semantic build gate.

Acceptance passed on 2026-08-12:

- The tracked six-record release produces the same ordered `SourceItem` values
  on repeated reads, republishes through `LocalJsonlSourceCatalog.write`, and
  preserves the expected candidates and metadata.
- Each pinned invalid bundle still refuses with a located schema violation.
- A tampered `data/source-items.json` refuses on its declared digest.
- A read-only `admit` pass over
  `urn:spicy-regs:source-catalog-release:v1:3414d0a5812ddc6f0c50af0aa377d891a5a822246876681762bba27b5b2bda27`
  consumed its 2,556,982,433-byte item member and reconciled 1,992,343 records:
  83,928 active, 120 deleted, and 1,908,295 excluded. It completed in 440.97
  seconds with a 43,008,000-byte maximum resident set. It fetched no rendition
  and published no `DocumentRelease`.
- `uv run pytest -q` passes 285 tests with 1 deselected, and
  `uv run ruff check src tests tools` passes.

**Exit gate:** Met. A digest-pinned wire `SourceCatalogRelease` now produces the
validating `SourceItem` stream behind the existing port, the tracked valid
fixture is stable on repeated reads, the invalid fixtures are rejected at the
schema boundary, and the real released input reconciles with bounded memory.
Phase 2 capture may begin. The `DocumentRelease` v2 candidate
(`urn:rulespec:core:ff444f84…`) remains Phase 3.

### Phase 2: Capture, extract, segment

- Capture the selected source files by exact bytes from the release's declared candidates.
- Produce normalized representations, document nodes, structural segments, evidence coordinates, and coverage information.
- Keep the existing `Extractor`, `Segmenter`, `Processor`, `BlobStore`, and `ContentFetcher` ports and their explicit injection.

The first composition slice is implemented. `SourceReleaseCatalogView` at
`src/docspec/adapters/source_catalog.py:338` maps the existing
`SourceCatalogRef` to `SourceReleasePin`, delegates every operation to the
injected `SourceReleaseReader`, and refuses an identity mismatch. It performs
no parsing, validation, persistence, or identity construction. This lets the
unchanged planner and reconciler consume either local or wire release readers
without republishing the source items under a second catalog identity.

The bounded application test at `tests/test_application_pipeline.py:268` now
runs planning, capture, extraction, segmentation, reconciliation, and commit
through this view and confirms the resulting `DocumentRelease` retains the
original source reference. The wire fixture test exercises the same view over
`LocalWireSourceReleaseReader`.

The HTTPS acquisition slice is implemented. The DRY trace found no current or
historical `ContentFetcher` implementation for HTTPS. It retained the current
`ContentFetcher`/`FetchStream` boundary, execution-owned digest and size checks,
retry classification, and `RoutingContentFetcher`. It reused the archived
choice of `httpx`, streaming reads, redirects, timeouts, and a descriptive user
agent; the archived whole-response corpus helpers were not restored because
they do not implement the current stream boundary or restrict redirects.

`HttpsContentFetcherConfig` and `HttpsContentFetcher` at
`src/docspec/adapters/content_fetchers.py:48` and `:264` bind the exact allowed
hosts, user agent, chunk size, timeouts, redirect limit, connection limit, and
identity content encoding into one configuration digest. The fetcher rejects
credentials, fragments, explicit ports, non-HTTPS URLs, and any initial or
redirect host outside the allowlist before requesting that destination. It
streams raw bytes, enforces the caller's byte bound and any declared size,
detects truncation, and closes responses on success and failure.
`RoutingContentFetcher` at `:637` adds HTTPS only when the caller injects that
delegate; its existing local-and-S3 composition remains unchanged. `httpx` is
available through the optional `docspec[http]` extra, so the core package still
has no required dependency.

A locator-only inventory of the already digest-pinned real release found the
complete selected-candidate allowlist: 87,555 renditions at
`mirrulations.s3.amazonaws.com` and 1,954 at
`downloads.regulations.gov`, matching all 89,509 selected renditions. This was
configuration discovery, not a second semantic or member-digest validation.
The focused acquisition and package-boundary suite passes 23 tests, including
the real `httpx` streaming interface with an in-memory transport. The full
suite passes 291 tests with 1 deselected, Ruff passes, and the source and wheel
distributions build successfully.

The bounded composition path is also implemented without a second runner.
`_compose_local_run` and `_execute_local_run` accept an optional existing
`SourceCatalog`; their default remains `LocalJsonlSourceCatalog`. This is the
same injection pattern they already use for `ContentFetcher`, and both the
planner and reconciler already depend on the general `SourceCatalog` port. The
tracked wire-release composition test selects one item, streams its HTTPS bytes
through an in-memory `httpx` transport, completes planning, capture,
extraction, segmentation, reconciliation, and release commit, then reopens the
release and confirms it retains the wire release's original source reference.
It does not republish the source catalog or introduce another execution path.

Open under this phase: seal the real two-host fetcher configuration into a
bounded production plan and run request, then expand through the existing work
budget. No production rendition capture has started.

**Exit gate:** Every source item in the input release reaches one terminal disposition, and coverage information accounts for every declared candidate.

### Phase 3: Publish the document release

- Publish a portable, immutable `DocumentRelease` from the pipeline's output, atomically by rename, with a content-addressed identity.
- Carry the parent `SourceCatalogRelease` identity and digest in the published release.
- Validate the published release against the Rulespec Core `DocumentRelease` schema set.

**Exit gate:** An independent verifier, given only the published release bytes, verifies the release and reads it without a DocSpec checkout, a SpicyRegs checkout, or any path outside the release.

### Phase 4: Remove the crossings and the repeated hash

- The listed sites in `tools/fr_mirrulations_qualification.py` and `tools/fr_mirrulations_support.py` are deleted; see "Where the sibling-checkout crossings were".
- The Mirrulations draw is a tracked input pinned by digest; see "Where the draw pin lives".
- The captured Federal Register root is named by the operator through `--federal-register-root`, which has no default. Its 192 MB of cached source bytes are not pinned in tree.

**Exit gate:** Met for the crossings. No file under `tools/`, `src/`, or `tests/` names a SpicyRegs path, imports a `spicy_regs` symbol, or sets a subprocess `cwd` outside this repository. `tests/test_package_boundary.py:111` `test_no_repository_code_names_a_sibling_checkout_or_an_outside_working_directory` proves it, and fails when any of the four forms returns. `uv run pytest -q`: 271 passed, 1 deselected; `uv run ruff check src tests tools` passes. Recorded in DocSpec `2df6e4f`, `8af75cc`, and `bdca9d1` on 2026-08-11.

Open under this phase: the Mirrulations draw is pinned, the Federal Register content root is not. Pinning it means publishing 192 MB of captured bytes as a sealed artifact, which is Phase 2 and Phase 3 work — the release the pipeline consumes — not a path deletion.

### Phase 5: Confirm standalone consumption

- Exercise the pipeline against a released input from an installed DocSpec package outside the source checkout.
- Keep the seam the only integration surface a consumer touches.

**Exit gate:** The published `DocumentRelease` verifies and opens from an installed wheel with no source checkout of any product present.

## Non-goals

- Adding a discovery, listing, crawling, or selection capability to DocSpec. Source discovery and selection sit on the other side of the seam under REF-024.
- Adding a second port when one Protocol plus one adapter method covers the boundary.
- Reusing the name `AcquisitionSource` (`ports/content_fetcher.py:88`), `SourceCatalog`, or `SourceCatalogReader` (`ports/source_catalog.py:41,53`).
- Authoring the `SourceCatalogRelease` or `DocumentRelease` schema sets inside DocSpec. They arrive in the Rulespec Core wheel as one digest-addressed bundle.
- Publishing a second schema distribution. The three schema files under `fixtures/wire/source-catalog-release-v1/schemas/` are a tracked test pin, byte-identical to what Rulespec Core publishes and verified against the digests its candidate names. They are read by tests and by an injected gate. They are not published, not installed, and not in the built wheel; `tests/test_package_boundary.py:272` asserts every wheel member starts with `docspec/`.
- Adding a dependency-injection framework, plugin registry, service locator, or runtime discovery system.
- Keeping a compatibility path for the sibling-checkout harness after its replacement lands.
- Removing membership, path, containment, symlink, or digest verification at admission and open.

## Definition of done

1. Met. DocSpec consumes a sealed `SourceCatalogRelease` referenced by digest through `SourceReleaseReader` at `src/docspec/ports/source_release.py:50`. `LocalWireSourceReleaseReader` at `src/docspec/adapters/wire_source_release.py:496` reads the published wire format with bounded memory and refuses a wrong root or member digest. `LocalSourceReleaseReader` continues to implement the same port for the local `docspec-source-catalog` distribution.
2. The pipeline captures the selected source files and produces normalized representations, document nodes, structural segments, evidence coordinates, and coverage information.
3. DocSpec publishes a portable, immutable `DocumentRelease` that names its parent source release by identity and digest.
4. The published release verifies and opens with no checkout of any product on disk.
5. Met. No file under `tools/`, `src/`, or `tests/` names a SpicyRegs path or sets a subprocess `cwd` outside this repository. Every site listed at recording time is deleted: `tools/fr_mirrulations_qualification.py:55`, `:56`, `:58`, `:69-74`, `:90`, `:95`, `:96`, `:128`, `:142-160`, `:161-168`, `:174`, `:241`, `:313-315`, `:630`, and `tools/fr_mirrulations_support.py:853-855`, `:908`, `:910`. The three remaining mentions of the producer are not paths: the draw-identity URN namespace `urn:spicyregs:mirrulations-document-draw:` recomputed at `tools/fr_mirrulations_support.py:638`, the sibling-package deny-list at `tests/test_package_boundary.py:25-27`, and the pinned predecessor remote URL at `tests/test_boundary_code.py:27`.
6. `tools/fr_mirrulations_support.py:1019-1027` is gone, and one complete verification per boundary crossing remains at admission and at open.
7. Met. `tests/test_package_boundary.py:111` `test_no_repository_code_names_a_sibling_checkout_or_an_outside_working_directory` walks `src/`, `tests/`, and `tools/` and fails on a home-directory path constant, a `spicy-regs` path segment, a dotted `spicy_regs` module in a string or an import, or a subprocess working directory outside the closed set of five repository-rooted expressions. It also fails when one of those five stops being used.
8. SpicySearch, and any other consumer, integrates with DocSpec through the published `DocumentRelease` and the installed package alone.
