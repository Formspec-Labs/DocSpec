# Pre-review hypotheses

Recorded before inspecting REVIEW.md, at DocSpec 40e891ccece79495f7ab850d441ed8633be73449.

- H1 (medium): Atomic catalog selection plus protected immutable preparation can provide one durable authoritative commit boundary without a distributed transaction. Check how roots and native snapshots interact with concurrent reclamation and recovery.
- H2 (medium): Monotone per-input coverage and exact version bindings may not by themselves establish a semantically consistent multi-input result; inspect treatment of related sources, derived inputs and strict cutover.
- H3 (medium): Shared evidence and independent field groups can preserve occurrence multiplicity and attribution if correspondence is stable under deletes, updates, compaction and one-to-many transformations. Inspect explicit model requirements before alleging a gap.
- H4 (medium): Completed-phase reuse needs a durable operation identity and explicit handling of ambiguous acknowledgements to preserve failed-attempt history and avoid duplicate external effects. Check linked recovery tasks.
- H5 (medium): Retention roots are sufficient only if acquisition, transfer, selection and deletion use an atomic exclusion/fencing protocol; inspect linked retention plan.
- H6 (low): The proposal may duplicate a native storage engine data model without a user-visible benefit; compare native column groups and catalog responsibilities on their own terms.

Evidence motivating all hypotheses: target README.md lines 15-29, 45, 66-80, 88-94, and the requested concurrency/recovery review lens. No historical review conclusions inspected.
