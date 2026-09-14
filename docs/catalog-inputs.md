# Build a catalog from provider data or supplied records

`build_local_catalog` accepts source objects, a workspace, an interpretation
policy, a catalog identity, and the producer that will declare the output. It returns the
existing verified catalog reference and summary. Catalog-only work creates only
the workspace's `sourceCatalog` storage; no document is fetched or processed.
Use [catalog preview and iteration](catalog-iteration.md) to review selected
candidates and reasons, compare successive full snapshots, and reuse unchanged
document work while updating catalog descriptions.

## Supply a small set of records

```python
from docspec.runtime import build_local_catalog, open_local_catalog
from docspec.source_catalog import (
    SourceCatalogCandidate, SuppliedRecordCatalogPolicy, SuppliedRecordSource,
)
from pathlib import Path

namespace = "urn:example:my-notes"
source = SuppliedRecordSource(({
    "recordId": "notes-1", "sourceIssuedVersion": "draft-2", "title": "Project notes",
    "metadata": {"owner": "contributors"},
    "candidateRenditions": [SourceCatalogCandidate(
        "body", "text/plain", "immutable-object", "notes.txt",
    ).to_dict()],
},), source_system_id=namespace, source_system_version="1",
    source_state_scope="complete-snapshot", max_records=100, max_bytes=1024**2)
workspace = Path(absolute_workspace_path)
result = build_local_catalog(
    (source,), workspace, policy=SuppliedRecordCatalogPolicy(namespace, "1"),
    catalog_id="urn:example:my-notes:catalog", producer=catalog_output_producer,
    max_scratch_bytes=16 * 1024**2,
)
catalog = open_local_catalog(result.reference, workspace, producer=accepted_catalog_producer)
for row in catalog.iter_mappings():
    print(row["documentId"], row["selection"])
```

The input shape has exactly the five keys shown. `title` may be `None`; arbitrary
JSON metadata stays in the saved source facts. Candidate renditions use the
existing public type and may include expected SHA-256 and byte-size values.
An empty candidate list produces an `unavailable` catalog row with a reason.
It remains available for catalog inspection. The policy does not infer agencies,
dates, source URLs, or observed topics from arbitrary metadata.

The source snapshots canonical bytes within the supplied record and byte limits.
Later mutations to caller objects or returned row dictionaries cannot change its
identity. Record order does not matter. Duplicate IDs within a source namespace
refuse; the same local record ID in another namespace has a different item ID.
Use your own namespace, and change `sourceIssuedVersion` when the caller's record
revision changes. Raw fields, candidates, namespace, scope, and schema all affect
the snapshot pin; these are input evidence rather than downloaded-document hashes.

`complete-snapshot` describes the complete universe the caller submitted.
`observed-crawl` records an explicitly incomplete observed population. Neither
setting establishes publisher-wide coverage, a successful upstream collection,
or document acquisition. `metadata` preserves assertions as supplied. Captured
bytes and acquisition receipts appear only when a later run actually fetches
documents.

Every catalog still defines the whole chosen dataset universe for a processing
run. When a successor uses a base result, prior items absent from its new catalog
enter planning as deletions, subject to the run's selection filters, including
when the input scope is `observed-crawl`. That
scope describes upstream coverage; it does not request incremental updates or
preserve omitted items. Include all items you intend the successor to retain.

Run [the supplied-record example](../examples/supplied_records.py) without a
provider package or sibling checkout:

```sh
uv run --frozen --extra dagster python -m examples.supplied_records --output /absolute/new-notes-catalog
```

## Use an installed provider reader

Choose the `spicy-docs` package extra for this integration. In a checkout, add
`--extra spicy-docs` to the extras you select for `uv sync` and `uv run`.
The optional provider is pinned to
SpicyDocs `0.7.0`; its base installation adds source reading without acquisition,
analytics, PDF or Dagster dependencies. See the
[wheel installation instructions](../CONTRIBUTING.md#install-the-optional-source-reader)
for use outside the checkout. Ordinary supplied-record experiments need no
provider package.

The [GAO topic example](gao-topics.md) maps admitted source fields and evidence
into supplied records, then applies an exact-label filter without body capture
or a new processor type.

The [FEC committee example](fec-committees.md) joins retained committee records
and evidence once, then builds a bounded metadata catalog through supplied
records. It preserves full source fields and the original observation references
without making API requests or treating committee metadata as document bodies.

The [SpicyRegs comment example](spicyregs-comments.md) preserves retained table
fields, nulls, diagnostics and attachment candidates, then filters by docket ID
before any document is fetched. Its Parquet fixture uses the provider's optional
table dependencies; the admitted source reader remains independently usable.

The existing source port streams already admitted records and renditions. The
optional SpicyDocs adapter uses the installed provider reader; importing DocSpec
does not import that package. Supply its exact artifact pin and independently
accepted verifier implementation, then choose DocSpec's interpretation policy:

```python
from docspec.source_catalog import (
    FederalRegisterCatalogPolicy, SpicyDocsSourceNativeAdapter, spicy_docs_source_profile,
)

profile = spicy_docs_source_profile("federal-register")
source = SpicyDocsSourceNativeAdapter.from_local(
    provider_artifact_path, blob_root=provider_blob_path,
    logical_id=expected_logical_id, artifact_digest=expected_artifact_digest,
    profile=profile, accepted_verifier_implementation_ids=accepted_provider_verifiers,
)
result = build_local_catalog(
    (source,), workspace, policy=FederalRegisterCatalogPolicy(profile.source_system_id),
    catalog_id="urn:example:federal-register-catalog", producer=catalog_output_producer,
    max_scratch_bytes=128 * 1024**2,
)
```

This uses the same builder and verification as source-catalog commands. The
output producer describes who made this catalog; it does not authorize its
upstream source. The provider reader's accepted verifiers and a later catalog
reader's accepted producer remain separate, explicit choices. The
installed-provider test uses a pinned SpicyDocs wheel and bounded fixtures;
it does not establish live provider completeness. Other admitted sources can
implement `SourceNativeRecordSource` and supply their own matching policy.

## Decide whether to accept rejected source records

The current installed SpicyDocs reader reports collection outcomes separately
from document acquisition and processing. `source.describe().collection_outcome`
preserves that report; `source.describe().to_dict()` returns a mutable JSON copy.
The saved catalog retains the complete description under `sourceNativeInputs`,
along with the build's `acceptedRecordOutcomes`. Ordinary catalog opening,
preview, and separately authorized source inspection expose this evidence.

By default, a build accepts `empty` and `no-record-rejections`. An empty
observation does not prove publisher-wide absence. To accept a published subset
whose provider rejected some records, make that choice explicit:

```python
from docspec.source_catalog import DEFAULT_ACCEPTED_RECORD_OUTCOMES

result = build_local_catalog(
    (source,), workspace, policy=policy, catalog_id=catalog_id,
    producer=catalog_output_producer, max_scratch_bytes=128 * 1024**2,
    accepted_record_outcomes=DEFAULT_ACCEPTED_RECORD_OUTCOMES | {"partial-rejection"},
)
```

This still refuses `total-rejection`, which contains no published records.
Include that outcome separately only when you intend to accept it. The CLI's
repeatable `--accepted-record-outcome` sets the complete accepted set when used;
repeat it for `empty`, `no-record-rejections`, and `partial-rejection` to match
the Python example. Acceptance is checked before creating build output and is
part of existing resume identity. It does not change the interpretation policy.

Accepting a partial or totally rejected input still builds a full dataset
snapshot. With a base result, omitted prior items enter planning as deletions,
subject to selection filters. Acceptance does not request append semantics or
preserve records that the input no longer publishes.

The report retains the provider's original requested scope, collection policy,
warnings, and count names. For the qualified provider, discovered records equal
input observations plus failed records; input observations equal published
records plus discarded observations. Those units describe upstream collection,
not selected documents or processing attempts. The provider validates its count
arithmetic, traversal, and evidence; DocSpec preserves its reported evidence,
bound to the original source pin, without repeating that semantic admission.

Supplied records report `collectionOutcome: null`: no upstream collection outcome
was observed. The current provider refuses unresolved traversal and transient or
unclassed collection failures instead of publishing an admissible release.
DocSpec preserves that refusal boundary; an accepted partial release represents
deterministic record rejection, not unresolved collection or permanent absence.

Use `source.record_evidence(source_record_id)` for a published record's original
observation, `source.iter_failures(limit=20)` for a bounded rejection sample, and
`source.read_evidence(blob_ref, max_bytes=...)` for its original bytes. These methods
delegate to the provider's existing membership and byte checks. Failure IDs may
be provider-assigned placeholders for unclassifiable records. A limit bounds
returned failures, not necessarily the number of ledger rows scanned. Exhaust
or close iterators promptly. The catalog retains the source pin and report;
reading raw provider evidence later requires that original provider artifact,
its blobs, and independent verifier acceptance. No duplicate failure ledger is
stored in the dataset.

## Reopen, resume, or process

`open_local_catalog` requires existing storage and creates no directories. It
returns the existing admitted reader described in [catalog evidence](catalog-evidence.md).
Exhaust or close row iterators to release resources. A wrong producer or artifact
pin refuses. Full producer-diagnostic recomputation remains the explicit
`SourceCatalogArtifactReader.verify_snapshot` operation.

An optional absolute `resume_workspace` path reuses the builder's existing SQLite
recovery state after interruption. Identical source descriptions, outcome
acceptance, policy, producer, and catalog identity are required. The supplied path remains after completion;
ordinary temporary scratch is removed on success or failure. `max_scratch_bytes`
reserves database and rollback-journal space, separately from staged/published
catalog bytes and other concurrent work. Supplied-record memory is separately
bounded by `max_records` and canonical `max_bytes`; those limits do not bound
arbitrary Python objects the caller already owns.

Open the admitted catalog, then pass its `SourceCatalogItem` rows to
`workspace.documents(fetcher=...).import_sources(...)` in a `CoreWorkspace`.
Run the resulting source state to capture and process now or later. An explicitly
configured `LocalFileContentFetcher` resolves relative candidates below its input
root; catalog construction does not create the source files. See [Python runs](python-runs.md).

Catalog succession uses `supersedes`. Neither building nor opening silently
selects current. The CLI retains the same atomic catalog publication checks.

## CLI build reports and verification

`docspec source-catalog build` emits a `docspec-source-catalog-build-report` on
standard output after successful publication. Save that output when you need
the invocation's source paths, accepted source-verifier IDs, selected provider
profiles, or actual serial/parallel execution details. Its `catalog` field is
the ordinary `SourceCatalogRef` containing the logical ID, digest, and locator.
The command requires no `--receipt` path and creates no second command receipt.

`docspec source-catalog verify` takes `--root`, a `--reference` JSON file, and the
accepted `--implementation-id` and `--verifier-implementation-id`. It verifies
the catalog's exact pinned bytes and semantics through the existing reader.
It works after relocation and with catalogs built through Python. It does not
require the provider package, original source directories, or a saved build
report. Its `docspec-source-catalog-verification` output uses format `2.0`.

The artifact retains its sealed policy, source descriptions and outcomes,
counts, diagnostics, byte measurements, and build receipt. Verification checks
those values against the catalog rows. Invocation-specific logs remain separate
from that dataset evidence. This replaces the redundant command-receipt path;
see the [cleanup decision](cleanup-decisions.md#verify-the-catalog-once).
