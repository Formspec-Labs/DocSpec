# Preserve required experiment inputs in a bounded storage preview

The existing retention service missed two real dependencies. Selecting or
retaining a successor opens its predecessor, whose verification requires its
own active bytes. A shorter successor can omit those bytes from its active
rows. A standalone planned store can also require its plan's captured or
processed base before it contains any of those bytes itself.

The selected approach extends that service to follow required predecessors
iteratively and deduplicate exact visits in its existing bounded workspace.
Release roots supply their saved plans. Standalone stores require explicit plan
references because a store holds only a plan ID; no new plan lookup registry is
introduced. Retention-set 2.0 records those explicit inputs.

The alternative was to remove predecessor admission from selection/retention.
That could change the evidence promised by those operations. It is unnecessary
to make the current dependency inventory accurate, so this slice retains the
existing admission behavior. Conservatively retained predecessor bytes may
therefore exceed the chosen result's active bytes; the preview explains why.

The public runtime composes the existing service and storage adapters. One
inventory implementation moves out of the CLI and serves both callers. The
existing local blob-profile state writer is shared with run preparation.
Execution plugins, automatic root discovery, deletion, background cleanup,
and another lifecycle system are outside this implementation.

The independent solutions architect approved this approach and the explicit
scope revision to D25: verified blob dependencies for supplied immutable roots
and a repeatable read-only inventory. Destructive pruning was never implemented
and is not claimed complete. The imported-set reader checks supplied rows and
bytes, not a second derivation of all root dependencies. Caller-visible scope
and documentation preserve that distinction.

Validation exercises the actual predecessor-admitting selection operation,
planned work resumed from retained captures, sibling alternatives sharing a
base, and interruption followed by a repeatable preview. Any fixture deletion
is limited to the test's temporary dataset. Execution results and independent
code review are recorded separately.
