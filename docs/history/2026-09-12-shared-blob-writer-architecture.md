# D31: shared blob writer adoption deferred

DocSpec keeps its current runtime blob writer. The attempted delegation to
Rulespec Artifacts 1.0.12 was reverted before commit because a deterministic
concurrency check rejected valid unchanged bytes. No compatibility writer,
local retry layer or broad integrity-error reclassification was added.

The [reproduction probe](probes/shared_blob_concurrency.py) uses the public
`LocalBlobWriter` and two threads. Writer A publishes a blob and pauses before
unlinking its pending hardlink. Writer B starts verifying that same known
object. After B captures its first file state, A removes the hardlink; B then
reads the unchanged bytes. The unlink changes the inode's ctime, and the shared
writer raises `BlobIntegrityError` at `_blobs.py:98`. The probe verifies that A's
published bytes remain exact and pending staging is empty before observing B's
failure. It uses native IO synchronization and no sleeps or content mutation.

Parent qualification on September 12 failed exactly this expectation: 1 failed
in 1.58 seconds, retained in `/tmp/docspec-shared-blob-concurrency-gate.log`.
The probe sits outside the default test suite. Run it explicitly with:

```sh
uv run --frozen --extra dagster --extra s3 pytest -q docs/history/probes/shared_blob_concurrency.py
```

DocSpec classifies corrupt artifact bytes as a nonretryable integrity failure.
Treating this ordinary concurrent same-content operation as corruption could
reject a valid acquisition task. Retrying every integrity error locally would
hide the distinction and would not replay an already-consumed unknown-digest
stream safely. The shared implementation needs to handle this case while still
refusing actual byte mutation, replacement, growth and malformed objects.

A second requirement concerns the source-catalog transaction writer. It holds
a root device/inode identity admitted earlier, but LocalBlobWriter accepts only
a path before adopting the currently named directory. Before/after checks can
detect replacement after creating files there. The shared API must accept the
expected root identity, or an equivalent public descriptor admission, before
that transaction can migrate without weakening its existing guarantee.

Both requirements belong to the [Rulespec RS03 follow-up](../../../rulespec/TODO.md#rs03).
Its acceptance must qualify same-content concurrent reuse and ordinary writes,
retain corruption refusal, and support the already-pinned transaction root.
D31 remains open. The object layout, typed byte-limit/integrity errors, known
and computed digests, bounded verification and caller-owned stream closure
otherwise fit DocSpec; those findings do not override the failed concurrency
requirement. Independent reviewer agreed with deferral.
