# Core implementation plan: recursive validation

Reviewed 2026-09-13 against checkout `3ffff839b410753198379115d08c64cd47f6238d`
and the working-tree Core plan and task list. Three independent review agents
and the primary reviewer performed initial analysis, peer review of findings,
correction, and revised-document validation.

**Verdict: APPROVE the corrected plan for implementation.** The reviewers found
no remaining substantive inconsistency within their checked scopes. The selected
stack and 25-task scope remain. Legacy compatibility, workspace migration, a
second bulk engine, and an unconditional native component remain excluded.
The Core spec is unchanged.

This is design and evidence validation. Production capacity, adapter durability,
installed-package behavior, and Core conformance remain implementation acceptance
checks. In particular, the native canonical encoding path has not been qualified
by this review, and the planned disk-objectstore/prov dependencies are not yet
pinned and installed in the production package.

## Findings and applied corrections

Every concern below was corrected in the plan or task list and rechecked.
Locations identify the corrected sections; earlier wording and initial hashes
are recorded for traceability below.

| Finding | Evidence and consequence | Applied correction and owner |
| --- | --- | --- |
| F01 — Generated keys can be material | The old absolute exclusion contradicted Core §§6.1–7.1 and the plan's explicit key-sensitive selections. An operation returning generated keys can observe their change. | Plan §3.1 makes generated keys irrelevant by default but includes them whenever material. C14 checks key-sensitive correspondence. |
| F02 — Policy cannot establish adequacy | Matching unknown resource-version labels is not evidence that material resources correspond. Core §§5.2, 6.2, 7.2 separate evidence from policy. | Plan §5 and C16 require adequate evidence before eligibility; policy may reject an eligible result but cannot waive a material gap. |
| F03 — Recovery is not always the same attempt | Reconciliation of an uncertain publication commit differs from invoking a producer again after losing its output. Core §4.1 preserves particular attempts. | Plan §4 and C11 distinguish publication retry, verified continuation, and a new producer attempt. Earlier attempts remain recorded. |
| F04 — JSON Patch has its own member rules | RFC 6902 §4 ignores operation members not defined for that operation; fixed Core records reject unknown fields. Applying one rule indiscriminately breaks valid patches. | Plan §§3.1, 5 and C04/C12 state and test the exception. Duplicate-key rejection still applies. |
| F05 — Engine extraction does not prove canonical bytes | DuckDB 1.5.5 changes a control-character escape from lowercase to uppercase during extraction. Values remain equal, but bytes and SHA-256 digests differ. The original engine probe also omits edit validation and meaningful order. | Plan §3.3 narrows the benchmark claim. C03/C05 require canonical encoding or verified byte equivalence before hashing, including strings and nested values. |
| F06 — Fast decoders do not supply every admission check | The bounded probe shows DuckDB accepts malformed Pointer escape `/x~2` as absent, while msgspec collapses duplicate object keys. Post-decode validation cannot recover the duplicates. | Plan §§3.2, 5 and C03/C04/C14 require Pointer syntax validation and duplicate-key rejection before information is lost. |
| F07 — Content maintenance needs a concrete recovery boundary | The reviewed library source requires exclusive access for deletion and allows partial completion; destructive repacking contains a directory-sync caveat. | Plan §4 and C07/C18 require method-specific access restrictions, durable deletion intent/outcomes, protection outside the authorized scope, and separate qualification before enabling destructive repacking. |
| F08 — A selected PROV subgraph may exceed memory | `prov` builds an in-memory model. Selecting one large state does not establish a memory bound. | Plan §2 and C22 require record/byte budgets and explicit refusal without silently truncating membership or change claims. No new paging framework is required. |
| F09 — Failure and later evidence need authoritative writes | Successful-result publication alone cannot record pre-publication failures, later omissions, or policy definitions. Keeping old control files for these would leave two owners. | Plan §4 and C08 name bounded internal writes in the same ledger, with stable identities and explicit visibility/retry rules. Imported states share admission without invented execution. |
| F10 — Processor ordering is not provenance validity | Core §4.3 prohibits generation self-dependence/cycles; sorting operation definitions does not check entity relationships across new and retained records. | C02 provides invalid-provenance fixtures; plan §4 and C09 validate actual relationships at admission, independently of conservative dependency traversal. |
| F11 — Root admission is a lifecycle prerequisite | A retained capture result needs its originating occurrence under Core §4.2. The earlier task order promised capture before providing production root admission. | C10 now admits roots through C09; C11 then implements the common lifecycle. Subsequent references and all dependency edges were rechecked. |
| F12 — Superseded work remained actionable | C08 still required schema upgrades despite the no-legacy decision. D48/D54 still directed a separate Dagster-centered lifecycle. | C08 now checks initialization/reopen/unsupported-version refusal. D48/D54 identify their superseded scope and route shared work to C11/C19/C21/C23. External search qualification remains separate. |
| F13 — The ledger probe did not bound native savings | SQL commit was outside its timer while Python commit was inside. The subtracted aggregate query performs different work, so subtraction is not a measured insertion floor. | Corrected both timing boundaries, removed the savings bound, and withdrew the million-document extrapolation in plan §8. The prior review now links this correction. |

## Artifact summary and lineage

The product outcome is complete, inspectable dataset revisions and selective
reuse through one common lifecycle, with bounded DuckDB/Arrow data flow and less
duplicated document-specific machinery. The plan selects the mechanisms; the
task list assigns implementation, checks, and retirement work. The review is
proof infrastructure serving that product outcome.

| Artifact | Relationship and authority |
| --- | --- |
| [Core spec](../core-model.md), §§1–8 and optional §9 | Normative target; constrains the plan and conformance claims. |
| [Implementation plan](../core-model-implementation-plan.md) | Initial binding, selected stack, publication rules, and capacity requirements. |
| [Implementation tasks](../core-model-implementation-tasks.md) | Delivery dependencies and acceptance criteria; all remain proposed. |
| [Current-code comparison](2026-09-13-core-model-implementation-comparison.md) | Prior implementation evidence and consolidation targets, not normative authority. |
| [Earlier plan review](2026-09-13-core-model-implementation-plan-review.md) | Decision history; its broad engine and ledger savings claims are superseded here. |
| [Canonical JSON](../canonical-json.md) | Existing admitted-value and byte-encoding rules reused by the new binding. |
| [Contributor guidance](../../CONTRIBUTING.md) | Existing ownership, direct caller updates, and validation workflow. |
| [Dataset checklist](../dataset-experiments-todo.md), D48/D54 | Overlapping earlier execution work, explicitly redirected to the Core tasks. |
| Owner instructions in this conversation | Simplify, use bounded bulk processing from the start, add later capabilities through the same implementation, and exclude legacy support. |

Existing code is evidence for what must be replaced or preserved. It does not
make an old API, storage format, or Dagster ownership decision mandatory.

## Relationship and invariant checks

| Component or boundary | Owner | Inputs and consumers | Checked commitment |
| --- | --- | --- | --- |
| Admission and encoding | C04/C05 | Raw JSON, Python values, retained representations; consumed by all operations | One accepted-value rule set; preserve presence, type, identities, and canonical bytes. |
| State and selected-value evaluation | C10/C12–C15 | Retained values, membership, ordered edits, definitions | Recover complete retained states; preserve order/multiplicity and immutable occurrences. |
| Logical metadata and publication | C08/C09 | Content readiness, execution records, evidence, selections | One authority; actual provenance checks; no successful-retention claim before required data is recoverable. |
| Physical content and maintenance | C07/C18 | Immutable bytes and authorized removal scope | Share physical content without merging logical identities; protect remaining retention commitments. |
| Correspondence and selection | C16/C17 | Definitions, adequate dependency evidence, availability, policy | Separate eligibility, policy, availability, and the exact historical selection. |
| Execution and consumers | C11/C19–C21 | Admitted roots/artifacts, application procedures, direct callers, optional jobs | One semantic lifecycle; preserve document functionality and optional adapter boundaries. |
| Standards-facing export | C22 | Authoritative records in a bounded requested scope | Export established meaning; Core conformance is independent of serializer support or export size. |
| Consolidation and qualification | C01/C23–C25 | Existing paths, independent fixtures, assembled implementation | Remove duplicate owners; prove behavior and measure full-workload costs. |

These preserve the Core retention, original-provenance, immutable-state, and
exact-selection commitments. The initial binding newly chooses string member
keys, strict duplicate refusal, explicit Pointer absence semantics, JSON Patch
editing, and bounded export refusal. Those choices are stated as implementation
rules, not additional requirements imposed on every Core implementation.

## Recursive review method and evidence

1. **Independent analysis.** The semantic reviewer traced the complete spec and
   normative W3C/RFC clauses. The tool reviewer checked installed packages,
   official source, and bounded probes. The task reviewer traced prerequisites,
   current owners, tests, links, and sibling plans. The primary reviewer checked
   publication boundaries, benchmark methods, and cross-review reconciliation.
2. **Peer challenge.** The semantic reviewer checked tool findings; the tool
   reviewer checked task findings; the task reviewer checked semantic findings.
   Each distinguished a demonstrated error from an unimplemented acceptance
   requirement or a permissible stricter binding rule.
3. **Correction and revalidation.** The primary reviewer applied the agreed
   changes. All three reviewers reread the revised documents. The final task
   audit confirmed 25 matching task definitions, 41 valid dependency edges, an
   acyclic dependency order, and correct C10/C11 references. Its 344 checked
   local links resolved. Subsequent edits added this review link and two precise
   evidence-wording corrections; final mechanical checks covered those edits.

The main hypotheses were that the task graph supplied each required capability
before its consumer, that chosen APIs preserved the declared semantics, that
benchmark numbers supported their conclusions, and that publication remained the
single owner of retained meaning. F05/F06/F11/F13 refuted specific versions of
those hypotheses; the corrected design states the missing checks and boundaries.

### Reproduced tool behavior

The saved [codec probe](probes/2026-09-13-core-codec-probe.py) and
[result](probes/2026-09-13-core-codec-probe.json) record DuckDB 1.5.5,
msgspec 0.21.1, PyArrow 25.0.1, and jsonschema-rs 0.52.1. The primary reviewer
reran it independently and obtained identical output. Nine of eleven extraction
cases matched canonical bytes; the scalar and nested U+001F cases did not.
The probe also records type/presence distinctions, permissive malformed-Pointer
handling, and loss of duplicate-key evidence during msgspec decoding.

```sh
PYTHONPATH=src .venv/bin/python docs/history/probes/2026-09-13-core-codec-probe.py
.venv/bin/python docs/history/probes/2026-09-13-sqlite-ledger-binding-probe.py 20000 100000
```

The corrected ledger probe ran on Python 3.12.9, SQLite 3.51.0, and PyArrow
25.0.1. It completed both durability modes with correct row counts. These are
single-run diagnostic measurements with prepared source tables, schema creation
outside timing, no concurrent readers, and one write transaction per case:

| Rows | Setting | SQL generation, insert, and commit | Prepared Arrow conversion, insert, and commit |
| --- | --- | --- | --- |
| 20,000 | NORMAL | 0.026 s | 0.034 s |
| 20,000 | FULL | 0.025 s | 0.034 s |
| 100,000 | NORMAL | 0.118 s | 0.149 s |
| 100,000 | FULL | 0.110 s | 0.153 s |

These paths perform different preparation work. Their difference does not
measure native-binding overhead or predict production throughput. No production
performance claim rests on these small runs. The tool reviewer also independently
ran the corrected script at 1,000 rows. All three probe scripts passed syntax
checks; no production implementation was changed and no full runtime regression
or conformance run is claimed by this review.

### Primary-source checks

- [PROV-Dictionary](https://www.w3.org/TR/prov-dictionary/#dictionary): complete
  change semantics and the limits of membership assertions. Its permissive
  missing-key removal relation does not forbid a stricter application edit API.
- [PROV-DM Association](https://www.w3.org/TR/prov-dm/#term-Association) and
  [PROV-CONSTRAINTS](https://www.w3.org/TR/prov-constraints/#generation-precedes-usage):
  Plan interpretation can omit an unknown agent; generation/usage consistency
  still applies to represented entities and activities.
- [RFC 6901](https://www.rfc-editor.org/rfc/rfc6901.html#section-7) and
  [RFC 6902 §4](https://www.rfc-editor.org/rfc/rfc6902#section-4): unresolved
  selection handling is an application decision; malformed syntax and Patch
  operation-member behavior have defined rules.
- [DuckDB JSON functions](https://duckdb.org/docs/current/data/json/json_functions)
  and [larger-than-memory limits](https://duckdb.org/docs/current/guides/performance/how_to_tune_workloads#larger-than-memory-workloads-out-of-core-processing):
  typed extraction and spill support do not establish canonical bytes or bounded
  memory for every aggregate/query shape.
- [disk-objectstore source at ba13ca6](https://github.com/aiidateam/disk-objectstore/blob/ba13ca67216152fbd7c6df4b84fdec456a9d5478/disk_objectstore/container.py#L2461):
  concrete maintenance restrictions to check against the eventual pinned release.
- [prov architecture](https://prov.readthedocs.io/en/latest/explanation/architecture.html)
  and [conformance matrix](https://prov.readthedocs.io/en/latest/reference/conformance.html):
  in-memory export and serializer coverage do not establish full DocSpec or
  Keyed-State conformance.

## User value, alternatives, and failure criteria

The chosen shape pays down duplicated parsing, publication, reuse, and execution
rules while adding general revision semantics. A second engine, unconditional
Rust ledger, migration framework, or new export-paging framework would add work
without resolving the demonstrated issues. The corrections fit existing owners.

The design fails its value test if the document pipeline still bypasses shared
publication/reuse, small local changes needlessly decode the complete dataset,
or duplicate lifecycle paths survive C23. A pinned library's inability to meet
a required semantic or capacity check invalidates that operation's proposed
mechanism; C03/C-N1 must resolve it before that operation ships. It does not
invalidate the spec's semantics or justify claiming an unmeasured improvement.

Removing the common ledger would first break recoverable failed-attempt and
exact-selection ownership. Removing native batch interfaces would first weaken
the promised bounded data path. Existing document adapters subsume source-specific
behavior, but do not already implement general Core state and reuse semantics.
The likely future criticism is incomplete retirement or unqualified capacity;
C23 and C25 make both explicit completion conditions.

**Final assessment:** intent and shape match; product value is supported;
ownership and Core commitments are preserved; planned conceptual debt decreases
if the retirement criteria are met. Confidence is high in the checked document
consistency and bounded reproductions. Unbuilt production behavior remains to
be demonstrated by the task acceptance checks.

## Reviewed versions

| Artifact | Initial SHA-256 | Final SHA-256 |
| --- | --- | --- |
| Core spec | `977e13b341e1ca7524b85d04e624c1f8ffd3455aac597a53e206d72addc10b81` | unchanged |
| Implementation plan | `a8fa3c74544e158fd3769d5cf669b2b108da5c971ddb41e95c415b829af41e08` | `9a46a286a572c2550639cbd98dca31c496c493570fd3911c26cd28432217b28d` |
| Implementation tasks | `21d44237c298916aef686fda5a26a0755be17e5c997f863a06a18ccb67a5ff97` | `6eafa2503ce2b78464ec219d15fef83d9b338e24b0a80f543e2e0e07af208caa` |

The agent revalidation preceded only the final review links and two evidence
wording corrections. The primary reviewer checked those final deltas and the
hashes above. No commit, package publication, or deployment is recorded here.
