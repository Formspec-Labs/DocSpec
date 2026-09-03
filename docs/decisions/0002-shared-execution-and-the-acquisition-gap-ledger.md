# Decision 0002: what makes a run finish, and who owns which half of it

- Date: 2026-09-02
- Status: accepted for local implementation; nothing minted, nothing published
- Accepted-by: owner, this session. Two rulings are his and quoted below: that
  spicy-docs should leverage DocSpec rather than recreate it, and that DocSpec is
  an experimental dataset runner rather than a publication apparatus. The
  analysis, the phase split in Rule 4, and the sequencing are the agent's.
- Supersedes: the absolute reading of
  `docs/superpowers/specs/2026-08-25-source-native-release-spec.md` §5, that
  `failedRecordCount` "MUST be zero for a publishable release". Rule 4 makes it
  phase-conditional. It does not supersede the no-import boundary; an early
  draft proposed to and was wrong.
- Resolves the hole left by
  `~/Work/spicysearch/docs/history/2026-09-01-script-product-disposition.md`,
  which assigned "work budgets, task identity, candidate fallback, and
  resumability" to DocSpec, told SpicyDocs not to migrate "append-only failure
  ledgers, directory rescans, skip-if-file-exists state", and named no owner for
  a run driver.
- Carries forward: rules 6, 7, 9, 10 and 11 moved to
  `docs/history/2026-09-02-source-native-seam-findings.md`; rule 8 moved to
  `docs/decisions/0003-federal-register-record-identity.md`.

Conventions follow Decision 0001: every obligation is a checkable rule against a
named file and line; **What this does not decide** makes an absent rule a
recorded absence.

## What DocSpec is for

The owner's framing, and the reason this record was reorganized around it:
DocSpec is an experimental dataset runner. You point it at a population, it
acquires the files once, and it runs processing over them in a way you can
start, stop and distribute. The processing is saveable on its own and can be
done piecemeal, so tagging or extraction or segmentation can happen inline with
acquisition or separately afterwards. Adding another hundred thousand documents
later joins the first hundred thousand rather than replacing them. A consumer
such as SpicySearch then searches whichever dataset it pins.

Every requirement in that paragraph exists in the code today. The run lifecycle
is exposed as `prepare`, `start`, `resume`, `reconcile` and `status`; a plan
carries its own processor set, an optional base release and an open `selection`;
`document-catalog compare` diffs one logical layer across two releases; the blob
store is content-addressed with put-if-absent, so bytes already held are never
fetched twice; and processors-only execution re-runs a changed processor without
reacquiring anything.

**What does not exist is the ability to finish.** The largest population ever
carried through to a committed catalog is one thousand documents. Ten thousand
was attempted three times and never completed. That is the gap this record is
about, and it is why the obligations are ordered the way they are.

## Minting is the mechanism, not the goal

The owner's first framing was that DocSpec "is not supposed to be some giant
minting thing", and his second was that "it does mint by very nature". Both are
right and the reconciliation belongs on the record, because without it the rules
below read as though they were relaxing discipline for its own sake.

Identity discipline is what makes the runner work. Two datasets are only
comparable because each is digest-addressed. A second hundred thousand joins the
first because a successor release carries complete current state and reuses
unchanged partitions by exact `blobRef`. A consumer can pin what it searched
because the artifact has a sealed name. Strip that out and there is a directory
of files and no way to say which experiment ran on what.

So the question is never minting against not-minting. It is whether minting is
the consequence of a run finishing or the event the system is organised around.
Right now it is the event: the release format was minted and verified three
times in two days while the ten-thousand document dataset never completed once.

The precise error is applying publication discipline at the wrong stage.
Refusing to publish anything unless every record succeeded is not the same
property as sealing what was published. A dataset of ninety-nine thousand five
hundred documents plus five hundred named gaps, under one digest, is fully
minted, fully reproducible and fully comparable to the next run. What is
unusable at a hundred thousand is no artifact at all because one record carried
a bad date.

## Why runs do not finish

### A gap vocabulary wired to a constant

Three independent implementations declare a way to say "these are missing" and
then make it structurally impossible to say it.

| Where | Declared | Wired to |
|---|---|---|
| `spicy-docs/src/spicy_docs/source_native.py:315` | `failedRecordCount` | literal `0` at `:1384` and `:2304` |
| `spicy-docs/src/spicy_docs/source_native.py:392` | ledger `failure` | type `{"type": "null"}`; emitted `None` at `:873` |
| `spicy-docs/src/spicy_docs/schemas/source_native_release/1.0/failure.schema.json` | `{code, evidenceDigest, stage}` | sealed into `releaseSchemaDigest`, never instantiated |
| `src/docspec/application/reconcile.py:262` | `coverage.sourceCatalog.complete` | literal `True`, one write site, no reader |
| `src/docspec/application/reconcile.py:265` | `unaccountedInputCount` | literal `0`, one write site, no reader |
| `src/docspec/conformance/runner.py:479` | the report's `documentStores` block and all five `bytes` fields | hardcoded `0`, then sealed into `reportId` by `stable_urn` at `:487` |
| `src/docspec/conformance/runner.py:292-293` | the conformance verdict | a function of a hand-edited `status` string, not of whether the checks pass; CI recomputes it under `continue-on-error` into `$RUNNER_TEMP` (`.github/workflows/ci.yml:55-62`) and discards the result |

A completeness field that can only report completeness is a formatting
assertion, not a verification. `coverage.complete = True` is the clearest case:
it is named for the exact fact it cannot express.

### A failure ends the run instead of becoming a row

The same defect appears in both repos, at both stages of the pipeline.

At acquisition, one unusable record ends the whole fetch. That cost eighty-five
minutes on one document out of roughly 526,000, and the strictness was correct
twice out of three times: the EPA duplicate-observation abort and the FAA and
OSHA date aborts were real profile bugs that a failure ledger would have hidden.
The third case, forty-seven agencies lost to S3 read timeouts, was not a data
fact at all and the strictness bought nothing.

At processing, a failed item is stuck forever. Rule 12 states the mechanism.

## Obligations

Ordered by what a run of the owner's size needs, not by rule number. Numbers are
stable because they have been cited elsewhere.

### First: what lets a long run finish

**Rule 4 — `failedRecordCount` becomes phase-conditional where a profile has
phases, and stays absolute where it does not.**

**Scoped 2026-09-03, after spicy-docs falsified the general form.** An earlier
draft of this rule stated the two-phase split as a property of acquisition. It is
a property of a *profile*. For Mirrulations it holds and could not be broken:
`listed_entries()` seals the complete, strictly-ordered, ETag-pinned key set
before any GET runs, the ordering assert enforces it, and `processed_keys` is
refused outright on that path, so the denominator genuinely exists before the
fan-out. For Federal Register it is simply false: the strategy is cursor
traversal with recursive window bisection, pages are discovered as they are
fetched, and there is no listing phase distinct from the fetching phase. A
failure at page 400 of 1,072 leaves no sealed denominator at all, so a fetch
failure there is exactly as unnameable as a listing failure is elsewhere.

The rule therefore belongs to the profile, not to the release format. A profile
may admit fetch failures as named gaps **only if it can show a sealed
denominator before its fan-out begins**; a profile that discovers its universe as
it traverses keeps the absolute rule. Making this the format's rule would license
a gap ledger on Federal Register that could not honestly be filled.
Acquisition has two phases and the current rule treats them as one.
`listed_entries()` establishes the complete, strictly-ordered, ETag-pinned key
set before any object is fetched
(`spicy-docs/src/spicy_docs/sources/mirrulations.py:676-697`); the GET fan-out
then fills it. A failure in listing is unnameable and MUST still abort — that is
what the `processed_keys` refusal at `:668-669` is protecting, and it is right.
A failure in fetching is nameable by key, because the denominator is already
sealed. Fetch failures become disposition rows; listing failures keep the
current behavior.

**Rule 5 — the gap ledger is DocSpec's, not a fourth one.**
`src/docspec/schemas/document_release/2.0/source-dispositions.schema.json:5` is
the artifact the owner described: "Every member of U gets a row, so a consumer
obtains corpus membership and exclusion coverage from this release alone." It
has a closed disposition enum including `unavailable` and `failed` (`:32-38`), a
closed 21-code reason vocabulary
(`src/docspec/adapters/document_release_verify.py:184-219`), and an
`unaccountedCount` genuinely derived from the rows (`:989-1006`) rather than
written as a literal. SpicyDocs adopts that shape and deletes
`failure.schema.json`. Adopting it moves `releaseSchemaDigest`; say so in the
commit.

**Rule 12 — the planner reads the prior release's failures and admits them as
repairable.** This is the defect that breaks piecemeal work, and it is entirely
inside DocSpec. Every entry emits a live `source-items` row regardless of its
disposition (`src/docspec/domain/delivery.py:163-172`), the planner scans that
one layer and no other (`src/docspec/application/planner.py:550`), and
classification compares only the source digest
(`src/docspec/application/planner.py:597-600`), so an item that failed last run
returns as `UNCHANGED` and is dropped from the plan. The `failures` layer is
written on every release and read exactly once, at commit, to build a summary
(`src/docspec/application/commit.py:135`). Nothing consumes it.

The platform spec already requires the capability: an operator "MUST be able to
create targeted `DocumentStore` jobs for one extractor, segmenter, processor,
**failed population**, source partition, or logical bucket without reacquiring
unrelated files" (§11.3). The failures are recorded, the selection field is open
enough to name them, and the three are not connected. Connect them, and a run
that ended with five hundred failures becomes a run you can finish.

### Second, and deliberately deferred: who runs which half

These change the topology of the machinery producing the platform's only data
right now. `spicy-docs/tools/source_native_campaign.py`, the runner Rule 1 says
to delete, is mid fan-out as this is written, and Rule 2 concedes on its own
terms that its change is "a design step rather than a refactor". Inserting a
design step between here and the first finished dataset is how a platform gets
neither. They resume once a catalog composes.

**Rule 1 — DocSpec runs the acquisition, and SpicyDocs deletes its runner.**
`spicy-docs/tools/source_native_campaign.py` hand-rolls a job runner: bounded
worker pool (`:269`), durable per-task receipts (`:157-163`), resume by stored
receipt (`:112-121`), failed-attempt quarantine by rename-aside (`:173-175`),
single-owner locking (`:233-243`), and signal forwarding (`:245-254`). Every one
of those is `ExecutionBackend`'s job, and DocSpec already has it with a sealed
ledger: the handoff pins `expected_task_count` and `task_set_digest` and refuses
a stream that differs (`src/docspec/domain/execution.py:438-461`). The agency
shards become tasks in a DocSpec acquisition run, and the script is deleted, not
improved.

**Rule 2 — the acquisition task payload is generalized, not duplicated.**
`StoreTask` is three fields and `input_store` is typed `StoreRef`
(`src/docspec/domain/execution.py:244-250`), so it cannot carry an agency shard.
The coupling is confined to `_TaskStreamVerifier` and `_validate_result`; the
`execute()` loop itself — bounded capacity, `FIRST_COMPLETED`, yield on
completion (`src/docspec/adapters/execution.py:122-155`) — is already payload
agnostic. Make the backend generic over its task and result types, keep
`StoreTask`/`StoreTaskResult` as the document instantiation, and add an
acquisition instantiation. Nothing in §10.4 requires a DocumentStore payload;
it requires scheduler neutrality.

**Rule 3 — one failure taxonomy, at the control plane.** The acquisition run
classifies through `docspec.domain.jobs.FailureRecord` (`src/docspec/domain/jobs.py:58-64`)
with `retryable` derived from the class (`:75-77`). The per-transport retry
loops stop being policy: botocore and httpx supply attempts, the taxonomy
decides whether an attempt is owed. This makes the S3 retry finding (rule 6, now in
`docs/history/2026-09-02-source-native-seam-findings.md`) a configuration change
rather than a third retry loop.

### Elsewhere

Rules 6, 7, 9, 10 and 11 are bounded defects with fixes attached rather than
decisions, and live in `docs/history/2026-09-02-source-native-seam-findings.md`.
Rule 8, the Federal Register record identity, is contested and needs an owner's
ruling; it is `docs/decisions/0003-federal-register-record-identity.md`.

## Which way the arrow points

Two earlier drafts of this record asked how `spicy_docs` could import DocSpec's
execution port — first by amending the no-import boundary, then by moving the
port down into Rulespec Core. Both were solving the wrong problem. The question
that dissolves it, from the owner: *DocSpec runs the jobs and consumes the
spicy-docs wheel; why would spicy-docs need an execution port at all?*

It does not. **DocSpec drives; spicy-docs is a library of source knowledge.**
Under that arrangement the execution backend never leaves DocSpec, spicy-docs
imports nothing, no boundary is amended, no wheel is bumped, and platform
decision 0001 (`~/Work/spicysearch/docs/decisions/0001-four-product-boundary.md`,
2026-07-31: "A repository must not import another product's source package")
stands untouched.

That direction is not speculative — it is already built and running.
`src/docspec/source_catalog_cli.py:548` imports
`docspec.adapters.spicyregs_source_native` and calls `spicyregs_source_profile`
at `:573`; the adapter resolves `spicy_docs.*` first, falling back to
`spicy_regs.*` (`src/docspec/adapters/spicyregs_source_native.py:30`); and
`tests/test_source_catalog_installed_wheel.py:16-30` installs a digest-pinned
`spicy_docs-0.1.0` wheel and states its role outright: "It is test input, not a
DocSpec dependency."

What is missing is only that DocSpec consumes the *published release* and not
the *act of producing it*. Production runs outside, in spicy-docs' own CLI and a
hand-rolled campaign script. Closing that is the work.

### What moves, and what must not

Moves to DocSpec, because DocSpec already has it and spicy-docs' copy is thinner:

| spicy-docs today | lines | DocSpec's existing home |
|---|---|---|
| `tools/source_native_campaign.py` | 330 | `ExecutionBackend` + `LocalExecutionBackend` (`src/docspec/adapters/execution.py:101`) |
| `src/spicy_docs/source_native_store.py` | 324 | `BlobStore` port with local **and** S3 adapters over one behavioral suite (`src/docspec/ports/blob_store.py:12`, `adapters/storage.py:330`, `adapters/s3_blob.py:75`); spicy-docs' is local-only |
| retry budgets in `source_native_cli.py` and `sources/mirrulations.py` | ~100 | `FailureRecord` with `retryable` derived from the class (`src/docspec/domain/jobs.py:58-77`) |
| partition and manifest writing in `source_native.py` | ~200 | record-layer partitioning and manifests (`adapters/storage.py:1142-1224`) |

Stays in spicy-docs, because it is source meaning and DocSpec's §2.2 excludes it:
`regulations_gov_source_native.py` (2,168), `federal_register_source_native.py`
(817), `gao_product_pages_source_native.py` (740), `sources/mirrulations.py`, the
profiles, the schemas, the collapse rules, and every refusal.

Roughly 950 lines of duplicated plumbing against roughly 4,700 lines of genuine
source knowledge.

### The one real design step

DocSpec's model is defined over a *sealed* input: the planner diffs a new
complete source-catalog snapshot against a prior `DocumentRelease`
(§11.2). If DocSpec also produces that snapshot, it needs two run kinds, not
one — an acquisition run whose output is a source-native release, and a
processing run that consumes it as a pinned input. Same backend, same failure
taxonomy, same receipts, different plan. That is a design step rather than a
refactor, and it is what makes "DocSpec runs the jobs, spicy-docs supplies the
knowledge" true rather than aspirational.

It also matches the written ruling better than today's code does. The
2026-09-01 disposition already gave DocSpec "work budgets, task identity,
candidate fallback, and resumability".

## Corrections this record has made to itself

Recorded rather than edited away, because a decision record that hides its own
reversals teaches the next reader to trust it more than it deserves.

- Two drafts asked how `spicy_docs` could import DocSpec's execution port, first
  by amending the no-import boundary, then by moving the port into Rulespec
  Core. Both were dropped when the owner pointed out that DocSpec drives and
  consumes the producer wheel, so spicy-docs needs no execution port at all.
- Rule 6 was written as open and asserted in the narrative that the S3 path was
  unchanged. It had landed in `b13a1de`. The rule was corrected and the
  narrative was not, for one commit, which is the exact contradiction this
  record exists to catch.
- Six citations into spicy-docs stopped resolving after `b13a1de` and `c178e6b`
  inserted code above them, including Rule 4's own evidence. Re-derived.
- Rule 8 prescribed a composite record identity and sequenced it ahead of the
  Federal Register crawl, on the argument that a collision census over the
  current shape would measure its own filter. That was false:
  `observation_census.py` replays acquisition evidence rather than published
  records. The remedy was withdrawn and the question moved to 0003.
- This record was untracked until `d1b556b`, having governed the acquisition
  topology for a day as an unstaged file.

## What this does not decide

- **Whether a partial release publishes.** Rules 4 and 5 make a named gap
  *expressible*. They do not authorize `sourceStateScope` to claim
  `complete-snapshot` with gaps present. DocSpec's boundary record closes that
  enum to two values (`src/docspec/ports/source_catalog.py:33-34`); a third
  value is a separate decision with a downstream cost, and nobody has yet stated
  a consumer who needs the 9,500 before the 500 land.
- **Whether the tie check moves.** It needs the whole `observations` table
  because it is a `GROUP BY ... HAVING count(*) > 1` over it
  (`spicy-docs/src/spicy_docs/source_native.py:704-709`, run at `:1199` after
  the page loop drains), which is why EPA surfaced at minute 85 rather than
  minute 3. Listing keys are strictly ordered and `mirrulations.py:686-688`
  enforces that, so a per-identity streaming collapse looks tempting. **It does
  not work as the code stands.** Record identity comes from the parsed body, not
  the key: `source_record_id` returns `record["data"]["id"]`
  (`regulations_gov_source_native.py:733-734`). Sorted key order therefore does
  not order identities, and a streaming group could close a group early.

  Making it work needs an invariant that does not exist. The key admission at
  `regulations_gov_source_native.py:1672-1682` checks ASCII, the
  `raw-data/{agency}/` prefix, the `/{collection}/` segment, and global sort and
  distinctness, and then takes identity from the body without ever comparing the
  two. So the key decides agency and collection membership while the body decides
  identity. The final path segment is the identity by convention (the fixtures
  template it as `raw-data/EPA/EPA-2026-0001/text-1/documents/{identity}.json`),
  and the mirror's `" (N)"` refetch suffix means the check is "strip the suffix,
  then compare", not string equality.

  **Updated 2026-09-02:** that refusal has since landed as `_key_claimed_identity`
  (`regulations_gov_source_native.py:1691`), so the invariant now exists on the
  acquisition path — but only there. The finding recorded as rule 9 in
  `docs/history/2026-09-02-source-native-seam-findings.md` moves it into the
  shared replay parser, which is what a streaming collapse would need to rely
  on. Streaming collapse stays downstream of that, and this record still does
  not decide it.
- **`ReleaseCompactionService` is not the accretive model, and is not adopted.**
  It refuses any successor whose logical state digest differs
  (`src/docspec/domain/maintenance.py:191-192`) and takes one release in, one
  out. It cannot add a row. §11.4 says so directly: "a new physical publication
  of the same logical release, not a new logical release."
- **`ChangeKind.REPAIR` is not the retry kind.** It means the source item did not
  change but the plan did (`src/docspec/application/planner.py:480`, `:495-500`).
  It is not a failure-recovery path.
- **Which scheduler runs the acquisition backend.** `LocalExecutionBackend`
  suffices for the current wave. The Dagster path is a separate composition and
  is not itself an `ExecutionBackend`; its `RetryPolicy` is injected by hand and
  no code derives one from `ExecutionLimits`
  (`src/docspec/adapters/dagster.py:116-128`).
- **The three no-docspec assertions in SpicyDocs' tests stay exactly as they
  are.** They enforce platform decision 0001 and this record does not weaken
  them. Under the direction this record settled on, spicy-docs imports nothing
  new and those tests keep passing untouched.

- **Where shared execution primitives would live, if they ever needed a home.**
  An earlier draft routed them into Rulespec Core and two bullets here still
  argued that case; both are removed, because the direction in "Which way the
  arrow points" makes the question moot. DocSpec keeps its execution port and
  spicy-docs consumes nothing. If a second fan-out consumer ever appears, the
  question reopens on its own terms and nothing here prejudges it.
