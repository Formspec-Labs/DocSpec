# In-process Iceberg write catalog

**Yes: DocSpec's writes no longer need Docker. Each record store runs PyIceberg's
SQL catalog in its own process and serves DuckDB's Iceberg writer through a
loopback REST adapter. The PM01 derive benchmark wrote byte-identical output in
all 25 runs (fingerprint `35708d0f…0f50`, the same as the
[2026-09-23 receipt](2026-09-23-derive-encode-once.json)). On the final code, each
invocation finished 1.03 s sooner end to end, and the derive phase took 0.23 s
less wall time.** The Docker fixture's own lifecycle cost 0.62–0.85 s per
invocation before any work began. The in-process catalog starts in 5 ms and
closes in 0.2 ms. Importing it costs 0.10 s, once per process that writes.

The [receipt](2026-09-25-inprocess-iceberg-catalog.json) records every number
below, including each run, the call trace and both catalogs' `add_files` results.
Software: DuckDB 1.5.5 with Iceberg extension `45163a28`, PyIceberg 0.12.0,
SQLAlchemy 2.0.51, PyArrow 25.0.1 and Python 3.12.9, on a 14-CPU arm64 Mac shared
with other work. Every command ran through `uv run --frozen`.

## Why a catalog remains

The catalog was already only a write handle. `IcebergRecordStorage._write_table`
registers a temporary name from explicit base metadata and commits. It then drops
the name without purging. `_pin` seals the resulting metadata file by path. Reads
use `iceberg_scan` with an explicit metadata version, or PyIceberg's
`StaticTable` with a no-op catalog.

Writes still need a REST endpoint. DuckDB performs every data write through
`ATTACH '' AS iceberg (TYPE iceberg, ENDPOINT …)`, and this extension attaches
nothing else. `ENDPOINT_TYPE 'local'` fails with "accepted options are: glue,
s3_tables", and both of those are REST services. The REST service also authors
each commit's metadata file. That is why the Docker fixture had to see the
workspace at its own path. A `type='sql'` catalog alone therefore cannot serve
DuckDB. The adapter answers only the calls DuckDB makes; PyIceberg's
`SqlCatalog` applies every requirement and update. No Python REST catalog server
package turned up on PyPI under the obvious names, and PyIceberg's own
integration tests also run a REST catalog in Docker.

| DuckDB call, one new layer | Result |
| --- | --- |
| `GET /v1/config` | 200 |
| `GET …/tables/write_<uuid>` | 404, the existence check |
| `GET /v1/namespaces/docspec` (twice) | 200 |
| `POST …/tables` with `stage-create` | 200, metadata built but not persisted |
| `POST …/tables/write_<uuid>`: `assert-create` plus ten creation updates | 200 |
| `GET …/tables/write_<uuid>` | 200 |
| `POST /v1/transactions/commit`: `add-snapshot`, `set-snapshot-ref` | 204 |

An incremental edit makes three calls: two table loads and one transaction commit
with its delete and insert snapshots. Registration and dropping stay in process.
`SHOW TABLES` on the writer's connection also lists namespaces and tables.
The adapter refuses any other call with 400, including an unstaged create, and
any caller without its random token with 401. It also refuses a transaction
spanning several tables, which PyIceberg's per-table commits could not make
atomic. DocSpec commits one table per transaction.

DuckDB 1.5.5 can also write Iceberg with no catalog, through
`COPY … TO … (FORMAT iceberg)`, but that cannot carry DocSpec's revisions. Every
COPY writes a fresh one-snapshot table holding only its own rows, and `APPEND` is
accepted and ignored. There is no catalog-less target for DELETE or MERGE. Its
files are SNAPPY, and `COMPRESSION zstd` does not change that.

A follow-up commit retired the REST option: `DOCSPEC_ICEBERG_URI`,
`DOCSPEC_ICEBERG_TOKEN`, `IcebergCatalog` and `CoreWorkspace(catalog=)`. Nothing
outside DocSpec constructed it, and CI never ran it. A handle that is dropped
after every commit has no reader outside the writing process. The variables are
now ignored.

## Results

Each set interleaves before, after, before, and so on. Before is DocSpec main
`3133850` under its `tools/with_iceberg.py` fixture. After is this branch without
Docker; the last column names its code. Times are medians of three runs, in seconds.

| Set | Load (1 min) | Derive wall | Derive CPU | Process wall | After arm ran |
| --- | ---: | ---: | ---: | ---: | --- |
| A | 16–17 | 1.873 → 1.677 | 1.695 → 1.837 | 3.403 → 2.542 | the first adapter; same derive path |
| B | 44–48 | 2.009 → 1.717 | 1.704 → 1.814 | 3.784 → 2.503 | wake-pipe adapter, eager import |
| C | 33–43 | 4.379 → 2.868 | 1.859 → 2.038 | 7.629 → 4.616 | commit `92e0496` |
| D | 15–18 | 1.877 → 1.644 | 1.695 → 1.813 | 3.421 → 2.388 | final code, REST option retired |

Every run read 5,000 values and produced the same rows-state identity. Derive CPU
rises by 0.11–0.18 s because catalog work now runs in the measured process. The
Java catalog's CPU ran inside the Docker VM, which `getrusage` does not see. Under
heavier load the Docker arm degrades most: one before run took 11.6 s end to end.
Reads never touch a catalog; their times differ only by noise.

| Lifecycle | Seconds |
| --- | ---: |
| Docker fixture around `true`: run, readiness, removal (3 runs, load near 8) | 0.615, 0.845, 0.830 |
| Import the adapter after the storage package, once per writing process (median of 5) | 0.104 |
| In-process catalog start, median of 20 (first: 0.013) | 0.0054 |
| DuckDB `ATTACH` to it, median of 20 | 0.0066 |
| Close, median of 20 | 0.0002 |

The in-process rows were measured on the final code at load 15. The first adapter
closed by polling and took 50 ms per close. The final adapter blocks on a wake pipe
and uses no CPU while idle. A store dropped without `close()` still stops it,
through `weakref.finalize`. Record storage imports the adapter only when a store
first writes, so startup is unchanged. `import docspec.runtime` took 0.273 s on
main and 0.273 s on this branch, and neither loads SQLAlchemy or an HTTP server.

## Equivalence checks

| Check | Result |
| --- | --- |
| Derive output, 25 runs | Identical fingerprint and rows-state identity |
| Metadata authorship, one before and one after workspace | Same top-level fields. Only Java's informational `created-at` property differs; both write ZSTD data. |
| Parquet codec | The Java service added `write.parquet.compression-codec=zstd` to every new table. PyIceberg adds nothing: a DuckDB table created without the property wrote SNAPPY. DocSpec's `CREATE TABLE` now states zstd, and a test pins it. |
| `add_files` of a producer file without field IDs | Identical name mapping, schema, file list, rows and relocated rows. The file is registered in place, byte-identical. Only the Java-injected codec property differs. The REST arm ran at `92e0496`, before that client was retired. |
| Catalog-specific locations in metadata | None in either catalog. Metadata paths stay under the table directory. |
| Relocation | The existing copy-and-verify test passes in process, and relocated `add_files` reads match. |
| Java-authored base revised in process | `upsert` onto a workspace that main wrote under Docker returned exactly the expected 17 rows. Two revised tables committed onto metadata the Java service wrote. |
| URL syntax in the scratch path | A `?` in it once moved the SQLite file outside, and a second store then failed. A test now pins that each store's file stays inside its own scratch directory. |
| Full suite, no Docker, final code | 1,581 passed, 1 skipped (the optional PDF extra), 0 failed |
| Built wheel in an empty environment | Writes in process. It resolved SQLAlchemy 2.1.1 there, and the lock pins 2.0.51. |

## Limits

- Timings come from a shared machine while other work ran; each set records its
  load. The fingerprints and identities are contention-immune.
- A table created through the PyIceberg client, as the admit-by-reference spike
  does, gets no codec property from this catalog. An implementation of that
  path should state `write.parquet.compression-codec=zstd`, as DocSpec's own
  `CREATE TABLE` does. Otherwise, later DuckDB writes into such a table produce
  SNAPPY files.
- The benchmark tool is the PM01 gate's `tools/bench_derive.py` (sha256 in the
  receipt), copied with only its output root moved out of the gate directory.
  The before arm ran main's exported source on `PYTHONPATH`, the method
  `tests/support/older_workspace.py` documents.
