# Preserve source collection outcomes without owning collection

DocSpec consumes the existing public collection outcome and bounded evidence
methods of the pinned SpicyDocs 0.2.0 reader. It does not own source traversal,
record classification, collection retries, or a second failure ledger.

`SourceNativeDescription` snapshots the provider's exact report, or records
`collectionOutcome: null` when no collection outcome was reported. Supplied
records retain that distinction. Catalog receipt 2.0 stores full descriptions
alongside their ordinary source input pins. The existing catalog reader checks
the description identities and the build's acceptance decision. The provider
still owns count equations, traversal rules, and original evidence membership;
the saved report is not a second independent verification of upstream truth.

The accepted-outcome set defaults to `empty` and `no-record-rejections`. Partial
and total rejection require separate explicit choices. A single allow-rejection
boolean would have authorized a completely rejected source when a contributor
only intended to accept a usable subset. That distinction matters because each
successor catalog is a full dataset universe: omitted prior items enter planning
as deletions, subject to selection. Neither acceptance nor `observed-crawl`
requests append semantics or preserves omitted items.

The same immutable description snapshot serves preflight, the existing resume
identity, and publication. Admission occurs before local output/scratch creation
for refused outcomes. Existing small-member limits bound retained metadata;
overflow refuses rather than truncating provider warnings or evidence. Command
receipts use the same description parser and compare exact canonical JSON, so
distinct JSON values such as `1` and `true` remain distinct.

Ordinary catalog preview and separately authorized source inspection expose the
saved descriptions and accepted outcomes. Upstream rejection counts remain
separate from selected-document acquisition and processing failures. Original
record/rejection evidence remains in the provider's existing pinned artifact and
blob store, accessed through its bounded public methods.

The current provider refuses unresolved traversal and transient or unclassed
collection failures before publishing an admissible release. DocSpec preserves
those refusals instead of inventing an accepted unresolved source. This decision
does not qualify live collection completeness or add execution controls already
provided by systems such as Dagster.

The independent architecture review approved this shape. Qualification covers
the actual installed provider's empty, partial, total-rejection and unresolved
cases, source evidence access, local no-write refusals, resume identity, exact
receipt comparisons, and the successor-omission consequence. Runtime results are
recorded in the task checklist after the gate completes.
