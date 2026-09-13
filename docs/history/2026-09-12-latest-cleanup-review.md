# Independent semi-formal code review: latest DocSpec cleanup

**Verdict: APPROVE. No actionable findings in the reviewed range.**

Review date: 2026-09-12. Repository: `/Users/mikewolfd/Work/DocSpec`.
Range: **`722e0ce..1d73408`**, with `722e0ce` excluded and `1d73408` included. The inspected checkout was exactly `1d73408`; the only untracked file was the protected, unrelated history file, which was not read or changed.

This review covers the profile declaration cleanup, direct catalog schema validation, unused declarations/dependencies, and contributor-document changes in these five commits. It does **not** certify the earlier catalog/runtime/export/provider/Dagster implementation, the earlier D38 qualification, or all D39 acceptance. Those reviews and their evidence remain separate.

## 1. Patch summary

The patch removes declarations that do not control execution: a default profile verifier label, two unused profile limits, two unused staging path copies, one unused JSON type alias, two unused diagnostic sets, and an unused test policy constant. It removes a duplicate configuration-digest comparison while preserving the earlier check. It replaces `_CompiledSchemaGate` with direct use of the already-required `jsonschema-rs` validator and the same Python diagnostic function. It removes PyMuPDF, ijson, and the duplicate development declaration of the core jsonschema dependency. It updates the corresponding lock and contributor documentation.

Important changed call sites are `_CatalogRowPartitioner.stage` (`src/docspec/adapters/catalog_artifact/builder.py:201`), `_iter_partition_stream` (`src/docspec/adapters/catalog_artifact/rows.py:76`), `ProfileRegistry.from_file` (`src/docspec/profile_registry.py:112`), and the catalog CLI build diagnostic (`src/docspec/cli/source_catalog.py:217`).

All 25 changed files were inspected, including both packaged profiles, the lockfile changes, the three changed test modules, and all eight changed contributor documents. Searches included production, tests, tools, examples, conformance declarations and package configuration; no surviving reference to the removed names or removed dependencies was found.

## 2. Exploration notes

- **H1: Removing profile settings and the repeated digest check might weaken saved-plan admission.** Refined and resolved: `from_file` still checks the supplied configuration digest at lines 129–131; `_description_identity` includes limits and executable fields at lines 45–66; `select` pins that complete description at lines 186–209; runtime compares those pins before storage assembly at `runtime/storage.py:49–59`. The two removed limit names have no callers. The deleted `ProfileDescription.verifier_id` was not the machine profile's conformance reference.
- **H2: Direct schema validation might change which rows pass or lose refusal detail.** Resolved for the supported schema: the new helper has the same old fast-accept/otherwise-Python-validate sequence. Both builder and reader call it; canonical-byte, size, ordering and semantic checks remain around it. Required dependency/compilation errors now stop import instead of silently selecting the expensive alternate engine. That is the intended removal of an unsupported execution mode.
- **H3: Removing cached paths might affect publication or cleanup.** Resolved: retained pinned-directory objects, session identity and descriptor-relative operations own those behaviors. The deleted fields were never read.
- **H4: Removing dependencies might break lazy PDF or streaming behavior.** Resolved: the only PDF provider loaded by the implementation is pypdf; no remaining source/test/tool/example uses PyMuPDF, fitz or ijson. The required core jsonschema dependency and shared encoder's msgspec development dependency remain.

## 3. Function trace

| Function or relevant declaration | File:line | Input → output | Verified behavior |
| --- | --- | --- | --- |
| `_verify_item_schema` | `src/docspec/adapters/catalog_artifact/schemas.py:34` | Decoded row + label → return or `IntegrityError` | Required compiled validator accepts valid rows; otherwise calls unchanged Python diagnostic owner. Same decision sequence as the removed wrapper when its required engine was available. |
| `_schema_error` | `src/docspec/adapters/catalog_artifact/schemas.py:22` | Python validator + value + label → return or `IntegrityError` | Preserves first validation error, absolute field path and human-readable message. The local jsonschema implementation raises from `iter_errors` (`.venv/lib/python3.12/site-packages/jsonschema/validators.py:448`). |
| `_CatalogRowPartitioner.stage` | `src/docspec/adapters/catalog_artifact/builder.py:201` | Ordered policy items + workspace → bounded canonical rows and accounting | Calls the shared schema function before serialization/storage; keeps interpretation order, maximum row size, distinct item order and accounting checkpoints. |
| `_iter_partition_rows` / `_iter_partition_stream` | `src/docspec/adapters/catalog_artifact/rows.py:54`, `:76` | Pinned partition descriptor/stream → typed items or mappings | Bounded line read, newline, canonical parse, schema, interpretations, candidate semantics, partition membership, strict ordering and final row count remain. The pre-existing admitted-byte re-read branch is unchanged. |
| `_build` | `src/docspec/cli/source_catalog.py:143` | Parsed CLI arguments → catalog build | Diagnostic now names the sole required engine directly; stdout build reporting and stderr diagnostics remain separate. Uses `SourceCatalogBuilder` at line 232. |
| `ProfileDescription.__post_init__` | `src/docspec/domain/profiles.py:35` | Description fields → normalized description | Removes only the unused verifier label check; role/text/collection validation and JSON value normalization remain. |
| `ProfileDescription.configuration_digest`, `description_digest`, `pin`, `to_dict` | `src/docspec/domain/profiles.py:65`, `:69`, `:75`, `:86` | Description → digests/pin/mapping | Configurations remain hashed; description mappings omit the unused label. Registry-selected pins continue using the more complete machine-description digest. |
| `_description_identity` | `src/docspec/profile_registry.py:45` | Machine profile mapping → identity-bearing fields | Retains implementation module, configuration, schemas, capabilities, real limits, compatibility and conformance test ID. Mutable evidence status stays outside the digest. |
| `ProfileRegistry.from_file` | `src/docspec/profile_registry.py:112` | Profile file → `RegisteredProfile` | Checks closed shape, version, secret-free content and configuration digest before constructing the description. Removing the second comparison does not remove the first admission check. |
| `ProfileRegistry.select` / `_local_profiles` | `src/docspec/profile_registry.py:186`; `src/docspec/runtime/storage.py:49` | Selected IDs / saved plan → admitted profile set | Dependencies and implementation status are checked; runtime refuses stale machine-description pins and unsupported local modules. |
| `_profile_limit` / `_local_storage` | `src/docspec/runtime/storage.py:42`, `:65` | Admitted profiles → storage adapters | All consumed positive bounds remain: record/member/root/open-member/merge scratch, blob size/chunk, manifest limits. Neither deleted setting participates. |
| `LocalSourceCatalogStaging.__init__`, `put_blob`, `commit`, `close` | `src/docspec/adapters/source_catalog_store/staging.py:61`, `:199`, `:538`, `:567` | Pinned directories → staged/published bytes and cleanup | Still retains the actual session/directory descriptors and session identity. Publication uses them; cleanup verifies identity and operates relative to the pinned staging directory. |
| `LazyPypdfExtractor.__init__`, `_read_pages` | `src/docspec/processing/extraction.py:348`, `:454` | PDF configuration / captured bytes → pinned provider/page text | Reads the installed pypdf distribution version without importing its parser; loads only `pypdf` when selected and rejects unavailable or changed versions. |
| Removed diagnostic sets / alias / test constant | `src/docspec/adapters/catalog_artifact/digests.py:140`; `src/docspec/processing/bounded_segmentation.py:74`; `src/docspec/domain/identity.py:24`; `tests/helpers.py:60` | No surviving consumers | Actual derivation path values remain emitted in `derivation.py:261`, `:354`, `:421`; individual exclusion codes remain used at `bounded_segmentation.py:696`, `:704`. No behavior is replaced by another abstraction. |

## 4. Data flow and invariants

**Catalog rows.** Policy objects produce dictionaries, the schema helper checks them, canonical serialization creates bytes, and the existing bounded workspace stores them (`builder.py:214–244`). Consumer bytes are bounded and canonically parsed before the same schema helper runs (`rows.py:87–100`). Schema definitions did not change. The helper and caller error categories did not change. The Python validator still decides the detailed refusal path, including the pre-existing behavior if it accepts a row that the compiled validator rejects; this patch does not introduce that behavior.

**Profile identity.** Configuration comes from the closed JSON file, is hashed before construction, and is copied through the existing JSON normalization (`profile_registry.py:118–131`; `domain/profiles.py:47–63`). The complete machine-description digest includes `limits` (`profile_registry.py:63`). Removing unused limit declarations therefore deliberately changes those profile pins; an older plan refuses instead of silently executing under a different description (`runtime/storage.py:52–53`). That is appropriate for this greenfield change. The unchanged profile version labels are not the only binding: complete description digests are enforced.

**Publication state.** The removed string paths had no reads. Pinned objects escape construction into blob verification, publication and cleanup; the retained `_session_identity` is what prevents cleaning a replacement session (`staging.py:87–98`, `:584–588`). No filesystem guard or durable write was removed.

**Dependencies.** `pyproject.toml:6–11` still requires both validators; lines 31–33 retain pypdf as the PDF extra. Lockfile removals correspond to PyMuPDF, ijson and the redundant dev reference only. The domain/application import direction remains independent of these adapter validators (`tests/conformance/test_import_directions.py:194–226`). Removing an already-declared required engine's fallback does not add an optional dependency to the core.

## 5. Tests and concrete edge cases

These tests were read and their assertions traced. Expected outcomes below are static predictions, not newly executed results.

| Existing test | Evidence and expected behavior |
| --- | --- |
| `test_the_compiled_validator_and_the_authority_agree_on_real_and_mutated_rows`, `tests/test_source_catalog_rows.py:86` | Builds real rows, removes each top-level field, flips types and adds an unknown field; asserts compiled/Python/helper verdict parity. Expected pass with direct validator objects. This is a bounded differential corpus, not proof of equivalence for every possible JSON value. |
| `test_a_stored_catalog_row_is_byte_identical_to_its_reserialized_item`, same file `:58` | Builds and verifies a catalog, then checks exact domain round-trip bytes. Expected unchanged. |
| `test_every_description_on_disk_is_closed_versioned_digest_pinned_and_capable`, `tests/conformance/test_profile_descriptions.py:48` | Covers all packaged profiles, closed shape, pinned fields and evidence-status independence. Expected pass with the two unused settings absent. |
| `test_unpinned_descriptions_are_rejected_at_load`, same file `:92` | Mutates each profile configuration without updating the digest; remaining first check must raise `ProfileError`. Direct coverage of the removed duplicate's invariant. |
| `test_unknown_unpinned_capability_and_digest_mismatched_pins_fail_before_work`, same file `:111` | Four pin mutations refuse before control state exists; covers stale description/configuration/capability admission. |
| `test_profile_pin_identifies_the_complete_machine_description`, `tests/test_profile_registry.py:37` | A real limit change changes complete description/pin while configuration digest stays the same. Confirms that removed declaration fields cannot hide behind a stable configuration digest. |
| `test_local_source_catalog_store_uses_pinned_staging_root_during_cleanup`, `tests/test_source_catalog_storage.py:180`; same-name replacement case `:236` | Directory replacement preserves outside/replacement sentinels and refuses unsafe cleanup. Uses surviving pinned state, not removed string fields. |
| Derivation fallback assertions, `tests/test_source_catalog_workers.py:293–315` | Compare fallback and serial digests and retain `serial-fallback` measurement evidence. Removal of an unused set does not remove emitted values. |
| `test_every_excluded_range_carries_a_machine_legible_code_and_reader_prose`, `tests/test_bounded_segmentation.py:317` | Individual exclusion codes and explanations remain public and exercised; no consumer of the removed aggregate set exists. |
| `test_pypdf_is_loaded_only_when_selected_and_pages_round_trip`, `tests/test_processing_pipeline.py:123`; missing-extra case `:180`; changed-version cases `tests/test_processing_stage_identity.py:98` | Exercise lazy selection, receipt identity, page coordinates, missing provider and refusal before a changed parser runs. These use a fake PDF provider and are not a fresh real-PDF qualification. |
| `test_project_declares_shared_artifact_utilities_and_one_command`, `tests/test_package_boundary.py:175`; isolated wheel test `:396` | Checks retained core dependencies/PDF extra, exact packaged schemas and all ten profile files, installs the wheel into a fresh environment, and exercises current public example paths. Expected pass without deleted dependencies. |

No changed executable path lacks an existing behavioral check. The removed declarations themselves need no tests that merely assert their absence. Absence of a required `jsonschema-rs` installation now causes an import error rather than a fallback; package dependency resolution is the supported boundary, not a legacy execution mode.

## 6. Findings and limits

**Actionable findings: none.** No regression, unsafe removal, incorrect profile-pin behavior, new dependency-direction violation or broken maintained-document target was identified in this range.

The documentation describes current APIs and separates root-authored responsibility analysis from independent D39 review (`docs/cleanup-decisions.md:220–235`; `docs/dataset-experiments-todo.md` D33/D36/D39). The patch does not claim the unfamiliar-human exercise is completed.

This was an independent static review. I ran no tests, installs or broad regression commands and made no repository edits. `git diff --check 722e0ce..1d73408` passed. The parent supplied **1,071-pass full-suite evidence and a 164-pass final focused gate with installed-package checks**; those runs were not independently repeated here. The installed jsonschema Python diagnostic implementation and jsonschema-rs interface declarations were available to inspect; the Rust validator's implementation source was not audited. Its supported behavior is backed here by unchanged call semantics and the existing differential tests, not a review of its internals.

**VERDICT: APPROVE**

- The patch achieves its stated cleanup without replacing removed declarations with new machinery.
- Coverage of changed paths: **ADEQUATE** for this bounded cleanup.
- Confidence: **HIGH** for the reviewed repository changes; not a certification of all earlier D39 slices, every schema-engine input, or live/real-PDF provider behavior.
