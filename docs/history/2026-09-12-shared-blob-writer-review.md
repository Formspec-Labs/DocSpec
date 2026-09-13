# D31 shared blob writer suitability review

## 1. Summary and scope

**Verdict: APPROVE deferral; do not adopt the current shared writer.** The proposed replacement removed a real duplicate physical write loop, but qualification exposed a normal concurrency case that the current installed Rulespec writer refuses. Revert the uncommitted DocSpec adoption rather than add a local retry/locking system or weaken corruption classification.

The reviewer statically inspected the proposal, `src/docspec/adapters/storage/blobs.py`, `tests/test_shared_blob_writer.py`, `tests/test_runtime_blob_reuse.py`, the proposed storage guide, and the installed public Rulespec implementation. The reviewer ran no tests/builds and changed no source. The parent executed the deterministic qualification described below.

## 2. Function and data traces

The attempted wrapper delegated only physical `put_if_absent` to `rulespec_artifacts.LocalBlobWriter`, using the existing `objects/sha256/<two digits>/<digest>` object keys. It retained DocSpec's media type/reference, reads, logical capture accounting and dataset lifecycle. Known digest plus size allowed verification and reuse without consuming a fresh stream; the existing `application/execution.py:456–483` FetchStream context closes that unconsumed body. One/no pin still consumes and checks bytes. Read-only construction deferred writer layout creation until an explicit put.

In the installed wheel, `rulespec_artifacts/_blobs.py:248–258` verifies a known object and returns reuse. `_verify` at `:78–102` captures `LocalFileState` before reading and compares it with descriptor and visible-path state afterward. `LocalFileState` includes ctime (`_artifact.py:1214–1233`). A publishing writer hardlinks its pending file at `_blobs.py:294–300`, then removes the pending link at `:305–311`. That legitimate unlink changes the destination inode's ctime while another reader may be verifying the same unchanged bytes.

The shared writer explicitly documents this refusal and tells its caller that retry may be needed (`_blobs.py:217–220`). DocSpec's proposed adapter translates the resulting `BlobIntegrityError` into its existing nonretryable `IntegrityError`. A benign concurrent same-content operation can therefore reject document work. The same physical store is used by local and native managed workers; this is a supported execution seam, not a hypothetical new scheduler.

## 3. Invariants and simplification judgment

A shared replacement must preserve exact-byte integrity, no unsafe replacement, bounded streaming, meaningful failures and ordinary concurrent immutable reuse. It must not require every consumer to recreate a shared physical writer's internal race handling.

The wrapper, typed error translation, existing stream closure, object layout and fixed shared 1 MiB verification chunk were otherwise suitable. The shared verification chunk is distinct from a caller's configured DocSpec read chunk; neither is a total process-memory guarantee. There was no reason to add another tuning model.

A second independent suitability gap remains for source-catalog transactions: DocSpec holds transaction directory identities before writing, while the public shared writer accepts a path and adopts its then-current identity. Without a public expected-root-identity or equivalent descriptor-admission input, a path-based adaptation could write into a replaced directory before detecting it. Do not use private fields, subclasses or `/dev/fd` shims to work around that boundary.

Both gaps belong to the shared implementation owner. DocSpec should retain its current writer until the public installed dependency meets these requirements. No new local retry policy, global lock, classifier exception or parallel storage framework is justified.

## 4. Evidence

The reviewed proposed wrapper tests exercise exact old-layout references, known reuse without iteration, one/no-pin consumption, limits, corrupt objects, directory replacement, symlink refusal, zero-byte content, original iterator exceptions and cleanup. Public runtime tests inspect exact retained bytes, current fetch metadata, one stream close, logical captured-byte counts and corruption refusal. These establish useful intended behavior but do not override the concurrency failure.

The retained deterministic probe is [shared_blob_concurrency.py](probes/shared_blob_concurrency.py). It uses two actual public LocalBlobWriter instances and coordinates their native `os.unlink` and `os.read` calls with events. Writer A publishes, then pauses immediately before removing its pending link. Writer B captures the file's initial state and pauses before reading. A finishes legitimate cleanup; B reads the unchanged bytes and then performs the existing final comparison. The test verifies A's exact bytes and empty pending directory before requiring B to succeed. It uses no arbitrary sleeps, byte mutation or mocked writer result.

**Parent execution:** the qualification failed as predicted, **1 failed in 1.58 seconds**, with `BlobIntegrityError` from the benign pending-hardlink cleanup race. The reviewer inspected the probe but did not execute it. This is evidence against adoption of the current installed writer, not a successful migration gate.

## 5. Findings and destination requirements

**Blocking F1 — legitimate concurrent immutable writes become artifact-integrity failures.** The shared writer's ctime comparison can reject unchanged content after another writer's ordinary pending-link cleanup. Preserve corruption refusal while making this concurrent publication/reuse behavior reliable inside the shared writer. A destination-owned regression must exercise the deterministic overlap before DocSpec retries adoption.

**Deferred F2 — transaction root identity cannot be supplied.** Source-catalog transactions need a public way to retain their already-admitted root identity before any writer layout mutation. The replacement remains inappropriate for that transaction path until the shared interface supports it.

Record both requirements in the destination repository's RS03 work. DocSpec's checklist should link that dependency and describe the evidence-backed deferral. Revert the attempted adapter change and remove unneeded adoption-only tests/guide claims; retain the qualification evidence rather than preserving an inactive implementation.

## 6. Verdict

**APPROVE the partial D31 outcome as a justified deferral, with the migration reverted.** The experiment showed why the currently shared implementation is not yet a safe substitute. Preserving the current working path and requesting the smallest shared fix meets the user's simplicity and ownership requirements better than a local workaround. No generic writer adoption or complete cross-repository deduplication is claimed.
