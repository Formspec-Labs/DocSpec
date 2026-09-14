# Repair failed work without repeating completed stages

A failed Core attempt remains recorded with its actual progress. Correct the
input or injected implementation and start a document run with a new `run_id`.
The document pipeline selects reusable successful stages through the same Core
evaluator used for ordinary work. A repaired extraction can therefore reuse its
retained capture; unchanged processor prerequisites can also be reused.

Inspect the original execution or result through `workspace.inspect` before
repairing it. A failed run is not a successful selected dataset merely because
some bytes were written. The original failure history remains after repair.
Use `fresh=True` only when the operation should execute despite eligible retained
results.

An explicitly suspended operation has a different path: supply its continuation,
unchanged pinned definition and checkpoint verifier to `operations.resume`.
`operations.recover` handles completed publication. These paths do not replay a
failed producer as if its previous attempt were still running.

The [offline walkthrough](offline-walkthrough.md) repairs a missing local file.
[Document tests](../tests/test_core_documents.py) verify extraction repair without
recapture; [continuation tests](../tests/test_core_continuation.py) verify the
original-attempt path. [Retry ownership](retry-ownership.md) distinguishes SDK
requests, native scheduling and actual Core attempts.
