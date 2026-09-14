# Core version 1 acceptance fixtures

The production declarations are [Core records](../../../src/docspec/domain/core.py);
the schema comes from those exact types. [Admission](../../../src/docspec/domain/core_admission.py)
uses the shared canonical decoder before strict typed conversion. Python callers
can supply records or mappings to `encode_record`; persisted wire input must
include `format_version: 1` and use canonical JSON bytes. The value codec refuses
floats, invalid Unicode and foreign Python values before they can be coerced.
Schemas describe structure; record-local and cross-record semantics have their
named admission owners.

`lifecycle.json` supplies independent logical records and expected relationships.
It is formatted for review, not a serialized Core artifact. Defaults may be filled
when a fixture record is encoded; known-answer bytes in the record tests pin the
wire format. The record's ID is a logical reference, never an implicit content
digest, member key, request ID, or attempt ID. Selection IDs are distinct from
selected-value IDs.

| Required distinction | Fixture or executable check |
| --- | --- |
| Immutable occurrence versus stable member key; equal values at distinct keys | `o1` → `o2` at `s1/a` → `s2/a`; `o3` and `o4` both hold integer 1 |
| Logical state versus physical checkpoint | `State` and `StateRepresentation`; `test_state_has_known_canonical_bytes_and_no_physical_identity` |
| Explicit edit order and immutable value edit | `r1` and execution `xp`; reference-model invalid-prefix and generated-history checks |
| Implementation identity/version, effective configuration, material resources | `capture`, `patch`, `adopt`; resource uncertainty/supplement test |
| Request versus fresh attempt versus exact reused result | `q1` has `x1` and `x2`; `q2` selects `c1`, preserving `x1` and its original `o1` origin |
| Equal content without collapsed entities or executions | `a1` and `a2` share the exact empty-byte digest but have separate generation events |
| Contextual raw/derived roles and new/adopted outputs | `c1` returns new `a1` as raw; `ra` adopts that same `a1` as derived without another generation |
| Input retention scope versus narrower declared dependencies | `test_narrow_dependencies_do_not_change_whole_or_state_input_bindings`; expected bound values in `lifecycle.json` |
| Direct selected-value retention versus recovery from retained parent | `url1`/`url2`; `test_state_member_origins_and_parent_recovery_are_retained_separately_from_comparison` |
| Whole/field/member-field definitions, origin, absent/null/types/composites | `selected-values.json`; selected-member and exact type reference checks |
| Membership multiplicity, material keys/identities, consumed order and array positions | Reference checks for duplicate roots, renamed/swapped members, ordered keys and reevaluated array indices |
| Explicit success with outputs, empty or null; failed, interrupted or incomplete | Outcome parameter cases and invalid-combination tests in `test_core_records.py` |
| Generation, usage and established derivation remain distinct from declarations | Only `rp` asserts a derivation. Capture declares/uses its source without fabricating derivation; selection creates no execution |
| Self-dependence, cycles, conflicting generation and stream timing | Independent provenance oracle refusal cases; partial-stream usage names `chunk`, never not-yet-generated `complete` |
| Qualified derivation refers to the matching events and their order | `rp` names `gp` and `up`; independent qualified-event refusal cases |
| Explicit retention policy and preserved original dependency evidence | `keep-current`, `omission`, `supplement`; supplements are separate records |

The PROV interpretation is explicit: occurrence/artifact IDs are Entities; state
IDs are Collections and Dictionaries; execution IDs are Activities. Definition
IDs identify Entities serving as the Plan of an Association with an execution.
Membership rows map to dictionary membership. Generation, usage and derivation
records carry only established relations. Requests, selections, resources and
dependency declarations alone assert none of those events. C09 validates incoming
relationships against retained records; C24 checks the complete recoverable
interpretation. This fixture suite is not a general PROV validator or a completed
runtime conformance claim.

The independent eager model and Hypothesis generators live in `tests/support/`.
They import no production algorithms. They deliberately materialize small inputs;
the production engine must remain bounded. `core_workload.py` streams the fixed
C01 capacity data, with seven-digit keys, 1,024 duplicate-value pairs and the
eight-case typed metadata cycle. Its small tests establish reproducibility, not
8 GiB workload capacity.

Normative semantics come from the [Core spec](../../../docs/core-model.md),
[PROV constraints](https://www.w3.org/TR/prov-constraints/), and the adopted
[JSON Pointer](https://www.rfc-editor.org/rfc/rfc6901) and
[JSON Patch](https://www.rfc-editor.org/rfc/rfc6902) bindings. In particular, JSON
Patch's extra operation members are ignored; other fixed-record extra fields
refuse. Event order and explicit membership-edit order are separate concepts.
