# Retry failed items without repeating valid completed stages

Decision: use the existing plan selection and execution modes to repair accepted
failures. Reuse complete, compatible capture, extraction, segmentation, and
processor results. Keep the prior retained result as the failure history. This
implements [D16](../dataset-experiments-todo.md#d16) without another run ledger
or a second execution loop.

The solutions architect and independent reviewer agreed on these boundaries
after tracing current planning, retained dispositions, checkpoint validation,
and base reprocessing. The implementation follows this approach; the
[independent review](2026-09-11-failed-item-repair-review.md) records the assessed
code and executed checks separately.

## Explicit retry intent

`selection.retryFailures` has three values: `none` (the default), `transient`,
and `selected`. Existing selection fields choose the population. `transient`
admits temporary failures in that population; `selected` explicitly admits its
accepted failures, including deterministic failures. `RetryPolicy` continues
to govern retries inside an invocation. Changing successor retry selection must
not invalidate healthy outputs or rewrite the meaning of an invocation policy.

Source changes and applicable governing changes still schedule new work.
Changes to inputs or settings needed by unfinished work can admit a repair.
Changing independent processor B does not automatically retry unchanged
deterministic failure A. Keep that entire item's old result, policy, and failure
until repair is admitted. Per-item inspection makes those retained settings
visible. Running B while carrying A's failure into a partly changed item would
require broader per-processor failure semantics and is outside this change.

Removing the failed processor, or stopping before its failed stage, can produce
a successful result from the complete prefix. The new result then has no
terminal failure; the old retained result preserves its history.

## Preserve final classification and derive completion

Add `terminalFailure` to the existing disposition. It is the entry's last
`FailureRecord` only when the entry finishes in failure; successful entries use
null even when earlier attempts failed. Validate classification and its link to
the retained failure evidence. Unordered failure rows and candidate-local attempt
numbers cannot reliably identify the last failure.

Do not add a persisted failed-stage flag. Verified candidate coverage,
extraction receipts, segmentation receipts, and complete processor invocation
sets establish the completed prefix and unfinished work. Empty segmentation
requires its receipt. A processor is complete only when all required segment
invocations completed.

The planner records the chosen existing `EntryExecutionMode` and exact processor
subset before saving work. It streams retained evidence through the existing
bounded temporary record workspace. It must not repeatedly scan every layer
for each failed source or silently choose a deeper prefix during execution.

## Reuse only complete, verified work

Keep the deepest prefix compatible with the new settings. Rerun incomplete or
changed processor nodes and their dependents; preserve complete unaffected
nodes. The existing base helper and checkpoint verifier still check immutable
bytes, output relationships, and actual selected implementations. Missing
coverage may mean incomplete work; contradictory identities or damaged promised
evidence must refuse admission rather than become an implicit full rebuild.

Discard unfinished downstream outputs from the successor. Do not copy old
attempt or failure records into its accounting. Existing `reused-base` receipts
identify inherited processor results, while current attempts consume the new
store's budget. Interrupted repair restores current charges once and preserves
the planned mode and processor subset. Arbitrary partial-stage reuse would
need finer accounting and restart semantics; complete-prefix repair avoids that
additional machinery.

Rejected runs remain ineligible as retained bases. Accepted failures can be
retained and repaired. Skipped failures remain visible in the active result;
repaired failures remain inspectable through their original retained reference.
