# Retained records in Iceberg

DocSpec uses `IcebergRecordStorage` behind the `RecordStorage` interface.
DuckDB writes Parquet data and positional deletes through an Iceberg REST catalog.
PyIceberg parses retained metadata and manages temporary catalog registrations.
SQLite remains the authoritative ledger for provenance, publication, progress and
retention. Opaque document bytes remain in the content-addressed blob store.

A record root contains one pinned Iceberg metadata reference, schema and row count;
it does not copy the complete file inventory. The format is
`docspec-iceberg-records`, version 1. Core state manifests remain version 2 and
selected-member manifests remain version 3. Physical snapshot IDs are fresh;
logical IDs and canonical comparison digests keep their existing meanings.

## Configure writes

Set `DOCSPEC_ICEBERG_URI` to a REST catalog endpoint and, when needed,
`DOCSPEC_ICEBERG_TOKEN`. Python callers can instead pass
`IcebergCatalog(uri, token=...)` from `docspec.adapters.storage` to `CoreWorkspace`.
The catalog must support table registration, and its service must see the local
workspace at the same absolute path as DuckDB. The implemented storage profile is
local filesystem storage; a remote object-store profile is not implemented.

For development and tests, Docker can run Apache's pinned REST fixture:

```sh
uv run --frozen python tools/with_iceberg.py pytest tests/test_iceberg_snapshots.py
uv run --frozen python tools/with_iceberg.py python -m examples.offline_demo --output ./experiment
```

The helper shares the current directory and its temporary directory with the
catalog, sets the endpoint for the command, then removes its own container.
Output workspaces must be under the current directory. With an already configured
endpoint it simply runs the command; configure shared paths yourself in that case.
The fixture is for local development, not a deployed catalog recommendation.
The wheel does not start Docker. Reads of retained states and exports need no
catalog service. Subsequent writes register pinned metadata with a catalog at
the original local table path. Relocated snapshots support reads; a writer
refuses them before creating files at the former location.

## Snapshot publication and maintenance

Each write registers a temporary table from its explicit base metadata. Branches
therefore start at their named state, regardless of other writes. DuckDB commits
changed rows and positional deletes together. The adapter syncs new data,
manifests and metadata before returning a retained reference, then drops the
catalog name without purging files. SQLite publishes the logical state only after
those files are durable. Catalog names are disposable write handles.

Updates preserve base data files. The generic partition replacement API selects
rows for deletion; it no longer rewrites physical hash buckets. New inputs target
128 MiB files by default, with a 1 MiB row-group target. These are writer targets,
not exact sizes; actual files must fit the configured 256 MiB member limit.
Compaction rewrites a snapshot and verifies exact logical equivalence. Appending
another layer preserves base files and writes the added rows into that table.

Core maintenance follows each retained snapshot's current data files, delete files,
manifests and metadata. It protects files shared by other retained states before
removing any bytes. Every retained historical state has its own pin. Do not run
catalog purge or independent snapshot expiration against DocSpec's files. Failed
writes can leave unreferenced files; there is no automatic orphan-file sweep.

## What is stored and queried?

Each Parquet row has three required columns:

| Column | Purpose |
| --- | --- |
| `record_identity`, string | The logical identity named by the layer's `RecordSchema` |
| `partition_value`, string | The source or other grouping value named by that schema |
| `record_json`, binary | Canonical record bytes, or canonical comparison bytes for a typed selected-member layer |

Routing columns let DuckDB find one document's rows without Python decoding
every other document. DuckDB sorts new rows by identity
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

For SQL analysis, use the admitted layer's `relation()` or `RecordStorage.relations()`.
These read the exact Iceberg metadata version, including its delete files. A raw
Parquet glob would also read superseded rows. Core state APIs join admitted
membership and values. A string-only JSON extraction cannot supply correspondence
values: canonical comparison preserves number/string distinctions, absence, null
and composite fields.

## When are files checked?

`verify_members(reference)` freshly checks the pinned root and physical member
files against a pinned checksum tree that follows Iceberg metadata,
manifest lists, manifests and data files. Unchanged manifest checksums are shared;
replacing both a file and its checksum does not change the retained root. `verify(reference)` additionally checks every logical row and count. A catalog
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

Iceberg owns the shared file inventory and row-level deletes. A changed
`members-v1` digest also requires the complete comparison stream: SHA-256 chunk
hashes cannot be combined to reproduce that sequence digest. Iceberg does not
remove that semantic cost. Native joins and fresh availability checks can also
still examine the whole snapshot; bounded writes do not imply constant-time reads.

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

DuckDB, PyArrow and PyIceberg are core dependencies. SQLite is the authoritative Core
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
