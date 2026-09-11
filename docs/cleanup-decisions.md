# Maintained cleanup decisions

These decisions accompany the maintainability work begun at `b1736e9` on
2026-09-11. They explain deliberate API changes and cases where a few repeated
lines preserve useful ownership. The maintainer subsequently clarified that
legacy compatibility is not required; current workflows still govern which
behaviors belong here.

## Retire compatibility-only import paths

Removed `adapters.source_catalog_artifact` and
`adapters.document_release_verify` after redirecting every current code, tool,
test, and installed-wheel probe to the implementation owners. The public
`docspec.source_catalog` API now imports those owners directly. Portable bundle
verification lives at `adapters.document_release.verify.verify_document_release`.

The application release lifecycle and portable bundle lifecycle both serve
current callers. Both advertise `2.0`, and their structures differ; removing an
old import path does not make either current workflow obsolete. Predecessor
portable-format handling is retired as described below.

## Retire the predecessor portable reader

The portable verifier accepts the current eight-schema shape, strict JSONL
members, framed set digests, and content-based release identity. Removed schema
aliases, generation inference, JSON-array member parsing, and predecessor digest
rules no longer create a second validation path. Current per-kind accounting,
retention floors, source versions, and indexed-byte ownership checks remain.

Decision 0001 explicitly made the predecessor reader temporary until restamping.
The current restamper produces the supported corpus and does not read the old
DocumentRelease corpus. Both frozen fixture trees remain sealed provenance;
`source_catalog_release_v1/valid` remains a required input to the current recipe.
No packaged schema or sealed fixture bytes changed. The
[dated retirement note](decisions/0001-document-release-2-0.md#migration-and-the-builders-obligations)
supersedes the earlier promise to accept both portable generations.

## Retire the unused application wrapper

`DocSpecApplication` and `docspec.application.service` have been removed.
Repository source, tests, tools, entry points, and profiles had no caller.
Targeted searches of the available SpicySearch, SpicyDocs, RefSpec, and Rulespec
checkouts also found no use. The project no longer retains compatibility for
unknown external scripts.

The wrapper delegated five methods and supplied no lifecycle behavior. Actual
coordination constructs a handoff after planning and gives that evidence to
reconciliation. Requiring every service up front did not simplify that sequence.
Its `Iterable[StoreRef]` reconciliation annotation also disagreed with the
implemented `Iterable[StoreTaskResult]` input.

Call the existing services directly: `RunPlanner`, `StoreExecutionService`,
`StoreDeliveryService`, `RunReconciler`, and `ReleaseCommitService`. They retain
their existing modules; public application exports remain for the services that
were already exported. Reconciliation consumes task results, including their
handoff and task identity, rather than an unqualified list of sealed stores.
The working local composition is in `src/docspec/cli/local.py` and
`src/docspec/cli/execution.py`.

## Small duplicate candidates

| Candidate | Decision and reason |
| --- | --- |
| Builder/restamper member descriptors | One `member_descriptor` in `document_release_support.py` now derives the same seven fields from the actual member file. Both builders use it; sealed fixture identities remain the check. |
| Source policies' `to_member` | Retain each small explicit serialization method. It sits beside that policy's inverse reader and names its own version and configuration. A generic serialization function would add parameters and indirection without reducing the code needed to understand either policy. The shared domain schema checks both shapes. |
| Source policies' cached `policy_digest` | Retain the short per-instance cache. Lazy calculation preserves configuration-error timing and avoids hashing the whole configuration for each item. A shared mutation helper or inheritance layer would obscure ownership of the frozen policy's cache. The Regulations.gov method retains the measurements that explain why this cache matters. |

## Dormant public helpers

The same repository and known-consumer searches covered these candidates,
including import strings and tool entry points. The no-legacy decision allows
removal where no current use was found.

| Helper | Disposition |
| --- | --- |
| `PinnedCorpus.run_roots` | Retained and used by `preserved_captures`; the existing full → intermediate → smoke ordering and capture provenance remain explicit. |
| `ProfileRegistry.to_inventory` | Removed after another current-tree search found no caller. The CLI keeps its existing, different inventory response. |
| CourtListener `build_catalog` | Removed. Its untyped `catalog.write` publication path has no current caller and is separate from `SourceCatalogBuilder`. |
| CourtListener `capture_digest_of` | Removed after another current-tree search found no caller. |

Parser handlers and profile implementations remain live through registration
even when a direct-call search finds no caller.

## Large modules reviewed by responsibility

The remaining outlier assessment distinguishes declarations from execution
flow. These decisions follow inspection of the implementation, its callers and
tests, with independent architecture review.

| Module at review | Decision and contribution boundary |
| --- | --- |
| `adapters/source_catalog_store.py` — 1,697 lines | Split into `source_catalog_store/pinned_fs.py` (435), `staging.py` (585), `store.py` (326), and `current.py` (409), with a 13-line public import file. Filesystem operations, staging, immutable publication, and pointer advancement have distinct reasons to change. Keep the staging transaction together: its descriptor ownership, publication, and cleanup form one lifetime. |
| `domain/source_catalog.py` — 1,077 lines | Keep the typed catalog rows and their closed schema family together. `source_catalog_schemas()` contains 681 lines of declarations, shares its field vocabulary locally, and feeds the artifact schema owner. `tests/test_package_boundary.py` compares generated and packaged schemas byte for byte. Splitting schema fragments would add navigation to one schema-maintenance task. |
| `domain/scale.py` — 1,560 lines | Keep the closed profile/result family together. `ScaleProfile` admits exactly the document-processing and source-catalog variants; `ScaleResult.verify_profile` binds evidence and rejects impossible pass claims for both. Its longer method contains two explicit variant checks and their resource-limit tables. `tests/test_scale_profile.py` covers both variants. No separate scale format or per-type modules are needed. |
| `processing/bounded_segmentation.py` — 1,104 lines | Keep the cohesive region → unit → packing → coverage algorithm and its provenance. `_bound` is the one owner of both boundaries and byte accounting. The introductory rationale explains source adaptation, tokenizer choice, excluded headings, and reversible evidence; it remains beside the algorithm. `tests/test_bounded_segmentation.py` covers deterministic output, token limits, coverage, and refusal cases. |

Source-catalog filesystem checks remain separate from `adapters/storage/files.py`.
They pin open directory descriptors and verify device/inode identities before
publication or cleanup. The path-based storage helpers do not provide that same
guarantee. Repeated identity checks at different points protect against changes
between operations; they are deliberate behavior, not duplicate convenience code.

These are justified size exceptions, not a waiver for unrelated additions.
Review a new responsibility on its own merits and retain the governing rules,
measurements, and failure explanations when moving code.

## Source-policy boundaries

The source policies share the six ordered interpretation forms through
`application.catalog_policy.catalog_interpretations`. Each caller supplies its
own pin, input-scope order, join evidence, sampling result, topic source path,
and rendition order. `selection_failure` only records a stopping decision that
the source policy has already chosen.

The following differences remain explicit:

- Regulations.gov document selection checks publisher fixtures, withdrawal,
  sample draw, required metadata, candidate availability, and the selected-item
  budget in that order. It records a successful budget decision only when a
  budget is configured. Docket/comment selection has different wording and
  records that successful decision even without a configured limit.
- Comment versions require nonempty exact source text, using `postedDate` only
  when `modifyDate` is null. Documents retain their existing `unknown` fallback
  for nonselected rows. The shared representation does not erase that difference.
- Federal Register accepts a non-string sequence of source diagnostics;
  Regulations.gov accepts a list. Their observation readers stay separate.
- A Federal Register join checks the named document-number field used by its
  index. A composite source-record identity is not a substitute for that key.
  Cross-filed documents retain the discarded filing as evidence while preserving
  the selected owner's facts and candidate files.

Named normalization functions retain explicit field order, source paths, and
unparseable values. These declarations can exceed the ordinary function-length
prompt; splitting each field into its own helper would make a metadata change
harder to follow. The conversion methods now compose joins, normalization,
selection, provenance, and interpretation recording without a generic policy
framework or source-kind mixins.
