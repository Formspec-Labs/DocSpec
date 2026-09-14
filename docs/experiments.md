# Retained experiments and current selection

An experiment imports a complete keyed input state and resolves operations over
it. Each actual execution has its own identity; reuse selects an existing result
with an explicit association to the current request. Operation definitions pin
implementation, settings and resources. Correspondence is an eligibility check,
not a guarantee that every matching historical result remains available.

Use `CoreWorkspace.documents` for document work and `workspace.operations` for
general operations. Run capture first with `extract=False, segment=False`, then
run the same source state with extraction, segmentation and chosen processors.
The common evaluator can reuse retained successful stages. `fresh=True` requests
new work; a changed definition or material input makes unaffected earlier stages
independently eligible for reuse.

Retaining a result and selecting it as current are separate actions. Supply a
`dataset` to the document run when the resulting state should be selected, or
call `workspace.maintenance.select_current` with an explicit expected prior
value. Alternatives preserve their original execution history.

A new run ID names a new requested experiment. Repeating an existing selection
recovers that exact selection when its request still agrees. Explicit operation
suspension and continuation preserve one actual attempt; starting again after a
failure produces another attempt. See [operations](operations.md),
[inspection](inspection.md), and the [offline example](offline-walkthrough.md).
