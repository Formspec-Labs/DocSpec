> Review provenance: this independent review was supplied with receiving commit
> `4df1b44` on `codex/govinfo-bill-example`. Paths and line numbers describe
> that worktree. The current integration uses one SpicyDocs 0.3.0 wheel in
> `vendor/`, adds wrong-bill and placeholder refusal cases, and updates the
> documented command. Those later changes passed local installed tests;
> they have not received a fresh independent review. The separate test
> maintenance patch described at the end was already superseded here.

# DocSpec GovInfo bill example review

**VERDICT: APPROVE.** No remaining actionable finding in the reviewed example.
One acquisition-timestamp finding was fixed and covered by an installed-wheel
regression. This was a static review; the implementing agent executed the tests
and documented command. I inspected their retained proof files and independently
checked the provider wheel digest.

Reviewed worktree: `taskdir/docspec-govinfo-bill-example`, 2026-09-12.

## Findings and resolutions

**Resolved P2 — response observation was recorded as acquisition start.**
The initial adapter supplied `capture.observed_at` to
`FetchMetadata.acquisition_started_at` (`src/docspec/ports/content_fetcher.py:19`).
SpicyDocs samples `observed_at` after response EOF
(`spicy_docs/transport/capture.py:164`), so those are distinct events.

The final adapter samples its injected clock before `acquire_text`
(`examples/govinfo_bill_fetcher.py:96`), stores that value in the acquisition
receipt and `FetchMetadata` (`:109`, `:115`), and retains response observation
separately (`:48`). The probe injects start 11:59:59 and response 12:00:00 and
asserts both fields (`tests/support/govinfo_bill_probe.py:150`). Closed.

The parent's earlier findings are also resolved in the reviewed code:

- Configuration identity reads the actual acquiring client's budget
  (`govinfo_bill_fetcher.py:89`).
- Refusal retention creates its parent directory (`govinfo_bill_fetcher.py:54`).
- `_finish` saves run inspection and failures before attempting retention or
  later indexing captured-file records (`govinfo_bills.py:70`).
- Run timestamps come from the caller's clock separately from source response
  observations (`govinfo_bills.py:165`, `:172`). The guide accurately describes
  these as pre-run evidence timestamps, not measured finish times.

## Patch summary and scope

The example composes an installed SpicyDocs provider with existing DocSpec APIs:
one explicit bill identity → retained BILLSTATUS → reviewed version/format
metadata → one explicit XML candidate → capture/extract/segment/process → a
second processor run over the retained upstream layers.

Read the two new example modules, all three synthetic XML fixtures and their
README, installed-wheel test/probe/manifest, the new guide, and changed fetcher
guide/to-do sections. Inspected the ordinary catalog, experiment, inspection,
content-fetcher and visible-text calls and the existing phrase processor.
No DocSpec library code or mandatory provider dependency was added.

The fixtures deliberately use invented document content with real locator
shapes. The guide does not present them as congressional source captures.

## Function trace

Paths are relative to the DocSpec review worktree unless marked SpicyDocs.

| Function | Location | Input → output | Verified behavior |
| --- | --- | --- | --- |
| `run_example` | `examples/govinfo_bills.py:96` | output, bill identity, explicit package, live flag → summary | Refuses existing/relative output; captures status before selection; preserves all parsed versions/formats and supplies only selected XML as a candidate. |
| `fixture_transport` | `examples/govinfo_bills.py:42` | request list → MockTransport | Only GET for exact status/IH/EH URLs; unknown requests fail. |
| `provider_installation` | `examples/govinfo_bill_fetcher.py:26` | installed distribution → package/file identity | Rejects editable install; hashes installed provider files; absent installer wheel digest stays null. |
| `capture_facts`, `retain_refusal` | `examples/govinfo_bill_fetcher.py:44`, `:52` | capture/error → inspectable facts/files | Retains complete known response bytes and digest; keeps source failure details before rethrow. |
| `BillContentFetcher.locator`, `configuration_digest` | `examples/govinfo_bill_fetcher.py:81`, `:85` | status, package, client → exact locator/digest | Uses provider selection rules; pins status digest, actual budget and provider installation. |
| `BillContentFetcher.fetch` | `examples/govinfo_bill_fetcher.py:92` | candidate and task byte allowance → FetchStream | Requires exact selected XML; delegates to provider with the byte limit; saves request evidence; yields unchanged bytes and bound task/attempt identity. |
| `_processor` | `examples/govinfo_bills.py:61` | phrases/revision → configured existing processor | Saves exact resource bytes and digest; a changed phrase resource changes processor identity. |
| `_finish` | `examples/govinfo_bills.py:70` | prepared run → retained result/inspection | Runs, inspects, records failures first; retains successful run and opens read-only inspection. |
| `_matches` | `examples/govinfo_bills.py:91` | inspected derived layer → matches | Reads the selected processor's saved derived records. |
| `main` | `examples/govinfo_bills.py:205` | CLI arguments → exit/output | Requires explicit package ID; live work is opt-in. |
| `build_local_catalog`, `open_local_catalog` | `src/docspec/runtime/catalogs.py:25`, `:66` | supplied records → admitted catalog | Ordinary bounded builder and exact source-catalog admission; no document fetch occurs. |
| `SuppliedRecordSource`, policy | `src/docspec/adapters/supplied_records.py:28`; `application/supplied_records_catalog.py:74` | bounded caller records → source/catalog rows | Complete-snapshot explicitly describes submitted records, not all publisher bills. Identity/candidate equality is checked. |
| `prepare_local_experiment` | `src/docspec/runtime/experiments.py:65` | catalog, implementations, base, limits → prepared run | Builds the normal plan and calls existing run preparation; no parallel experiment format. |
| `open_local_inspection` | `src/docspec/runtime/inspection.py:21` | saved plan/run/release → InspectionView | Reads retained evidence without constructing fetchers or processors. |
| `VisibleTextExtractor._read`, `extract` | `src/docspec/processing/visible_text_runtime.py:97`, `:111` | captured bytes → representation/evidence | Checks captured digest, invokes existing XML parser, keeps original-byte mappings. |
| `XmlVisibleTextExtractor.extract`, `_parse_xml` | `src/docspec/processing/visible_text.py:362`, `:432` | XML bytes → normalized blocks | Expat tracks source byte coordinates; metadata is also visible text, as documented. |
| `PhraseMatchProcessor.process` | `examples/phrase_match_processor.py:105` | pinned segment/resource → lexical derived record | Literal matching uses original UTF-8 segment offsets, resource limits and enclosing source evidence. No legal classification. |

## Data flow and invariants

1. Status capture bytes are retained under `source-evidence`; status facts,
   provider identity, all text versions, and selected package enter catalog
   metadata (`govinfo_bills.py:118` onward). Only one source-stated XML URL enters
   `candidateRenditions`; PDF/HTML remain inspectable metadata.
2. `acquire_text` rechecks original status bytes and exact offered XML before
   requesting the body. The adapter passes the task's remaining byte allowance
   through to the provider (`govinfo_bill_fetcher.py:98`). It adds no transport,
   parsing, version selection, or fallback implementation.
3. Raw bill bytes remain in the captured-file blob. Normalized visible text is a
   separate representation; processor quote offsets address segment bytes, with
   an enclosing original-XML span. The guide distinguishes these coordinates.
4. The first source client closes before the second `prepare_local_experiment`
   call (`govinfo_bills.py:170`). The second call supplies `base_release` and a
   changed processor. The example checks payload equality for files,
   representations and segments plus zero new work in those layers (`:177`).
5. A requested-but-unoffered package leaves status evidence and stops before any
   text request. A requested XML refusal keeps source refusal evidence and the
   failed run, without manufacturing a successful experiment summary.

## Tests and execution evidence

`tests/test_govinfo_bill_installed_wheel.py:16` builds DocSpec, creates a clean
environment, installs both wheels, copies only example assets, removes checkout
import environment variables, and invokes the probe. It also checks DocSpec's
wheel contains neither provider modules nor bundled wheels (`:35`).

The probe's assertions cover:

| Branch | Probe location | Assertion trace |
| --- | --- | --- |
| Installed provider identity | `:31` | Wheel SHA, site-packages imports and installed-file/wheel-byte equality. |
| Exact XML and evidence spans | `:53` | One captured file equals the authored XML; derived records point to saved segments and original-XML bounds; quote offsets decode exactly. |
| Two processing runs | `:78` | Source closes before second run; only one provider text call, zero repeated upstream layers. |
| All offered formats | `:94` | Catalog retains both packages, formatted-text and PDF links while only IH XML is fetched. |
| Existing output and unoffered selection | `:101`, `:110` | Existing bytes unchanged; selection failure retains status and no text request evidence. |
| Text 404 refusal | `:121` | Refusal body/digest/request count survive, failed run is inspectable, successful summary is absent. |
| Missing evidence directory | `:143` | Refusal receipt survives with newly created parents. |
| Changed budget and timestamp roles | `:149` | Digest uses acquiring client budget; start and response-observation clocks stay distinct. |

Inspected retained execution evidence supplied by the implementing agent:

- `taskdir/bill-docspec-installed-proof.json`: pass with provider 0.3.0, one text
  call, client closed before reprocessing, 686 exact XML bytes, 2 first matches
  and 3 later matches, refusal and unoffered-selection checks true.
- `taskdir/bill-docspec-documented-command-summary.json`: the documented command
  produced an IH capture, all 2 versions/5 formats, matching capture digest, and
  zero new upstream work. Installer wheel hash is null; installed-file digest
  is retained, exactly as documented.

The final provider manifest pins source revision
`8e485fe052c794cf18b041debe970c813812c05b` and wheel SHA-256
`bef15f967b0840ccc119c812edca92b38c63adb8943074be17655b86c96f83f1`.
Independent `shasum` of the committed-candidate fixture wheel matches that digest.

## Hypotheses and conclusion

- H1: The adapter might duplicate publisher URL/parse policy. **Refuted**: it
  calls the provider's selector/acquirer and contains no publisher parser.
- H2: Processor-only reprocessing might contact the source again. **Refuted** by
  the closed-client composition and inspected installed-wheel proof/assertions.
- H3: Acquisition evidence might confuse response observation with start time.
  **Confirmed, fixed, and regression covered.**
- H4: The example might imply legislative semantics or collection coverage.
  **Refuted** by explicit one-version scope, synthetic fixtures and lexical-only
  guide; metadata inclusion and enclosing-span limitations are stated.

Coverage of changed paths: **ADEQUATE** for this bounded example. Confidence:
**HIGH** on reviewed source/provenance behavior. No live DocSpec example run or
collection-wide coverage is established by this review. Broader repository test
results and commit/push state remain the coordinating parent's responsibility.

## Separate test-maintenance review

**APPROVE**, static review only. The two-file correction changes no runtime code.

- `tests/test_regulations_gov_catalog.py:375` now expects the public Rulespec
  `ArtifactVerificationError` with the stable diagnostic prefix for a lone
  Unicode surrogate at `$/configuration/language`. Pinned Rulespec 1.0.12
  `_artifact.py:236–247` deliberately converts the encoder's `UnicodeEncodeError`
  to this error; its public package exports the type. DocSpec
  `domain/identity.py:254–257` delegates to that encoder without changing errors.
- The test still verifies source refusal first (`:369–371`) and an uncached
  policy digest (`:373`). Policy computation writes its cache only after
  successful encoding (`application/regulations_gov_catalog/policy.py:298–303`).
  The replacement assertion therefore checks the current dependency interface
  without weakening the original ordering or cache checks.
- Removing `Mapping` from `tests/test_source_catalog_rows.py` removes an unused
  import. A search found no other occurrence in that file.

Both defects were present in `HEAD`; the retained baseline pytest and Ruff
outputs reproduce the three stale exception expectations and unused import.
The implementing agent owns focused and full test execution after this change.
