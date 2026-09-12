# Retained records in Parquet

DocSpec stores immutable record layers in Parquet and queries their files with
DuckDB. This replaces the local JSONL record backend and its custom external
merge code. It does not create a second database copy for each experiment.
`RecordStorage` remains the application interface; the local implementation is
`LocalParquetRecordStorage`.

This is a greenfield format change. The new profile is
`urn:docspec:profile:record-storage:local-parquet:1`; record roots use
`docspec-record-layer/2.0`. There is no legacy JSONL record reader or alias.
Rebuild a dataset with the selected implementation and profile. Historical trials
retain their original pinned wheels and inputs.

## What is stored and queried?

Each Parquet row has three columns:

| Column | Purpose |
| --- | --- |
| `record_identity`, string | The logical identity named by the layer's `RecordSchema` |
| `partition_value`, string | The source or other grouping value named by that schema |
| `record_json`, binary | The complete canonical JSON record, preserving arbitrary accepted payload fields and values |

Routing columns let DuckDB find one document's rows without Python decoding
every other document in the same bucket. The writer requests grouping by
partition value and identity for efficient queries. Queries enforce logical
identity order independently of physical row order. The record schema continues to
govern the complete value. Payload fields are not separately typed Parquet
columns: querying those fields requires JSON extraction. This avoids inferring
a new physical schema for every processor payload or internal ledger.

For SQL analysis, use only the member files listed in the selected layer root.
Globbing the whole record store also includes other layers and superseded
results. After admitting the selected layer, a DuckDB query over that explicit
file list can select `partition_value` and use
`json_extract_string(decode(record_json), '$.payload.title')` for a payload field.
The public catalog reader provides the same selected-layer access without
requiring callers to understand file layout.

The existing hash buckets allow a later result to reuse unchanged files. New
records enter bounded Arrow batches; DuckDB handles sorting and Parquet writing.
Logical shard allowances guide output sizing, and actual encoded file sizes
must fit the physical member limit before publication. Very small limits can
refuse even one row because Parquet has footer and column overhead.

## When are files checked?

`verify_members(reference)` freshly checks the pinned root and physical member
files, including their bytes, declared counts and Parquet schema. A catalog
reader calls it once per complete layer reference when that layer is first
consumed. Failed admission is retryable. Metadata-only opening remains cheap.

Subsequent queries check the root and the logical rows they consume. They do
not hash whole files for every document lookup. A reader observes immutable
files; it does not lock or copy them. External mutation after admission can
affect later queries. A new reader re-admits the files, and `verify(reference)`
always performs a fresh physical and complete logical audit. Retention,
selection and export retain their complete checks.

Writing a layer validates new rows and the pinned base metadata, then references
unchanged base files. Producing that layer reference does not certify inherited
file contents; full verification or retention checks the resulting dependencies.
Application code admits inherited files before consuming their rows.

## Who owns the working resources?

The storage adapter creates its DuckDB connection lazily and uses separate
cursors for independent reads and writes. Iterators close their cursors when
exhausted or closed. A prepared run releases the connection after its workers
stop; later work can reopen it. Close iterators that are not exhausted.

Arrow batch counts and estimated input bytes are bounded. DuckDB runs one native
thread per query and uses its native memory and temporary-storage settings.
Managed memory has a 128 MiB floor; a native 512-partition probe failed with
16, 32 and 64 MiB and passed with 128 MiB. The fixed native partition-writer
open-file setting is one. These observations do not establish a general minimum,
and the memory setting is not a hard process-resident-memory ceiling. The profile
keeps record, root, physical member and scratch limits, and removes the obsolete
Python merge fan-in limit. Capacity qualification must measure the actual
process and temporary files.

DuckDB and PyArrow are core dependencies for this default backend. SQLite
remains appropriate for processor-cache updates, source-build resume state and
current scratch bookkeeping. Raw document bytes stay in the blob store. Dagster
continues to own its own execution state. Portable result exports keep their
existing independent format.

## Evidence and remaining qualification

The [prototype measurements](capacity-workloads.md#direct-parquet-query-prototype)
support the choice of direct file queries. They do not qualify this adapter's
complete experiment lifecycle. Independent review approved the implementation;
the strict suite passed 1,129 tests, including installed-package examples, with
one live integration deselected. The failed JSONL text4096 trial remains
recorded. Fresh larger workloads must pass the same declared time and memory
allowances before capacity qualification closes.
