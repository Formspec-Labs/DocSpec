# Configuration and catalog cleanup review

**Verdict: APPROVE all five scoped commits. No material findings.** The deletions remove unenforced declarations, unused behavior, or a second verification path while retaining the checks that decide whether work and artifacts are acceptable.

Date: 2026-09-12. Reviewer: independent `experiment_architecture` agent, using `/Users/mikewolfd/.agents/skills/semi-formal-code-review/SKILL.md`. This was a read-only, static review. I ran no tests, builds, or runtime probes and made no repository edits. The protected untracked historical findings file was neither read nor changed.

## Patch summary and scope

| Commit | Reviewed change | Decision |
| --- | --- | --- |
| `87b0e7fb72859bdc3ce8cb88e31381d653148f15` | Remove `ProfileGovernance`, its registry arguments/allowlist and ten packaged governance objects; storage-description format becomes `2.0`. | APPROVE. These were labels and identity inputs, with no enforcement consumer. Actual settings, profile pins and implemented plan policies remain. |
| `3adc7fc9fdbd485c5f9d30f68c46d0c86f9a7549` | Remove `NullProcessorResultCache` and its lazy adapter export. | APPROVE. The existing optional cache dependency already represents bypass; no source/test/example/tool caller constructed this class. |
| `7188e34f174e5286dc40d322c4f665032ebff74a` | Remove configuration-only cache profile/state artifacts and the generic artifact-list property; execution-profile format becomes `3.0`. | APPROVE. These artifacts neither configure nor restore SQLite. Worker verification and immutable processor-result evidence remain authoritative. |
| `231f15560177aae7eab01f3a13c84b183488ae67` | Remove the source-catalog command receipt, duplicate validators, CLI arguments and its root-file writer. Verify the explicit catalog pin through the existing full reader. | APPROVE. One artifact admission path now serves CLI and Python-built/relocated catalogs. Invocation details deliberately remain build-report output. |
| `a3ae903` | Extract typed runtime fixture arguments and retain a thin CLI writer; migrate thirteen caller files. | APPROVE, separately scoped. This removes test setup serialization, not behavioral assertions or public interface coverage. |

I inspected every changed diff in the four cleanup commits and the added typed-fixture commit, including packaged descriptions, selectors, documentation and test/probe migrations. Production traces below use the named commit's file and line where indicated. Shared reader/verifier references were checked against the current files; those files have no difference from `231f155`. Other current caller/test references support the scoped deletion assessment, not a new whole-system review. My separate pending cache-eligibility patch is excluded because I authored it.

## Function traces

| Entry point / function | Evidence | Inputs, effects and invariant |
| --- | --- | --- |
| `ProfileRegistry.from_file` → `RegisteredProfile` | `87b0e7f:src/docspec/profile_registry.py:112` | Reads a regular non-symlink JSON description, rejects unknown shape/version and secrets, checks configuration digest, schemas/media/capabilities and compatibility, and computes the complete description pin. Only governance parsing/hashing is removed. |
| `ProfileRegistry.select` → `_local_profiles` → `_local_storage` | `87b0e7f:src/docspec/profile_registry.py:188`; `87b0e7f:src/docspec/runtime/storage.py:49`, `:65` | Selection still rejects unimplemented/missing dependencies. Runtime compares the entire selected `ProfileSet` with the plan and checks supported module/profile-set before passing positive numeric limits into actual record/blob/store/catalog constructors. |
| `verify_processor_policies` and release verification | `src/docspec/application/processor_rules.py:21`; `src/docspec/application/commit.py:293` | Data-use digest, local-only/external execution and retry-policy compatibility remain enforced. Release retention dispositions must match the plan. These mechanisms did not depend on `ProfileGovernance`. |
| `StoreExecutionService` → `ProcessorRuntime._invoke_processor` | `src/docspec/application/execution.py:81`; `3adc7fc:src/docspec/application/processor_runtime.py:142` | `None` bypasses cache lookup/insertion. Cache eligibility additionally requires deterministic `EXACT_INPUTS`. Processing and durable result creation proceed without the deleted null object. |
| `_verified_cached_result` and cache insertion | `src/docspec/application/processor_runtime.py:264`, `:313` | Results are saved in the control repository before cache insertion. A cache reference is loaded by exact pin and checked against request reuse inputs, processor description, segment, prerequisites and data-use policy. Missing/invalid/unavailable cache entries trigger recomputation rather than acceptance. |
| `_prepare_local_run` → `_load_prepared_local_run` | `7188e34:src/docspec/runtime/preparation.py:51`, `:87` | Preparation saves worker composition, profile and handoff without the two unused cache declarations. Recovery still checks profile bytes/identity, operation, task-index bound, deadline, reconstructed worker, exact plan/ledger/sink/base binding. |
| `_ExecutionProfileBinding`, reconciliation and release execution-evidence verification | `7188e34:src/docspec/adapters/execution.py:38`; `7188e34:src/docspec/application/reconcile.py:377`; `7188e34:src/docspec/application/commit.py:37` | Replacing the generic loop with direct `controls.verify(worker_composition)` preserves the remaining required nested control check at every boundary. The profile's closed parser rejects retired fields and versions. |
| CLI `_verify` → `SourceCatalogArtifactReader.verify_snapshot` | `231f155:src/docspec/cli/source_catalog.py:109`; `src/docspec/adapters/catalog_artifact/reader.py:160` | Parses caller-supplied `SourceCatalogRef`, opens existing storage without creation, constructs explicit producer acceptance and calls Rulespec admission with expected logical/digest pin plus `SourceCatalogBuildGateVerifier`. A new reader per command does not inherit an old cached verdict. |
| `SourceCatalogArtifactVerifier` → `SourceCatalogBuildGateVerifier` | `src/docspec/adapters/catalog_artifact/verification.py:80`, `:271` | Checks closed product member roles and schemas, small-member byte pins, producer, source-input pin agreement, accepted reported outcomes, policy identity, partition completeness/counts, byte accounting and reason/join accounting. The full gate independently derives state/universe/selected digests, disposition/reason counts and diagnostics from rows. |
| CLI `_build` → builder → `LocalSourceCatalogPublication.publish` | `231f155:src/docspec/cli/source_catalog.py:230`; `src/docspec/adapters/catalog_artifact/builder.py:296`, `:343`; `src/docspec/adapters/source_catalog_store/store.py:109` | The builder still seals and admits its artifact before inner commit. Outer publication still checks pinned parent/session identity and uses no-replace publication. A success report is emitted only after publication. The removed root-file writer had no remaining production caller. |
| `_seeded_local_run_arguments` → `_seeded_local_run` | `a3ae903:tests/support/profiles.py:26`, `:94`; `tests/support/cli.py:23` | The existing source bytes/catalog and typed plan now directly supply runtime kwargs. CLI cases still write canonical plan/request files with independent explicit worker/in-flight defaults of 1/1. No new fixture class, runner or runtime API is introduced. |

## Data flow and deletion safety

**Governance:** the pre-change symbol/caller search found the class only in its model, export, registry and direct tests; the packaged values were five deployment/policy URNs. The registry merely allowed those URNs and hashed them. Removing them cannot remove an access, encryption or location check because none consumed them. This does not prove deployment security; the updated documentation correctly assigns those controls to the actual deployment/storage implementation. The breaking description format and full-description digest prevent an old governed description from silently becoming the new shape.

**Cache:** the deleted null adapter returned a miss and accepted arbitrary insertions without saving anything. The existing optional dependency is the smaller equivalent bypass mechanism. The subsequently deleted cache-state artifact contained only database location, observation time and a `configuration-only` label. Neither execution nor recovery used it to rebuild data. Real result references remain in processor invocation receipts and immutable controls; deleting these declarations does not remove their reachability or validation. Cache availability can change the amount of work without making invalid output acceptable.

**Catalog authority:** the removed command receipt previously required the full artifact reader anyway. Its second digest and original destination path prevented independent admission of Python-built or relocated output without adding artifact-content verification. The retained catalog receipt is a manifest-pinned member (`builder.py:533` and `:541`); its provider-reported descriptions and accepted outcomes are retained at `builder.py:493`. Exact upstream pins remain at `verification.py:136`. The CLI no longer checks invocation-specific source paths, chosen provider profiles and accepted source-verifier IDs against a separate saved file; that is the documented retirement decision, not an unnoticed missing check. Provider truth remains the provider's responsibility; catalog verification does not rerun collection or claim complete acquisition.

The generic CLI failure writer remains available to mutating commands with a `receipt` argument. Source-catalog build no longer has that argument, so deleting its private suppression flag does not create an unexpected failure file. The other removed private blob-evidence flag had no setter in source. No persistent source-catalog success artifact is replaced or repaired by verification.

**Fixture independence:** typed arguments preserve source/catalog creation, plan, roots, producers, policies, fixed completion time, deadline, sink and partition policy. The only setup files deliberately absent from Python tests are `run-request.json` and `plan.json`. The dedicated CLI parity test still exercises actual serialization/parsing; it does not compare two values both derived from the typed helper's execution limits.

## Tests and edge cases inspected

| Behavior | Actual assertions inspected | Assessment |
| --- | --- | --- |
| Retired governance/old description refusal | `tests/test_profile_registry.py:53` | Both old version and added governance field are refused. |
| Meaningful profile identity and pre-work refusal | `tests/test_profile_registry.py:37`; `tests/conformance/test_profile_descriptions.py:80`, `:99` | A real limit change changes full pins; configuration/capability/description mismatches fail before control/planning directories exist. Required dependency selection still has its own negative case. |
| Cache bypass, invalid cache and outage | `tests/test_processor_cache.py:237`, `:279`, `:313` | Real pipelines use `None`; missing result references are recomputed then later reused; unavailable cache still invokes the processor once. |
| Cross-plan cache reuse and concurrent winner | `tests/test_processor_cache.py:204`, `:279` | Call counts and receipt/result identity establish real reuse. The concurrent-winner case correctly observes two actual calls even when the final receipt says hit. |
| Worker bytes remain required | `7188e34:tests/test_execution_backends.py:267` | A self-consistent profile pointing at wrong worker bytes refuses before its handler is called. The adjacent full-profile tamper case remains. |
| Execution-profile closed boundary | `7188e34:tests/test_execution_backends.py:331` | Version 2, scheduler fields and removed cache fields are refused; changing the enforced index bound changes identity. |
| CLI admission independent of invocation | `tests/test_source_catalog_cli_verify.py:29`, `:41` | Real built catalogs verify after moving, with unchanged modification times; directly Python-built catalogs verify without a CLI command receipt. |
| Pin, producer and member refusal | `tests/test_source_catalog_cli_verify.py:53`, `:62`, `:72` | Wrong logical/digest pin, unaccepted producer and changed root/retained receipt bytes produce failure and no success output; verification leaves changed bytes untouched. |
| Full semantic check is exercised | `tests/test_source_catalog_build_safety.py:246` | Corrupting only the builder's first derived state triggers the independent second pass; no catalog is published. CLI delegates to this same full gate. |
| Payload integrity and accounting | `tests/test_source_catalog_snapshot.py:388`, `:401` | Changed payload refuses before rows; malformed reason ordering/counts refuse. These are shared-reader tests rather than duplicate command-receipt tests. |
| Publication failure/concurrency/crash | Changed `tests/test_source_catalog_cli_build.py`; `tests/test_source_catalog_storage.py` | Failed publication leaves no destination or success report; two publishers yield one admitted winner; actual process-exit cases still cover before/after rename. Test-only direct writes replace use of the removed root-file convenience method. |
| Installed provider and independent consumer | Changed `tests/support/installed_source_catalog_probe.py`; `tests/test_source_catalog_installed_wheel.py:222` | Build reports replace command receipts; artifact receipts still check accounting/reuse. Separate consumer admission still runs with explicit producer/reference and without requiring the provider invocation. |
| Typed lifecycle and independent CLI defaults | `tests/test_runtime_api.py:24`, `:48`; `tests/test_local_experiments.py:222` | Python run/recovery/retain work without request files, with one actual fetch/processor call. CLI parity still checks the independent 1/1 settings. The other twelve caller migrations change setup only. |

## Findings and qualifications

There are **no blocking or material findings** in the scoped diffs. The greenfield format changes deliberately refuse superseded shapes; preserving compatibility would contradict the accepted scope.

The review does not certify all profile fields as enforced, all test ownership as minimal, remote CI, publication readiness, dataset capacity, or deployment access/encryption controls. Parent-owned removal of other unused sink/status declarations and the pending cache-construction eligibility correction are separate work. I also did not independently re-review the broader lifecycle while checking `a3ae903`.

## Execution evidence and conclusion

The following are **parent-reported or commit-recorded execution**, not tests run by this reviewer:

- Governance slice: 89 focused tests, recorded in its architecture note; the cited preceding 1,071-test suite predates the change.
- Null cache deletion: 12 processor-cache/import-direction checks, recorded in the commit message.
- Cache declaration removal: 76 tests in 55.75 seconds, recorded in its architecture note, including installed local/native Dagster, hits, invalid-result repair and outages.
- Catalog CLI cleanup: 78 tests in 25.96 seconds, recorded in the checklist, including installed wheels.
- Typed fixture commit: parent reports 169 affected tests in 48.98 seconds and Ruff/diff checks; the default-parity correction was made before application. The preceding 1,076-test strict run is separate evidence.

**APPROVE**, with high confidence in the bounded deletion and fixture-safety traces. The full suite currently owned by the parent is additional integration evidence; its outcome is not inferred here. No new verifier, cache-state model, compatibility wrapper or test framework is warranted by this review.
