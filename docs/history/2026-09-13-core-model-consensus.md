# Core model and plan: consensus record, 2026-09-13

One page saying what was decided, by whom, on what evidence, and what stays
open. Parties: the owner; Claude Fable 5.1, which reviewed and refined the
spec, reviewed the plan twice, measured the engine and ledger choices, and
wrote this record; and GPT-6, which authored the plan and tasks and ran the
[recursive validation](2026-09-13-core-plan-swarm-validation.md). Versions
are pinned at the end. Nothing here is implemented; the 24 tasks are proposed.

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
| DuckDB is the sole bulk engine. | Owner directed a decision; Fable measured | Engine probe: equal speed within noise, about a tenth of the memory, typed extraction and SHA-256 in the engine, no new dependency; the swarm narrowed the claim to the tested query shape | Agreed |
| Standard-library `graphlib` and recursive SQL replace rustworkx. | Fable | The operation graph is a handful of nodes | Agreed |
| SQLite through standard-library `sqlite3`; no native ledger component in the foundation. | Fable measured; the swarm corrected the measurement | Ledger probe; F13 withdrew the subtraction and extrapolation | Agreed; both sides admit native code only on a measured gap |
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

## Residual disagreement

None on decisions. One difference of emphasis: the swarm treats the engine and
ledger probes as diagnostics of query shapes only; Fable holds that the ledger
probe still shows the whole Python ledger path costing about a second and a
half per million rows, which is why `sqlite3` needs no native binding. Nothing
turns on it, because the plan admits native code only on a measured gap.

## Open items

- F05's confirmed gap, canonical bytes from engine extraction, is resolved by
  design in plan §3.3 and by fixtures in C03 and C05: extracted values pass
  through the shared canonical encoder where they contain a control-character
  escape. The fixtures decide whether that filter is complete across the
  admitted domain.
- Capacity, adapter durability, installed-package behavior, and Core
  conformance remain acceptance checks on unbuilt code.

## Versions

Commit `185a981` on branch `docs/core-model`. SHA-256: spec
`977e13b341e1ca7524b85d04e624c1f8ffd3455aac597a53e206d72addc10b81`, plan
`9a46a286a572c2550639cbd98dca31c496c493570fd3911c26cd28432217b28d`, tasks
`6eafa2503ce2b78464ec219d15fef83d9b338e24b0a80f543e2e0e07af208caa`.

## Simplification, 2026-09-13, later

The owner asked how to simplify and ruled out deferrals: nothing is held
behind a trigger; a component is either needed for the first implementation
or it is out. Decided on that rule and applied to the plan and tasks:

| Component | Decision | Reason |
| --- | --- | --- |
| disk-objectstore | Out; the existing content-addressed blob store stays | Its deletion is non-atomic, needs exclusive access and soft-deletes, so the plan had to wrap it in a maintenance gate. The existing store already deletes under retention machinery and has an S3 adapter. Packing's benefit is unmeasured. |
| `prov` export | Out | Core requires the PROV interpretation to be recoverable, not exported. C24 checks it against the ledger. |
| Authored state ordering, `move` edits, positional insertion | Out; states are dictionaries | DocSpec's states are keyed and sorted by identity; order is derived, never authored. The initial binding is the Keyed-State Profile. |
| `state_members` selector kind | Out | A whole-state dependency binds the state as a whole value, which Core §6.2 permits as a conservative declaration. |
| Native component, conditional or otherwise | Out | No measured gap. The canonical-bytes gap is resolved with the shared encoder. |
| PostgreSQL and the rest of §7 | Out | Not needed. Dagster stays because it exists and is used. |
| Plan §8 implementation steps | Out; §8 is now acceptance only | The tasks file already sequences the work. |
| msgspec, Hypothesis | In | The only new dependencies. |

Effect: the plan went from 783 to 591 lines and the tasks from 437 to 413.
C22 is dropped with its identifier retained; C07, C13, C14, C15 and C18 are
trimmed; the conditional native task is removed. The task graph has 24 live
tasks and 37 edges and is acyclic. All 169 local links across the five
documents resolve. The swarm validation's pinned plan and task hashes are now
historical; current hashes are below. The spec is unchanged.

## Versions after simplification

Spec `977e13b341e1ca7524b85d04e624c1f8ffd3455aac597a53e206d72addc10b81`, plan `430ba669c39480d6541bcaf6281b04fd735e713ec6e845fd7110955e7a3d44d5`, tasks `7c36ee55f3daf0fd9b834c7f8e89f56d3ba47ddca45f6b29db6d02cee9472f25`.
