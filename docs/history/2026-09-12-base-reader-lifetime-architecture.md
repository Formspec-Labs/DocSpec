# Capacity preflight: reuse one admitted base during a prepared execution

Date: 2026-09-12. Reviewed checkout: `e854c39` in `/Users/mikewolfd/Work/DocSpec`.

Recommendation: fix the demonstrated per-store full-base admission before attempting the proposed 4,096-document qualification. Keep the workload proposal, but do not claim that it presently fits the proposed 30-minute operation budget. One lazy admitted reader per prepared execution service is the smallest justified correction. Do not change packing, partitioning, or the verifier's integrity rules in the same slice.

This is a static architecture assessment, supplemented by parent-executed exploratory measurements. I read the saved probe, measurement JSON and profile text; I did not execute a workload, test, package build, or shared environment command. No production files changed.

## What the exploratory evidence actually establishes

The public-API script is `/tmp/docspec-capacity-baseline/probe.py`. It builds a supplied-record catalog, captures local files, retains that result, then processes the retained captures and retains the successor. The `process` measurement includes preparation, planning, execution, reconciliation and retention (`probe.py:61–77`); it is not a measurement of the processor alone.

The completed 64-document output under `/tmp/docspec-capacity-baseline/small-64-v5` reports:

| Operation | Elapsed time | Native process peak RSS |
| --- | ---: | ---: |
| Build | 122 ms | 49,381,376 bytes |
| Capture and retain | 3,359 ms | 52,789,248 bytes |
| Later processing and retain | 28,112 ms | 71,221,248 bytes |
| Fresh inspection | 1,639 ms | 53,182,464 bytes |

The inspection reports 64 files, 51,840 captured bytes, 256 segments, 256 processor invocations, zero new capture bytes, and **64 sealed processing stores**. Thus these are distinct 810-byte documents with four paragraphs each. The proposed text population has 80 MiB of content and 20,480 segments; this probe has neither that byte distribution nor that output population.

The parent then profiled the same path at 128 documents. Saved evidence: `/tmp/docspec-capacity-baseline/processing-128.pstats` and `/tmp/docspec-capacity-baseline/processing-128-profile.txt`.

| Profiled function | Calls | Cumulative time |
| --- | ---: | ---: |
| Entire probe module | 1 | 214.627 s |
| `_reprocessing_reader` | 128 | 161.344 s |
| `LocalManifestDocumentCatalog.open_reader` | 129 | 162.633 s |
| `DocumentReleaseVerifier.verify` | 136 | 146.833 s |

These cumulative times overlap; they must not be summed. Instrumentation adds substantial overhead, so the 128-document profile is evidence of where repeated work occurs, not an unprofiled size-to-time comparison against the 64-document run.

## Confirmed repeated work

Every unfinished store containing a reused-prefix entry obtains a new base reader:

1. `application/execution.py:140` calls `_reprocessing_reader` once per store.
2. `application/execution.py:173–182` calls `document_catalog.open_reader(plan.base_release)` whenever any entry is not `FULL`.
3. `adapters/storage/catalog.py:193–196` obtains that reader through a complete `open`.
4. `adapters/storage/catalog.py:175–190` admits the artifact and calls `DocumentReleaseArtifactVerifier.read`; `adapters/platform_artifact.py:496` calls the release verifier.
5. `application/commit.py:187–230` verifies plan/run/commit links, ledgers, every active layer, retained blobs, cross-layer relationships, and the complete store receipt set. `_verify_active_layers` streams every active layer (`:350–362`); `_verified_store_digest` reloads every retained store (`:375–397`).

This is full dataset admission repeated for every store, even though the requested base reference is unchanged. The profiler independently confirms that this path dominates the instrumented processing run.

Store packing makes the effect stronger than the probe's `max_entries=16` suggests. `application/planner.py:243–258` assigns each noncapture item the entire store memory allowance when no explicit memory estimate is present. `_StoreBuffer.can_add` adds estimates (`:270–273`), so two such items cannot share a store. This explains the observed 64 stores. Increasing `max_entries` alone does not change that behavior. The public source-catalog mapping nests normalized metadata and the preserved catalog row (`domain/source_catalog.py:375–384`); simply adding a raw metadata field to the supplied fixture would not become a top-level planning estimate. No estimate-policy change is required to fix the repeated admission.

A second, separately evidenced cost remains after admission reuse: `base_reprocessing.py:86–145` asks for source-items, dispositions, files and receipts for each source. `catalog.py:95–109` filters the results of `RecordStorage.scan_partition_value`; `records.py:595–604` selects one hash bucket and streams that bucket. It does not seek directly to the source's rows. With a fixed 16-bucket policy, the amount of repeated partition work grows with the population. This report records that fact only. The present profile justifies fixing full admission first, then measuring again; it does not justify adding a new lookup index or changing partition semantics now.

## Smallest safe reader lifetime

Use the existing `DocumentCatalogReader`, which already promises a verified immutable view reusable for one application operation (`ports/document_catalog.py:13–28`). The view contains a fixed `DocumentRelease` and the existing record adapter (`adapters/storage/catalog.py:73–93`); it does not materialize the dataset or hold a second durable ledger.

Recommended implementation boundary:

- Lazily retain one successfully admitted reader on `StoreExecutionService`. Its constructor already belongs to one exact plan reference and one catalog; `runtime/composition.py:246–266` creates one service for a prepared run.
- Associate the cached reader with the **full** `DocumentReleaseRef`, not merely the logical release ID. Verify configuration as today before consulting it. Refuse an unexpected base-reference mismatch rather than silently repurposing the same service.
- Use a small service-local lock for first admission, cache publication and reset. Publish only after `open_reader` returns successfully. A failed admission leaves no cached success. Do not hold the lock while executing entries, reading payloads, or invoking processors.
- Treat the prepared run's execution lifetime as the operation boundary. `PreparedLocalRun.close()` should clear this reader alongside temporary task-membership state, after active workers have stopped. Existing documentation already requires that ordering (`runtime/execution.py:29–35,68–70`). Reusing the object after close should admit the base again.
- Do not share this state across independent prepared runs, catalogs, processes, or native resources. It is not a process-global cache or a persistent trusted-state flag.

The local helper can execute many stores through the same service, including local threads. They should share the admitted reader and retain independent entry processing. The existing reader's calls use local iteration state; no dataset-sized mutable result collection is needed.

Native Dagster resources reconstruct prepared runs in their own processes (`adapters/dagster.py:105–115,123–150`). Each reconstruction must independently admit its base. A native multiprocess deployment that creates one resource per store may therefore still perform one full admission per store. This change improves the current shared-service local path; it does **not** qualify or promise equivalent savings for every Dagster executor/deployment. Do not add a new cross-process admission service to make that claim.

## Integrity meaning of reuse

The reader pins the release description that was fully admitted at the start of the prepared execution. It is not an assertion that arbitrary files on a writable filesystem cannot change afterward.

Keep all existing consumption checks:

- Record access still verifies the pinned layer root and selected member digest, shape, ordering and counts (`records.py:315–345,347–422,558–570`).
- Prefix seeding still checks source acquisition inputs and requested configuration against the retained source (`base_reprocessing.py:85–118`) and verifies the resulting checkpoint (`:190–194`).
- Checkpoint admission still loads and verifies receipts, captured blobs and representation blobs (`execution_checkpoints.py:76–100,107–123`).
- Fresh prepared execution/recovery, explicit catalog open, and final retention retain their complete admission paths.

Accordingly, corruption of **used** rows, blobs or receipts must still refuse work. A change to an unrelated base object after the first full admission is no longer necessarily discovered before the next unrelated store begins; it is discovered at the next complete admission. This is an explicit per-operation observation boundary already used by the catalog reader, not a claim of transaction-wide filesystem immutability. There is no need to weaken digest checks, memoize arbitrary blob verification, or add a stat-based trust scheme.

Required focused proof before accepting the correction:

1. Several real reused-prefix stores in one prepared run perform one full reader admission and produce the same admitted retained result.
2. Concurrent first tasks share one successful admission without serializing their processing.
3. Failed first admission is not remembered as success; no entry execution begins through it.
4. A changed used record member, captured blob, or receipt after initial admission still refuses reuse through the existing checks.
5. Close and fresh prepared/native-resource construction force fresh admission. A corruption outside the previously consumed item is detected by that fresh admission.
6. Capture-only/FULL work remains lazy and does not open a base unnecessarily.

## Can 4,096 documents fit the proposed budgets?

Not established. Even a purely linear multiplication of the 64-document processing phase gives 1,799 seconds, essentially the proposed 30-minute ceiling, before accounting for the proposed much larger files and additional segments. That is a warning calculation, not a prediction. The measured per-store full-base admission is worse than linear in source population under the present one-item-store layout, so there is no responsible basis to start the large run expecting it to fit.

No obvious per-item limit blocks the proposed text shape: 128-KiB maximum captures, at most 32 segments for a document, and one bounded processor are below the probe's declared per-store byte/segment/work limits. This does not establish physical memory, total disk, or wall-time feasibility. The current 128-MiB `WorkLimits.max_memory_bytes` value measures materialized reservations, not process RSS (`work_budget.py:39–48`), and `max_duration_seconds=900` governs an active store attempt. The probe's far-future deadline is not a 30-minute operation deadline. Its built-in record profile permits 128 GiB of merge scratch, so an 8-GiB qualification budget is not currently enforced by that declaration.

Proceed in this order:

1. Land and test only the demonstrated reader-lifetime correction.
2. Re-run the same unprofiled small input and one diagnostic larger input with phase timing, full retained admission and operation counts. Use the profile only to confirm attribution; do not compare its wall time directly with unprofiled timing.
3. Keep the proposed 4,096-document input and predeclared acceptance budgets as a qualification candidate. Run it only after the diagnostic suggests reasonable headroom, with real installed execution, native per-process RSS, and workspace-plus-system-temp disk observations.
4. If it fails those budgets, report the result and measured limiting operation. Choose a smaller explicitly scoped qualification or justify the next specific correction; do not weaken checks or retroactively relabel a failure as capacity success.

This is a local serial synthetic qualification decision. Real-provider acquisition, parallel catalog building, network limits, native deployment capacity, PDF/image workloads, and publication qualification remain separate.

## Parent remeasurement after the correction

The root agent repeated the unprofiled probe against committed
`efe23a387e859b965f70147a1776768dbc827c4f`. The original 64-document run used
`e854c391bec99ac22a106cf50c8efcc1db14e566`. The probe logic and generated bytes
are unchanged; its declared implementation identity names the corresponding
commit. These are single local observations, not a statistical benchmark.

| Operation | Original 64 documents | Corrected 64 documents | Corrected 128 documents |
| --- | ---: | ---: | ---: |
| Build | 122 ms | 126 ms | 241 ms |
| Capture and retain | 3,359 ms | 3,411 ms | 6,756 ms |
| Later processing and retain | 28,112 ms | 12,962 ms | 25,170 ms |
| Fresh-process inspection | 1,639 ms | 1,835 ms | 3,481 ms |
| Processing process peak RSS | 71,221,248 bytes | 69,795,840 bytes | 83,361,792 bytes |
| Inspection process peak RSS | 53,182,464 bytes | 53,411,840 bytes | 57,950,208 bytes |

Processing and retention took about 54% less elapsed time in the repeated
64-document observation. Both 64-document inputs were compared byte for byte.
Their admitted results each contain 64 files, 64 representations, 256 segments,
256 derived records and zero failures; later processing records zero new
captures. The 128-document result has 128 files/representations and 512
segments/derived records, with zero failures. This supports proceeding to the
larger, differently shaped control; it does not establish its capacity.

The machine reports macOS 26.6 (25G5057c), arm64, 48 GiB RAM and 14 logical CPUs.
The local `getrusage(2)` manual specifies bytes for `ru_maxrss`. Each operation
uses a fresh Python process; elapsed measurements start after imports while
RSS covers the process lifetime. OS page-cache state and other machine activity
were uncontrolled. Native `/usr/bin/time -l` output was also retained for the
corrected capture/processing operations and 128-document inspection.

Scripts, native outputs and retained workspaces remain under
`/tmp/docspec-capacity-baseline/`: `probe.py`, `probe-after-reader.py`,
`small-64-v5`, `after-reader-64` and `after-reader-128`. This exploratory probe
does not supply installed-wheel qualification, sampled scratch peaks, complete
independent value/quote comparison, or recovery at the proposed population.
