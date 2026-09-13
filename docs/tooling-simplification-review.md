# A smaller DocSpec using existing tools

**Recommendation: test a thinner DocSpec built around native Dagster stages and
batch tables.** Keep document selection, exact captured inputs, source
coordinates, processor configuration and retained alternatives as DocSpec data.
Let existing tools execute work and store/query that data. This is a proposed
direction, not a completed migration or measured speedup.

Three agents investigated workflow ownership, storage and validation/reuse on
September 12, 2026. This review covers source `95645bb`; the installed performance
baseline is `c898512`. The baseline is unchanged while alternatives are assessed.

## What the current code costs

The repository contains 37,843 Python source lines. That count includes useful
document processing and source policies; it is not an estimate of removable code.
The important finding is ownership: the
[Dagster adapter](../src/docspec/adapters/dagster.py) runs a whole DocSpec task,
whose internals still manage stage dependencies, processor retries, checkpoints,
store revisions and delivery. Adding decorators around that task would retain
those mechanics.

The completed markup experiment's main workspace contains 7,424 processor-attempt
receipts, 7,424 invocation receipts and 7,424 result files: **22,272 small JSON
files** across two processor versions over 3,712 segments. These three categories
contain 52,091,425 logical bytes and occupy 91,226,112 allocated bytes on this
APFS filesystem. Each new file goes through its own `fsync` in
[`_write_once`](../src/docspec/adapters/storage/files.py). The
[processor runtime](../src/docspec/application/processor_runtime.py) creates all
three kinds for a successful uncached invocation. This establishes file and
durability-call multiplicity; it does not isolate their elapsed-time cost.

The [Parquet change](record-storage.md) improves retained-file queries, but its
three columns still contain the full row as canonical JSON bytes. It also feeds
bounded Arrow batches into a temporary DuckDB table before sorting and writing
Parquet. Text4096 recovery reached 996,540,416 resident bytes, about 950 MiB. The
process measurement does not locate that peak or establish a connection leak.

## Which tools help?

| Choice | What it can replace | Assessment |
| --- | --- | --- |
| **Native Dagster stages, resources and batch partitions** | Internal execution dependencies, retry loops, dispatch messages and execution-state revisions | Strongest candidate for reducing concepts and code. It requires retiring the inner execution path, with a deliberate batch recovery boundary. |
| **DuckDB and Arrow directly** | Temporary-table insertion machinery; SQL can also select missing work and compare batches | Smallest storage experiment. These dependencies are already present. A bounded Arrow reader can feed the existing native writer. |
| **dlt** | Connector ingestion, cursors, normalization and destination loading | Useful for a named ingestion problem. No current DocSpec execution machinery disappears simply by adding it. |
| **Daft** | Document-oriented batch transformations and heavier model processing | Worth considering for a measured processing bottleneck. Its documented checkpointing currently requires Ray and restricts joins, sorting and multiple sources; it is not the simplest default for this local workflow. |
| **Hugging Face Datasets** | Arrow-backed mapping, cached transformations and dataset streaming | Useful to experiment consumers. Its transform fingerprints and iterator checkpoints do not directly replace retained alternative results and source-linked evidence. Adding it to this core has no demonstrated deletion benefit. |

Dagster's own guidance supports direct resource/dependency-based I/O where SQL
or another library owns storage. A new custom I/O-manager framework is optional,
not a prerequisite. Native retries already support backoff and jitter.
[I/O guidance](https://docs.dagster.io/guides/build/io-managers),
[retry APIs](https://docs.dagster.io/guides/build/ops/op-retries).

DuckDB accepts Arrow `RecordBatchReader` inputs. Arrow also supplies a partitioned
dataset writer with row-group and open-file controls. Test these facilities
before maintaining more sorting, batching or file-management code. Neither
native memory settings nor smaller Python code prove lower process memory.
[DuckDB Arrow queries](https://duckdb.org/docs/current/guides/python/sql_on_arrow),
[Arrow dataset writing](https://arrow.apache.org/docs/python/generated/pyarrow.dataset.write_dataset.html),
[DuckDB memory scope](https://duckdb.org/docs/lts/configuration/pragmas).

The dlt assessment follows its documented
[resource model](https://dlthub.com/docs/general-usage/resource) and
[cursor behavior](https://dlthub.com/docs/general-usage/incremental/cursor).
Daft demonstrates a complete PDF-to-Parquet processing pipeline, but its
checkpoint skipping is keyed to inputs, independent of changes to transforms;
configuration-sensitive experiments still need appropriate version keys.
[Document processing](https://docs.daft.ai/en/stable/examples/document-processing/),
[checkpoint guarantees and limits](https://docs.daft.ai/en/stable/use-case/checkpointing/).
Hugging Face Datasets caches using dataset/transform fingerprints and offers
iterator-position recovery; that is a different scope from publishing and
selecting a complete historical experiment result.
[Caching](https://huggingface.co/docs/datasets/about_cache),
[stream recovery](https://huggingface.co/docs/datasets/stream#save-a-dataset-checkpoint-and-resume-iteration).

## The replacement worth testing

Use a small fixed stage graph: **catalog → capture → extract → segment → chosen
processors → retained result**. Each stage consumes and produces batch tables or
small references to them. Native resources inject the existing fetcher,
extractor, segmenter and processors. Keep the leaf processing functions.

Store results and necessary provenance as rows. Keep native attempt timing,
retries and worker events in Dagster. Persist useful failure outcomes and source
associations with the dataset so independently retained results remain readable.
This changes the physical evidence unit from an individually published file for
each processor event toward an atomic stage or batch output.

The runtime already knows the requested inputs, processor and configuration.
Local processors can return their values and outcomes without restating those
facts in a second receipt. Record successful empty output explicitly so it stays
distinct from failure or missing work. Validate external inputs and new outputs
at their admission/publication boundaries; repeated hashes of runtime-owned
statements establish consistency, not proof that a processor executed correctly.

Candidate deletions include the custom execution backend, task-membership index,
handoff messages, DocumentStore execution revisions, inner retry/state loops and
the store sealing/delivery handshake. Update consumers together; a compatibility
wrapper around the old path would defeat the simplification. Preserve the checks
that establish actual selected population, exact captured content, source
coordinates, requested processor inputs and terminal outcomes.

Dagster data versions describe declared code/input versions and materializations;
they do not independently verify file bytes or retrieve an arbitrary old result.
DocSpec still needs a small explicit description of input versions, processor
configuration/resources and retained output locations.
[Asset versioning](https://docs.dagster.io/guides/build/assets/asset-versioning-and-caching).
Preserve the current computation keys in the first batching trial. Processors
currently see plan and invocation metadata; removing those fields from a key
while callbacks can still use them would make reuse unsound. Narrow the callback
inputs before independently testing a simpler computation key.

The central tradeoff is recovery granularity. Larger batches reduce scheduler
and file overhead, but a failed incomplete batch may repeat some processing.
Creating a partition for every segment trades that for many native events and
steps. Start with a few stable batches and record actual repeated calls. If the
candidate recreates the current task ledger to compensate, reject it.
Cheap deterministic work can repeat in an unfinished batch. Expensive external
calls need actual provider idempotency or per-input transactional result storage;
receipts alone cannot provide exactly-once execution.

First compare a small installed native-stage prototype against the existing
fixture oracle: capture now/process later, change one processor resource, add
one document, return from version A to B to A, resume after interruption, and
retain then repair an explicit failed input. Check complete values, source
associations and coordinates. Verify that reused changed bytes are refused at
the chosen admission boundary.

Measure wall time, memory, physical file count, durability calls and actual
fetch/extract/segment/processor calls. Report the extra work after a partial
batch failure. Require a net reduction in implementation and durable state,
with acceptable replay cost. These decision checks must pass before replacing
the main execution path or claiming better performance.

Separately test the direct Arrow-reader-to-DuckDB writer using the same retained
rows and resource allowances. Keep that small storage experiment independent of
the workflow comparison so its effect can be measured. A new database or lakehouse
format becomes justified only if it replaces an identified versioning or
publication responsibility with less total machinery.

The independent workflow, storage and validation/reuse reports are retained
locally under
`/Users/mikewolfd/Work/corpora/docspec-tooling-investigation-2026-09-12-95645bb`.
The workflow and validation reviews agree on testing native batch execution with
actual deletion of overlapping state. The storage review recommends the smaller
Arrow-stream experiment first. None approves an unmeasured wholesale migration.
