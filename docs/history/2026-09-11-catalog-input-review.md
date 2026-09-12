# D04 catalog operations / D06 input sources — independent static review

VERDICT: APPROVE. No material finding remains in the reviewed catalog-only API and supplied-record input slice. Source coverage versus dataset replacement semantics required a documentation clarification, which is now present.

Repository: `/Users/mikewolfd/Work/DocSpec`; changes over `c99874a`. Scope: `runtime/catalogs.py` and exports; `adapters/supplied_records.py`; `application/supplied_records_catalog.py`; public source-catalog exports; bounded `SqliteCatalogPolicyWorkspace` changes; empty-catalog blob-directory publication; local catalog/workspace tests, public installed-provider assertion, installed runtime probe, supplied-record example, and catalog-input documentation. Concurrent D16 repair and D11 fetcher changes are separate reviews.

This review follows `/Users/mikewolfd/.agents/skills/semi-formal-code-review/SKILL.md`. It is static: no tests/builds were executed, no repository files were edited, and unrelated untracked history was not read. Executed outcomes belong in the parent's separate evidence appendix.

## 1. Patch summary

`build_local_catalog` supplies the local storage and bounded scratch around the existing verified catalog builder. `open_local_catalog` opens an exact catalog through the existing admitted reader and creates no storage. Callers choose source objects, a matching policy, catalog identity, output producer, workspace, and scratch allowance; they do not assemble storage adapters or publication internals.

`SuppliedRecordSource` snapshots bounded caller records into the existing source port. `SuppliedRecordCatalogPolicy` interprets the small closed shape through the existing catalog policy and schema machinery. Provider input uses the existing optional SpicyDocs adapter, now exported through the public module. The new source kind is evidence of supplied records, not evidence of upstream collection or document acquisition.

This is a useful reduction in ordinary setup. It adds no catalog format, general field-mapping language, current-selection pointer, collection executor, or second builder. The CLI's destination publication and command-receipt behavior remains a legitimate separate outer flow.

## 2. Function trace

| Function / method | File:line | Inputs → output | Verified behavior |
| --- | --- | --- | --- |
| `build_local_catalog` | `src/docspec/runtime/catalogs.py:20` | Sources, workspace, policy, explicit identity/producer/bound → existing build result | Validates the producer using its real parser and validates the request/resume path before scratch; composes only source storage and the existing builder. |
| `open_local_catalog` | `src/docspec/runtime/catalogs.py:56` | Exact reference and independent producer acceptance → admitted reader | Uses `LocalSourceCatalogStore(create=False)` and existing `admit_snapshot`; no execution plugins or write path. |
| `SourceCatalogBuildRequest.__post_init__` | `src/docspec/adapters/catalog_artifact/builder.py:92` | Catalog ID and optional succession → validated request | Existing explicit succession validation; no inferred current catalog. |
| `SourceCatalogBuilder.build` | `src/docspec/adapters/catalog_artifact/builder.py:252` | Source descriptions/rows and policy → admitted publication | Existing policy/schema checks, ordered staging, digest derivation, source/row accounting, and independent build gate remain authoritative. |
| `SuppliedRecordSource.__init__` | `src/docspec/adapters/supplied_records.py:40` | Iterable of caller records plus explicit namespace/scope/bounds → immutable source snapshot | Validates each record, stores canonical bytes, caps record count and retained byte sum, closes the source iterator, sorts qualified IDs, and rejects duplicate local IDs. |
| `supplied_record` | `src/docspec/application/supplied_records_catalog.py:45` | Raw object → checked five-field mapping | Closed shape, explicit record/version, optional title, JSON metadata, existing candidates, and duplicate rendition refusal. |
| `supplied_item_id` | `src/docspec/application/supplied_records_catalog.py:41` | Source namespace and local record ID → stable source item ID | Qualifies identity by source; same local ID in another namespace cannot silently collide. Source version remains separate from stable item identity. |
| `describe`, `iter_records`, `iter_renditions` | `src/docspec/adapters/supplied_records.py:96`, `:99`, `:107` | Immutable snapshot → repeatable source description and fresh row mappings | Caller or returned-dictionary mutation cannot alter the stored snapshot. Description binds namespace/version/scope/schema and framed sorted record bytes. |
| `supplied_renditions` | `src/docspec/application/supplied_records_catalog.py:64` | Checked candidates → source-native rendition assertions | Retains supplied locator/media/expected digest/size with deterministic rendition order; opens no document. |
| `SuppliedRecordCatalogPolicy.iter_items` | `src/docspec/application/supplied_records_catalog.py:101` | Existing policy input rows → existing catalog item | Requires matching namespace, source version, qualified row ID, schema digest, and exact rendition assertions. Preserves raw fields; normalizes only supplied title; selects supplied candidates or records explicit unavailability. |
| `_scratch_page_limit` | `src/docspec/adapters/catalog_policy_workspace.py:22` | Optional byte allowance → SQLite page ceiling | Rejects invalid/minimum-too-small allowances before directory creation; reserves database pages, rollback copy and metadata/header allowance. |
| Workspace `_open`, `_write`, `close` | `src/docspec/adapters/catalog_policy_workspace.py:100`, `:135`, `:149` | SQLite path and ceiling → bounded scratch service | Enforces expected page size and max page count, maps SQLite-full to bounded refusal, closes failed connections, and removes temporary state. Explicit resume files persist. |
| `_publish_blobs` | `src/docspec/adapters/source_catalog_store/staging.py:449` | Staged blobs → published blob root | Empty catalogs now create/sync the required blob directory during publication; reader strictness remains unchanged; directory descriptors close on every path. |
| `demonstrate` | `examples/supplied_records.py:20` | New absolute output path → catalog inspection result | Uses only public build/open/input types, actual output producer identity derived from the example bytes, one usable record and one unavailable record. |

The source adapter depends on existing application validation/policy helpers and domain types; the supplied policy remains inside application/domain/ports. Runtime owns local adapter composition. Publicly exporting the existing provider adapter does not import its optional package: that adapter still resolves `spicy_docs` only when the provider capability is invoked (`adapters/spicy_docs_source_native.py:28`).

## 3. Data flow and invariants

1. **Input snapshots are exact and repeatable.** The adapter stores canonical record bytes, not mutable caller dictionaries. Iteration decodes fresh mappings. Sorted qualified IDs make record order immaterial, while changes to raw fields, supplied candidates, namespace, scope, or source version change the snapshot description (`supplied_records.py:63`–`:94`). Candidate ordering inside a record remains supplied data; no broader order-independence is claimed.
2. **Identity is qualified without inventing source facts.** Local `recordId` becomes the catalog's document ID; source item identity includes its namespace. Caller-issued record version and raw metadata remain supplied assertions. Unknown dates, agencies, URLs and topics are absent, rather than fabricated (`supplied_records_catalog.py:123`–`:147`).
3. **Selection is explicit.** Candidate-bearing records are selected; no candidates yields `unavailable` and `supplied.no-candidate`. Selected means the policy selected a proposed input, not that its document exists or has been fetched (`supplied_records_catalog.py:116`).
4. **Artifact and acceptance choices remain distinct.** The output producer is a declaration validated before local setup. Upstream provider verifier acceptance and later catalog-reader producer acceptance are independently supplied. The builder still admits its exact staged catalog using existing verification; reading uses an explicit expected reference (`runtime/catalogs.py:39`, `:70`; `builder.py:301`).
5. **Catalog-only operations stay catalog-only.** Build creates source-catalog storage, and optional explicit resume scratch, but no document roots, content files, processor caches, runs, or current result pointer. Opening uses read-only storage and verifies an exact pin. The existing source catalog builder handles publication rather than a second runtime publication implementation.
6. **Bounds have precise scope.** Supplied records have count and retained canonical-byte bounds; they do not bound arbitrary preexisting Python object overhead or the transient serialization of one caller object. SQLite uses a page ceiling with rollback allowance; staged/published artifacts and other workers are separate. Temporary scratch is cleaned on success or failure; explicit resume state intentionally remains (`catalog-inputs.md:113`). These tests do not qualify large-corpus resource use.
7. **Coverage is not partial-update semantics.** `observed-crawl` says upstream observations are incomplete. Each resulting catalog still supplies the chosen dataset universe; omitted prior items enter planning as deletions, subject to the run's population filters. It does not instruct the runtime to preserve all omitted items. The guide now states this next to the scope definition (`docs/catalog-inputs.md:59`).
8. **Resume keeps the existing identity checks.** The convenience wrapper supplies the same explicit scratch to the existing builder recovery ledger; sources, policy, producer and catalog identity still determine compatibility. It neither creates an independent resume authority nor changes a current catalog implicitly.

Hypotheses confirmed: bounded supplied records can use the existing source/policy seam; useful local setup can be expressed without a second schema or service family; installed provider use can remain optional; empty catalog publication must create its own required blob resolver directory. The initial wording that might imply observed-crawl preserves omitted base items was corrected.

## 4. Test behavior and edge cases

These are static assertions inspected, not execution results generated by this reviewer.

| Test / probe | File:line | Evidence inspected |
| --- | --- | --- |
| Immutable/order-independent source | `tests/test_local_catalogs.py:43` | Reversing records preserves description; mutating caller metadata or returned rows cannot alter an existing snapshot; new namespace/scope/data changes its pin. |
| Same builder result and honest facts | `tests/test_local_catalogs.py:59` | Public convenience build equals explicit service assembly; exact raw metadata retained, only title normalized, topics absent, unavailable reason explicit; read-only opening leaves tree modification times unchanged. |
| Empty catalog and example | `tests/test_local_catalogs.py:86`, `:94` | Empty snapshot publishes and reopens; public supplied-record example creates only sourceCatalog storage and returns selected/unavailable rows. |
| Qualified identities | `tests/test_local_catalogs.py:102` | Equal local document IDs in separate namespaces retain equal document IDs but distinct source IDs and catalog references. |
| Wrong source/acceptance/reference | `tests/test_local_catalogs.py:113`, `:119` | Wrong policy namespace refuses; wrong accepted producer and altered digest refuse; opening missing workspace creates nothing. |
| Invalid supplied input | `tests/test_local_catalogs.py:134` | Count/byte excess, duplicate ID/candidate, unknown field and unknown scope refuse; started source generator closes on consumption failure. |
| Setup preflight | `tests/test_local_catalogs.py:162`, `:169` | Too-small scratch and malformed actual producer identifier refuse before source/resume paths are created. |
| Existing durable resume | `tests/test_local_catalogs.py:179` | Failure after source rows are staged leaves explicit resume state; retry produces the same exact artifact as a clean build. |
| Scratch cap and cleanup | `tests/test_catalog_policy_workspace.py:88`, `:101`, `:116` | Oversized row refuses under SQLite ceiling and physical test-directory size stays within allowance; minimum refusal/connection failure removes temporary state; oversized existing resume database refuses without losing its data. |
| Installed local path | `tests/support/installed_runtime_probe.py:42` | Public helper replaces manual catalog assembly; bounded supplied metadata-only record builds/opens outside checkout with exact facts and only catalog storage; existing capture/process lifecycle uses the provider-style fixture catalog. |
| Installed provider path | `tests/test_source_catalog_installed_wheel.py:563` | Public SpicyDocs adapter admits a pinned provider artifact using explicit verifier acceptance; public build/open yields expected document IDs and exactly the adapter's qualified source IDs; only catalog storage is created. |

The installed-provider check uses the existing isolated pinned-wheel fixture environment. It verifies this public integration path; it is not evidence of live collection completeness, network acquisition, arbitrary provider support, or a sibling package release. The root owns execution and final package gate evidence.

## 5. Findings

### F1 — Resolved: observed-crawl wording could imply omitted-item preservation

The source adapter honestly labels an incomplete observed population, while existing planner snapshot comparison schedules omissions as deleted items. The original guide explained coverage but immediately recommended later processing without explaining that distinction. `docs/catalog-inputs.md:59` now states that omitted prior items enter planning as deletions subject to the selection filters, and that observed-crawl is not a partial-update instruction. This resolves the ambiguity without inventing a partial-update system.

### F2 — Resolved during implementation: empty publication had no readable blob root

The empty-input test exposed that `_publish_blobs` returned before creating `.blobs` when no staged digest tree existed. The new path creates and syncs this directory during publication (`adapters/source_catalog_store/staging.py:476`), then closes the descriptor normally. Strict read-only opening is preserved. The actual empty snapshot test builds and reopens the result (`tests/test_local_catalogs.py:86`).

### F3 — Resolved during implementation: producer preflight needed the real serialized validator

Checking only `isinstance(Producer)` does not validate a replaced malformed identifier. `build_local_catalog` now calls `Producer.from_dict(producer.as_dict(), ...)` before scratch or storage (`runtime/catalogs.py:41`). A negative test uses an otherwise valid producer with a malformed implementation identifier and an absent resume parent, then verifies both paths remain absent (`tests/test_local_catalogs.py:169`).

### Remaining findings

None in the bounded D04/D06 catalog slice. This change does not implement collection outcomes/partial-input policy (D08), a preview-and-growth workflow (D07), export convenience, or per-candidate observed transport metadata. Those should not be inferred from the new input source or its arbitrary metadata field.

## 6. Conclusion and acceptance

**APPROVE, high confidence for the scoped static review.** The new catalog-only public operations simplify real local setup and preserve existing admission, storage and resume behavior. The supplied-record path uses a bounded immutable snapshot, stable source-qualified identity, the existing catalog schema and explicit unavailability without fabricated collection/acquisition evidence.

The D06 acceptance shape is present: a local-record example and a pinned installed-provider example use public interfaces and do not require a sibling checkout for ordinary use. Parent-executed installed validation establishes the runtime outcome. This also closes D04's catalog-only composition gap; it does not establish D04 export convenience or all broader lifecycle acceptance.

No tests or builds were executed by this reviewer. Parent-owned focused/full/installed results should be appended separately, distinguishing the tested snapshot from subsequent changes.

## Parent execution evidence

The final focused gate passed **74 tests in 9.52 seconds** using
`uv run --frozen --extra dagster pytest -q` over catalog inputs, catalog scratch,
installed wheels, import directions, local experiment configuration, worker
identity, and release integrity. The installed probes use isolated package
environments; no live collection or network acquisition is claimed.

An earlier combined regression run passed 1,152 tests and failed six. Its stale
composition allowlist and unrequested transport-version expectation were corrected;
the recovery scenario passed in this final gate after the disposition profile
edit finished. The other three failure cases concern separately reviewed D16
fixture expectations and are outside this catalog slice. Full regression
validation of the combined final tree follows the remaining transport edits.
