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
portable-format handling is a separate retirement still under review.

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
