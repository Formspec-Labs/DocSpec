# Core spec and implementation plan compared with the current code

Compared on 2026-09-13 against checkout `3ffff83` and the working tree, including
its uncommitted Parquet/Arrow changes. This is an implementation assessment, not
a Core conformance certificate or a change to the spec or plan.

**Verdict: retain the tested document-processing capabilities and storage
mechanisms, and introduce the general Core model as a substantial semantic
addition.** The current implementation has useful retention, recovery, exact
input reuse, immutable results, and guarded current selection. Its public model
still centers on source documents, extraction, segmentation, and processors.
Changing dependencies or renaming those records will not implement the new model.

The saved plan now selects DuckDB and `graphlib`/recursive SQL. This comparison
uses that version, rather than the earlier open engine decision.

## Scope and decision lineage

| Artifact | Authority and relationship |
| --- | --- |
| [Core model](../core-model.md) | Target semantics: §§1–8 Core; §9 optional Keyed-State Profile. |
| [Implementation plan](../core-model-implementation-plan.md) | Chosen implementation and added capabilities, including generic revision/selection records, the SQLite ledger, and packed content. |
| [Current architecture](../architecture.md) and [record storage](../record-storage.md) | Describe the existing document product and its storage boundaries; checked against source below. |
| [Public runtime](../../src/docspec/runtime/__init__.py#L41) | Current consumer boundary: prepare a document-processing plan, execute/recover, inspect, retain, and export. |
| [Contribution guidance](../../CONTRIBUTING.md) | Preserve the runtime → application → ports/domain dependency direction and deliberate adapter ownership. |

Core does not mandate SQLite, msgspec, a PROV exporter, broad reuse, or support for
every possible operation. Differences from those plan choices are implementation
gaps, not automatically standards violations. Likewise, a library named `prov`
is not necessary for conformance: the required interpretation can be recovered
from ordinary records. That interpretation still needs to be specified and checked.

## Semantic comparison

| Area | Current implementation and evidence | Comparison and required work |
| --- | --- | --- |
| Root values and occurrences — Core §3.1; plan §3.1 | `SourceCatalogItem` has a fixed document schema; `SourceItem` requires an acquisition-oriented shape. [Catalog row](../../src/docspec/domain/source_catalog.py#L257), [processing item](../../src/docspec/domain/content.py#L192). | Add general root values and separate occurrence identity from member keys and value equality. Keep source-document rows as a supported application of that model. |
| Complete states and revisions — Core §§3.2–3.3; plan §3.1 | Immutable Parquet layers preserve full record JSON and reuse untouched partition references. Input and read order is record-identity order. [Layer writer](../../src/docspec/adapters/storage/records.py#L336), [reader](../../src/docspec/adapters/storage/records.py#L248). | Reuse the physical layer mechanism. Add occurrence membership, meaningful order, partial value edits, ordered membership edits, conflict rules, and recovery from those edits. Partition replacement is not the new state resolver. |
| Operations and results — Core §4; plan §2 | Versioned processor descriptions, requests, result references, retry receipts, and stored outputs exist. Processing remains tied to extraction and segmentation. [Stage requirements](../../src/docspec/domain/plans.py#L69), [request/result records](../../src/docspec/domain/processors.py#L401), [attempts](../../src/docspec/application/processor_runtime.py#L190). | Generalize definitions and execution/input/result bindings, including contextual raw/derived roles and new/adopted entities. Preserve the existing attempt and outcome evidence. |
| Selected-value dependencies — Core §6; plan §3.2 | Acquisition compares a fixed subset of source fields, while processor requests bind exact segment records and an allowed-field list. [Acquisition comparison](../../src/docspec/domain/content.py#L214), [request construction](../../src/docspec/application/processor_rules.py#L64). | No general retained selector definition/evaluator was found. Add type/presence semantics, composites, state structure, parent recovery, and records of material dependency omissions. Existing field filtering is useful behavior to reproduce, not the complete new mechanism. |
| Correspondence and reuse — Core §7; plan §6 | Exact reuse works across plan identities; a cache hit records the new request and exact existing result. Cache lookup is enabled for deterministic processors and retains one winner per key. [Selection receipt](../../src/docspec/application/processor_runtime.py#L125), [eligibility restriction](../../src/docspec/application/processor_runtime.py#L156), [cache schema](../../src/docspec/adapters/processor_cache.py#L25). | Preserve request/result associations and original provenance. Add the planned multi-result candidate index, selected-value correspondence, independent freshness/availability policy, and an explicit fresh-execution path. Conservative current caching is not itself a Core violation. |
| Retention and recovery — Core §5; plan §4 | Release verification audits records, receipts, and blobs; staged publication preserves retained alternatives independently of current selection. Restart checks reject damaged evidence. [Audit](../../src/docspec/application/commit.py#L225), [retention/publication](../../src/docspec/adapters/storage/catalog.py#L394). | This is the strongest reusable area. Generalize successful-retention checks to the new whole-value/state/selected-value bindings and implement the plan's transactional publication units. Existing release publication is file/manifest based, not the proposed SQLite ledger. |
| Current selection and retention policy — Core §5.6; plan §§2, 4 | Expected-current checks, immutable alternatives, explicit retention rules, and reachability construction exist. [Selection](../../src/docspec/adapters/storage/catalog.py#L459), [policy](../../src/docspec/domain/policies.py#L51), [reachability](../../src/docspec/application/maintenance.py#L117). | Carry these behaviors into the new metadata owner. The planned generic `remove_under_policy` operation and per-removal records still need implementation; an inventory preview is not a removal operation. |
| Physical compaction — Core §8; plan §3.1 | Compaction checks logical-content equivalence, then creates and selects a maintenance successor release. [Compaction](../../src/docspec/application/maintenance.py#L473). | Keep the equivalence and recovery checks. Separate logical state identity from physical publication identity so plan-style checkpoints can change storage without creating another logical state. The existing successor model is a plan mismatch, not by itself proof of a Core violation. |
| PROV and Keyed-State interpretation — Core §§2, 9; plan §§2, 8 | Typed stage/processor evidence and independent result exports exist. No explicit Core-to-PROV mapping, PROV constraint suite, or Keyed-State relation implementation was found. [Evidence recording](../../src/docspec/application/processor_runtime.py#L125), [export scope](../../src/docspec/runtime/exports.py#L25). | Add the explicit interpretation and its checks; implement bounded `prov` export as planned. A typed evidence export does not establish the required PROV interpretation automatically. |

## Important identity boundary

`SourceItem.identity` hashes only `itemId` and `version`; metadata remains part of
the item's value. I checked two items with the same candidate, item ID, and
version but `language: en` versus `language: fr`: their values differ, their
source identities match, and `same_acquisition_inputs` returns true.
[Identity and comparison](../../src/docspec/domain/content.py#L210).

That supports today's metadata-only acquisition reuse. It also means the existing
source identity cannot simply be relabeled as Core's immutable occurrence identity.
The new model should keep the stable source/member key, assign a new occurrence
to the changed value, and still allow the unchanged URL dependency to reuse capture.
This is a required mapping decision, not an allegation that the current source-key
semantics are defective.

The same distinction applies to execution identity. The public API documents that
identical work in one workspace recovers progress rather than creating another
trial. Preserve that recovery behavior, and add the plan's explicit fresh-execution
semantics rather than overloading the recovery key.
[Runtime behavior](../../src/docspec/runtime/__init__.py#L72).

## Implementation tools and ownership

| Plan choice | Current code | Practical change |
| --- | --- | --- |
| DuckDB + Arrow/Parquet | Already used for queries, sorting, partitioned writes, and bounded streams. [Storage](../../src/docspec/adapters/storage/records.py#L94). | Keep the engine and useful adapters; add the actual resolver and selected-value queries. |
| Authoritative SQLite ledger | Immutable JSON controls and manifest releases are authoritative. SQLite is used for a processor cache and temporary bookkeeping. [Controls](../../src/docspec/adapters/storage/controls.py#L22), [cache](../../src/docspec/adapters/processor_cache.py#L1). | Implement the new logical ledger and its batch operations. The existing SQLite cache is not that ledger. Avoid leaving two owners for the same publication decision. |
| disk-objectstore | Custom local storage writes one object path per SHA-256 digest. [Blob store](../../src/docspec/adapters/storage/blobs.py#L20). | Replace the local physical content adapter with packing while preserving exact-byte references, streaming, verification, and retention behavior. |
| msgspec fixed records | Dataclasses, explicit shape checks, and handwritten serialization dominate. msgspec is a development dependency used as an optional shared-codec accelerator. [Dependencies](../../pyproject.toml#L39), [records](../../src/docspec/domain/content.py#L191). | Introduce typed Core records at public admission/serialization boundaries. Keep semantic checks that span records outside structural validation. |
| jsonschema-rs supplied schemas | Already validates catalog item acceptance; Python jsonschema supplies refusal diagnostics and other fixed catalog validation. [Schema admission](../../src/docspec/adapters/catalog_artifact/schemas.py#L13). | Extend to the intended supplied-payload boundary and consolidate error/acceptance semantics. This is an expansion and cleanup, not a new technology introduction. |
| SHA-256 and canonical encoding | Already present through the shared canonical codec and hashlib. [Identity](../../src/docspec/domain/identity.py#L303). | Reuse exact encoding rules; add the new versioned correspondence structures. No hash-family migration is needed. |
| graphlib and recursive SQL | Processor ordering and invalidation use custom Python graph loops. [Graph](../../src/docspec/domain/processors.py#L927). | Replace the small graph algorithm with graphlib; add ledger-backed result-dependency traversal. |
| `prov`, Hypothesis, conditional native functions | `prov` and Hypothesis are not current dependencies; no DocSpec native component is present. [Dependencies](../../pyproject.toml#L1). | Add the planned exporter and reference-model sequence tests. A native component remains conditional, not unfinished baseline infrastructure. |

The current product also has optional S3 storage and Dagster execution, source
adapters, document extraction/segmentation, evidence coordinates, and result
exports. Preserve these as document-facing capabilities while adapting their
record boundaries; the Core plan's optional scope is not a reason to discard
working features. [S3 adapter](../../src/docspec/adapters/s3_blob.py),
[Dagster adapter](../../src/docspec/adapters/dagster.py),
[public runtime](../../src/docspec/runtime/__init__.py).

## Performance implications

DuckDB is already doing useful native work, but the production data path still
materializes and processes Python records. The writer iterates records and
canonically encodes each one before forming Arrow batches; the reader fetches
rows, parses JSON, validates, and yields dictionaries. Planning also walks source
items and writes per-item workspace records.
[Writer](../../src/docspec/adapters/storage/records.py#L365),
[reader](../../src/docspec/adapters/storage/records.py#L272),
[planner](../../src/docspec/application/planner.py#L344).

These are concrete places to measure and reduce conversion work. They are not
measured proof that all Python code is slow or that ledger binding dominates.
The new engine probe executes its own membership/extraction/hash query directly;
it does not measure the current planner, retained publication, full revision
semantics, or the complete selected-value evaluator.
[Probe scope](probes/2026-09-13-engine-resolver-probe.py#L44).

## Simplification target

The intended result is fewer custom mechanisms, fewer representations of the same
fact, and one owner for each lifecycle rule. General Core behavior must replace the
corresponding document-specific machinery as it lands. Source-specific extraction,
normalization, and evidence rules remain application code; they do not need a
second retention or reuse lifecycle.

| Consolidate | Target | What should disappear |
| --- | --- | --- |
| Handwritten fixed-record parsing and serialization | msgspec Core records plus separate semantic checks | Parallel serializers and structural validators for the same record format. |
| Stage-specific eligibility and processor-cache policy | One correspondence/selection service with explicit document dependencies | Duplicated implementations of eligibility and result-association rules. |
| Distributed authoritative metadata decisions | One transactional metadata owner | Competing publication authorities; content manifests can remain retained data or exports. |
| Generic local object-storage mechanics | A thin disk-objectstore adapter | Custom implementations of the storage mechanisms the library supplies. |
| Custom graph traversal/order machinery | graphlib and scoped SQL | Hand-maintained implementations of those standard algorithms. |

Keep the batch interfaces small. Core explicitly permits several conceptual
relationships to share records, manifests, and transactions; do not create a class,
repository, service, or persistence step for every noun in the spec. Reuse the
existing runtime/application/adapter boundaries rather than introducing another
framework around them.

Some behavior is new: general immutable revisions and selected-value dependencies
are not merely cleanup. Their acceptance criteria should pair the new behavior
with the old path or duplicated rule it replaces. A migration step is complete
when its callers use the common owner and the superseded path is removed or reduced
to the necessary application adapter.

## Recommended implementation boundary

1. **Keep the existing document capability and its evidence tests.** Fetchers,
   processors, exact-byte storage behavior, native record files, recovery,
   inspection, and retained alternatives provide working product value.
2. **Build the general Core records and metadata owner.** Define occurrences,
   states, operation executions/results, input/result bindings, selected values,
   and reuse associations without document-stage prerequisites. Map the current
   document pipeline onto those records through its existing application and
   adapter boundaries.
3. **Implement the resolver and selected-value evaluator on DuckDB.** Reuse
   partitioned content and integrity checks, and add the missing semantics and
   known-answer cases. Then connect correspondence and reuse to those records.
4. **Move publication authority deliberately.** Replace the relevant manifest/cache
   ownership with the planned ledger operations; retain manifests as retained
   content or exports where useful. Switch the local blob adapter independently.
5. **Prove the new semantics and preserve the old product behavior.** Add the Core
   and optional Keyed-State checks, bounded PROV export, and workload qualification
   while continuing to pass the existing document tests.

This consolidates the shared lifecycle in Core, with the current document workflow
using that implementation. It does not leave two permanent lifecycle engines.
It avoids forcing arbitrary datasets through segmentation or treating a document
source key as an immutable occurrence. A change that loses existing recovery,
retained alternatives, or exact input/result associations fails the preservation
test even if it matches the new tool list. Conversely, a tool-only migration that
cannot run the plan's two-field revision/reuse example has not delivered Core.

## Validation and limits

**75 focused tests passed in 22.73 seconds** on the current working tree. The run
covered 13 suites for prefix reuse, processor caching, retained alternatives,
stage recovery, blob admission, source snapshots, the uncommitted Arrow stream
change, incremental equivalence, catalog/record contracts, processor behavior,
evidence round trips, and import direction. Reproduce with:

```sh
.venv/bin/python -m pytest -q \
  tests/test_prefix_reuse.py tests/test_processor_cache.py \
  tests/test_experiment_retention.py tests/test_stage_checkpoint_recovery.py \
  tests/test_checkpoint_blob_admission.py tests/test_source_catalog_snapshot.py \
  tests/test_parquet_arrow_stream.py \
  tests/conformance/test_incremental_equivalence.py \
  tests/conformance/test_document_catalog_contract.py \
  tests/conformance/test_record_storage_contract.py \
  tests/conformance/test_processor_contract.py \
  tests/conformance/test_evidence_roundtrip.py \
  tests/conformance/test_import_directions.py
```

The separate in-memory identity check confirmed the metadata/source-key behavior
above. These are existing-behavior checks, not a new Core conformance suite. I did
not run the full suite, rerun the engine benchmarks, benchmark production capacity,
or perform live S3/Dagster qualification. No implementation, spec, or plan was
changed for this comparison.

Input document SHA-256 values:

- Core spec: `977e13b341e1ca7524b85d04e624c1f8ffd3455aac597a53e206d72addc10b81`
- Implementation plan: `2ca59ef21860d76f5fe97e00c3516f992837e317161aaf3ece3e1867b04e29f5`
