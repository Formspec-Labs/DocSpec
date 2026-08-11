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

### Where the sibling-checkout crossings are

All in `tools/fr_mirrulations_qualification.py`, with one supporting site in `tools/fr_mirrulations_support.py`.

| Face | Site | What it does |
| --- | --- | --- |
| Absolute sibling path | `:55` | `DEFAULT_SPICYREGS = Path("/Users/mikewolfd/Work/spicy-regs")` |
| Gitignored sibling output | `:56` | `DEFAULT_FR_ROOT = DEFAULT_SPICYREGS / "output/scale-dr-10k-2026-08-05"` |
| Subprocess into the sibling checkout | `:142-160` | `_run_checked([...], cwd=spicyregs)`; the module argument is at `:148`, `cwd=spicyregs` at `:159` |
| Private-symbol import through `python -c` | `:161-168` | `:164` imports `_draw_documents` from `spicy_regs.corpora.mirrulations_document_corpus`; `:168` executes that script with `cwd=spicyregs` |

Supporting sites that follow from the four faces:

- `:58` `BUILDER_RELATIVE = Path("src/spicy_regs/corpora/mirrulations_document_corpus.py")` names a file inside the sibling source tree.
- `:69-74` `_run_checked(arguments, *, cwd)` exists to run commands in that checkout. Three call sites pass `cwd=spicyregs`: `:95` (`git rev-parse HEAD` for the producer commit), `:142`, and `:168`. Deleting the helper covers all three; `:95`'s commit capture is replaced by a value carried in the input artifact.
- `tools/fr_mirrulations_support.py:1019` re-hashes each draw file on every execution-manifest validation; `:1024-1027` re-hashes the SpicyRegs builder source file named by `producer_spec["builderPath"]`.

### Rulespec dependency

- The `SourceCatalogRelease` and `DocumentRelease` schema sets arrive as one digest-addressed bundle inside the Rulespec Core wheel, alongside generated types, identity and digest helpers, validators, diagnostics, and valid and invalid conformance fixtures.
- Neither schema root exists in Rulespec at `b64ca67`. `SourceCatalogRelease` appears in `rulespec/TODO.md` only, and nowhere in code, schemas, or fixtures. The installed wheel currently carries 40 compiled rkaf kernel schemas under `rulespec_conformance/_data/compiled/json-schema/core/`. As of rulespec `20de071` (2026-08-11) both roots exist as sealed candidates — `SourceCatalogRelease` v1 (`urn:rulespec:core:2de89ad8…`) and `DocumentRelease` v2 (`urn:rulespec:core:ff444f84…`) — carried in the wheel as digest-addressed bundles beside the kernel schemas; the v2 spec's §6 deviation table is this repository's migration delta from its live 1.1 format.
- `SourceCatalogRelease` appears nowhere in DocSpec at `7e3d0f2`.

This is a fact about the input, recorded so the pipeline is written against the bundle rather than against a locally invented shape. Authoring and packaging the two schema sets is Rulespec work.

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

Exports: `ports/__init__.py:19-23,55-58` (33 names, from 29); `adapters/__init__.py:16,41`. `src/docspec/ports/` now holds 17 port modules. `ownership/modules.json` is regenerated from `tools/generate_ownership_manifest.py`, which names the new module under `SOURCE-CATALOG-CONTRACT` and `DOCUMENT-RELEASE-INTEGRITY`.

Tests are in `tests/test_source_catalog.py`, over a fixture release written by `LocalJsonlSourceCatalog.write`:

- `:87` admits a pin, matches the recovered reference and summary against the written ones, opens twice for the same items, and republishes the emitted stream through `LocalJsonlSourceCatalog.write` to the same content-derived reference.
- `:109` refuses a root that is not a contained relative path, a pin whose digest is not the root's, and the original pin after the root bytes are rewritten.
- `:129` refuses a tampered `items.jsonl` at both `admit` and `open`.

`uv run pytest -q`: 268 passed, 1 deselected. Recorded in DocSpec `5cdcf35` (port and adapter) and `ff604e4` (tests) on 2026-08-11.

The release this reads is the local `docspec-source-catalog` 1.0 distribution, not a Rulespec-published `SourceCatalogRelease`. No validation against the Rulespec Core schema bundle or its valid and invalid fixtures runs yet; the candidate bundles exist as of rulespec `20de071`, and wiring their validation in is the open bullet under Phase 1.

## Removing the four crossings

The four faces are one boundary. Remove them in one change set.

- Delete `:55` and `:56`. The pipeline takes a sealed `SourceCatalogRelease` reference, not a checkout root and not a path under another repository's gitignored `output/`.
- Delete the subprocess at `:142-160`. A draw is no longer produced by shelling into a sibling checkout; it arrives already published.
- Delete the `python -c` script at `:161-168`, including the private-symbol import at `:164`. Validation of the input is the release verification, performed against the release's own schemas and digests.
- Delete `:58` and the sibling-checkout use of `_run_checked` at `:69-74` with the faces they serve.

Closing all four together makes the re-hash at `tools/fr_mirrulations_support.py:1019-1027` deletable:

- `:1024-1027` re-hashes a file inside the sibling source tree. With the crossings gone that file has no path and the check has no subject.
- `:1019` re-hashes each draw file on every validation of the execution manifest. The draw arrives as an input artifact identified by digest and verified once at admission and once at open, so the per-validation repeat adds no guarantee that the boundary verification does not already give.

This deletes repeated hashing, not the verification. The boundary check itself stays.

## Implementation phases

### Phase 1: Consume the sealed input release

- The source-access Protocol is `SourceReleaseReader` in `src/docspec/ports/source_release.py:50`. The name is bound nowhere else in `src/docspec/`.
- The adapter is `LocalSourceReleaseReader` at `src/docspec/adapters/source_catalog.py:268`. `open` at `:294` yields the `SourceItem` values `LocalJsonlSourceCatalog.write` accepts at `:64`; `tests/test_source_catalog.py:87` republishes that stream through `write` to the same reference.
- Verification runs at admission and at open through `LocalJsonlSourceCatalog._open_root` and `_validated_items`: root digest, the declared member, closed membership, relative paths, containment, symlinks, member size and digest, closed root shape, identity-to-content and identity-to-locator binding, item order, item validity, and counts. Schema validation is the Rulespec bundle item below.
- `_reference` at `src/docspec/adapters/source_catalog.py:274` recomputes the root digest and refuses a pin whose digest is not the bytes read, before parsing them. `tests/test_source_catalog.py:109` covers a wrong digest and rewritten root bytes; `:129` covers a tampered member.
- Validate against the Rulespec Core schema bundle and its valid and invalid fixtures. Both candidates exist as of rulespec `20de071`: `SourceCatalogRelease` v1 (`urn:rulespec:core:2de89ad8…`) and `DocumentRelease` v2 (`urn:rulespec:core:ff444f84…`), each with sealed valid and invalid fixture corpora and a console validator in the `rulespec-conformance` wheel.

**Exit gate:** A sealed `SourceCatalogRelease` identified only by digest produces a byte-identical `SourceItem` stream on repeated reads, and every invalid fixture is rejected with a diagnostic rather than an exception from a lower layer.

### Phase 2: Capture, extract, segment

- Capture the selected source files by exact bytes from the release's declared candidates.
- Produce normalized representations, document nodes, structural segments, evidence coordinates, and coverage information.
- Keep the existing `Extractor`, `Segmenter`, `Processor`, `BlobStore`, and `ContentFetcher` ports and their explicit injection.

**Exit gate:** Every source item in the input release reaches one terminal disposition, and coverage information accounts for every declared candidate.

### Phase 3: Publish the document release

- Publish a portable, immutable `DocumentRelease` from the pipeline's output, atomically by rename, with a content-addressed identity.
- Carry the parent `SourceCatalogRelease` identity and digest in the published release.
- Validate the published release against the Rulespec Core `DocumentRelease` schema set.

**Exit gate:** An independent verifier, given only the published release bytes, verifies the release and reads it without a DocSpec checkout, a SpicyRegs checkout, or any path outside the release.

### Phase 4: Remove the crossings and the repeated hash

- Delete `tools/fr_mirrulations_qualification.py:55`, `:56`, `:58`, the subprocess at `:142-160`, and the `python -c` script at `:161-168`, together with the sibling-checkout use of `_run_checked` at `:69-74`.
- Delete `tools/fr_mirrulations_support.py:1019-1027`.
- Move the campaign's inputs to sealed input artifacts referenced by digest.

**Exit gate:** No file under `tools/`, `src/`, or `tests/` names a SpicyRegs path, imports a `spicy_regs` symbol, or sets a subprocess `cwd` outside this repository; a test proves it; and the campaign runs from released input artifacts alone.

### Phase 5: Confirm standalone consumption

- Exercise the pipeline against a released input from an installed DocSpec package outside the source checkout.
- Keep the seam the only integration surface a consumer touches.

**Exit gate:** The published `DocumentRelease` verifies and opens from an installed wheel with no source checkout of any product present.

## Non-goals

- Adding a discovery, listing, crawling, or selection capability to DocSpec. Source discovery and selection sit on the other side of the seam under REF-024.
- Adding a second port when one Protocol plus one adapter method covers the boundary.
- Reusing the name `AcquisitionSource` (`ports/content_fetcher.py:88`), `SourceCatalog`, or `SourceCatalogReader` (`ports/source_catalog.py:41,53`).
- Authoring the `SourceCatalogRelease` or `DocumentRelease` schema sets inside DocSpec. They arrive in the Rulespec Core wheel as one digest-addressed bundle.
- Publishing a second schema distribution or a DocSpec-local copy of the bundle.
- Adding a dependency-injection framework, plugin registry, service locator, or runtime discovery system.
- Keeping a compatibility path for the sibling-checkout harness after its replacement lands.
- Removing membership, path, containment, symlink, or digest verification at admission and open.

## Definition of done

1. DocSpec consumes a sealed `SourceCatalogRelease` referenced by digest through one internal port, and rejects a reference whose bytes do not match. The port is `SourceReleaseReader` at `src/docspec/ports/source_release.py:50`, addressed by a `SourceReleasePin` of root locator plus digest; the adapter is `LocalSourceReleaseReader` at `src/docspec/adapters/source_catalog.py:268`, and the refusal is at `:274`, covered by `tests/test_source_catalog.py:109` and `:129`. What it reads is the local `docspec-source-catalog` 1.0 distribution, not a Rulespec-published `SourceCatalogRelease`.
2. The pipeline captures the selected source files and produces normalized representations, document nodes, structural segments, evidence coordinates, and coverage information.
3. DocSpec publishes a portable, immutable `DocumentRelease` that names its parent source release by identity and digest.
4. The published release verifies and opens with no checkout of any product on disk.
5. No file under `tools/`, `src/`, or `tests/` names a SpicyRegs path or sets a subprocess `cwd` outside this repository — the Phase 4 gate is the criterion, not a line list. Known sites at recording time: `tools/fr_mirrulations_qualification.py:55`, `:56`, `:58`, `:69-74`, `:90`, `:95`, `:96`, `:128`, `:142-160`, `:161-168`, `:174`, `:241`, `:313-315`, `:630`, and `tools/fr_mirrulations_support.py:853-855`, `:908`, `:910` — the last two `resolve(strict=True)` against the sibling checkout at manifest-write time.
6. `tools/fr_mirrulations_support.py:1019-1027` is gone, and one complete verification per boundary crossing remains at admission and at open.
7. A test fails if any DocSpec file names a SpicyRegs path, imports a `spicy_regs` symbol, or sets a subprocess `cwd` outside this repository.
8. SpicySearch, and any other consumer, integrates with DocSpec through the published `DocumentRelease` and the installed package alone.
