# Open retained metadata or audit a complete result

`DocumentCatalog.open(reference)` and `open_reader(reference)` admit the exact
retained release metadata. They check the container pin, accepted producer,
release state, linked plan/run/commit and execution controls, profile references,
and declared layer inventory. They do not scan the saved document stores,
record members, or content blobs. Counts in this metadata are declarations bound
to the release, not a fresh recount of its dataset.

This makes an intact release descriptor browsable even when an unused content
object is unavailable. A reader checks each record member it consumes. Content
reads check their blob references; exhaust a stream to complete its byte checks,
or close it when stopping early. Opening metadata does not establish that every
referenced object is available or that all saved rows agree with one another.

Call `DocumentCatalog.audit(reference)` for complete retained-state validation.
It composes the same metadata checks with record and blob verification, logical
source/output relationships, stage and processor receipts, task/store agreement,
and the complete failure summary. It returns the same `DocumentRelease` type;
there is no second result model or saved verification status.

## Command line

Both commands use the existing local storage roots and explicit producer
acceptance:

```sh
docspec document-catalog open --reference release-ref.json \
  --catalog-root ./catalog --record-root ./records --blob-root ./blobs \
  --store-root ./stores --control-root ./controls \
  --implementation-id "$DOCSPEC_IMPLEMENTATION" \
  --verifier-implementation-id "$DOCSPEC_VERIFIER_IMPLEMENTATION"
```

Replace `open` with `audit` to check the complete dataset. `open` reports
`verificationScope: pinned-metadata-and-linked-controls` and
`verdict: metadata-valid`. A successful `audit` reports
`verificationScope: complete-retained-state` and `verdict: pass`.
`document-catalog compare` verifies the selected record layers; it does not audit
unselected layers or content blobs.

## Where complete checks remain required

Staging, retaining, and selecting a result audit the data before authorizing
publication or a current-pointer change. Export, garbage collection, compaction,
and the comprehensive `open_local_inspection` view also audit their retained
inputs. Inspection's exact population totals still require scans. This change
adds no separate quick-summary API.

Planning and execution may open metadata and consume only the members and bytes
needed for the selected work. Corruption elsewhere can remain undiscovered until
an explicit audit or retention. A reconciled run alone does not prove that an
untouched inherited object is available. Retention checks that complete result.

A newly built stage is audited once by its builder, then its published metadata
is read back. Retention audits its staged input once before an exclusive rename;
an independently pre-existing destination receives its own audit. Re-reading the
published metadata confirms the pin without repeating the full data scan in the
same operation. These operations do not lock external record or blob storage.
An audit observes the data it reads, not a permanent guarantee against later
mutation. Later reads and new audit/publication calls check their own inputs.
