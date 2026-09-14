# Core model and plan: consensus record, 2026-09-13

This record tracks decisions, their owners, evidence, and remaining acceptance
checks. The [current adopted decisions](#current-adopted-decisions) supersede
earlier entries where noted. Parties: the owner; Claude Fable 5.1, which reviewed and refined the
spec, reviewed the plan twice, measured the engine and ledger choices, and
wrote this record; and GPT-6, which authored the plan and tasks and ran the
[recursive validation](2026-09-13-core-plan-swarm-validation.md). Versions
at planning closure are pinned below. The [task checklist](../core-model-implementation-tasks.md)
owns subsequent implementation status and evidence; this record owns decisions.

## Decisions

| Decision | Settled by | Evidence | Standing |
| --- | --- | --- | --- |
| The [spec](../core-model.md) is the normative target. Its PROV bindings are correct; one row was corrected ("Specialized Activity" is not a PROV-DM construct) and operation definitions bind as the Plan of an Association. | Fable review against the W3C texts | [Spec review](2026-09-13-core-model-review.md) | Agreed; spec unchanged since the morning refinement |
| Spec refinements: "material" defined once; §6.3 undefined term; §5.6 keyword; §7.1 scoped to adequate declarations; §7.2 pointer; §9.3 "revisions". Fourteen words net. | Owner directed "refine surgically" | Same record, Resolution section | Applied |
| Code binding and gap analysis live in the plan, not the spec. | Owner ruling | — | Applied |
| The plan is judged on what is best, not against prior repository decisions. | Owner ruling | [Plan review](2026-09-13-core-model-implementation-plan-review.md) | Applied |
| Design the resolver and projections first; they drive tool choices. | Fable and GPT-6 agreed | Plan §3.1–3.2 | Applied |
| Revision resolves at two levels: a value patch derives a new occurrence with its own provenance, then membership changes at the key. Last writer per key is the algorithm; PROV-Dictionary fixes the outcome, not the algorithm. | GPT-6 correction, accepted | Plan §3.1 | Applied |
| Projections need more than JSON Pointer: type preservation, absent versus present, composites, an array-index rule, canonical encoding before hashing. | GPT-6 correction, accepted | Plan §3.2; the codec probe shows string extraction erases the number 1 versus the string "1" | Applied |
| DuckDB is the sole bulk engine. | Owner directed a decision; Fable measured | The tested DuckDB query preserved types and used less recorded memory. The Polars path erased types, so the timings are not equivalent semantic work; full-path capacity remains unqualified. | Agreed |
| Standard-library `graphlib` and recursive SQL replace rustworkx. | Fable | The operation graph is a handful of nodes | Agreed |
| SQLite through standard-library `sqlite3`; no native ledger component. | Fable measured; the swarm corrected the measurement; owner accepted the leaner stack | Ledger probe; F13 withdrew the subtraction and extrapolation | Adopted; no custom native component is planned |
| SHA-256 throughout. | Fable; GPT-6 | The existing blob store and Rulespec digests are SHA-256 | Agreed |
| disk-objectstore for local content. | GPT-6; swarm F07 | Source at `ba13ca6`, confirmed below | Superseded the same day; see Simplification below. The existing blob store stays. |
| msgspec for typed records; jsonschema-rs for supplied payloads; duplicate keys rejected by the shared Rulespec decoder at raw admission. | GPT-6; swarm F06 | Confirmed below | Agreed |
| Roles on result bindings; an established-or-uncertain marker on resources; a retention policy record every removal cites; a guarded current selection per dataset. | Fable recommended; owner accepted | Plan §2, §4, §5, §8; the swarm refined generated-key materiality (F01) and adequacy before policy (F02) | Applied |
| No legacy support: no importer, compatibility layer, migration framework, or dual write. | Owner and GPT-6 | Consistent with the to-do's breaking-changes decision | Applied |
| D48 and D54 scope routes to Core tasks C11, C19, C21 and C23. | Swarm F12 | To-do list edited | Applied |

## Relayed claims re-derived by Fable today

| Claim | Origin | Check | Result |
| --- | --- | --- | --- |
| DuckDB 1.5.5 rewrites the lowercase control-character escape for U+001F to uppercase on `json_extract`, scalar and nested; a whole-document cast preserves bytes | Swarm F05 | Ran against the shared canonical encoder | Confirmed |
| msgspec 0.21.1 collapses duplicate keys; so does the standard library | Swarm F06 | Ran | Confirmed |
| The shared Rulespec decoder rejects duplicate keys | Plan §5 | `object_pairs_hook=_reject_duplicate_keys` in `rulespec_artifacts._artifact` | Confirmed |
| disk-objectstore `delete_objects` must run with no other process accessing the repository, stops at the first error, and soft-deletes packed objects; `repack_pack` comments on a missing folder fsync | Swarm F07 | Source at `ba13ca6`, lines 2461–2496 and the `repack_pack` body | Confirmed |
| 25 tasks, 41 dependency edges, acyclic, no dangling references | Swarm audit | Parsed the tasks file | Confirmed |
| Local links resolve | Swarm (344 counted) | 181 local links across the five documents, none missing | Confirmed for those five |
| Spec, plan and tasks hashes equal the swarm's final hashes | Swarm | `shasum -a 256` | Confirmed; the validated text is the committed text |
| Codec probe output is reproducible | Swarm | Reran | Identical |
| The ledger probe timed SQL without commit and Python with commit, and subtracted an aggregate that does different work | Swarm F13 | Read my own script | Confirmed; the subtraction is withdrawn |
| The engine probe's generated removes can hit absent keys and omit full semantics | Swarm | Read my own script | Confirmed |
| Full suite on `3ffff83`: 1,189 passed, 1 deselected | Fable | Ran earlier today; only documents changed since | Stands |

## Earlier assessment before the final adopted scope

At that point there was no decision disagreement. One difference of emphasis:
the swarm treats the engine and
ledger probes as diagnostics of query shapes only; Fable holds that the ledger
probe still shows the whole Python ledger path costing about a second and a
half per million rows, which is why `sqlite3` needs no native binding. Nothing
turned on it under the then-current conditional-native rule. The current adopted
scope removes that rule and plans no native component. The diagnostic numbers
still do not establish production capacity.

## Open items

- C03/C05 must implement and check the shared decoder/encoder path for all new
  or changed JSON values, including extraction and edits. Unchanged admitted
  canonical bytes may be reused. The escape-only shortcut is removed; correctness
  and the bounded Python cost remain acceptance checks on unbuilt code.
- C07/C18 must implement policy-controlled blob deletion and qualify publication
  protection and durability. The existing inventory is read-only; retention
  preview does not implement deletion.
- Capacity, adapter durability, installed-package behavior, and Core
  conformance remain acceptance checks on unbuilt code.

## Versions

Commit `185a981` on branch `docs/core-model`. SHA-256: spec
`977e13b341e1ca7524b85d04e624c1f8ffd3455aac597a53e206d72addc10b81`, plan
`9a46a286a572c2550639cbd98dca31c496c493570fd3911c26cd28432217b28d`, tasks
`6eafa2503ce2b78464ec219d15fef83d9b338e24b0a80f543e2e0e07af208caa`.

## Earlier simplification, 2026-09-13

The owner asked how to simplify and ruled out deferrals: nothing is held
behind a trigger; a component is either needed for the first implementation
or it is out. The following decisions were applied at `d3561a0`. The current
adopted decisions below refine them; this table preserves their earlier scope.

| Component | Decision | Reason |
| --- | --- | --- |
| disk-objectstore | Out; the existing content-addressed blob store stays | Packing's benefit is unmeasured and the existing adapter supports retained bytes and S3. The earlier rationale incorrectly said the store already deleted under retention machinery; the current blob interface has no deletion operation. C07/C18 own that work. |
| `prov` export | Out | Core requires the PROV interpretation to be recoverable, not exported. C24 checks it against the ledger. |
| Authored state ordering, `move` edits, positional insertion | Out; states are dictionaries | Refined below: preserve meaningful positions as ordinary data and consumed-order rules in operation definitions. Sorting by identity cannot recover discarded source order. |
| `state_members` selector kind | Initially out; superseded below | Whole-state dependencies are permitted but reduce selective reuse for field-level bulk operations. The current adopted scope restores this selector. |
| Native component, conditional or otherwise | Out | The shared encoder is the chosen response to the confirmed canonical-bytes gap. Its implementation and full-workload cost still need qualification. |
| PostgreSQL and the rest of §7 | Out | Not needed. Dagster stays because it exists and is used. |
| Plan §8 implementation steps | Out; §8 is now acceptance only | The tasks file already sequences the work. |
| msgspec, Hypothesis | In | The only new dependencies. |

Effect: the plan went from 783 to 591 lines and the tasks from 437 to 413.
C22 is dropped with its identifier retained; C07, C13, C14, C15 and C18 are
trimmed; the conditional native task is removed. The task graph has 24 live
tasks and 37 edges and is acyclic. All 169 local links across the five
documents resolve. The swarm validation's pinned plan and task hashes are now
historical; this simplification's hashes are below. The spec is unchanged.

## Versions after simplification

Spec `977e13b341e1ca7524b85d04e624c1f8ffd3455aac597a53e206d72addc10b81`, plan `430ba669c39480d6541bcaf6281b04fd735e713ec6e845fd7110955e7a3d44d5`, tasks `7c36ee55f3daf0fd9b834c7f8e89f56d3ba47ddca45f6b29db6d02cee9472f25`.

## Current adopted decisions

After reviewing `d3561a0`, GPT-6 recommended the following scope. The owner
accepted it with “Execute.” These are the current plan decisions; they do not
claim implementation completion or a new swarm validation of this revision.

| Area | Adopted decision | Implementation and evidence |
| --- | --- | --- |
| Storage | Keep the existing blob store and S3 adapter. | C07/C18 implement backend deletion, policy authorization, recoverable intent/outcomes, shared/in-flight reference protection, and durability. [Current retention preview](../retention-preview.md) explicitly has no deletion operation. |
| Provenance | No `prov` export library. | Retain the complete PROV interpretation and check it against authoritative records in C02/C09/C24. |
| States and order | Keyed, unordered states; no separate `move` mechanism. | C10/C14/C19 preserve meaningful positions as member data and deterministic sorting rules as material operation configuration. Relevant consumed order remains part of comparison. |
| Bulk dependencies | Keep `state_members` for all or named members. | C05/C14/C17 apply the existing per-member field selector through DuckDB, preserving types, missing members/fields, multiplicity, material keys/identities, and consumed order. A title-only change must not invalidate a bulk URL dependency by itself. |
| Canonical JSON | Use the shared decoder/encoder for all new or changed JSON values. | C03–C05 remove the escape-only shortcut, reuse unchanged admitted canonical bytes, and measure bounded Python work. |
| Native code | No custom Rust component. | Capacity remains an acceptance requirement for the chosen implementation. No native task or trigger is retained. |
| Scheduling | Keep Dagster optional. | C11/C21 use one lifecycle for direct Python calls and scheduled jobs. |
| Planning | One task sequence, with 24 live tasks. | C22 stays dropped; no PostgreSQL, speculative integration, or legacy-support work is added. Each task removes the path it replaces. |

The working stack remains DuckDB, Arrow, sqlite3, the existing blob store, msgspec,
jsonschema-rs, SHA-256, graphlib, and optional Dagster. Hypothesis supports the
behavioral checks. Core conformance, crash recovery, and bounded full-workload
performance are completion criteria, not additional components.

## Verification before C01

Checked 24 live task definitions against their overview, 37 dependency edges
in build order, the isolated C22 stub, and all 173 local links across the plan,
tasks, this record, and documentation index. Whitespace and adopted-scope checks
passed. The spec is unchanged. These are document checks; no runtime, capacity,
or conformance test result is claimed for the unbuilt implementation.

These checks describe the adopted scope committed at `e7c2d70`. The plan and
task hashes were `126bb6ed899e0b103d24a67ee668e565cf3fa3e7b35e2a9ed4c52a0d42d99be5`
and `768b42631b9bacad8fee6672c21d7d49679025c42e458365e91074e6f978304b`.
The current hashes follow the C01 record below.

## Fable review of the adopted scope, 2026-09-13

Reviewed the uncommitted diff against the four corrections. All four are
accepted; the diff implements them coherently and the mechanical checks
re-derive: 24 live tasks and 37 edges, acyclic, C22 isolated; 174 local links
and the `#current-adopted-decisions` anchor resolve; plan tables are
consistent; the plan and task hashes above matched the files reviewed at `e7c2d70`.

- **`state_members` restored.** Right on user value. A bulk operation that
  consumes only member URLs must survive a title-only change, and a whole-state
  binding cannot give it that. The design carries the absent-versus-present
  discipline up one level, distinguishing a missing named member from a present
  member with a missing field, and bounds the encoding by streaming framed member
  encodings into the digest in chunks.
- **Positions as data, sorting rules in operation definitions, no `move`.** A
  correct refinement. The earlier wording that order is derived from keys or
  values would have discarded imported source order silently.
- **Shared encoder for every new or changed value; the escape-only shortcut
  removed.** Accepted. The shortcut rested on eleven fixture cases, not a proof
  over the admitted domain. Its cost is a Python canonical encode per newly
  extracted value on first evaluation, bounded afterwards by change; C03 and C05
  measure it as they should.
- **Blob store deletion and durability.** Accepted, and the correction of my
  claim is right. The `BlobStore` port exposes put, stat, read, read range,
  materialize and verify, and no delete; the adapters unlink only their own
  temporary files; the retention inventory is a read-only preview. I asserted
  that deletion existed without checking the port. C07 and C18 now own it.

One wording drift remained at that review: the §1 DuckDB row
still lists in-engine SHA-256 as part of the decision, while the adopted
canonical rule computes correspondence digests in Python over shared-encoder
bytes. In-engine hashing now serves content-level checks on already canonical
bytes. This was left unchanged at that review to keep its pinned hashes; the
owner subsequently requested the correction and C01 work recorded below.

## Planning closure and C01, 2026-09-13

GPT-6 applied the owner's instruction after Fable's accepted review at `e7c2d70`.
Plan §§1 and 3.3 now assign correspondence hashing to Python's `hashlib.sha256`
over shared-encoder bytes and versioned framing. In-engine SHA-256 serves content
checks on already canonical bytes. No tool choice or spec meaning changed.

C01 delivered the [ownership and acceptance map](../core-model-implementation-map.md):
current and target owners, preserved workflows and checks, direct API/format
retirement, and fixed workload time/memory/scan budgets. D31/D37/D48/D54 route to
the Core owners; maintainability E5 routes to D40. External campaign/search
qualification and the human contribution exercise remain separate. C02 supplies
executable fixtures; C03 and C25 measure the chosen implementation. The new
capacity budgets are engineering targets, not measured performance or a claim
that Fable reviewed this C01 deliverable.

The task list records C01 complete and 23 live tasks proposed. Planning is closed
on the adopted architecture; implementation starts from this map. No runtime
code, dependencies or retained data changed in C01.

## C01 verification and versions

Checked all 24 live task definitions against the overview: 37 prerequisite
edges, acyclic and in build order, C22 isolated, C01 complete and 23 proposed.
The dataset checklist has 47 completed items, four separate open items and five
routing references; maintainability E5 no longer duplicates D40's open task.
All 475 local links across the nine affected documents resolve, including 76
fragment references. Whitespace checks pass. These are document and source-map
checks, not runtime, conformance or capacity qualification. The spec is unchanged.

These hashes pin the C01 planning snapshot. Later implementation updates to the
task checklist do not change that historical verification.

| Artifact | SHA-256 at C01 completion |
| --- | --- |
| core-model.md | `977e13b341e1ca7524b85d04e624c1f8ffd3455aac597a53e206d72addc10b81` |
| core-model-implementation-plan.md | `20e50c1eab1dbdfa5f914924a779d00544443a3665a17d8ba4ece2daa030ce14` |
| core-model-implementation-tasks.md | `782bc7fe5dfbf72e8af94c7193cedfc05971a088027e18f58e1a88535be5a7fe` |

## Subsequent storage decision, 2026-09-14

The owner requested adoption of DuckDB's Iceberg writer after the two writer
probes. The [storage guide](../record-storage.md) records the resulting single
backend: DuckDB writes data and positional deletes, PyIceberg handles metadata
and temporary REST catalog registrations, and SQLite retains logical publication,
provenance and retention ownership. This supersedes earlier descriptions of the
custom Parquet file inventory and bucket replacement. Earlier pinned hashes in
this record remain historical; the spec is unchanged. The [task list](../core-model-implementation-tasks.md)
and [production measurement](probes/2026-09-14-iceberg-core-writer.json) record the
implemented behavior and limits.
