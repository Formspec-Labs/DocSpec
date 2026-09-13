# Core model and plan: consensus record, 2026-09-13

One page saying what was decided, by whom, on what evidence, and what stays
open. Parties: the owner; Claude Fable 5.1, which reviewed and refined the
spec, reviewed the plan twice, measured the engine and ledger choices, and
wrote this record; and GPT-6, which authored the plan and tasks and ran the
[recursive validation](2026-09-13-core-plan-swarm-validation.md). Versions
are pinned at the end. Nothing here is implemented; the 25 tasks are proposed.

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
| SHA-256 throughout. | Fable; GPT-6 | disk-objectstore is SHA-256 only | Agreed |
| disk-objectstore for local content, with maintenance restrictions taken from its source. | GPT-6; swarm F07 | Source at `ba13ca6`, confirmed below | Agreed |
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

- Plan §7 says a future PostgreSQL backend owns "migrations" while the plan
  excludes migration frameworks. One word. Left as is to keep the swarm's
  pinned plan hash.
- F05's confirmed gap, canonical bytes from engine extraction, is resolved by
  design in C03 and C05, not yet in fact. One shape to evaluate there: compute
  declared projections' canonical bytes at admission, where each record is
  already canonically encoded in Python, and re-encode in Python only the
  extracted values that contain a control-character escape.
- Capacity, adapter durability, installed-package behavior, and Core
  conformance remain acceptance checks on unbuilt code.

## Versions

Commit `185a981` on branch `docs/core-model`. SHA-256: spec
`977e13b341e1ca7524b85d04e624c1f8ffd3455aac597a53e206d72addc10b81`, plan
`9a46a286a572c2550639cbd98dca31c496c493570fd3911c26cd28432217b28d`, tasks
`6eafa2503ce2b78464ec219d15fef83d9b338e24b0a80f543e2e0e07af208caa`.
