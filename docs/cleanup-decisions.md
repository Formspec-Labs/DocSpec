# Maintained cleanup decisions

These decisions accompany the maintainability work begun at `b1736e9` on
2026-09-11. They explain deliberate API changes and cases where a few repeated
lines preserve useful ownership. The maintainer subsequently clarified that
legacy compatibility is not required; current workflows still govern which
behaviors belong here.

## Keep one current export path

The former compatibility imports `adapters.source_catalog_artifact` and
`adapters.document_release_verify` were removed during the first refactor.
`docspec.source_catalog` imports the current catalog owners directly.

The September 12 simplification retires the entire campaign-specific portable
builder/verifier, its retention-floor calibration, exclusive schemas and tests,
and fixture restamping chain. The owner explicitly removed historical
reproduction and legacy consumers from the requirements. Git retains the
implementation and fixture provenance.

Use [result exports](result-exports.md) to copy a retained active dataset through
Rulespec's shared artifact container. Existing document identities, outcomes,
coordinates and typed processing evidence keep their established validators.
The current retained-state adapter `platform_artifact.py` remains necessary for
later processing and already uses Rulespec; it is separate from the removed
portable verifier. Current visible-text and segmentation implementations remain
usable through injected processing stages.

## Verify the catalog once

The catalog CLI now verifies the caller's `SourceCatalogRef` directly through
`SourceCatalogArtifactReader.verify_snapshot`. That reader admits the shared
artifact, verifies its exact pin and producer, and recomputes the catalog's
digests, counts, policy diagnostics, and byte accounting. Both Python-built and
relocated catalogs use this path.

The former command receipt repeated those facts in another file, added a second
identity and closed-shape validator, and required the original destination path
during verification. It did not replace the reader's full verification. Remove
that receipt, its `--receipt` and `--expected-command-receipt-id` arguments, and
the now-unused publication root-file writer. The artifact's sealed
`catalog-build-receipt.json` remains part of its own evidence and verification.

Build output still reports source locations, the chosen provider profiles and
source-verifier acceptance, and actual execution diagnostics. Those describe
the invocation; they are emitted on standard output for callers or Dagster to
retain as logs. They are not another required dataset artifact. A failed
publication emits no success report, and an existing destination is never
replaced. The exact artifact remains authoritative if output logging fails.

Keeping the second file but sharing its validators would retain unnecessary
identity and location dependencies. Removing it makes verification work for
catalogs created through either public entry point and eliminates roughly 500
lines of production code without adding a new schema, reader, or run ledger.
This is the primary agent's architecture judgment; independent review is pending
because the reviewers reached their usage limit.

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
The working local composition now lives in `src/docspec/runtime/` and supports
both Python callers and the CLI. Its prepared object binds verified services,
task references, and recovery, replacing the former CLI-only setup. See
[Python runs](python-runs.md); the removed five-method wrapper remains retired.

## Small duplicate candidates

The public runtime's task lookup has a separate purpose from retained run state.
A store identity describes its content; membership in the sealed plan determines
whether this run selected it. A disposable bounded SQLite index checks that
membership before acquisition or delivery. Building it once avoids a full plan
scan for every task. It introduces no additional authoritative ledger and is
released after local execution or when the caller closes the prepared worker.

| Candidate | Decision and reason |
| --- | --- |
| Source policies' `to_member` | Retain each small explicit serialization method. It sits beside that policy's inverse reader and names its own version and configuration. A generic serialization function would add parameters and indirection without reducing the code needed to understand either policy. The shared domain schema checks both shapes. |
| Source policies' cached `policy_digest` | Retain the short per-instance cache. Lazy calculation preserves configuration-error timing and avoids hashing the whole configuration for each item. A shared mutation helper or inheritance layer would obscure ownership of the frozen policy's cache. The Regulations.gov method retains the measurements that explain why this cache matters. |
| Retained-catalog research readers | Seven tools now share manifest ordering, plain/gzip JSONL reading, and receipt path formatting in `tools/catalog_sample_support.py`. Sampling rules and receipt meanings stay with their callers. This helper reads research inputs; package verification still governs publication. |
| Tool `_unique` helpers | Retain the small local functions. Their copy behavior differs, and a parameterized abstraction would add more decisions than the few repeated lines remove. |

## Place tools according to their current purpose

The [tool inventory](../tools/README.md) names each family, its inputs and
outputs, and its executable checks. Architecture review led to one product
integration: policy-member creation now runs through the installed
`docspec source-catalog write-policy` command. It calls the existing application
policy constructors, validates a canonical round trip, and refuses overwrites.
The former `tools/write_catalog_policy_member.py` entry point is removed.
Source-catalog command handling also moved into `cli/source_catalog.py`, so
contributors can find command code in one tree.

The superseded portable mint and calibration tools are removed. The inventory
records the remaining research, source-selection, schema and reporting tools by
their current caller and purpose. Shared package code owns behavior used by the
normal workflow; tools retain only experiment-specific choices. A historical
filename or a previous measurement alone does not justify maintaining a tool.

## Require the current installed source reader

`SpicyDocsSourceNativeAdapter` in `adapters/spicy_docs_source_native.py` now loads
only `spicy_docs`. The predecessor `spicy_regs` fallback and old adapter names
are removed, with current imports and tests updated directly. The adapter requires
the installed reader's public `CURRENT_PRODUCER_PRODUCT` to be `spicy-docs`.
The current reader owns source format and policy admission; callers independently
supply the source artifact pin and accepted verifier implementation IDs. Historical
producer labels do not enable a compatibility path.

Reader selection failures use `SourceNativeReaderError`, which the CLI reports
as a structured error. Source admission failures retain the provider's exception.
Import failures inside an installed reader retain their original cause and are
not mistaken for an absent package.

The GovInfo bill example uses that same optional SpicyDocs wheel for acquisition.
The provider owns offered-version checks, bill/XML identity, HTTP bounds and
source refusals. The example chooses one version, maps the response to the
existing fetcher interface, and retains source observations before using normal
DocSpec capture and processing. Its later run changes only the phrase resource
and reuses the retained upstream layers after the provider closes.

Keep this composition in the example: a provider registry or general source
runner has no additional caller here. Promote a shared adapter only when another
actual workflow needs the same mapping. One current wheel and manifest qualify
both source reading and acquisition; the former reader wheel and a separate bill
test wheel have no continuing role. The imported example has an
[independent review](history/2026-09-12-govinfo-bill-handoff-review.md); the later
single-wheel integration is locally tested and awaits fresh independent review.

CourtListener's research tool now imports the provider's `BulkObject` and
`parse_listing_page` directly. This removes its second XML parser, filename and
media-type rules, and URL construction. Exact quoted ETags now reach candidate
versions unchanged, and download URLs use the provider's escaping. There is no
compatibility parser or wrapper class. DocSpec still owns dataset scope, retained
input pins, and consistency checks over the supplied pages. Those checks do not
prove that the original capture retained every intermediate publisher response.

The development dependency group installs the same provider wheel so these
tests run by default; isolated package checks still prove core use without it.
The handoff gate passed 39 tests, including the pinned 1,076-object listing,
catalog publication, source refusals and installed-package checks. One live
acquisition test was deselected. This is the primary agent's implementation and
architecture review; independent review remains open under D39.

The GAO topic example maps admitted records into `SuppliedRecordSource`, retaining
the provider record, source pin, collection outcome and evidence reference. Exact
topic filtering reads that catalog directly. Unexpected labels remain ordinary
source facts; a missing publisher topic remains a source refusal. The provider
offers no report attachment here, so the catalog preserves absent candidates
while still supporting metadata analysis. This needs no new core schema or
processing interface. Both source examples use one provider-identity helper,
and installed qualification reuses the ordinary behavior test in the existing
provider environment. The [guide](gao-topics.md) states its bounds and evidence.

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
| `adapters/content_fetchers.py` — 832 lines | Split into local-file, HTTPS, S3, and routing owners, with the same public exports. The largest is 335 lines. Transport-specific retries, credentials, optional imports, and resource lifetimes remain together. |
| `application/regulations_gov_catalog.py` — 2,131 lines | Split into policy configuration/dispatch, indexed rows, sampling, shared record facts, document conversion, and comment/docket conversion. The largest owner is 530 lines. The policy retains its selected count, resume order, and lazy identity cache; conversion helpers receive explicit inputs. Independent review caught and corrected an early identity calculation before the split was committed. |
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
