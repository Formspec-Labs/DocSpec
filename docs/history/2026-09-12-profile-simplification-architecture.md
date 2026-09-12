# D32: remove unenforced storage-profile governance labels

Remove `ProfileGovernance`, `governancePolicies`, their default allowlist, and
the registry arguments that extend that allowlist. The solutions architect
reviewer approved this scope on September 12 before implementation.

The five values name access, encryption, region, retention and redistribution
policies. Current code only checks their spelling, allowlists them and hashes
them. All ten packaged profiles repeat placeholder values. No runtime behavior
uses them to enforce those policies. Changing a label therefore invalidates
saved profile and plan identities without changing storage behavior.

Keep the physical role, implementation, configuration, schema, capabilities,
limits, dependencies and complete description pin. Keep secret checks and the
separate, implemented plan data-use and retention policies. Deployment access,
encryption and location settings belong to the selected storage implementation
and its deployment; DocSpec should not imply that a repeated label enforces them.

Update the closed `docspec-storage-profile` description to version `2.0`, change
all current packaged descriptions, and refuse the old shape directly. Physical
implementation versions remain unchanged. No legacy parser, governance resolver,
new policy registry or enforcement subsystem is needed.

The alternative was to implement resolution and enforcement for the labels.
No current caller requires that feature, so it would add configuration and
machinery unrelated to the document experiment workflow. Keeping the unused
labels also imposes unnecessary configuration and identity changes.

Qualification should cover current profile loading/selection, old-shape refusal,
real limit changes affecting pins, invalid configuration/dependency refusal
before writes, and the installed runtime's current profile resources. Replace
the obsolete governance assertion and its conformance selector; retain existing
meaningful profile checks. This slice does not finish D32's cache/declaration
inventory. Independent code review remains separate from architecture approval.

The implementation removes the class, allowlist and arguments; all ten packaged
descriptions now use format `2.0`. The focused gate passed 89 tests, covering
profiles, conformance, CLI, workspace, read-only storage, worker identity and
package boundaries, including an actual installed runtime/export exercise.
Ruff and diff checks pass. The preceding full suite passed 1,071 tests before
this change. Independent code review is pending: the subagents reached their
usage limit after architecture approval and before this implementation.

## Remove cache declarations that do not control the cache

The next D32 slice removes `ExecutionProfile.cache_profile` and `cache_state`,
the two artifacts written on every preparation, and their generic artifact-list
property. Execution profile format `3.0` directly pins its worker composition,
task-index bound, and deadline. Existing formats are refused, without migration.
This decision is the primary agent's judgment; independent review is pending.

Before the change, `runtime/preparation.py` always emitted a constant adapter
description and a configuration-only observation. Neither constructed, restored,
or measured the cache. The database path was derived from the reconciliation root
already recorded in the worker composition. Capture-only runs emitted these
artifacts even though their actual processor cache was `None`.

The simpler implementation derives that path once in `runtime/composition.py`.
It keeps `LocalSqliteProcessorResultCache` and the existing processor cache port:
those save real computation across runs. `application/processor_runtime.py`
verifies cached immutable results, recomputes missing or invalid results, and
falls back to processor execution when the cache is unavailable. Mutable cache
contents are not evidence of completed work. The scale report's observed cold
and warm cache conditions remain meaningful and are outside this removal.

Keeping the two declarations only for processor runs would reduce unnecessary
writes but retain duplicate facts with no behavior. Building a cache snapshot
or configuration system would add scope without a current caller. A future
cache implementation belongs at the existing injection point; its performance
settings should not alter document-result identity.

Worker bytes are still verified before execution, reconciliation, and retention,
and saved recovery compares them with the reconstructed worker. The former
cache-description tampering test now verifies refusal of changed worker bytes
before any handler call. Existing tests cover cross-plan cache hits, invalid
result repair, concurrent winners, and cache outages.

The focused cache, execution, worker-identity, inspection, local experiment,
package, and installed native Dagster gate passed **76 tests in 55.75 seconds**.
Ruff and whitespace checks passed. Independent review remains pending.
