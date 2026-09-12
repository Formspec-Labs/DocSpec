# Read a pinned source catalog

Consumers can admit a catalog once and choose domain objects or plain JSON
dictionaries. Dictionaries avoid constructing and freezing a `SourceCatalogItem`
only to turn it back into a dictionary. Both paths retain the supplying partition
digest, enforce row limits, and check row ordering and counts.

```python
from docspec.source_catalog import SourceCatalogArtifactReader

catalog = SourceCatalogArtifactReader(store, producer=producer).admit_snapshot(reference)
for located in catalog.iter_located_mappings():
    consume(located.item, supplied_by=located.blob_ref)
```

`catalog.summary` identifies the artifact and its recorded completeness evidence.
Its `source_native_inputs` retain full source descriptions, including the
provider's reported collection outcome or explicit `null` when unreported.
`accepted_record_outcomes` records which outcomes this build allowed. The reader
checks these descriptions against the catalog's source pins and saved acceptance;
it does not independently repeat upstream collection admission. See
[input acceptance and original evidence access](catalog-inputs.md#decide-whether-to-accept-rejected-source-records).
Use `catalog.iter_mappings()` when locations are unnecessary, or
`catalog.open_snapshot()` for the existing `SourceCatalogSnapshot` object stream.
Each call opens a fresh stream. Returned dictionaries belong to the caller;
editing them does not change the artifact or another read.

A caller that already admitted the artifact through Rulespec can keep that work:

```python
from docspec.source_catalog import open_admitted_source_catalog

catalog = open_admitted_source_catalog(
    artifact, member_source, blob_source=blob_source,
    producer=producer, expected_pin=pin,
)
```

Here `artifact` is the `VerifiedArtifact` returned by
`rulespec_artifacts.admit_artifact`, and `pin` is the caller's expected
`rulespec_artifacts.ArtifactPin`. DocSpec checks the root against that pin, checks
manifest digests and the small policy/receipt members, and applies its existing
receipt verifier. It does not repeat generic artifact admission or recompute
producer diagnostics from all catalog rows. The receipt remains the producer's
claim; `reader.verify_snapshot(reference)` separately performs that full audit.
Rulespec established closed membership at admission time. Opening the admitted
catalog rechecks the bytes this reader uses; it does not inventory unrelated
files added later.

Each partition is checked against its content digest before returning rows from
it. Rulespec's local blob reader already performs this check and detects changes
while its file is open. Other blob providers stream into a temporary file while
DocSpec hashes their exact bytes; memory stays bounded, and temporary disk use is
bounded by the declared partitions open at that time. This also supports sources
that cannot seek and prevents a later provider read from substituting different
bytes for the admitted payload.

Row dictionaries undergo canonical JSON, schema, and catalog row checks. A reader
that explicitly audited the same pinned catalog can reuse those row proofs while
still checking payload identity and structural ordering/counts. Complete
iteration checks the whole stream; an early stop has checked only the opened
partitions and visited rows. Close partially consumed iterators (or use
`contextlib.closing`) to release their files promptly.

The public API provides DocSpec's side of [SpicySearch's reader
request](history/2026-09-05-reader-api-requests.md). Updating that consumer and its
wheel pin belongs to SpicySearch SC01.
