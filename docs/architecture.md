# Current architecture

DocSpec supports repeatable document dataset experiments: build a selection
catalog, capture selected bytes, process retained inputs, and compare results.
A catalog is useful on its own. The intended workflow also allows capture-only
completion, processing now or later, and optional export to a consumer.

Current application services retain captures and checkpoints and support
processor-only reruns from a verified base release. A full-processing plan still
requires extraction and segmentation. Convenient stage completion and unified
attempt inspection remain [D24](dataset-experiments-todo.md#d24) and
[D19](dataset-experiments-todo.md#d19); the
[README](../README.md#what-you-can-use-today) identifies current entry points.
Dataset meaning belongs to DocSpec. Rulespec provides shared artifact-container
utilities, while the separate portable bundle still has DocSpec structural
checks, described under [outputs](#what-comes-out).

This maintained guide describes the code, with executable checks linked below.
Use the [decision index](decisions/README.md) for accepted changes and migration
limits, and [CONTRIBUTING](../CONTRIBUTING.md) to find a small change.
For deeper guidance, see [catalog evidence and processing](catalog-and-processing.md),
[extension points](extensions.md), and [recovery and maintenance](operations.md).

## What goes in?

A source adapter exposes a verified, immutable source-native release through
`ports/source_catalog.py`. The installed SpicyDocs reader is an optional
[adapter](../src/docspec/adapters/spicy_docs_source_native.py); core tests supply
local implementations of the same interface.
Upstream acquisition owns source facts. DocSpec owns their interpretation for
document processing.

`SourceCatalogBuilder` applies a pinned source policy to those records and
renditions. A `SourceCatalogItem` records candidate files, the selected candidate,
normalized metadata, interpretations, and per-field source paths. Selection and
exclusion remain observable decisions. For example, Regulations.gov cross-filed
documents retain the selected filing's bytes and record the discarded copies.
The catalog artifact seals the rows, schema, policy, counts, and digests.

See [catalog construction](../src/docspec/adapters/catalog_artifact/builder.py),
the public [catalog API](../src/docspec/source_catalog.py), and
[policy tests](../tests/test_catalog_policy.py).

Within `adapters/catalog_artifact`, `inputs` validates and resumes source reads;
`builder` stages and publishes policy output; `reader` opens pinned snapshots;
and `verification` reconciles the sealed artifact. `derivation` owns both serial
and spawned-worker execution, sharing byte rules in `digests` and bounded reads
in `rows`. The worker entry points remain module-level functions so a fresh
interpreter can import them. The public catalog API imports these owners directly.

Within [`adapters/source_catalog_store`](../src/docspec/adapters/source_catalog_store/),
`pinned_fs` checks open directory identities and owns safe cleanup; `staging`
keeps each blob and artifact transaction together; `store` provides immutable
lookup and destination publication; and `current` admits and advances catalog
pointers under a lock. Public store imports stay at the package entry point.
These filesystem checks deliberately remain stronger than the path-based
helpers used by other local storage adapters.

## What happens to it?

1. A `ProcessingPlan` pins the catalog, profiles, processor graph, policies,
   and work limits. `RunPlanner` creates bounded document jobs and seals their
   complete task population.
2. `StoreExecutionService` captures exact source bytes, extracts a representation,
   creates structural segments and evidence coordinates, and runs declared
   processors. Checkpoints retain completed stages. Recovery verifies retained
   work and restores cumulative budgets before reuse.
3. `StoreDeliveryService` verifies and delivers record streams. `RunReconciler`
   accounts for every planned task and checks durable results before producing
   a run receipt.
4. `ReleaseCommitService.retain_release` verifies and retains the assembled
   result independently of the current selection. Alternatives can share one
   pinned base. `commit_release` also selects the result if that base is still
   current; a stale selection leaves the verified result retained. An explicit
   `catalog.select` can choose another retained result using the caller's expected
   current reference, while each result keeps its original base. See
   [experiment identities and selection](experiments.md).

These steps exchange small immutable references so that local execution and
external workers use the same application services. Dagster is an optional
executor, not a source of document meaning. SQLite workspaces, streaming readers,
bounded merges, and checkpoints keep large runs within declared resource limits;
their extra steps are part of the behavior a refactor must preserve.

[`docspec.runtime`](../src/docspec/runtime/) connects the local adapters and
application services for both Python callers and commands. `composition` checks
typed inputs and assembles services; `storage` selects verified profiles and
local stores; `preparation` creates or verifies execution references; and
`execution` exposes the prepared run's task, delivery, and reconciliation methods.
The CLI parses request files and passes typed values into that same path. Core
services never import the runtime or concrete adapters. See
[Python runs](python-runs.md) for the public entry point and current limits.

The runtime binds acquisition metadata to the selected fetcher and checks exact
task membership before work. Its temporary SQLite lookup derives from the
verified planned-store ledger and has explicit size and lifetime bounds. This
keeps lookup memory bounded and avoids rescanning the full plan for every task.

See [application services](../src/docspec/application/),
[recovery tests](../tests/test_stage_checkpoint_recovery.py), and
[task portability tests](../tests/test_execution_backends.py).

The executor delegates retained-artifact checks to
[`EntryCheckpointVerifier`](../src/docspec/application/execution_checkpoints.py).
It reads the same controls, blobs, extractor identity, and retry policy, then
returns the completed frontier and verified processor results. Store writes and
the cumulative work budget stay in `StoreExecutionService`: verify every entry,
check duration, restore accounting, then start and save the attempt.
[`processor_rules`](../src/docspec/application/processor_rules.py) owns request
construction, permitted-input sizing, result validation, and record ordering
for execution and reuse.

[`ProcessorRuntime`](../src/docspec/application/processor_runtime.py) owns the
processor registry, retries, and cache operations. The executor supplies its
existing budget and mutable result, record, and receipt collections. Failed
attempts remain in those collections when an invocation raises, so the executor
can record the same partial work and decide whether the failure is accepted.
Shared [stage evidence functions](../src/docspec/application/execution_evidence.py)
persist receipt identities and classify errors without deciding the outcome.

[`prepare_base_reprocessing`](../src/docspec/application/base_reprocessing.py)
reads pinned base rows, verifies unaffected results, writes their current-plan
reuse receipts, and reconciles any retained checkpoint. It returns exact content
and the three mutable progress collections to the executor. The executor owns
the remaining-layer loop, memory scope, failure outcome, and checkpoint saves.
Preparation preserves current incremental reuse; an old request identity alone
does not invalidate a result whose inputs, processor, policy, and evidence still
match.

## What comes out?

There are two release representations in the current code. **Both advertise
version `2.0`; their shapes and verifiers differ.** A version string alone does
not identify which reader to use.

| Representation | Contents and owner | Verification |
| --- | --- | --- |
| Application release state | `domain.release.DocumentRelease`: plan, source catalog, active record layers, blob roots, receipts, and publication state; committed by `ReleaseCommitService` | `application.commit.DocumentReleaseVerifier`, with injected record, blob, control, and catalog access |
| Portable document bundle | Root plus a closed member manifest, document/disposition rows, text bodies, evidence, and embedded schemas; produced by `tools/build_document_release.py` | `adapters.document_release.verify.verify_document_release`, reading the bundle itself |

[Decision 0001](decisions/0001-document-release-2-0.md) governs the portable
publication format. The builder and sealed fixtures implement that format, but
the application state model remains a separate path. A cleanup must preserve
each path's identities and readers; converting the application lifecycle into
the portable publication workflow requires an explicit integration change.
Recorded local mint receipts establish local builds, not external publication.
The portable builder remains a historical mint recipe in `tools/`, with
campaign-specific assumptions. It is not an installed generic build API, and
application release output does not replace its historical mints byte-for-byte.

Catalog, plan, and release artifacts use Rulespec's generic container. Consumers
verify their pinned bytes and DocSpec meaning before reading them. Search,
ranking, and serving belong to consuming products.

## How do we check it?

The [contributor task map](../CONTRIBUTING.md#find-a-bounded-change) points to
focused tests. Full local tests cover invariants, recovery, byte identities,
rejected fixtures, optional imports, and installed-package behavior.
[Conformance](../conformance/specification.json) additionally requires evidence
that ordinary tests cannot establish. Keep local checks, conformance,
qualification, and publication status separate.

The four code areas have distinct responsibilities: domain types define valid
data; ports define required access; application services enforce workflow rules;
adapters implement access to files, providers, and executors. The CLI connects
those parts. Shared meaning stays with its owner, even when several adapters
need it.
