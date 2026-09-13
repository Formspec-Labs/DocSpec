# D07: catalog preview and successive snapshot growth

Decision: extend existing admitted catalog readers and prefix reuse. Catalog
preview does not need another planner, retained comparison index, or general
multi-policy router. Successive full snapshots satisfy the immediate growth
requirement while preserving explicit source identity and omission semantics.

## Evidence and ownership

The complete catalog row is retained in `SourceItem.metadata.sourceCatalogRow`.
The old planner compared that whole source description and scheduled full work
when only an interpretation's policy digest changed. The base reuse helper also
required complete source-row equality. This made a selection policy revision
capable of refetching every otherwise unchanged selected candidate.

Source metadata does not enter the current extractor, segmenter, or processor
payload interfaces. Their inputs are captured files, representations, and
segments with the configured stage identities. A small acquisition equality
rule can therefore separate current source descriptions from retained document
work without removing evidence from either.

`SourceItem.same_acquisition_inputs` now compares qualified item ID, source
version, active state, and the complete candidate tuple. Both planning and
retained-prefix preparation use it. A metadata-only change schedules an
ordinary repair entry with the fresh source row and the deepest compatible
prefix. The existing checkpoint verifier still checks files, selected stages,
blobs, receipts, and reusable processor results. Original capture provenance
remains attached to original captured bytes.

Non-stage governing changes keep conservative full execution. Failed-item
repair applies before metadata replacement: policy-only or unrelated metadata
changes do not admit a retry. Until an admitted repair, an inherited failed
item keeps its old description and failure. A future processor capability that
consumes source metadata must include that input in its identity before this
reuse rule can apply to it.

## Preview and comparison

`runtime.preview_local_catalog` opens exact local references under explicit
producer acceptance. `application.catalog_preview` consumes their existing
summaries and admitted mapping streams. It reports current selection counts,
candidate/reason samples, and optional additions, changes, and removals from an
older catalog. It exhausts the streams even with zero samples; sample count and
canonical-entry byte caps bound returned detail independently of totals.

The ordered before/after merge and field-difference function are shared with
D19 result inspection in `application.comparison`. Catalog comparison supplies
UTF-16 ordering; saved result comparison retains its existing record order.
Missing fields and explicit null values are distinct. There is no second
schema validator or persisted comparison format.

Catalog policy selection, later run filters, and actual saved work are separate
questions. Preview explains the first. Existing prepared-run inspection shows
the jobs execution will use. The catalog diff makes no cost or processing-work
prediction, and the work ledger does not claim to count every filter match.

## Snapshot and collision decisions

Each successor is a full chosen universe. Omitted prior items become planned
removals, subject to run filters. `observed-crawl` describes source coverage;
it neither preserves omissions nor requests an append operation. A preview
therefore says `removedFromCatalog`, not publisher deletion. `supersedes`
records predecessor evidence without merging or publishing a current pointer.

The chosen policy may already support multiple disjoint input parts. Supplied
records share one namespace/version and reject repeated qualified IDs. Existing
provider-specific collision rules remain with their policies and retain the
discarded source facts. Arbitrary mixed-policy composition is outside this
change. Different source namespaces keep different qualified identities.

## Acceptance and review

The architecture subagent and independent reviewer agreed with the bounded
direction. The implementation is qualified by `tests/test_catalog_iteration.py`:
real catalog policy changes and snapshot growth, metadata refresh without new
stage calls, exact old capture provenance, current source rows, clean live-value
comparison, explicit failure repair, conservative governing changes, crawl
omissions, repeated IDs, empty previews, bounds, iterator closure, and missing
versus null differences. Existing planner, prefix refusal, failure repair, and
D19 tests supply adjacent regression coverage.

Validation results are recorded with the eventual D07 checklist completion and
commit; this note does not claim unrun checks or live provider qualification.
