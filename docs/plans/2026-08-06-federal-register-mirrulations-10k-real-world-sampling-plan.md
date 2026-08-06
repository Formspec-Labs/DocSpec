# Federal Register and Mirrulations 10,000-Document Qualification Plan

**Status:** Implementation in progress; rewritten after architecture and evidence review

**Date:** 2026-08-06

**Target:** DocSpec acquisition, extraction, recovery, and release qualification against real source material

## Decision

Build one fixed qualification corpus with 10,000 document identities:

| Source | Documents | Required source objects | Material |
| --- | ---: | ---: | --- |
| Retained Federal Register cache | 6,408 | 6,408 | One exact XML file per document |
| Mirrulations public S3 mirror | 3,592 | 7,184 | One metadata JSON object and one HTML rendition per document |
| **Total** | **10,000** | **13,592** | XML, JSON, and HTML |

Reuse the 6,408 Federal Register files already retained by SpicyRegs. Use the existing SpicyRegs Mirrulations corpus builder to select the remaining 3,592 document identities. DocSpec will acquire the selected Mirrulations objects directly from the public S3 bucket.

The campaign tests DocSpec with real documents and real source acquisition. It does not establish formal DocSpec SCALE conformance or a production performance commitment.

## Success terms

The campaign uses three distinct status terms:

1. **Catalog sealed:** the input manifests prove exactly 10,000 document identities and 13,592 required source objects.
2. **Run closed:** every document has one terminal DocSpec disposition, and the campaign report classifies every required source object as processed, acquisition-failed, processing-failed, or not-attempted.
3. **Qualification passed:** all 13,592 source objects were acquired and completed their required extraction and segmentation stages; no document ended as accepted-failure or rejected-run; recovery, equivalence, negative, and release-verification gates passed.

A run may close with failures. Such a run produces useful evidence, but its qualification verdict is **failed**.

## Verified starting point

The implementation must recheck these facts and record the results:

- The SpicyRegs final Federal Register draw contains exactly 10,000 unique document identities.
- The retained cache contains 6,408 XML files and 6,408 receipts.
- All 6,408 retained files match their receipt byte counts and SHA-256 digests.
- All retained document numbers belong to the final draw.
- The other 3,592 final-draw identities have no retained XML file.
- All retained XML files pass DocSpec's current UTF-8 decoding and XML parsing behavior.
- SpicyRegs already owns a versioned Mirrulations draw schema, deterministic pair selection, canonical draw digest, and draw identifier.
- The public **mirrulations** S3 bucket currently permits anonymous listing under **raw-data/SEC/SEC-202**.
- DocSpec currently provides a local-file content fetcher. Its normal local-run composition always selects that fetcher.
- The campaign implementation now saves candidate-level checkpoints after capture and extraction. The qualification gates must prove that recovery uses those checkpoints without reacquiring verified source bytes.

These facts define the starting point. They do not prove campaign completion.

## Ownership

### SpicyRegs owns source discovery

SpicyRegs owns:

- the final Federal Register draw;
- Federal Register cache receipts and source URLs;
- Mirrulations listing and pairing rules;
- **mirrulations-document-corpus-draw-v1**;
- the Mirrulations draw digest and **draw_id**; and
- exclusion of comments, dockets, tombstones, superseded JSON revisions, and unpaired objects.

The campaign will run the existing SpicyRegs Mirrulations draw tool with **max_documents=3592**. The tool will write its sealed draw into DocSpec's ignored qualification output directory. A canonical producer receipt will record the SpicyRegs repository commit, the SHA-256 digest of the builder source file, the builder schema version, the draw path and digest, and the resulting **draw_id**. If a draw already exists, the runner must verify that receipt before using the draw. A missing receipt or a changed commit, builder path, builder digest, draw path, or draw digest must stop preparation.

DocSpec will not copy or reimplement Mirrulations selection. It will validate the SpicyRegs draw and translate its records into DocSpec source items.

### DocSpec owns execution evidence

DocSpec owns:

- the qualification source catalogs;
- source-item and candidate mapping;
- exact-byte acquisition;
- extraction and segmentation;
- candidate-level checkpoints;
- processing;
- terminal document dispositions;
- releases and release verification; and
- the qualification report and verdict.

### The qualification runner owns composition

A dedicated checked-in qualification runner will connect the frozen SpicyRegs artifacts to DocSpec. It will:

- validate producer artifacts without changing them;
- translate producer records into ordinary DocSpec records;
- build the routing content fetcher from a sealed execution manifest;
- invoke normal DocSpec planning, execution, delivery, reconciliation, commit, and verification services; and
- write campaign artifacts under DocSpec's ignored output directory.

The runner is a composition root, not a second processing engine. DocSpec's domain and application modules will not import SpicyRegs or boto3.

The SpicyRegs worktree remains read-only. The campaign must not modify or stage its existing user-owned changes.

## Input artifacts

### Federal Register

Read these artifacts:

~~~text
/Users/mikewolfd/Work/spicy-regs/output/scale-dr-10k-2026-08-05/
  draw-manifest-final.json
  cache-xml/documents/*.xml
  cache-xml/receipts/*.json
~~~

For each retained document, preserve:

- the final-draw document identity and metadata;
- the exact XML bytes;
- the receipt byte count and SHA-256 digest;
- **source_url** and **resolved_url**;
- retrieval time, ETag when present, and last-modified time; and
- the receipt as producer evidence.

Use **cache-xml/documents** as the sealed local fetcher root. Candidate locators must be relative file names under that root. Absolute paths must not enter a DocSpec source catalog.

The aggregate **cache-xml/source-lock.json** is supporting evidence only. It does not identify the final 10,000-document draw and cannot close the Federal Register population.

### Mirrulations

Run the existing SpicyRegs draw builder against:

- bucket: **mirrulations**;
- prefix: **raw-data/SEC/SEC-202**; and
- maximum documents: **3,592**.

Accept only a draw that:

- uses **mirrulations-document-corpus-draw-v1**;
- has a valid canonical **draw_id**;
- contains exactly 3,592 unique document identities;
- contains one selected metadata JSON object and one selected HTML or HTM rendition for each identity;
- records the bucket, key, size, ETag, and last-modified time for each selected object; and
- contains no object outside the declared bucket and prefix.

DocSpec consumes the completed draw. It does not read a mutable S3 listing during catalog translation.

## Exact DocSpec mapping

### Federal Register source item

| Field | Value |
| --- | --- |
| Source item ID | **urn:docspec:qualification:federal-register:<document-number>** |
| Source item version | **sha256:<receipt source_sha256>** |
| Candidate ID | **federal-register-xml** |
| Candidate locator | Receipt **cache_file**, relative to the sealed local root |
| Media type | **text/xml** |
| Expected size | Receipt **source_bytes** |
| Expected digest | **sha256:<receipt source_sha256>** |
| Transport version | Versioned canonical digest of the receipt URL and available HTTP version metadata |
| Metadata | Final-draw metadata, source URLs, receipt reference, and receipt retrieval fields |

The complete canonical SourceItem record remains the planner's change identity. The explicit version identifies the exact Federal Register bytes.

### Mirrulations source item

Each document becomes one SourceItem with two required candidates in this order:

1. **metadata-json**
2. **rendition-html**

DocSpec processes both candidates. They are not fallbacks.

Build **SourceItem.version** as:

~~~text
sha256:<SHA-256 of DocSpec canonical JSON over:
  {
    format: "docspec-qualification-mirrulations-source-version",
    formatVersion: "1.0",
    documentId: <document ID>,
    metadataObject: {
      bucket, key, size, etag, lastModified
    },
    renditionObject: {
      bucket, key, size, etag, lastModified
    }
  }>
~~~

Use this candidate mapping:

| Field | metadata-json | rendition-html |
| --- | --- | --- |
| Locator | **s3://mirrulations/<metadata key>** | **s3://mirrulations/<rendition key>** |
| Media type | **application/json** | **text/html** |
| Expected size | Listed S3 size | Listed S3 size |
| Expected digest | Unknown until capture | Unknown until capture |
| Transport version | Canonical S3 transport identity | Canonical S3 transport identity |
| Metadata | Raw bucket, key, ETag, last-modified time, size, and public source URL | Raw bucket, key, ETag, last-modified time, size, and public source URL |

Define the canonical S3 transport identity as:

~~~text
urn:docspec:s3-transport-version:v1:
  <SHA-256 of DocSpec canonical JSON over
    {bucket, key, size, etag, lastModified}>
~~~

The S3 fetcher must return that exact transport identity only after the response ETag, last-modified time, and content length equal the sealed draw. The ETag is transport metadata, not a SHA-256 digest.

Derive the public HTTPS source URL from the bucket and percent-encoded key with one versioned mapping rule. Use the **s3://** locator for acquisition and the HTTPS URL for provenance.

## Sealed execution composition

The qualification runner will require a canonical **qualification-execution-manifest-v1** before planning or fetching. The manifest will contain:

- the Federal Register final-draw reference and SHA-256 digest;
- the Federal Register receipt-set digest;
- the sealed Federal Register local root;
- the SpicyRegs Mirrulations draw reference, digest, and **draw_id**;
- the SpicyRegs builder commit and source-file digest;
- the S3 region, anonymous mode, allowed bucket, and allowed prefix;
- local and S3 fetcher implementation identifiers;
- local and S3 fetcher configuration digests;
- the routing rule for local and **s3://** locators;
- chunk size, maximum object bytes, retry policy, and worker count;
- the DocSpec processing plan, profiles, and policies;
- the source-catalog reference;
- the output roots; and
- the qualification runner implementation digest.

The runner will verify the manifest, reconstruct the same routing fetcher for every worker, and pass it to **StoreExecutionService**. Worker reconstruction must fail if any implementation ID, configuration digest, root, bucket, prefix, or input digest differs.

Manifest validation must rebuild both source populations before acquisition. For Federal Register material, it must reread every retained receipt, verify every XML byte count and SHA-256 digest, recompute the receipt-set digest, and compare all counts and byte totals with the manifest. For Mirrulations material, it must revalidate the draw, **draw_id**, pair counts, object counts, and byte totals.

This explicit composition leaves the published local-run request unchanged. No hidden campaign state may choose the fetcher.

## Acquisition cleanup

Extend **FetchStream** into an explicitly closeable resource:

- **close()** must be idempotent;
- **__enter__** and **__exit__** must support context-managed use;
- the resource must close its underlying local descriptor or S3 response body directly; and
- closing an unstarted or partly consumed stream must release the underlying resource.

Update **StoreExecutionService._capture_candidate()** to close every returned FetchStream in a **finally** block around blob capture. An adapter that fails after opening a source but before returning a FetchStream must close the source itself.

The S3 fetcher must:

- use unsigned boto3 requests without ambient credentials;
- accept only the configured bucket and prefix;
- send **IfMatch** with the sealed ETag;
- compare returned ETag, last-modified time, and content length with the sealed draw;
- stream chunks through DocSpec's blob store;
- enforce the per-object and remaining-store byte limits;
- reject truncated, oversized, changed, missing, and inaccessible objects with typed failures; and
- close the S3 response on success, producer failure, consumer failure, and early termination.

The routing fetcher must reject unknown locator schemes before any I/O.

## Candidate-level recovery

DocSpec must preserve verified progress inside a multi-candidate source item.

Permit two nonterminal acquisition frontiers:

1. an ordered prefix of candidates with verified captures and matching verified representations; and
2. the same prefix plus one verified captured file awaiting extraction.

After each candidate capture, save a verified checkpoint. After each extraction and its receipt, save another verified checkpoint. Checkpoint verification must confirm:

- captured files form an ordered candidate prefix;
- representations and extraction receipts cover the corresponding captured prefix;
- at most one captured file awaits extraction;
- every referenced blob passes size and digest verification;
- candidate ID, media type, transport version, and expected source evidence still match; and
- cumulative work-budget counters reconstruct from verified progress.

On resume:

- reuse a verified capture that still awaits extraction;
- reuse every verified capture and representation in the completed prefix;
- begin acquisition at the first uncaptured candidate; and
- reject a partial checkpoint with a gap, duplicate, changed transport version, missing receipt, or invalid blob.

Recovery tests must interrupt:

- after JSON capture but before JSON extraction;
- after JSON extraction but before HTML acquisition;
- during HTML streaming after JSON completed;
- after HTML capture but before HTML extraction; and
- after the complete extraction stage.

Each test must prove that DocSpec does not refetch verified JSON or HTML objects.

### Run-level restart

A bounded **DocumentStore** job is the restart block. The runner must distinguish durable planning state from a final run receipt:

- If no planned-store ledger exists for the plan, run planning once and seal that ledger.
- If the planned-store ledger exists, resume from it even when interruption prevented creation of **run-reference.json**.
- If the ledger path exists but its root, member, or planned store references fail verification, stop. Do not treat corrupt state as a missing plan and do not replace it.

For each planned job, load its latest durable revision:

- If the latest revision is sealed, skip extraction, segmentation, processing, and source acquisition. Verify its delivery receipt and return the sealed reference for reconciliation.
- If the latest revision is not sealed, execute that job from its latest verified candidate or stage checkpoint.

The normal reconciliation step must still verify the complete planned task set, every sealed store, and every delivery receipt before it writes a run receipt. Restart may therefore scan durable task and result records, but it must not rebuild the source plan, refetch completed candidates, or deeply re-execute sealed jobs.

The local store repository's **latest** lookup may validate only the newest revision after checking that the revision directory contains only declared regular members. The explicit **revisions** operation must load and structurally validate every historical member. It does not prove that valid historical bytes remained unchanged because DocSpec does not maintain an anchored revision chain.

Immutable writes must create temporary files in a dedicated staging directory outside every declared revision directory. A hard kill may leave staging debris, but that debris must not make a valid revision set unreadable. Undeclared members inside a revision directory must still fail closed.

## Reproducible catalogs

Build three sealed snapshot catalogs from the same fixed source artifacts:

| Tier | Federal Register documents | Mirrulations documents | Documents | Required source objects |
| --- | ---: | ---: | ---: | ---: |
| Smoke | 64 | 36 | 100 | 136 |
| Intermediate | 641 | 359 | 1,000 | 1,359 |
| Full | 6,408 | 3,592 | 10,000 | 13,592 |

Select members for the smaller tiers by stable SHA-256 ordering within each source. After selection, sort SourceItem records by item ID and version before writing them through **LocalJsonlSourceCatalog**.

Every tier must record:

- the parent Federal Register and Mirrulations manifest references;
- the selection rule and its version;
- ordered member identities;
- document and source-object counts;
- source breakdown;
- media-type breakdown; and
- its canonical catalog digest.

Two builds from unchanged inputs must produce byte-identical catalog distributions.

## Pipeline

Run each tier through the normal DocSpec lifecycle:

1. verify the qualification execution manifest;
2. verify the sealed source catalog;
3. plan bounded DocumentStore jobs once, or load the existing verified planned-store ledger on restart;
4. acquire and preserve exact bytes;
5. extract XML, JSON, and HTML;
6. segment extracted representations;
7. run the configured deterministic processor set;
8. checkpoint candidate and stage progress;
9. deliver and reconcile terminal stores;
10. commit the release; and
11. verify the release independently.

Run the smoke tier first. Run the intermediate tier only after smoke and recovery gates pass. Run the full tier only after intermediate measurements establish safe worker, byte, memory, and duration limits.

## Candidate census and verdict

The campaign reporter must derive one final status for each of the 13,592 catalog candidates:

- **processed:** captured, extracted, and represented in the verified release with a valid segmentation receipt; the receipt may name zero segments for valid empty content;
- **acquisition-failed:** acquisition ended in a recorded failure;
- **processing-failed:** capture succeeded, but extraction or segmentation failed;
- **not-attempted:** an earlier required candidate ended the source item before this candidate began.

The report must reconcile:

~~~text
processed
+ acquisition-failed
+ processing-failed
+ not-attempted
= required catalog candidates
~~~

The report must also reconcile all 10,000 source items to one terminal DocSpec disposition.

Set the qualification verdict to **passed** only when:

- all 13,592 candidates are **processed**;
- all 10,000 source items are captured successfully;
- accepted-failure and rejected-run counts are zero;
- recovery and equivalence gates pass; and
- the full release passes independent verification.

Any other closed outcome receives a **failed** verdict with complete evidence.

## Validation gates

Before preparation, the runner must execute one closed, checked-in mapping from each required gate name to exact pytest selectors. Each gate must contain at least one selector. The gate receipt must record the selectors and their collected, passed, failed, errored, and skipped counts. It must also record the full repository test result and lint result. Every gate passes only when all collected cases pass and none fail, error, or skip.

The runner must hash every source and evidence file before and after the checks, excluding only declared version-control, environment, cache, build, and campaign-output directories. It may seal the gate receipt only when both file sets match. The execution manifest must name that receipt and its identity. Any later source or test change invalidates the receipt and every manifest that depends on it.

### Source gates

- Federal Register membership is exactly 6,408 unique final-draw identities.
- All 6,408 XML files match their receipt size and SHA-256 digest.
- The SpicyRegs Mirrulations draw validates under its existing schema and **draw_id** rule.
- The Mirrulations draw contains 3,592 unique identities and 7,184 selected objects.
- Every Mirrulations identity has exactly one selected JSON object and one selected HTML or HTM object.
- No source locator escapes the configured local root, S3 bucket, or S3 prefix.
- The full catalog contains exactly 10,000 SourceItem records and 13,592 candidates.

### Composition gates

- The execution manifest seals every acquisition input and fetcher choice.
- Worker reconstruction produces the same routing and fetcher configuration digests.
- The runner rejects an altered root, bucket, prefix, anonymous mode, implementation ID, or configuration digest.
- No DocSpec domain or application module imports SpicyRegs or boto3.

### Cleanup gates

Tests prove direct response cleanup after:

- a successful complete read;
- an oversized response declared before iteration;
- a stream that crosses the byte limit;
- a truncated response;
- an ETag mismatch;
- a last-modified mismatch;
- a content-length mismatch;
- a digest mismatch in blob capture;
- a consumer exception; and
- an unstarted stream that the consumer closes.

### Recovery gates

- Candidate-level checkpoints pass independent verification.
- Resume never refetches a verified candidate.
- Resume rejects tampered checkpoint records and blobs before source I/O.
- Restart without a final run reference uses the durable planned-store ledger and does not invoke planning again.
- A sealed job bypasses deep execution; an unfinished job enters normal checkpoint recovery.
- A corrupt existing planned-store ledger fails closed instead of triggering a new plan.
- Latest-revision lookup avoids a full-history read, while the explicit revision scan still loads and structurally validates every historical member.
- A simulated hard-kill staging file cannot block latest-revision recovery, while undeclared revision members remain invalid.
- Resumed and uninterrupted runs produce equal active logical state for the same catalog and policies.
- Release verification succeeds for both runs.

### Negative source gates

Tests fail closed for:

- modified Federal Register XML;
- modified or mismatched Federal Register receipt;
- Federal Register membership outside the final draw;
- duplicate source item or candidate ID;
- missing Mirrulations JSON or HTML pair;
- changed S3 ETag;
- changed S3 last-modified time;
- wrong S3 content length;
- object over the byte limit;
- unknown locator scheme; and
- locator outside the allowed root, bucket, or prefix.

### Tier gates

- The 100-document tier closes and passes before the 1,000-document tier starts.
- The 1,000-document tier closes and passes before the 10,000-document tier starts.
- Before starting a later tier, the runner must parse the predecessor census as canonical JSON and recompute every verdict-bearing field from the predecessor's verified release, source catalog, controls, and gate receipt. The runner must reject an edited or stale census.
- The full run closes exactly 10,000 source items and 13,592 candidate statuses.
- The full qualification verdict follows the strict pass rule above.

## Implementation phases

### Phase 0: Freeze producer inputs

- Revalidate the final Federal Register draw, receipts, and retained files.
- Run the existing SpicyRegs Mirrulations draw tool with 3,592 documents.
- Record producer schema, repository commit, builder digest, artifact digests, counts, and source boundaries.
- Validate the draw with SpicyRegs before DocSpec consumes it.
- Reject a preexisting draw unless its canonical producer receipt matches the current SpicyRegs commit, builder, and exact draw bytes.

**Exit gate:** Both producer inputs are sealed, validated, and read-only.

### Phase 1: Build the qualification translator and catalogs

- Define the DocSpec-owned qualification execution manifest and report schemas.
- Translate Federal Register receipts and the sealed Mirrulations draw into the exact mappings in this plan.
- Build reproducible smoke, intermediate, and full catalogs.
- Add schema, identity, ordering, count, and tamper tests.

**Exit gate:** Repeated builds produce byte-identical catalogs with exact counts and canonical identities.

### Phase 2: Implement safe mixed acquisition

- Add the closeable FetchStream behavior.
- Add anonymous S3 and routing content fetchers.
- Add strict S3 response-version and allow-list checks.
- Add deterministic cleanup tests.
- Build the routing fetcher only from the sealed execution manifest.

**Exit gate:** Unit tests cover successful reads, every cleanup path, every version mismatch, and every boundary rejection.

### Phase 3: Implement candidate- and run-level recovery

- Permit and verify the two legal partial-candidate checkpoint frontiers.
- Save checkpoints after candidate capture and extraction.
- Resume from the first incomplete candidate stage.
- Restore work-budget counters from verified partial progress.
- Detect restart from the durable planned-store ledger rather than the final run reference.
- Bypass deep execution for sealed jobs and send only unfinished jobs through checkpoint recovery.
- Keep full task-set and delivery-receipt verification in reconciliation.
- Add the required two-candidate interruption tests.

**Exit gate:** Every recovery test proves zero reacquisition of verified candidates, no replanning after durable planning completes, fail-closed handling of corrupt recovery state, and equal final logical state.

### Phase 4: Run smoke qualification

- Run 100 documents and 136 candidates.
- Exercise all recovery and negative gates.
- Produce a closed candidate census and independently verified release.
- Seal exact cleanup, negative-source, recovery, equivalence, release-verification, and repository-governance selector results.

**Exit gate:** All 136 candidates are processed, all 100 items succeed, and the smoke verdict is passed.

### Phase 5: Run intermediate qualification

- Run 1,000 documents and 1,359 candidates.
- Measure bytes, memory, duration, retries, and worker behavior.
- Set full-run resource bounds from the measured evidence.

**Exit gate:** All 1,359 candidates are processed, the release verifies, and measured bounds support the full run.

### Phase 6: Run full qualification

- Run 10,000 documents and 13,592 candidates.
- Resume from verified checkpoints after operational interruption.
- Reconcile every item and candidate.
- Verify the full release independently.
- Publish the human-readable and machine-readable reports.

**Exit gate:** The run closes, the candidate census balances, and the strict qualification rule determines the final verdict.

## Outputs

Keep generated artifacts outside Git under:

~~~text
/Users/mikewolfd/Work/DocSpec/output/qualification/fr-mirrulations-10k-v1/
~~~

The campaign will produce:

- the sealed SpicyRegs Mirrulations draw;
- producer validation receipts;
- the qualification execution manifest;
- the sealed per-gate selector and repository test receipt;
- smoke, intermediate, and full source catalogs;
- exact-byte blob references;
- plans, checkpoints, terminal dispositions, and store receipts;
- releases and release-verification receipts;
- the 13,592-candidate census;
- a machine-readable qualification report; and
- a concise Markdown report.

The report must separate document counts from candidate counts. It must show source and media-type breakdowns, bytes, elapsed time, retries, resource limits, cleanup tests, resumed work, failures, not-attempted candidates, release verification, and the final verdict.

## Non-goals

- Fetching or reconstructing the 3,592 missing Federal Register XML files.
- Defining a second Mirrulations draw schema or selection implementation.
- Requiring Parquet, Iceberg, a database, or the transformed SpicyRegs document table.
- Changing the published DocSpec local-run request for this campaign.
- Adding search, ranking, semantic approval, or document-specific exceptions.
- Setting a production throughput commitment from one workstation run.
- Treating terminal failures as a successful qualification.
- Claiming formal SCALE conformance from this campaign alone.

## Definition of done

The implementation is complete when:

1. SpicyRegs produces and validates the sole Mirrulations draw;
2. the producer inputs prove the exact 6,408 + 3,592 composition;
3. the full DocSpec catalog proves 10,000 source items and 13,592 candidates;
4. the sealed execution manifest reconstructs the same mixed fetcher in every worker;
5. every fetch stream closes on success, failure, and early termination;
6. candidate-level checkpoints prevent reacquisition of verified JSON, HTML, and XML candidates;
7. an interrupted run resumes from its durable planned-store ledger, skips deep execution for sealed jobs, and continues unfinished jobs from their latest verified checkpoint;
8. smoke, intermediate, negative, recovery, and equivalence gates pass;
9. the full run closes every source item and candidate status;
10. an independent verifier accepts the full release; and
11. the final report applies the strict qualification verdict without changing DocSpec's formal conformance status.
