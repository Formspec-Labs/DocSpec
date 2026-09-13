# DocSpec experiment foundations: independent static review

Reviewed through `c41b00bb1e0bef4ba19e4f163fb9d5726dddf9ef` on 2026-09-11 using the semi-formal-code-review skill. This consolidates completed reviews; it is not a new broad audit. The final check covered the reconciliation-root-only worker test and related documentation clarification. Repository paths below are relative to `/Users/mikewolfd/Work/DocSpec`.

**Verdict: APPROVE for the bounded foundations below. No unresolved material correctness finding.** This reviewer performed static analysis only. Runtime results, counts, package execution, and the full-suite outcome must be supplied separately by the coordinating agent.

## 1. Patch summary and separate outcomes

| Slice | Static outcome and user value |
| --- | --- |
| D01 documentation and packaged profiles | Approved. Documentation distinguishes implemented entry points from intended lifecycle features and distinguishes application state from portable document export. Built-in profile lookup works through package resources rather than a checkout-level directory; the reviewed move preserved all ten profile files and their identities. |
| D02 retain/select | Approved. Two verified alternatives can share base X and remain independently readable. Selecting B after A requires the caller's expected current A; B continues to name X as its base. |
| Zero-work reconciliation | Approved. Stateful results retain verified base blob roots with inherited layers, including when no new stores are delivered. Stateless reconciliation does not inherit result layers or roots. |
| D09 admitted catalog reader | Approved. Consumers can reuse one admission and stream validated objects or mappings through the public facade without duplicating admission or domain rules. |
| Workspace defaults | Approved as a configuration foundation. One root derives the existing runtime locations; explicit overrides and independently configured producers remain available. It adds no dataset or run ledger. |
| Saved-worker validation | Approved. Preparation and reconstruction share one description of effective settings; changed settings refuse saved work before fetching. Fetcher identity comes from the actual implementation. |

## 2. Function and caller traces

| Function or path | Evidence | Inputs, output, and verified behavior |
| --- | --- | --- |
| Built-in/local profile selection | `src/docspec/profile_registry.py:101`, `:105` | Package resources pass through existing profile validation and selection. |
| Workspace roots | `src/docspec/workspace.py:32`, `:55` | Validated root and overrides produce deterministic path settings. Request parsing alone creates no dataset state. |
| Retain application result | `src/docspec/application/commit.py:429` | Base plus run reference produces the existing receipt and complete release; stages and retains it without consulting current. Returned identity and digest must match. |
| Combined commit | `src/docspec/application/commit.py:416`; `src/docspec/adapters/storage/catalog.py:469` | Retain, then select against the pinned base. A selection failure leaves a valid retained result. |
| Retain storage | `src/docspec/adapters/storage/catalog.py:394` | Takes the write lock before resolving staged/published location; verifies artifact, dependencies, pinned base, and exact sorted sealed-store evidence before immutable publication. |
| Select current | `src/docspec/adapters/storage/catalog.py:445` | Reopens candidate and base, checks current under the write lock, accepts an exact already-current retry, otherwise refuses stale expectation or atomically updates the pointer. |
| CLI retain/select | `src/docspec/cli/releases.py:36`; `src/docspec/cli/catalog.py:116` | Closed action-specific requests call those services and produce standard operation receipts. Selection requires explicit `expectedCurrent`. |
| Base state reconciliation | `src/docspec/application/reconcile.py:132`, `:294` | Opens the verified base; returns inherited layers and stateful roots. Delivery roots merge through the existing disagreement check at `:204`; sorted roots enter the run receipt at `:281`. |
| Compaction recovery | `src/docspec/application/maintenance.py:435`, `:518` | Reuses an already-current equivalent successor through the existing verifier before creating another timestamped run. The stale-race fallback remains. |
| Public admitted reader | `src/docspec/adapters/catalog_artifact/reader.py:83`, `:137` | Authentic admitted artifact plus explicit expected pin opens public streaming access; ordinary reader admission happens once. |
| Reader bytes and rows | `src/docspec/adapters/catalog_artifact/payloads.py:24`; `src/docspec/adapters/catalog_artifact/rows.py:76` | Consumed blob bytes remain bound to their pin. Generic providers use bounded reads and temporary storage; object/mapping paths share row validation and propagate closure. |
| Worker prepare/load | `src/docspec/cli/local.py:97`, `:345` | One settings builder records and checks roots, actual fetcher identity/configuration, policies, producers, clock, partition settings, and sink. `LocalJsonControlRepository.load` at `src/docspec/adapters/storage/controls.py:53` already verifies bytes and identity. |
| Worker execution callers | `src/docspec/cli/runs.py:68`, `:116` | Saved task execution and reconciliation load the checked handoff before executing work. |

## 3. Data flow and invariants

- Immutable lineage remains release/plan/run/prepared receipt base X. `DocumentReleaseVerifier._verify_receipt_links` checks those links at `src/docspec/application/commit.py:264`. The mutable current pointer records only the present selection; changing it never restamps result bytes.
- `CatalogCommitReceipt.expectedHead` means the prepared base, not evidence that selection occurred. The revised standalone specification section 5.2 and `docs/experiments.md:80` state that distinction.
- Retention and selection keep the existing artifact-membership, record, receipt, sealed-store, and blob checks. No second dataset, run, or selection ledger was added.
- Reader admission establishes closed membership at admission time. Reusing admission rechecks consumed bytes; it does not rescan the source inventory or repeat full derivation. Explicit verifier acceptance remains separate from supplied artifact metadata.
- Effective workspace defaults and explicit equivalent settings produce the same prepared references. Saved-worker matching reads current fetcher properties rather than trusting a stale copied descriptor.

## 4. Tests and concrete edge cases inspected

| Evidence | Assertion traced statically |
| --- | --- |
| `tests/test_package_boundary.py:374` | Installed-wheel/profile access is exercised outside checkout assumptions. |
| `tests/test_workspace.py:103` | Explicit defaults freshly prepare with resume and reproduce the implicit handoff/profile references, then load the saved handoff. |
| `tests/conformance/test_document_catalog_contract.py:180`, `:225`, `:268` | Alternative retention/selection, stale refusal, exact retry, damaged dependencies, and a writer moving staged data before lock acquisition. |
| `tests/test_experiment_retention.py:68` | Processor alternatives preserve exact capture/extraction/segmentation payloads and do not increase their execution counts. |
| `tests/test_release_selection_cli.py:43` | CLI retention leaves current unchanged; explicit replacement succeeds; stale replacement fails. |
| `tests/test_maintenance.py:312`, `:340` | A real zero-work successor preserves roots and full logical state with zero new-work accounting; stateless reconciliation emits no inherited result state. Unrelated-current compaction leaves controls, stores, and release count unchanged. |
| `tests/test_source_catalog_public_reader.py:43`, `:100`, `:150`, `:187`, `:210` | One admission, changed-member/payload refusal before yielding, bounded provider reads, resource closure, and object/mapping refusal parity. |
| `tests/test_local_worker_identity.py:38`, `:57`, `:88`, `:98`, `:113` | Valid reconstruction and completed-work reuse; changed settings/mutable fetcher configuration/wrong fetcher implementation refuse; missing descriptors refuse before local control writes. |

The final `reconciliationRoot` case at `tests/test_local_worker_identity.py:67` changes only that root while preserving fetcher configuration and plan. It closes the earlier test-specificity gap: the source-content-root case also changed the fetcher digest and could not independently prove root-map binding.

## 5. Findings resolved and remaining scope

Resolved during review: stale documentation claims; tautological workspace-reference assertion; object-stream closure propagation; staged-location race during retention; zero-work loss of base blob roots; missing direct stateless/compaction-refusal coverage; fixture translator sharing the plain local fetcher's identity; and worker root-binding coverage. The fixture now has a distinct descriptor and matching acquisition metadata (`tests/helpers.py:359`).

The final documentation clarification accurately describes required fetcher descriptors, worker-description version 2.0, and saved-handoff matching (`docs/offline-walkthrough.md:74`; `docs/operations.md:35`). It leaves broader configurable extraction/segmentation and public lifecycle work open.

Operational observation: retention/selection hold an exclusive catalog write lock through full verification. Competing writes fail promptly and may retry. This correctness tradeoff is documented; no performance qualification or automatic retry claim was made. Truly overlapping distinct compaction runs can retain more than one valid artifact while selection chooses one; no cleanup mechanism or stronger once-only guarantee was introduced.

Remaining scope is not a finding against these slices: the complete installed lifecycle, independent identical-configuration trials, arbitrary stage stopping points, capture-only completion, configurable extraction/segmentation, unified attempt inspection, full Dagster parity, scale qualification, and unfamiliar-contributor usability remain open checklist work. These reviews do not establish completion of D03/D04 or the wider D39 program.

## 6. Conclusion

**VERDICT: APPROVE. Coverage of the bounded changed paths: ADEQUATE. Static confidence: HIGH.** The changes support useful experiment behavior through existing ownership and verification paths. No unresolved material review finding remains; runtime evidence is intentionally separate.

## Coordinating agent's runtime evidence

At `c41b00b`, `uv run --frozen --extra dagster pytest` passed 907 tests in
114.14 seconds; one live integration test was deselected by the repository's
default test selection. This included the installed-wheel checks. Ruff passed
for `src`, `tests`, and `examples`. A separate link check resolved all 138
cross-repository task links and confirmed 3 of 51 local checklist items complete.

These are local checks. They do not establish CI, publication, deployment,
performance at corpus scale, or completion of the full experiment workflow.
Later runtime API changes require their own review and validation.
