# Retained records in Parquet

DocSpec stores immutable record layers in Parquet and queries their files with
DuckDB. Bulk state operations use those immutable files directly.
`RecordStorage` remains the application interface; the local implementation is
`LocalParquetRecordStorage`.

Record roots use `docspec-record-layer/4.0` and the local Parquet profile version 3.
Core state manifests use version 2; selected-member manifests use version 3.
They reference those layers; logical identities remain distinct from file digests and
physical representations. Historical trials retain their original pinned inputs
and wheels, while current code has one native record implementation.

## What is stored and queried?

Each Parquet row has three required columns:

| Column | Purpose |
| --- | --- |
| `record_identity`, string | The logical identity named by the layer's `RecordSchema` |
| `partition_value`, string | The source or other grouping value named by that schema |
| `record_json`, binary | Canonical record bytes, or canonical comparison bytes for a typed selected-member layer |

Routing columns let DuckDB find one document's rows without Python decoding
every other document in the same bucket. DuckDB sorts by bucket and identity
before the writer chooses file boundaries. Public row and batch readers
enforce logical identity order independently of physical row order. Native joins
leave sorting to the consumer that needs it. The record schema continues to
govern the complete value. Ordinary payload fields stay in canonical JSON;
querying those fields requires JSON extraction. `RecordSchema.columns` can name
explicit string/binary columns for internal records, without inferring a physical
schema from user payloads. Selected-member layers use this facility for occurrence
IDs, sorting keys and external content references. Their comparison bytes stay
directly in `record_json`, so ordering and hashing require no JSON unwrapping.
The same writer, byte limits, admission, physical sharing and compaction apply to
both forms. Full selected-row admission checks keys, types, materiality, content
references and canonical bytes before successful retention.

For SQL analysis, use only the member files listed in the selected layer root.
Globbing the whole record store also includes other layers and superseded
results. After admitting the selected layer, a DuckDB query over that explicit
file list can select `partition_value` and use
`json_extract_string(decode(record_json), '$.payload.title')` for a payload field.
Core state APIs provide admitted membership and values. Do not use a string-only
JSON extraction as a correspondence value: canonical comparison must preserve
number/string distinctions, absence, null, and composite fields.

The existing hash buckets allow a later result to reuse unchanged files. New
records enter bounded Arrow batches; DuckDB sorts them and the shared PyArrow
writer packs them into Parquet. File targets default to half the physical member
limit (128 MiB with the default settings). Row groups target 384 KiB of logical
column bytes or 2,048 rows, independently of file size. A larger individual
record occupies its own group within the existing value limit. DuckDB can prune
these groups during selective reads. File targets are logical-byte estimates,
not promises about compressed sizes.
Each file description retains exact `identityMin` and `identityMax` values
computed during writing. Admitted identity lookups and union checks skip files
whose ranges cannot overlap the requested identities. Ranges may overlap, so
this reduces file opening without promising constant-time lookup. Full logical
admission checks every row against its declared bounds; raw row readers keep
their consumed-row checks. Union results preserve all base files.

Logical file allowances guide output sizing, and actual encoded file sizes
must fit the physical member limit before publication. Very small limits can
refuse even one row because Parquet has footer and column overhead.

## When are files checked?

`verify_members(reference)` freshly checks the pinned root and physical member
files, including their bytes, declared counts and Parquet schema. A catalog
reader calls it once per complete layer reference when that layer is first
consumed. Failed admission is retryable. Metadata-only opening remains cheap.

Queries opened from references check the root and the logical rows they consume.
Native joins and streamed batches reuse the descriptor and checked file paths
from an existing layer admission within its owning operation. The record store
keeps at most eight admitted layers per thread during shared publication
protection. State reads and ledger entity lookups use this same bounded cache.
Nested operations share it; leaving the outer protection scope clears it. An
evicted layer checks availability again. Within each fresh availability check,
up to 256 shared parent directories are checked once; every member still receives
its own regular-file and byte-size check. This directory reuse ends with the
call. Exclusive cleanup and unprotected
read-only exports do not reuse this cache, and explicit audits remain fresh.
Readers do not hash whole files for every document lookup. A reader observes immutable
files; it does not lock or copy them. External mutation after admission can
affect later queries. A new reader re-admits the files, and `verify(reference)`
always performs a fresh physical and complete logical audit. Retention,
selection and export retain their complete checks.

Writing a layer validates new rows and the pinned base metadata, then references
unchanged base files. Producing that layer reference does not certify inherited
file contents; full verification or retention checks the resulting dependencies.
Application code admits inherited files before consuming their rows.

## Incremental selections and comparison evidence

Retained selected-member manifests include their checked `ComparisonEvidence`.
External admission recomputes it from the admitted rows. Later reads reuse the
saved evidence under the same retention and availability checks.

An evaluator-certified earlier selection supplies unchanged canonical values.
The state resolver records which revision it actually applied; its publisher-created
certificate binds the exact revision and immutable result. Certified edit keys
narrow the address comparison. Untrusted or missing certificates use a full native
membership comparison. The selected-value owner always checks actual occurrence
IDs and evaluates only changed members.

Changed addresses and selected rows live in session-owned DuckDB temporary
tables. They may spill to scratch; 2,048 rows and 8 MiB are batch limits, not
limits on the complete change set. Comparison against the retained base removes
intermediate edits that revert. Unordered equality compares canonical bytes and
signed duplicate counts. Ordered equality can reuse evidence when changed keys
keep the same bytes and sorting tokens; otherwise it computes the full ordered
comparison. Origins are updated even when the comparison stays equal.

Ordinary state writes do not hash complete membership. Admission checks exact
membership when adding another representation of an existing state. Compaction
transfers its already checked equivalence. New external data still passes full
record and membership admission.

The flat file inventories and touched-bucket rewrites remain. A changed
`members-v1` digest also requires the complete comparison stream: SHA-256 chunk
hashes cannot be combined to reproduce that sequence digest. This implementation
adds no table-format dependency or second storage backend.

## Who owns the working resources?

The storage adapter creates its DuckDB connection lazily and uses separate
cursors for independent reads and writes. Iterators close their cursors when
exhausted or closed. Selection caches retain at most 32 native plans; eviction
and session exit close their temporary-table connections. `CoreWorkspace` releases its connections when it closes; later work can reopen
the workspace. Close iterators that are not exhausted.

Arrow batch counts and estimated input bytes are bounded. DuckDB runs one native
thread per query and uses its native memory and temporary-storage settings.
The shared default memory allowance is 6 GiB, configurable through
`CoreWorkspace(engine_memory_bytes=...)`. It is not a hard process-resident-memory
ceiling. Scratch, record, root and member limits still apply. Measure actual
process memory and temporary files for the intended workload; do not treat the
engine setting as evidence of a complexity bound.

DuckDB and PyArrow are core dependencies. SQLite is the authoritative Core
metadata ledger and also supports source-build recovery and disposable record
spools. Raw bytes remain in the blob store; Dagster owns its execution state.

## Evidence and remaining qualification

[Record tests](../tests/test_storage_records_catalog.py),
[layer conformance](../tests/conformance/test_record_storage_contract.py),
[batch tests](../tests/test_record_batches.py), and
[state tests](../tests/test_core_states.py) verify native storage behavior.
[Historical measurements](capacity-workloads.md#direct-parquet-query-prototype)
record their actual inputs; current capacity acceptance remains in the
[Core task list](core-model-implementation-tasks.md).
