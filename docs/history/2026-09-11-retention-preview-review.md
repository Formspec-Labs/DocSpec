# D25: shared experiment retention review

## 1. Summary and reviewed scope

**Static verdict: APPROVE for verified blob dependency inventory and repeatable read-only preview.** The implementation makes the existing maintenance service usable through the public runtime and fixes two missing dependencies: a retained successor can still need predecessor bytes during selection, and a planned checkpoint can need base bytes before its own entries contain them. It does not introduce deletion, automatic root discovery, a scheduler, or another garbage-collection system.

Reviewed the current D25 changes over `a89aa63` in `application/maintenance.py`, `domain/maintenance.py`, `adapters/blob_inventory.py`, `runtime/maintenance.py`, shared runtime storage/composition/exports, `cli/blobs.py`, `tests/test_retention_runtime.py`, the existing maintenance/CLI tests relevant to the moved behavior, and the retention/operations guides. Concurrent canonical-encoding and result-export work is excluded. The unrelated historical findings file was not read.

The reviewer read source, test assertions, and the parent-provided test log; the reviewer did not execute tests or builds, modify source, or commit. Executed validation is attributed separately below.

## 2. Function and data traces

| User action | Concrete trace | Result and boundary |
| --- | --- | --- |
| Build evidence for explicit results/checkpoints | `runtime/maintenance.py:23` → `_local_profiles` / `_local_storage(create=False)` at lines 41–44 → existing `BlobRetentionSetService.build` at `application/maintenance.py:117` | Uses the admitted local machine profiles, explicit accepted document producer, existing storage adapters, and existing SQLite workspace. The plan argument chooses profiles; explicit root arguments choose what to keep. No execution plugin is constructed. |
| Retain both attempts of one logical plan | `maintenance.py:125–129` keys explicit results by `(release_id, digest)`; lines 219–232 use the same physical distinction for iterative visits | A logical derivation ID is not mistaken for an exact result. Exact repeated pins deduplicate; conflicting locator metadata for the same pin refuses. |
| Follow predecessor dependencies | `_retain_release_content` at `maintenance.py:251` opens each result through the existing catalog verifier, loads its exact processing plan, checks `plan.base_release == release.previous_release`, retains active blobs and the run's stores, and returns the predecessor | Iterative outer traversal preserves bytes required by existing retain/select admission, including outputs removed by a shorter successor. Existing catalog verification remains the authority; no alternate graph verifier is introduced. |
| Preserve an unfinished checkpoint's base | Explicit plans at `maintenance.py:175–178` are loaded and indexed by their checked plan ID at lines 234–249; their base chains are visited before direct stores. `_retain_store` at lines 356–358 refuses an unmapped plan ID | No invented lookup-by-ID registry. A standalone store requires the exact saved plan reference, available from the prepared handoff. A retained release supplies its own saved plan. |
| Preserve actual blob references | Active `files`, `representations`, and `segments` at `maintenance.py:303–330`; store entry bytes and receipt references at lines 360–383; deduplication and byte verification at lines 390 onward | Uses the existing profile-state-plus-locator identity, refuses conflicting immutable metadata, verifies direct store blobs, and relies on admitted release verification for active release blobs. Mixed blob profile states refuse. |
| Save the result of traversal | `maintenance.py:186–206` writes/verifies an ordinary retention reference layer and saves `BlobRetentionSet`; `domain/maintenance.py:146–180` uses closed format 2.0 with `retainedPlans` | The builder writes immutable maintenance evidence. It does not change current selection or erase any object. Previous retention-set formats are explicitly retired. |
| Preview local inventory | `runtime/maintenance.py:58–84` opens storage with `create=False` and uses temporary scratch; `adapters/blob_inventory.py:46–132` checks options, the exact set reference, record/blob profiles, local root, all declared reference rows, counts, and every listed blob | This admits the saved set and its listed bytes. It does not rederive an imported set's completeness from the declared roots. |
| Report objects outside the supplied set | `blob_inventory.py:135–178` uses context-managed directory scans, strict content-addressed names, no symlinks, the existing scratch membership lookup, an age filter, and a bounded sample | Returns counts/bytes and samples, with `scope="relative-to-supplied-retention-set"` at line 184 and `dryRun=True` at line 202. A candidate is not deletion authority. |
| Use the CLI | `cli/blobs.py:16` now uses the normal `LocalContentAddressedBlobStore(create=False)` constructor; `cli/blobs.py:41` delegates inventory to the same adapter | Removes the obsolete `object.__new__` construction and the duplicate CLI inventory body. The existing CLI still requires `--dry-run`. |

## 3. Invariants and simplicity assessment

- **Exact saved results matter.** The source of the reported identity defect was verified at `adapters/storage/catalog.py:149–150`: release IDs derive from plan/partition policy. The fix now distinguishes different artifact digests for one logical result ID, rather than weakening exact reference checking.
- **Reachability follows the operations users can actually perform.** Existing retain/select still opens a predecessor. Keeping predecessor blobs is therefore conservative correctness, not a speculative lifecycle feature. Removing that dependency would be a separate semantic change; this slice does not need it.
- **Plan references remain explicit.** A store contains only a plan ID. Supplying its saved plan reference is simpler and more reviewable than introducing a discovery registry. The checked plan supplies a base reference; callers do not manufacture that dependency list.
- **The inventory remains observational.** The public builder computes dependencies, while a preview of an imported set is explicitly relative to that set. No second receipt, deletion authorization, or completeness ledger is added. Concurrent changes or omitted alternatives can change which bytes are needed.
- **One implementation serves Python and CLI.** The public module is composition code; the inventory is the moved existing implementation. SQLite remains disposable scratch and existing records/control storage remains the persisted authority.
- **Bounds have accurate names.** `max_spooled_bytes` limits canonical scratch-record payloads, not the physical SQLite file. Samples and cache size are separately bounded. Explicit root lists are intended to be small; traversal visits and blob membership use the existing bounded workspace. Full catalog admission inherits the existing verifier's work and memory costs and is not newly qualified for large corpora.
- **Resource cleanup is explicit on the new inventory path.** Its retention-row iterator and scratch workspace are both context-managed at `blob_inventory.py:99`; directory scans are context-managed. The public preview uses temporary scratch outside the dataset.

The retained complexity buys observable user value: users can inspect two alternatives, keep one without invalidating its shared captured base, and preserve an unfinished checkpoint whose inputs are not yet copied into that checkpoint. A generic garbage collector, automatic root discovery, background cleanup, or lifecycle framework would not improve this bounded operation and is excluded.

## 4. Test evidence

The reviewer inspected these concrete acceptance assertions in `tests/test_retention_runtime.py`:

| Case | Inspected assertion |
| --- | --- |
| Shorter successor | Lines 94–110 create capture → segmentation → capture results, retain three generations' required bytes, run the real predecessor-admitting selection operation, and assert one fetch total. |
| Standalone planned checkpoint | Lines 113–130 refuse a missing explicit plan, preserve the base capture before execution, remove only disposable-fixture candidates, then finish and retain two segments without another fetch. |
| Shared alternatives | Lines 133–150 drop one sibling root, remove only its unreferenced fixture bytes, read the kept result, then use that result as a later processing base without refetch. |
| Interrupted observation | Lines 153–168 inject `KeyboardInterrupt` into inventory, compare all dataset file bytes before/after, repeat preview, and assert a repeated build returns the same saved reference. |
| Same logical plan, different exact results | Lines 171–181 create two real retained results with equal plans and logical IDs but different artifact digests, keep both roots, and verify both are visited. |

Existing maintenance cases cover mixed profile roots, corrupt bytes, and conflicting immutable metadata; existing CLI cases exercise the moved dry-run behavior.

**Execution supplied by the parent:** `tests/test_retention_runtime.py`, `tests/test_maintenance.py`, and `tests/test_cli.py` completed with **34 passed in 22.77 seconds**. The reviewer read the completion line from `/tmp/docspec-retention-final-gate.log`. This is a focused D25 gate, not a claim that the concurrent whole worktree passed a new full regression.

## 5. Findings and resolution

1. **Resolved — logical IDs collapsed distinct retained attempts.** Initial root/visited deduplication used `release_id` alone and refused two exact results from one plan. It now includes the artifact digest, with a real regression case at `test_retention_runtime.py:171`.
2. **Resolved — superseded CLI construction.** `_blob_reader` used `object.__new__` despite the existing read-only constructor. It now invokes that constructor normally.
3. **Resolved — implicit stream cleanup.** The moved inventory now explicitly closes the retention-record iterator on refusal and interruption.
4. **Resolved — scale/completeness wording.** Public arguments and the guide distinguish scratch payload bounds from SQLite overhead; runtime docstrings, guide, operations text, and returned report scope distinguish supplied-set inventory from complete deletion authority.

No remaining material correctness finding in the reviewed D25 source. Remaining limits are deliberate scope: no automatic discovery of every useful attempt, no deletion of blob/control/store/catalog files, no destructive-cleanup recovery guarantee, no concurrent inventory snapshot guarantee, and no corpus-scale capacity qualification.

## 6. Verdict and D25 acceptance

**APPROVE.** The agreed revised D25 acceptance is supported: verified local blob dependency evidence for explicitly retained results and checkpoints, including shared bases and predecessor dependencies, plus repeatable read-only inventory. Existing state and evidence mechanisms are reused, and the additional code is proportionate to the demonstrated user need.

The former checklist wording about pruning and interrupted destructive cleanup is not established by this implementation. It should be explicitly replaced by the agreed inventory/preview scope rather than marked complete under its old wording. The guide already states this limitation at `docs/retention-preview.md:44–61`. Deletion remains unsupported; the disposable test deletions neither provide a production deletion API nor authorize cleanup of user datasets.
