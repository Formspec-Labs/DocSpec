# DocSpec Bulk Content Processing Platform

## Implementation and conformance specification

**Editor's Draft — 5 August 2026**

## Status

This specification defines the work required to make DocSpec a standalone
platform for acquiring and processing large collections of files.

This Markdown file records intent. It does not prove implementation or
conformance. Current code and machine-generated evidence establish what exists.
Each requirement becomes complete only when its named executable check passes
against the exact code, configuration, inputs, and outputs under review.

## Abstract

DocSpec consumes a fixed source catalog or change set. `DocumentCatalog` opens an
optional prior `DocumentRelease`, which is one immutable snapshot of DocSpec's
corpus state. A planner divides the requested changes into bounded
`DocumentStore` jobs and emits small, serializable task references. A local
runner or an established scheduler such as Dagster executes those tasks. Workers
acquire files, preserve exact bytes, extract representations, create segments,
run injected processors, and deliver bulk results directly to an injected sink.
The execution tool streams terminal job references back to DocSpec.
`DocumentCatalog` reconciles the complete verified job set and commits the next
`DocumentRelease`.

DocSpec supports two workloads:

1. a bounded initial backfill that may contain millions of files, images, pages,
   or segments; and
2. incremental updates that acquire and process only additions, changes,
   deletions, repairs, or outputs invalidated by a changed processor.

The initial backfill is a one-time operation. Routine updates MUST NOT rebuild,
redownload, or republish the complete collection.

```text
SourceCatalog + DocumentRelease N
                 |
                 v
        DocumentCatalog.open(N) -> Planner -> saved DocumentStore jobs
                                                |
                                      small task/reference stream
                                                |
                                                v
                          local runner / Dagster / Ray / queue
                                                |
                       terminal StoreRefs + direct sink delivery
                                                |
                                                v
                               verify + reconcile planned job set
                                                |
                                                v
                              DocumentCatalog.commit(N, run receipt)
                                                |
                                                v
                                      DocumentRelease N+1
```

DocSpec is the document-state and evidence kernel in this flow. It does not
reimplement a scheduler, queue, cache service, object store, table format, or
analytics engine. It defines the small messages, immutable identities, evidence,
idempotency rules, receipts, and publication checks that let maintained packages
perform those jobs safely.

## 1. Outcome

A conforming DocSpec deployment answers four questions.

### 1.1 What goes in?

- A sealed source-catalog snapshot or change set.
- Stable source-item identifiers and file locators.
- An optional prior `DocumentRelease`.
- Extraction and segmentation policies.
- Zero or more injected processor definitions.
- A selected set of release-manifest, catalog, record, blob, job-persistence,
  and delivery profiles.
- Resource, retention, delivery, and publication policies.
- A sealed execution profile that selects a local runner or external execution
  tool and records its operational limits without changing document semantics.

### 1.2 What happens?

`DocumentCatalog` opens prior state. DocSpec plans and saves bounded
`DocumentStore` jobs. The selected execution tool consumes their small
references, while workers acquire exact bytes, derive representations, create
segments, execute processors, checkpoint verified stages, and deliver bulk
results directly to the selected sink. DocSpec accepts terminal references in
any order, rejects missing or conflicting results, reconciles the run, and
commits the next immutable catalog snapshot.

### 1.3 What comes out?

- Sealed `DocumentStore` job receipts.
- Content-addressed source and derived files when retention is requested.
- Partitioned record datasets for file, representation, segment, and processor
  records when the selected sink saves a dataset.
- A bounded result stream when the selected sink returns data.
- Complete dispositions for every selected source item.
- A `DocumentRelease` for every stateful run.
- Receipts that identify exact inputs, implementations, policies, counts,
  failures, resource use, and output digests.
- A bounded task and terminal-reference stream suitable for a scheduler, queue,
  or local caller.

### 1.4 How is it checked?

Independent verifiers check artifact membership, digests, schemas, evidence
coordinates, dispositions, incremental equivalence, recovery, and scale. A
successful command, clean working tree, or prose status statement does not
establish conformance.

## 2. Scope

### 2.1 Required capabilities

DocSpec MUST:

- read a source catalog without owning or changing it;
- open corpus state through `DocumentCatalog` at an explicit `DocumentRelease`;
- divide catalog entries into bounded `DocumentStore` jobs;
- acquire whole files, including images, PDFs, XML, HTML, JSON, and text;
- preserve exact source bytes before interpretation;
- deduplicate identical bytes without losing logical source membership;
- derive one or more representations through replaceable extractors;
- create deterministic, source-grounded segments;
- execute optional injected processors against files, representations, segments,
  or earlier derived outputs;
- isolate processor meaning and dependencies from the DocSpec core;
- save large outputs as partitioned datasets, stream bounded results back to a
  caller, or do both;
- hand saved jobs to a replaceable execution tool as small references and accept
  terminal results in any order;
- select physical release-manifest, catalog, record, blob, job-persistence, and
  delivery formats through replaceable profiles;
- resume work without repeating verified tasks;
- publish configured durable outputs atomically and acknowledge returned data;
- commit each stateful run as a new immutable `DocumentRelease`;
- perform incremental updates without recurring full dumps; and
- process millions of image or page units within a sealed scale profile.

### 2.2 Excluded capabilities

DocSpec does not define or reimplement:

- the meaning or lifecycle of an upstream catalog;
- the semantic meaning of tags, classifications, or other processor outputs;
- a mandatory parser, model, record format, table format, storage vendor,
  scheduler, or cloud;
- scheduler placement, worker pools, queues, triggers, or retry timing;
- a distributed cache, object-storage service, table engine, or analytical
  transformation engine;
- a query, ranking, search, or public-serving interface;
- legal or policy conclusions; or
- approval of a processor's output for a downstream use.

DocSpec executes declared processing and preserves evidence. The processor and
its consumer remain responsible for the output's meaning. A deployment SHOULD
use maintained packages for general infrastructure. Dagster, Ray, or a queue may
schedule work; Parquet or Arrow libraries may encode records; Iceberg, Delta, or
a database may manage tables; dbt may run an injected dataset transformation;
and Redis may provide a disposable acceleration cache. None becomes DocSpec's
source of truth.

## 3. Core concepts

**Source-catalog release**
: A sealed upstream snapshot or change set that identifies selected source
  items and candidate files.

**Source item**
: One logical catalog entry. A source item may name several files or renditions.

**Captured file**
: Exact acquired bytes, identified by a SHA-256 digest and a capture receipt.

**Representation**
: Content derived from a captured file by a named extractor and configuration.
  Examples include embedded text, optical character recognition text, page
  images, normalized HTML, or decoded JSON records.

**Segment**
: A deterministic unit derived from a file or representation. Examples include
  a page, image region, paragraph, section, table, frame, or record.

**Processor**
: An injected implementation that consumes declared DocSpec records and emits a
  named derived record set. A processor may tag, classify, redact, summarize,
  transform, embed, measure, or perform another bounded operation.

**Document entry**
: One source item within a job. It records file information, requested work,
  content and output references, stage outcomes, dispositions, and receipts.

**DocumentStore**
: A bounded job manifest and its evolving execution ledger. A planned store
  contains a fixed set of document entries and requested stages. A sealed store
  is the immutable receipt for that job. It references large bytes through
  locators; it is not the physical blob store.

**Store task**
: A small, serializable request that names a stable task identity, operation,
  saved `DocumentStore` reference, worker-composition profile, and optional sink
  reference. It never contains a source file or complete result set.

**Store task result**
: A small, serializable terminal message that names the task and its processed or
  sealed `DocumentStore` reference, or a persisted terminal failure reference.
  Bulk bytes and records travel through blob and result-sink adapters, not the
  execution tool's control messages.

**Execution profile**
: A sealed description of how one run is handed to a local runner or external
  tool. It identifies the adapter and configuration digest, worker-composition
  profile, concurrency and in-flight bounds, queue or pool, scratch limits,
  provider limits, cache state, and deadline. It governs operations, not logical
  document meaning.

**Result sink**
: An injected destination that accepts verified result records and returns a
  delivery receipt. A sink may persist a dataset, send a bounded stream to a
  caller, or combine both behaviors.

**DocumentCatalog**
: The DocSpec-owned interface for opening, querying, comparing, and advancing
  corpus state. Its state is always identified by a `DocumentRelease`; it is not
  a second corpus artifact.

**Storage profile**
: A versioned, machine-readable description of one physical implementation for
  catalog state, record datasets, blobs, saved jobs, result delivery, or release
  manifests. Profiles implement DocSpec interfaces; they do not change DocSpec's
  logical records.

**Layer release**
: One immutable dataset for files, representations, segments, dispositions, or
  a processor's output.

**DocumentRelease**
: One immutable `DocumentCatalog` snapshot. It pins the prior release, source
  catalog, active logical layers, selected storage profiles and their state
  references, blob roots, schemas, policies, sealed store receipts, counts,
  coverage, and integrity digests.

**Receipt**
: A closed machine record of one plan, `DocumentStore`, attempt, delivery, layer
  build, verification, or publication.

## 4. Architecture boundary

### 4.1 Dependency direction

DocSpec MUST use this dependency direction:

```text
commands -> application services -> DocSpec ports and domain records
                                      ^
                                      |
 source-catalog/document-catalog/blob-store/extractor/segmenter/processor/
 execution-tool/result-sink adapters
```

Application services and domain records MUST NOT import adapters or vendor
software. One composition root MUST select and inject concrete adapters.

### 4.2 Required ports

The core MUST define project-owned ports for:

- `SourceCatalogReader`;
- `DocumentCatalog`;
- `DocumentStoreRepository`;
- `BlobStore`;
- `RecordStorage`;
- `Extractor`;
- `Segmenter`;
- `Processor`;
- `ExecutionBackend`;
- `ResultSink`.

Ports MUST exchange DocSpec records. They MUST NOT expose a source catalog's
classes, storage-format or cloud-provider response types, a model provider's
types, or a batch engine's task objects.

### 4.3 Adapter rules

Adapters MAY depend on external packages. The core package MUST install and run
with local adapters and in-memory fakes only.

A source-catalog or document-catalog adapter MAY import its catalog client. A
storage adapter MAY import its storage client. A processor adapter MAY import
its model or transformation library. Those imports MUST remain inside the
registered adapter package.

The default implementation SHOULD adapt maintained packages instead of
duplicating them. Local JSON, SQLite, file, and threaded adapters MAY provide a
small portable reference and test fixture. A production adapter MAY delegate to
Dagster, Ray, a queue, S3 or R2, Parquet or Arrow, Iceberg or Delta, dbt, Redis,
or another maintained package. DocSpec MUST NOT copy those systems' scheduling,
locking, caching, table, or storage engines into its core.

Redis or another cache MAY accelerate leases, throttling, duplicate suppression,
or verified-result lookup. Cache loss, eviction, or stale data MUST NOT change a
logical output or publication decision. Durable object digests, stage receipts,
partition roots, and releases remain authoritative.

Tests MUST prove that each adapter can be replaced without changing application
services, `DocumentStore` schemas, or `DocumentRelease` schemas. A durable
dataset sink MAY compose `LayerWriter` components behind `ResultSink`;
`DocumentCatalog` alone commits the release.

### 4.4 Repository boundary

The installed package MUST contain only DocSpec capabilities. Vocabulary
management, search engines, ranking, query serving, and source-catalog
publication MUST remain outside the core package.

DocSpec MUST contain no copied production implementation from the pinned
SpicyRegs `origin/main` baseline. `BOUNDARY-CODE` MUST compare exact blobs and
normalized syntax-token fingerprints. Compatibility imports are allowed only in
registered adapters; copied predecessor code is not.

The repository MUST maintain a machine-readable inventory of every production
module, its capability, its owner, and its conformance test. Markdown ownership
notes are not authority.

## 5. Source catalog, document catalog, and planning

### 5.1 SourceCatalogReader

`SourceCatalogReader` MUST:

- open a snapshot or change set by expected identity and digest;
- verify the complete catalog distribution before yielding records;
- stream records in stable order;
- expose stable source-item identity;
- expose candidate locators, expected media types, versions, and digests when
  available;
- expose deletion or absence observations when available;
- report catalog counts, partitions, and coverage; and
- operate without reading mutable producer state.

DocSpec MUST accept a full snapshot, a change set, or both. A change set MUST
identify its base snapshot or prior change so that DocSpec can detect a missing
interval.

### 5.2 DocumentCatalog

`DocumentCatalog` MUST:

- open corpus state by exact `DocumentRelease` identity and digest;
- verify the release before exposing records;
- look up a document, version, file, representation, segment, derived record,
  store receipt, or disposition;
- scan active records and changes in stable order without loading the corpus;
- compare two releases by logical identity;
- stage verified outputs without making them current;
- commit sealed `DocumentStore` receipts against one expected base release;
- reject a stale base or conflicting commit;
- publish and return the new `DocumentRelease`; and
- reconstruct the same logical state from the committed release.

`DocumentCatalog` MUST NOT maintain corpus state that its `DocumentRelease`
cannot identify and reproduce. A mutable `current` pointer MAY help operators,
but consumers and jobs MUST use an explicit release identity.

In this model, `DocumentCatalog` corresponds to the versioned corpus and
`DocumentRelease` corresponds to one commit. The ordered release lineage is the
catalog's history.

### 5.3 ProcessingPlan

Every run MUST begin from a sealed `ProcessingPlan`. The plan owns choices that
can change logical document state or the interpretation of a result. It MUST
identify:

- the source-catalog input and optional prior `DocumentRelease`;
- selected source partitions or an exact selection rule;
- `DocumentStore` size and cost limits;
- document-catalog, record-storage, blob-storage, document-store persistence,
  result-delivery, and release-manifest profiles;
- extractor and segmenter policies;
- the ordered processor graph;
- retention and data-use policies;
- per-file, per-entry, per-stage, and per-job byte, memory, item, attempt, and
  duration safety limits that affect acceptance or output;
- failure classification, finite stage-attempt, and accepted-failure policies;
- logical partitioning policy; and
- result sink and delivery policy.

The plan identity MUST derive from canonical plan content. Changing a meaningful
field MUST create a new plan identity.

The plan MUST NOT encode scheduler worker placement, queue implementation,
cluster topology, or a cache product's native configuration. Those operational
choices belong to a separate sealed `ExecutionProfile`. The execution profile
MUST identify:

- the execution adapter and its version or deployment identity;
- a digest-pinned worker-composition profile that can reconstruct all adapters;
- worker, concurrency, and in-flight bounds;
- queue, pool, partition, or task-mapping configuration by identity and digest;
- scratch-disk, network, request-rate, and provider limits;
- retry timing owned by the execution tool;
- cache implementation and initial cache state, if any; and
- an absolute deadline.

The run receipt MUST pin the execution profile and the execution tool's returned
run or event-log reference. A `ScaleProfile` MUST pin it when operational
resources are part of a performance claim. Changing only an execution profile
does not invalidate document content. If an operational change also changes a
logical input, policy, accepted failure, or deterministic output, the
`ProcessingPlan` MUST change as well.

DocSpec MAY provide a local default execution profile. A deployment SHOULD let
Dagster, Ray, a queue, or another maintained tool own worker placement,
concurrency, triggers, and retry timing rather than reproducing those features in
DocSpec.

### 5.4 Change planning

For an initial backfill, the planner MUST enumerate the selected catalog once
and create bounded `DocumentStore` jobs. Each job identity MUST derive from the
plan, logical partition, ordered document-entry identities, and requested
stages.

For an update, the planner MUST compare the new source-catalog state with the
prior `DocumentRelease` opened through `DocumentCatalog`. It MUST classify each
item as:

- `added`;
- `changed`;
- `unchanged`;
- `deleted`;
- `repair`; or
- `excluded`.

The planner MUST schedule only work whose inputs or governing policy changed.
An unchanged item MUST reuse verified content and layer records by digest. The
planner MUST NOT place the full corpus into one `DocumentStore`.

## 6. Acquisition and exact files

### 6.1 Capture requirements

For each selected candidate, DocSpec MUST record:

- source-item and candidate identities;
- source locator;
- acquisition start and completion time;
- transport version metadata;
- downloader identity and configuration;
- declared and actual byte size;
- media type;
- SHA-256 digest;
- task and attempt identities;
- retry history; and
- final disposition.

The system MUST stream downloads through configured byte limits. It MUST NOT
load an unbounded file into memory.

### 6.2 Exact-byte preservation

DocSpec MUST preserve source bytes exactly throughout acquisition and
processing. Extraction, normalization, OCR, and redaction MUST create new
representations or derived objects; they MUST NOT replace the captured object.

The retention policy MAY discard source bytes after the `DocumentStore` seals.
In that case, the receipt MUST preserve the source locator, digest, size,
transport version, and `notRetained` disposition. A durable sink that claims
source-byte availability MUST retain the verified object.

Identical bytes MAY share one physical object. Every logical source item MUST
still appear in the document-entry ledger and, when used, the durable file
layer.

### 6.3 BlobStore

`BlobStore` MUST support:

- stat by immutable locator;
- bounded streaming reads;
- contained local materialization;
- put-if-absent while computing or checking SHA-256;
- range reads when supported;
- verification by size and digest; and
- immutable object identity.

The default object key SHOULD be:

```text
objects/sha256/<first-two-hex>/<64-lowercase-hex>
```

The reference implementation MUST provide local, Amazon S3, and S3-compatible
adapters. All adapters MUST pass the same behavioral suite.

### 6.4 Dispositions

Each selected item MUST end in exactly one acquisition disposition:

- `captured`;
- `unchanged`;
- `deleted`;
- `excluded`;
- `accepted-failure`; or
- `rejected-run`.

A missing disposition MUST stop the store from sealing. A deletion MUST remain
visible in the sealed store and any new durable release. It MUST NOT erase prior
immutable history.

## 7. Representation and segmentation

### 7.1 Extractors

An extractor MUST accept exact file bytes or an immutable object locator. It
MUST return representations, evidence mappings, warnings, and a receipt.

Each representation MUST identify:

- the source file digest;
- extractor name and version;
- configuration digest;
- media type and encoding;
- representation digest and byte size;
- page, frame, record, or region boundaries when available;
- warnings and fallback outcomes; and
- resource use.

A representation identity MUST change when its file, extractor, configuration,
preprocessing, model, or material fallback changes.

### 7.2 Supported content

The reference profile MUST process:

- standalone raster images;
- multi-page PDFs;
- XML;
- HTML;
- JSON; and
- plain text.

Additional formats MAY use registered extractor adapters.

For images, representations MAY include image metadata, optical character
recognition text, thumbnails, tiles, or detected regions. Each derivative MUST
retain its exact relationship to the source image.

For PDFs, DocSpec MUST retain the PDF and identify page boundaries. Optical
character recognition MUST create a named representation; it MUST NOT overwrite
embedded or publisher text.

### 7.3 Segmenters

A segmenter MUST accept one immutable file or representation and emit ordered,
bounded segments.

Every segment MUST identify:

- source-item, file, and representation identities;
- segmenter name, version, and policy digest;
- stable segment identity and ordinal;
- structural kind;
- segment content or object digest;
- half-open byte coordinates for text when available;
- page, frame, record, or image-region coordinates when available; and
- ordered derivation steps.

The same semantic inputs and policy MUST produce the same segment identities.

### 7.4 Evidence

Every segment MUST resolve to its exact representation and captured file.
Text coordinates MUST reproduce exact text. Image coordinates MUST identify the
source image or page and a closed coordinate system. Lossy derivations MUST name
the loss and preserve the preceding evidence link.

`EVIDENCE-ROUNDTRIP` MUST verify every fixture segment. Evidence failure MUST
stop the affected store from sealing or delivering the invalid segment.

## 8. Injected processors

### 8.1 Processor description

Each processor MUST publish a closed `ProcessorDescription` that identifies:

- processor name and version;
- accepted input record kinds and schemas;
- output schema and media types;
- configuration digest;
- external resources and model identities;
- determinism and cache policy;
- data-use policy;
- item-level limits;
- retry policy; and
- processor dependencies.

Processor dependencies MUST form an acyclic graph. The plan MUST pin the exact
graph before execution.

### 8.2 Execution boundary

A processor MAY consume captured files, representations, segments, or earlier
derived records. It MUST emit a named derived record set in the
`DocumentStore`. A durable sink MUST save each processor's records in a separate
derived layer.

DocSpec MUST invoke a processor with a closed, reference-based
`ProcessorRequest`. The request MUST name the pinned `ProcessorDescription`,
exact input record identities and digests, prerequisite processor outputs,
allowed fields from the data-use policy, item limits, and invocation identity.
The processor MUST return a closed `ProcessorResult` containing its disposition,
derived records, resource use, warnings, and provider or implementation receipt.
Application services MUST depend on these DocSpec-owned records and ports, not a
concrete processing package's result classes.

A processor MUST NOT:

- mutate a captured file, representation, segment, or prior derived layer;
- change base-layer eligibility or dispositions;
- fetch undeclared external resources;
- write directly to a published release root; or
- expose provider-specific types to DocSpec application services.

Running a processor in the DocSpec worker pool is an execution choice. It does
not make the processor's semantics part of DocSpec.

A processor adapter MAY delegate to a model SDK, OCR package, dbt invocation,
container, or another maintained transformation tool. DocSpec owns the input and
result records and verifies their declared limits; it does not reimplement that
tool's execution engine.

### 8.3 Processor output

Each output record MUST identify:

- the processor description;
- exact input records and digests;
- output schema;
- output value or object digest;
- provider or implementation receipt;
- warnings;
- review or acceptance state when the processor defines one; and
- final processor disposition.

Each scheduled item MUST end as `produced`, `abstained`, `excluded`,
`accepted-failure`, or `rejected-run`.

### 8.4 Reuse

A deterministic processor result MAY be reused only when all input identities,
processor versions, configurations, external resources, policies, and output
schemas match.

Exact-result reuse MUST be mediated by a `ProcessorResultCache` or stage-result
repository port. The durable cached value is an immutable verified result and
receipt. Redis or another cache MAY index or accelerate that lookup, but a cache
miss or outage MUST fall back to normal execution and MUST NOT change the
logical result or publication decision.

A processor that declares non-deterministic behavior MUST create a distinct run
identity. Its output MUST NOT replace an earlier derived layer in place.

Changing one processor MUST invalidate only that processor and processors that
depend on it. It MUST NOT reacquire unchanged files or rerun unaffected
extractors, segmenters, or processors.

## 9. DocumentStore and result delivery

### 9.1 DocumentStore role

One `DocumentStore` represents one bounded job. It MUST contain:

- store identity, schema version, revision, and state;
- plan identity and logical partition;
- a fixed ordered set of document entries;
- requested extractor, segmenter, and processor stages;
- stage inputs, outputs, dispositions, and warnings by entry;
- content and output locators rather than unbounded inline bytes;
- byte, entry, segment, memory, and time limits;
- attempt and delivery receipts; and
- reconciled counts and final verdict.

The allowed states are `planned`, `running`, and `sealed`. A state transition
MUST create a new store revision. A sealed store is immutable, carries a
`completed`, `accepted-failure`, or `rejected` verdict, and is the authoritative
receipt for that job. An interrupted store remains `running` and resumable; a
failure is a sealed outcome, not mutable runtime state.

A `DocumentStore` MAY include small inline metadata or results within its sealed
byte limit. It MUST reference source files, large representations, images, and
large processor outputs through immutable locators.

### 9.2 Ephemeral and saved stores

A small synchronous job MAY keep its active `DocumentStore` in memory. It MUST
still emit a sealed store receipt before it reports success.

A resumable or distributed job MUST save the planned store before execution and
save verified checkpoints through `DocumentStoreRepository`. A worker restart
MUST reconstruct the job from the saved store and referenced objects without
repeating verified stages.

Checkpoints MUST be granular enough to preserve a verified capture, extraction,
segmentation result, and processor invocation independently. Each checkpoint
MUST have a stable work identity derived from exact inputs and governing policy.
Concurrent or repeated attempts MAY settle the same work identity only when the
verified output is identical.

A saved store MUST use the selected `DocumentStorePersistenceProfile`. The
profile MUST support immutable revisions, bounded reads and writes, checkpoints,
sealed receipts, and independent verification. A store whose entry ledger
exceeds the configured inline limit MUST store the ledger separately through a
bounded record format. A `DocumentStore` MUST NOT become one unbounded object.

A portable profile MAY use a small UTF-8 JSON root with partitioned record
members. SQL, scheduler metadata, or another format MAY implement the same
interface when it preserves the same logical revisions and receipts.

The run-level receipt MUST list every planned store and its terminal store
revision. Missing, duplicate, or unsealed stores MUST fail the run.

### 9.3 ResultSink

`ResultSink` MUST accept a bounded stream of verified records and return a closed
`DeliveryReceipt`. The receipt MUST identify:

- result-sink implementation and configuration;
- source `DocumentStore` and entry population;
- accepted, rejected, retried, and undelivered record counts;
- saved object or stream acknowledgement identities;
- bytes delivered; and
- final verdict.

The receipt counts and entry population MUST be identity-bearing. A successful
receipt MUST account for every offered record exactly once. A parse-only or
structural inspection MUST identify its limited verification scope and MUST NOT
emit a complete `pass` verdict.

Every delivered record MUST carry an idempotency key derived from its source
store, entry, stage, and output identity. A retryable returned-result sink MUST
require the receiver to acknowledge that key. A receiver without idempotent
acknowledgement MAY be used only by an explicitly non-resumable ephemeral job.

DocSpec MUST provide three sink behaviors:

1. a durable dataset sink that publishes immutable objects and partitioned
   record datasets;
2. a returned-result sink that streams records to a caller with backpressure and
   acknowledgement; and
3. a hybrid sink that persists configured objects and returns configured
   records.

The returned-result sink MUST NOT aggregate a multi-million-record result in
memory. A lost or incomplete delivery MUST prevent the `DocumentStore` from
sealing as successful.

### 9.4 Durable dataset sink

The durable sink MUST stage separate immutable layers for:

1. file records and capture dispositions;
2. representations;
3. segments and evidence;
4. each injected processor's derived records;
5. failures and coverage; and
6. document-store, build, delivery, and verification receipts.

A `DocumentRelease` MUST identify the exact active state reference for each
saved layer under its selected profile. A processor-only update MUST be able to
commit a new derived layer and `DocumentRelease` without rebuilding base layers.

The sink stages data; `DocumentCatalog` verifies the staged outputs, reconciles
the sealed stores, and commits the release. A sink MUST NOT advance catalog
state directly.

### 9.5 Storage profile system

DocSpec's logical records and application services MUST remain independent of
physical formats. A deployment MUST select a compatible profile for each role:

- `ReleaseManifestProfile` for the small interoperable release root;
- `DocumentCatalogProfile` for catalog lookup, comparison, staging, and commit;
- `RecordStorageProfile` for large logical record layers;
- `BlobStorageProfile` for source bytes and large derivatives;
- `DocumentStorePersistenceProfile` for planned jobs, revisions, checkpoints,
  and sealed job receipts; and
- `ResultDeliveryProfile` for saved, returned, or hybrid delivery.

A profile MUST have a closed machine description containing its role, profile
identifier, version, implementation identity, configuration digest, supported
logical schemas, physical media types, capabilities, limits, compatibility
rules, and verifier identity. The composition root MUST inject the selected
adapters. Core modules MUST NOT import their client libraries or expose their
native objects.

Each stateful `ProcessingPlan` MUST pin the complete selected profile set. Its
`DocumentRelease` MUST pin the same profile identifiers and versions, plus the
opaque state reference and digest for each active logical layer. Changing a
profile or identity-bearing configuration creates a new plan and release; it
MUST NOT reinterpret an existing release in place.

Every conforming profile set MUST provide:

- bounded streaming reads and writes;
- stable logical identity and deterministic comparison;
- lookup and scan without loading the corpus;
- immutable snapshot or release identity;
- staged writes and one explicit activation point;
- incremental reuse and stable logical partitioning;
- pruning or indexed access suitable for the declared scale;
- closed schema and evolution rules;
- complete membership and integrity verification; and
- export to the canonical logical records used by conformance tests.

The reference implementation MUST provide at least one portable profile set and
at least one profile set that passes the scale class. One profile set MAY satisfy
both requirements. A profile SHOULD be a thin adapter over a maintained package
when one already implements the physical work. Manifest files, Apache Iceberg,
Delta Lake, or a database MAY implement catalog state. Parquet, Arrow IPC, JSON
Lines, or another bounded record format MAY implement record layers. Local
files, S3, R2, or another immutable object service MAY implement blob storage.

JSON Lines and local files are sufficient portable defaults. Parquet on
immutable object storage is the preferred first scale profile when columnar
scans and compression matter. Iceberg or Delta SHOULD be added only when a
deployment needs table transactions, multi-writer commits, long snapshot
history, or row-level table maintenance. DocSpec MUST NOT implement a new table
engine to satisfy this specification.

### 9.6 Canonical DocumentRelease manifest

All catalog profiles MUST read and publish the required
`docspec-release-manifest/1` profile as canonical UTF-8 JSON:

```text
format: docspec-document-release
formatVersion: 1.0
releaseId: urn:docspec:document-release:v1:<release-digest>
```

This small release root is DocSpec's sole required physical interchange format.
All catalog state, record layers, blobs, saved jobs, and delivery mechanisms
behind it remain profile-selected.

The identity-bearing content MUST include:

- the optional prior `DocumentRelease`;
- the source-catalog release;
- the processing plan and complete selected profile set;
- each profile's exact identifier, version, implementation identity,
  configuration digest, and declared capabilities;
- each active logical layer's identity, logical schema, profile role, opaque
  state reference, and digest;
- active blob roots and retention dispositions;
- the complete sealed `DocumentStore` receipt-set digest;
- the run and catalog-commit receipts;
- counts, failures, and coverage; and
- the partition policy.

The release MUST describe complete current logical state. It MAY reuse objects,
record files, manifests, database pages, or backing snapshots from prior
releases, but a consumer MUST NOT replay release history to discover current
membership.

The reconciler MUST verify every staged layer against its selected profile
before it conditionally publishes the release root. Workers MUST NOT activate
catalog state for individual documents or stores. If a profile updates several
tables, files, or services, those updates are staging data until the release
root publishes. Unpublished state MAY be collected only after its retention
period.

Release verification MUST cover every referenced active layer, sealed store,
profile state root, captured source object, retained representation object, and
retained segment object. A verifier MAY reuse a digest-bound verification
receipt or disposable verification cache. It MUST fail when an authoritative
object is absent or differs, regardless of cache availability.

### 9.7 Format-neutral layer requirements

The core MUST define layer membership, logical schemas, evidence, and identity
without requiring a physical record layout. Every `RecordStorageProfile` MUST
map its physical members to those logical records and prove bounded streaming,
partition pruning, closed-schema validation, incremental partition reuse, and
independent verification.

Every `BlobStorageProfile` MUST support immutable, content-addressed objects and
SHA-256 integrity. Every saved receipt and layer root MUST remain small and
bounded; a profile MUST move large membership or record lists into declared
members rather than one expanding root object.

An Iceberg profile MAY pin table and snapshot identifiers. A manifest profile
MAY pin immutable record members. A Parquet profile MAY store closed-schema
record layers. These are adapter choices, not DocSpec domain types.

### 9.8 Partitioning

The partition policy MUST assign stable logical buckets from source-item
identity. A catalog or source partition MAY provide an additional prefix.

Each saved layer root MUST list the complete active partition map. An
incremental run MUST reference unchanged partitions by digest and replace only
affected partitions. It MUST NOT rewrite every partition to publish a small
update.

Physical shard targets MUST be configurable. Each record profile MUST declare a
recommended target and a hard safety limit, and MUST reject a member above that
limit. The scale profile MUST record the active values.

Partition identity MUST include schema, partition policy, and ordered logical
content. Physical row-group layout MAY vary without changing logical records.

### 9.9 Artifact integrity and publication

Every saved document store, layer, and `DocumentRelease` MUST:

- use a closed, versioned root schema;
- derive its identity from canonical identity-bearing content;
- declare every member, role, media type, byte size, digest, record count, and
  schema;
- reject missing, extra, duplicate, escaped, or symlinked members;
- verify members before publishing the root;
- refuse to replace an existing root; and
- remain complete without reading an earlier root.

Writers MUST write new objects and partitions into an unpublished namespace.
`DocumentCatalog` MUST verify the complete output before publishing the
`DocumentRelease` root with a conditional create. Duplicate, stale, or
conflicting attempts MUST NOT publish.

Garbage collection MUST be a separate, explicit maintenance operation. It MUST
produce a dry-run inventory, honor minimum retention, and preserve every object
reachable from a retained store, profile state reference, or `DocumentRelease`
root.

## 10. Execution and recovery

### 10.1 Work state

The control plane MUST distinguish:

- plan;
- base `DocumentRelease` and opened catalog snapshot;
- logical partition;
- planned `DocumentStore`;
- active store revision;
- attempt;
- verified entry and stage result;
- delivery receipt;
- sealed store receipt;
- run receipt;
- layer build;
- staged document-catalog update;
- `DocumentRelease` verification; and
- release-root publication.

Backing-profile staging and commit steps apply only when the result sink
persists data.
Every stateful run commits a `DocumentRelease`, including a run that retains only
metadata and delivery receipts.
These units MAY coincide in a small local run. The system MUST keep their
identities distinct.

### 10.2 Bounded work

The planner MUST bound each `DocumentStore` by entry count, estimated bytes,
pages or frames, expected segments, processor cost, memory, and duration.
Document count alone is insufficient.

Acquisition, extraction, segmentation, processing, and writing MUST use bounded
queues. Slow stages MUST apply backpressure rather than grow an unbounded
in-memory backlog.

Those queues MAY be supplied by the selected execution tool, object-store
client, or sink library. DocSpec MUST declare and verify the active bounds and
MUST keep its messages small; it need not implement the queue itself.

No coordinator operation may require all source items, files, segments, or
document stores in one in-memory object graph. It MAY keep bounded active-store
summaries and a partition-level run ledger.

### 10.3 Retry and resume

The control plane MUST classify failures as:

- transient external failure;
- transient resource failure;
- deterministic input failure;
- policy exclusion;
- artifact-integrity failure; or
- implementation defect.

Retries MUST have finite limits and bounded backoff. The `ProcessingPlan` owns
which failures may be retried and the maximum semantic stage attempts. The
execution tool MAY own task retry timing and worker replacement under the sealed
`ExecutionProfile`. A resumed run MUST verify a completed stage, entry, or store
checkpoint before reuse. Stale attempts and duplicate delivery MUST remain
idempotent.

The sealed store and run receipt MUST preserve terminal failures and accepted
exclusions. A durable sink MUST also preserve them in its release.

### 10.4 Execution backends

DocSpec MUST expose these scheduler-neutral application functions:

```text
plan_run(source_catalog_ref, base_document_release_ref, plan_ref)
    -> stream[planned_document_store_ref]
execute_store(planned_document_store_ref) -> processed_document_store_ref
deliver_store(processed_document_store_ref, sink_ref) -> sealed_document_store_ref
reconcile_run(stream[sealed_document_store_ref]) -> run_receipt_ref
commit_release(base_document_release_ref, run_receipt_ref) -> document_release_ref
```

Each argument and result MUST be a small, serializable value or immutable
reference. Scheduler messages MUST NOT contain source files, complete result
sets, provider clients, open streams, or framework-specific objects.

`plan_run` MUST save the planned jobs before returning their references. A
scheduler adapter MAY wrap each reference in a `StoreTask`. The complete handoff
MUST also have a small sealed root that identifies:

- the `ProcessingPlan` and `ExecutionProfile`;
- the planned-store ledger and exact expected task count;
- the worker-composition profile;
- the result sink;
- the base `DocumentRelease`, if any; and
- the task and result schema versions.

The handoff root MUST reference a bounded, streamable member when the job list is
large. It MUST NOT inline millions of task objects.

The same functions and task semantics MUST run through:

- a local execution backend; and
- one maintained external scheduler adapter.

Backend selection MUST NOT change logical output. Each backend MUST accept the
same planned `DocumentStore` and return the same sealed store shape. Worker loss,
coordinator restart, duplicate delivery, and slow storage MUST affect only
incomplete work.

An execution backend MUST accept a stream of `StoreTask` values and return a
stream of `StoreTaskResult` values. Results MAY arrive out of order and MAY be
replayed. DocSpec MUST match them to the sealed planned-store ledger, verify the
referenced store revision, and deduplicate an identical replay. An unknown task,
conflicting duplicate, missing terminal task, or unsealed result MUST fail run
reconciliation.

Workers MUST send source bytes, representations, segments, and large derived
records directly to the injected blob store or result sink. The scheduler or
queue carries only task references, terminal references, progress, and bounded
diagnostics. A returned-result sink MAY stream bounded records through the same
caller connection, but those records remain a sink stream with acknowledgement;
they are not scheduler task metadata.

A purely ephemeral invocation MAY stop after returning its sealed stores and run
receipt. Such an invocation is stateless: it MUST NOT advance `DocumentCatalog`
and MUST NOT serve as the base of an incremental run.

### 10.5 Scheduler portability

An external scheduler such as Dagster MAY map one `DocumentStore` reference to
one operation, dynamic task, or partition. The scheduler owns worker placement,
triggers, queues, concurrency, task retry timing, event storage, and monitoring.
DocSpec owns task identity, idempotency, checkpoint verification, dispositions,
delivery receipts, expected-task reconciliation, and release publication.

Adapters MUST NOT require DocSpec domain code to import the scheduler. A Dagster
job, queue consumer, Ray task, or local loop MUST call the same application
functions. A scheduler adapter SHOULD translate DocSpec task records to the
tool's native operations and translate its terminal events back; it MUST NOT
reimplement the tool's scheduler inside DocSpec.

`SCHEDULER-PORTABILITY` MUST execute one shared fixture graph through the local
backend and either Dagster or another maintained scheduler. Both runs MUST
produce equivalent sealed stores and run receipts. The test MUST also prove that
tasks and results survive serialization, out-of-order results reconcile, and
replaying one scheduled store does not duplicate saved output or returned data.

An adapter that calls an external scheduler's in-process test helper proves only
the adapter's local composition. External-scheduler conformance requires the
same serialized handoff to cross a real process boundary and be reconstructed
from pinned profiles without a captured Python closure.

## 11. Initial backfill and incremental operation

### 11.1 Initial backfill

The initial backfill MUST:

- seal the complete source-catalog input before work begins;
- partition work into bounded `DocumentStore` jobs before material execution;
- save each planned store before distributed execution;
- checkpoint verified entry stages and store revisions;
- seal every store and one reconciled run receipt;
- reconcile every selected item; and
- commit the initial `DocumentRelease` through `DocumentCatalog`.

The backfill MAY take hours or days under its approved profile. It MUST not
become a recurring requirement for normal updates.

### 11.2 Incremental update

An incremental update MUST:

- open and verify the prior `DocumentRelease` through `DocumentCatalog`;
- consume a new snapshot or a complete change sequence;
- identify changed source items and invalidated descendants;
- create stores only for changed or explicitly repaired work;
- reuse unchanged objects, records, and partitions by digest;
- rebuild only affected logical buckets;
- record additions, changes, deletions, repairs, and exclusions; and
- seal a new run receipt and commit a successor `DocumentRelease` with a complete
  active catalog state.

`INCREMENTAL-EQUIVALENCE` MUST prove that the `DocumentCatalog` records opened
from an incremental `DocumentRelease` equal a clean build over the same current
source catalog and policies. Physical layout, backing snapshots, and reused
object locations may differ.

### 11.3 Targeted reprocessing

An operator MUST be able to create targeted `DocumentStore` jobs for one
extractor, segmenter, processor, failed population, source partition, or logical
bucket without reacquiring unrelated files.

The resulting run receipt MUST state the exact selected population. A stateful
rerun MUST commit a `DocumentRelease` that preserves complete catalog state by
referencing unchanged partitions or backing state references.

### 11.4 Compaction

For a durable sink, compaction MAY combine small physical shards. It MUST commit
new backing state and a new `DocumentRelease`, preserve all active logical
records and evidence, and avoid source reacquisition or semantic reprocessing.

## 12. Operator interface

One `docspec` command MUST expose the lifecycle:

```text
docspec source-catalog verify
docspec profile list
docspec profile verify
docspec document-catalog open
docspec document-catalog compare
docspec plan create
docspec document-store create
docspec document-store verify
docspec run prepare
docspec run start
docspec run resume
docspec run reconcile
docspec run status
docspec task execute
docspec sink verify
docspec document-release commit
docspec document-release verify
docspec document-release diff
docspec document-release compact
docspec blob-store verify
docspec blob-store gc --dry-run
docspec conformance run
docspec conformance report
```

Every mutating command MUST accept an explicit destination, refuse replacement
by default, and emit a machine receipt. Read-only commands MUST make no external
state change.

Commands MUST use explicit files, immutable locators, or released packages.
They MUST NOT select sibling worktrees or mutable producer state implicitly.

`run prepare` MUST save the bounded jobs and emit the sealed execution-handoff
reference without executing them. `task execute` MUST accept one serialized task
and emit one serialized task result, so an external tool can use it as a worker
entry point. `run reconcile` MUST consume a bounded result stream or saved result
ledger and verify it against the planned-store ledger. `run start` MAY compose
those operations as a local convenience. None of these commands may print bulk
file or record payloads as scheduler metadata.

## 13. Scale profile

### 13.1 Sealed profile

Before a scale run, DocSpec MUST seal a `ScaleProfile` that identifies:

- exact corpus or deterministic generator;
- real input-shape sample and sampling method;
- file, image, page, byte, representation, and segment distributions;
- extractor, segmenter, and processor graph;
- worker and coordinator resources;
- document-store sizing policy and result sink;
- selected storage profile set, document-catalog adapter, and base release;
- storage and network placement;
- cache state;
- partition and task policy;
- wall-time and resource targets; and
- acceptance authority.

The `ScaleProfile` MUST pin the `ProcessingPlan`, `ExecutionProfile`, execution
tool's configuration digest, and exact storage and sink profiles. The execution
tool and storage packages MAY supply scheduling, queue, cache, network, and
resource measurements. DocSpec MUST preserve their evidence references and the
DocSpec task, store, byte, and release counts needed to verify the claim.

A changed corpus, processor graph, resource allocation, or target creates a new
profile.

### 13.2 Ordered campaigns

The reference implementation MUST pass these campaigns in order:

1. 100,000 representative image or page units;
2. 1,000,000 representative image or page units; and
3. at least 5,000,000 representative image or page units.

Samples MUST cover byte-size, page-count, segment-count, media-type, and
observed-cost ranges. A convenient prefix is not representative.

### 13.3 Base-platform targets

With source bytes colocated with the processing store, the five-million-unit
base campaign SHOULD complete within 24 hours on no more than 256 effective
worker CPUs. The profile MUST set an absolute deadline before execution.

The base campaign includes file verification, reference extraction,
segmentation, partition writing, document-catalog commit, and release
verification. Source-limited download time MUST be measured separately.

Injected processors have independent throughput targets because their cost may
range from a local calculation to model inference. Each processor required by a
scale campaign MUST declare its own deadline, concurrency, provider limits, and
cost estimate before the run.

Scale conformance measures the composed deployment; it does not require DocSpec
to provide its own distributed scheduler, cache server, object store, or table
engine. A campaign MAY use Dagster for scheduling, Redis for disposable
coordination or lookup acceleration, an object store for bytes, and a maintained
Parquet or Iceberg implementation for records, provided all selected adapters
meet DocSpec's identity, boundedness, and verification rules.

The reference profile has these hard bounds:

| Measure | Bound |
| --- | --- |
| Worker peak resident memory | 8 GiB or less |
| Coordinator peak resident memory | 16 GiB or less |
| Coordinator item growth | Bounded by active stores, workers, and partition count, not corpus size |
| Unexplained item loss | Zero |
| Unsealed store reported as successful | Zero |
| Worker-initiated catalog activation | Zero |
| Partial root publication | Zero |
| Reacquisition during processor-only rerun | Zero unchanged files |
| Reprocessing during a metadata-only update | Zero unchanged content |

### 13.4 Incremental target

After the initial backfill, an update of at most 10,000 changed source items and
10 GiB of changed bytes SHOULD complete delivery within 30 minutes on the
reference hardware, excluding source-imposed delay and processor profiles with
longer sealed deadlines.

The update MUST report bytes and records reused, rewritten, and newly produced.
It MUST prove that unaffected partitions were referenced rather than rewritten.

### 13.5 Prediction and recovery

The million-unit campaign MUST predict full-campaign worker-hours, wall time,
final storage, and temporary storage. The full campaign MUST land within 25% for
worker-hours, 30% for wall time, and 20% for each storage measure.

Before the million-unit campaign, a two-worker-node test MUST prove safe recovery
from worker loss, coordinator restart, duplicate delivery, slow storage, full
local disk, oversized input, and one deterministic processor failure.

## 14. Security and policy

Readers MUST reject path traversal, symlink escape, unsafe archive members,
decompression beyond configured limits, and objects above the active byte limit.

Logs and receipts MUST exclude credentials and provider secrets. Store profiles
MUST declare access, encryption, region, retention, and redistribution rules.
Unknown policy MUST stop publication.

An external processor MUST receive only fields allowed by the sealed data-use
policy. The system MUST record or digest its requests and responses as that
policy requires. Failure MUST NOT trigger an undeclared provider or processor.

## 15. Machine evidence and conformance

### 15.1 Evidence report

Each conformance run MUST emit a closed JSON report containing:

- specification version;
- conformance class;
- source revision or source-tree digest;
- dependency-lock digest;
- exact input and output identities and digests;
- plan and configuration identity;
- command and environment identity;
- required test identifiers and verdicts;
- document-store counts, dispositions, retries, and failures;
- bytes read, reused, written, delivered, and published;
- wall time and peak memory when applicable;
- verifier identity and version;
- first registered failure code; and
- overall `pass` or `fail`.

A required test that is absent, skipped, or xfailed MUST fail the class.

### 15.2 Required tests

| Test ID | Required proof |
| --- | --- |
| `CORE-INSTALL` | A built wheel installs, imports, and shows help in an empty environment |
| `BOUNDARY-IMPORT` | Core code depends only on DocSpec ports and domain records; vendor and concrete processing types remain in adapters |
| `BOUNDARY-CODE` | Production code contains no copied predecessor implementation |
| `SOURCE-CATALOG-CONTRACT` | Full snapshot and change-set readers pass shared valid and invalid fixtures |
| `PROFILE-DESCRIPTION` | Every selected profile has a closed, versioned, digest-pinned description with declared capabilities and limits |
| `RELEASE-MANIFEST` | Every catalog profile reads and publishes the canonical release root and rejects unknown or incomplete roots |
| `DOCUMENT-CATALOG-CONTRACT` | Every registered catalog profile opens, compares, stages, and commits the same logical release fixtures |
| `RECORD-STORAGE-CONTRACT` | Every registered record profile streams, partitions, prunes, reuses, and verifies the same logical layer fixtures |
| `PROFILE-COMPATIBILITY` | Each supported profile set composes successfully or fails before work begins with a registered incompatibility |
| `DOCUMENT-STORE` | Planned, stage-checkpointed, ephemeral, rejected, accepted-failure, and completed store fixtures reconcile with complete resource and disposition counts |
| `BLOB-STORE-CONTRACT` | Local, S3, and S3-compatible blob stores pass one immutable-object suite |
| `ACQUISITION` | Capture, deduplication, deletion, retry, byte-limit, and disposition paths reconcile |
| `REPRESENTATION` | Supported content produces identified, receipted representations |
| `SEGMENTATION` | Deterministic segments and coverage pass for every supported content type |
| `EVIDENCE-ROUNDTRIP` | Every fixture segment resolves to its representation and exact source file |
| `PROCESSOR-CONTRACT` | Fake and real processor adapters pass reference-based input, dependency-output, limit, cache-outage, retry, data-use, output, and provenance tests |
| `RESULT-SINK` | Durable, returned-result, and hybrid sinks pass complete receipt, delivery, replay, acknowledgement, and backpressure tests |
| `SCHEDULER-PORTABILITY` | The same serialized task handoff crosses local and external process boundaries and produces equivalent sealed stores and run receipts |
| `DOCUMENT-RELEASE-INTEGRITY` | Layer and DocumentRelease fixtures verify every retained authoritative object and reject invalid snapshots or missing source bytes |
| `INCREMENTAL-EQUIVALENCE` | Initial, incremental, targeted, and compacted active states reconcile |
| `RECOVERY` | Interruption and duplicate-delivery scenarios reuse verified work safely |
| `SCALE` | Ordered campaigns satisfy the sealed profile |
| `PACKAGE-RELEASE` | Published package and fixtures install and verify by version and digest |

Each registered optional profile adds its own required test. For example, an
Iceberg catalog profile MUST pass `ICEBERG-DOCUMENT-CATALOG`, including staged
files, batched commits, pinned snapshots, conflicts, and abandoned-staging
reconciliation. An unregistered optional profile does not add that test to the
core conformance class.

### 15.3 Negative fixtures

Artifact fixtures MUST include:

- unknown format or schema version;
- changed identity-bearing content under an existing identity;
- missing and extra members;
- size or digest mismatch;
- duplicate logical identity;
- invalid foreign key or evidence coordinate;
- incomplete disposition population;
- path escape or symlink;
- unsupported extractor, segmenter, processor, or policy identity;
- unknown, unpinned, capability-mismatched, or digest-mismatched profile;
- unpinned, missing, or mismatched backing state reference;
- stale base release or conflicting catalog commit;
- missing or unsealed document-store receipt;
- missing, unknown, duplicate, conflicting, or unsealed execution task result;
- missing retained captured, representation, or segment object;
- unknown execution profile, worker-composition profile, or policy identity;
- broken change-set ancestry; and
- attempted in-place replacement.

Producer and independent verifier tests MUST share exact fixture bytes, not a
fixture generator.

## 16. Implementation sequence

Each step MUST leave DocSpec installable and independently testable.

Before adding infrastructure, the implementer MUST inspect maintained packages
and the read-only predecessor archive for direct reuse or a proven algorithm.
Prefer a thin adapter when a maintained package supplies most of the required
behavior. The archive may supply tests, invariants, fixture bytes, and algorithm
ideas; its predecessor schemas, product identities, sibling-package coupling,
and production modules MUST remain archived.

DocSpec MUST NOT create a homegrown scheduler, distributed queue, cache server,
object store, table engine, or analytics engine as part of this sequence.

### 16.1 Keep the standalone kernel independently verifiable

- Maintain the production-module inventory, archive fingerprint, clean package
  boundary, and sibling-free installation checks.
- Make application services depend only on DocSpec domain records and ports;
  move concrete processing result and verification implementations behind those
  surfaces.
- Build shared sealed fixture distributions for catalogs, files,
  representations, segments, processors, stores, sinks, and releases.
- Run producers and independent verifiers against the same exact fixture bytes.
- Keep the predecessor archive read-only and verify its complete inventory.

### 16.2 Complete integrity, receipts, and policy records

- Make `DocumentRelease` verification resolve and verify every retained source,
  representation, segment, active layer, sealed store, and profile-state object.
- Add complete identity-bearing delivery population, accepted, rejected,
  retried, undelivered, byte, resource-use, and final-verdict fields.
- Complete capture retry history and retention disposition; representation
  encoding and resource use; segment derivation identity; and derived-record
  processor-description and exact-input digests.
- Replace open retention and data-use dictionaries with closed, versioned policy
  records. Make profiles declare access, encryption, region, retention, and
  redistribution policy identities.
- Add credential and secret redaction tests for errors, logs, and receipts.
- Make every verification command state its exact scope. Only complete composed
  verification may emit a complete `pass` verdict.

### 16.3 Make stage execution reusable and processor-neutral

- Persist independently verifiable capture, extraction, segmentation, and
  processor-invocation checkpoints under exact work identities.
- Resume at the first incomplete stage and verify every reused output.
- Introduce closed, reference-based `ProcessorRequest` and `ProcessorResult`
  records that support declared file, representation, segment, and dependency
  outputs.
- Enforce processor item limits, retry classification, dependency order,
  dispositions, and data-use field filtering in DocSpec application services.
- Add a `ProcessorResultCache` or stage-result repository port. Provide a small
  local reference implementation; keep Redis or another shared cache optional,
  disposable, and non-authoritative.
- Adapt real processing libraries behind `Processor`; do not reproduce their
  engines.

### 16.4 Implement the portable job handoff

- Add closed `ExecutionProfile`, execution-handoff, `StoreTask`, and
  `StoreTaskResult` records with canonical identities and strict size limits.
- Make `run prepare` save all planned stores and emit a small handoff root plus a
  bounded task ledger.
- Make `task execute` reconstruct its worker from a pinned composition profile,
  execute and deliver one store idempotently, and return only a terminal
  reference or persisted failure reference.
- Make `run reconcile` stream results, accept out-of-order completion, deduplicate
  identical replay, and reject missing, unknown, conflicting, or unsealed tasks.
- Make the local runner use the same serialized tasks and results.
- Implement one thin maintained-scheduler adapter, preferably Dagster, using its
  native dynamic mapping, resources, retries, event log, and process launcher.
  Do not close over an arbitrary local callable or implement scheduling inside
  DocSpec.
- Pass `SCHEDULER-PORTABILITY` across a real process boundary.

### 16.5 Compose scalable storage and reconciliation profiles

- Retain local JSON Lines, SQLite, files, and threads as portable reference
  adapters and bounded test defaults.
- Add a Parquet `RecordStorageProfile` through a maintained Arrow/Parquet
  library as the first production-oriented record profile when required by a
  deployment.
- Add Iceberg, Delta, or database catalog profiles only when their transaction
  and history capabilities are needed; adapt their libraries instead of
  rebuilding them.
- Keep S3, R2, and compatible object services behind `BlobStore` adapters.
- Bound durable sink memory independently of store size by streaming or using
  the selected record writer's bounded batches.
- Reconcile partitions independently and finalize from small sealed partition
  receipts so an initial build is not one corpus-sized coordinator task.
- Use digest-bound verified-reader and partition-verification receipts to avoid
  repeated corpus-wide scans during one commit. A disposable verification cache
  MAY accelerate this path but cannot authorize publication.

### 16.6 Complete incremental and maintenance operations

- Enumerate a source snapshot once per plan and use verified reader handles to
  avoid redundant complete scans.
- Prove a clean build and an incremental build over the same state expose the
  same logical records.
- Support targeted extractor, segmenter, processor, failed-population, source-
  partition, and logical-bucket runs without reacquiring unrelated content.
- Implement format-neutral compaction behind `RecordStorage` and
  `DocumentCatalog`, preserving logical state and committing a successor
  release.
- Build garbage-collection retention sets from verified reachability across
  retained stores, profile state, and releases before reporting collectable
  objects.
- Pass `INCREMENTAL-EQUIVALENCE` for initial, incremental, targeted, and
  compacted state.

### 16.7 Finish the independent product surface

- Expose prepare, task execution, reconciliation, commit, inspection, and
  conformance through one `docspec` command and the same application services.
- Enable clean installation, continuous integration, package builds, and sealed
  fixture publication.
- Publish the package, profile descriptions, and fixture releases by version and
  digest when a release destination is selected.
- Keep conformance fail-closed: an absent, skipped, partial, or fixture-free
  required check does not pass.

### 16.8 Qualify the composed deployment

- Seal the exact processing, execution, storage, sink, and scale profiles.
- Run local and maintained-scheduler recovery campaigns, including worker loss,
  coordinator restart, duplicate result delivery, slow storage, full scratch
  disk, and deterministic processor failure.
- Run the ordered representative scale campaigns and record the execution
  tool's event evidence with DocSpec's task, byte, partition, store, and release
  evidence.
- Publish machine-readable conformance reports. Keep unrun external campaigns
  distinct from locally verified implementation.

## 17. Completion decision

DocSpec is complete only when:

- it consumes a sealed source catalog through a replaceable reader;
- `DocumentCatalog` opens, compares, and advances corpus state only through
  explicit `DocumentRelease` identities;
- each stateful run commits one immutable `DocumentRelease`;
- physical storage remains behind pinned profiles and never changes DocSpec's
  logical records;
- at least one portable profile set and one scale-qualified profile set produce
  equivalent logical releases;
- it divides catalog entries into bounded `DocumentStore` jobs;
- it emits those jobs as a sealed bounded task ledger, lets a replaceable local
  or external tool consume them, and reconciles streamed terminal references;
- every successful job has a sealed, independently verifiable store receipt;
- it preserves and verifies exact whole files and every retained derived object;
- it derives representations and source-grounded segments;
- it executes injected processors without owning their meaning;
- an injected sink can save immutable partitioned datasets, return bounded
  result streams, or do both;
- it updates only changed data and invalidated descendants;
- it resumes without repeating verified work;
- scheduler, queue, cache, object-store, record-format, and table-engine
  capabilities remain delegated behind DocSpec ports and profiles;
- it installs and runs without sibling worktrees;
- its local and external-scheduler backends produce equivalent logical output;
- its package, fixtures, and conformance reports are independently verifiable;
  and
- the ordered scale and recovery campaigns pass.

No Markdown status statement may substitute for these results.

## Appendix A. Suggested package layout

```text
src/docspec/domain/
src/docspec/ports/
src/docspec/application/
src/docspec/profiles/
src/docspec/adapters/source_catalogs/
src/docspec/adapters/document_catalogs/
src/docspec/adapters/record_storage/
src/docspec/adapters/stores/
src/docspec/adapters/extractors/
src/docspec/adapters/segmenters/
src/docspec/adapters/processors/
src/docspec/adapters/execution/
src/docspec/adapters/sinks/
src/docspec/artifacts/
src/docspec/conformance/
```

This layout is informative. Import-direction and ownership checks govern the
installed package.

## Appendix B. Required machine files

```text
ownership/modules.json
conformance/specification.json
conformance/test-matrix.json
conformance/scale-profile.schema.json
profiles/
fixtures/execution-handoffs/
fixtures/source-catalogs/
fixtures/storage-profiles/
fixtures/document-catalogs/
fixtures/document-stores/
fixtures/files/
fixtures/representations/
fixtures/segments/
fixtures/processors/
fixtures/sinks/
fixtures/document-releases/
```

The JSON files and executable validators govern their own shapes. This appendix
does not authorize empty placeholder files.
