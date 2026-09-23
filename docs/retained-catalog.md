# Read retained states and their evidence

`CoreWorkspace.rows(state_id)` streams admitted occurrence records in key order.
`workspace.inspect(kind, identity)` reports recorded retention, availability,
requests, selected results and execution progress. Close partially consumed
iterators to release their storage resources.

Inspection is scoped to the requested record and recorded output status; it does
not certify every byte in the workspace. A state read follows its admitted
physical representation and validates the data it consumes. Publication and
current selection use the shared publisher's required-content checks.

A record whose payload was removed under policy can retain its identity, digest
and provenance while its value is unavailable. A state member that no publication
has referenced has no record of its own: it reads through its state's entity
layer, and stops resolving once that state is removed. Data still marked
available must exist and pass integrity checks. Neither a descriptor nor an old successful test
is fresh proof of complete physical availability.

Use a pinned [result export](result-exports.md) for read-only independent access
without the original workspace. Source-catalog admission and full policy
verification retain their separate [catalog reader](catalog-evidence.md).
