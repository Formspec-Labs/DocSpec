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
