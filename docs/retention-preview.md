# Inspect retention and authorized cleanup

Retention and availability are recorded per Core record. Current selection is
one protected root, while retained results, required observations, selected
values and recoverable operations can also require shared bytes.

Use `workspace.inspect(kind, id)` to read recorded status. For removal, retain a
Core `RetentionPolicy` with an explicit record-key scope, then call
`workspace.maintenance.remove_under_policy`. The maintenance owner inventories
state and selected-value files, current bulk sources, direct content references
and recoverable checkpoints/publications under the publication lock.

Explicit orphan candidates can be supplied through the Python API when the
policy allows unreferenced collection. That inventory is shared with portable
export; callers should not build another reachability model or infer garbage
from a directory listing. A file shared with an available record stays protected.

The current implementation performs authorized removal and records durable
per-file outcomes. It has no separate blob-store dry-run command or imported
retention-set format. Use [operations](operations.md) for the removal and resume
sequence, and [maintenance tests](../tests/test_core_maintenance.py) for shared
bytes, interruption and restoration behavior.
