# Reference experiment and pinned phrase processor

The solutions architect and independent reviewer agreed to keep the useful
processor example-scoped. The current `ProcessorDescription`,
`ProcessorResourceIdentity`, request/result verification, retained-prefix reuse
and inspection interfaces already support the experiment. No new processor
registration, resource loader, result-builder API, scheduler or saved ledger is
needed. Dagster remains the owner of scheduling and execution management.

The matcher accepts digest-verified immutable JSON reference data and finds
literal mentions with a fixed Unicode boundary/overlap rule and configurable
case sensitivity. It preserves original quotes and segment byte offsets, with
unchanged enclosing captured-source evidence. It does not infer narrower raw
source coordinates for transformed representations or claim domain meaning.
Resource, input, match count, output bytes and duration are bounded; overflow
refuses instead of silently dropping results. No matches produces a successful
empty match list, distinct from absent or failed work.

The four-document reference workflow uses public supplied records and actual
local-file transport. One document is excluded by run selection. One selected
file is initially absent, producing a recorded accepted transient failure; after
its exact expected bytes are materialized, explicit retry repairs only that
item. The initial result remains inspectable. Later processing and two processor
alternatives reuse exact upstream output. A clean run checks the changed-resource
output values, and saved-handoff recovery checks completed work reuse.

The old example's synthetic Federal Register adapter, URL-relabeling fetcher,
manual plan/request files and CLI commit assembly are removed. The focused HTML
representation example and detailed installed-runtime probe use supplied records
and local transport directly. The installed-package test also executes the actual
copied reference example, without checkout, test-helper or sibling imports.

RefSpec is a possible later resource source, not a requirement or claimed
qualification. A built-in matcher would add a supported production commitment
without a current caller; a larger taxonomy or pattern language would exceed the
example's value. Literal mention review demonstrates the processor seam while
keeping domain interpretation outside DocSpec's execution core.

The implementation is qualified by the parent-owned focused/full gates and
independent static review. A successful synthetic example does not establish
live resource authority, provider completeness, scale, or package publication.
