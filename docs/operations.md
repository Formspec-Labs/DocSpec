# Operate retained Core datasets

Open `CoreWorkspace` as a context manager. Its `operations`, `publisher`, and
`maintenance` share the same ledger and content protection. The
[Python guide](python-runs.md) covers ordinary calls; use `docspec --help` for
current commands.

## Record, recover, and continue work

Every actual operation receives an execution identity. Progress records actual
uses, generations, failures and interruptions. A failed attempt remains recorded
even if its outputs were never retained.

`operations.recover(execution_id)` recovers completed publication through its
retained result or pending publication journal. It does not infer successful
completion from partial bytes. An explicit suspension uses
`operations.resume(execution_id, continuation, definition=..., verify=...)`:
the verifier checks checkpoint content and the pinned definition before claiming
the original attempt. Only one continuation may claim it. A normal retry is a
new attempt; SDK and scheduler retry policies remain with their owners.

Recovery documents and their required inputs and prepared outputs remain
protected while recovery is available. A completed retained result releases its
redundant journal for explicit policy-authorized collection. See
[continuation tests](../tests/test_core_continuation.py) and
[execution tests](../tests/test_core_execution.py).

## Resource limits and recovery

A document `run_id` pins its source, processing configuration and limits. Newly
fetched bytes and newly produced bulk rows consume that run's cumulative budget;
reused stages, source import and summary assembly do not. Failed production stays
charged, including rows emitted before a producer failed. Successful empty
processor results remain actual recorded attempts and can be reused.

After a handled failure, reopen the workspace and call `run` with the same ID
and configuration. The ordinary Core checkpoint restores the counters and
completed stages remain available for reuse. Changed limits or configuration
refuse before new work. If a process was killed during uncheckpointed work,
continuation refuses because its consumption is uncertain. Choose a new run ID
to explicitly start another budget; that run can reuse completed stages, but it
does not claim the previous run stayed within its budget. See the
[fresh-process budget checks](../tests/test_document_run_budgets.py).

Shared control-record bounds, native engine allowances, per-file byte limits and
processor limits remain active alongside document-run accounting.

## Retain and select

Publication makes content ready before committing the authoritative metadata
unit. Readers rely on committed ledger state. A crash can leave unreferenced
physical bytes, but cannot make those bytes a retained result by themselves.

`workspace.maintenance.select_current(unit_id, dataset, target, expected_current)`
validates the retained target and changes current only if the expected value
still matches. The target is a `(kind, id)` pair for a state or result; `None`
means no prior current value. A stale selection refuses, leaving the retained
alternative available. CLI `select` uses `--target KIND ID`, optional
`--expected KIND ID`, and an idempotent `--unit`.

## Remove bytes under policy

Retain a Core `RetentionPolicy` before requesting removal. The current policy
shape explicitly names record keys in `description.remove` and permits orphan
collection through `description.collect_unreferenced`. The maintenance owner
checks that scope, current selections, required content, shared physical files
and recovery obligations under the publication lock.

`remove_under_policy` records a durable removal intent and changes availability
before deleting authorized bytes. Outcomes record deleted, absent, retained or
failed targets. `resume` continues the same intent after interruption; CLI
`resume-removal` uses the original unit ID. Explicit orphan candidates are a
Python API input, not an automatic scan of the entire store.

Shared files remain until no protected reference needs them. Removing bulk
payloads preserves the ledger identity, digest, provenance and availability
history of every row. A state member has a row only once a publication has
referenced it by identity, so a policy removes its state; an unpinned member stops
resolving with that state. Known unavailable values may read as `None`; loss of
bytes still marked available is an integrity error. Restoration must match the
original canonical digest.

## Compact without changing logical meaning

`CoreStateStorage.checkpoint` materializes a state through its existing native
storage owner. It preserves the state and occurrence identities while replacing
the preferred physical representation. Old bytes become removable only when
explicit policy and shared reachability permit it. Compaction does not run a
processor or create another logical generation.

[Maintenance tests](../tests/test_core_maintenance.py) cover shared references,
actual byte reclamation, exact restoration, interrupted deletion and recovery
protection. [Checkpoint tests](../tests/test_core_checkpoints.py) cover logical
identity across physical changes. Use the [qualification guide](qualification.md)
for assembled regression and capacity evidence.
