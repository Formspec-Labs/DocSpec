# Independent hypotheses recorded before historical review

Target: DocSpec 40e891ccece79495f7ab850d441ed8633be73449. Static reliability and integration review. No peer results read.

- H1 (medium): Catalog commits plus immutable native snapshots can preserve acknowledged results through worker failure, provided publication intent and native retention protection cover every pre/post-commit crash position. Test detailed R1/R2/R6 plans.
- H2 (medium): Public-release correctness depends on authoritative storage-boundary fencing and a disclosure boundary over physically shared objects, not just logical selected rows. Test release plans and consumer admission.
- H3 (medium): Restore points, active readers, completed phases, and releases can share one retention inventory if deletion and reference creation use one exclusion rule; stale readers and native format cleanup may reveal a split authority.
- H4 (medium): Reusable phase completion requires typed exact identity, trust and validation scope; resource refusal must preserve the expensive completed phase and have a supported operator continuation.
- H5 (low): Adoption gates may leave consumer compatibility or acknowledged failure domain open as explicit product decisions, which alone is not a defect; identify only internal contradictions or an unowned necessary seam.
- H6 (medium): Greenfield field-group/version choices must preserve complete Core outcomes and occurrence/evidence semantics without inheriting current Python/storage design.
