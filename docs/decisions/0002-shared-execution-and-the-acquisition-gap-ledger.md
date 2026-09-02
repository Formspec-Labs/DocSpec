# Decision 0002: DocSpec runs acquisition; SpicyDocs supplies source knowledge

- Date: 2026-09-02
- Status: accepted for local implementation; nothing minted, nothing published
- Accepted-by: owner, this session, verbatim: "spicydocs should directly leverage the docspec stuff, not recreate anything". The analysis below, the phase split in Rule 4, and the sequencing were the agent's; the reuse-over-reimplementation ruling was the owner's.
- Supersedes: the absolute reading of `docs/superpowers/specs/2026-08-25-source-native-release-spec.md` §5, that `failedRecordCount` "MUST be zero for a publishable release". Rule 4 makes it phase-conditional. **It does not supersede the no-import boundary**, and an earlier draft of this record wrongly proposed to. See "Which way the arrow points" below.
- Resolves the hole left by `~/Work/spicysearch/docs/history/2026-09-01-script-product-disposition.md`, which assigned "work budgets, task identity, candidate fallback, and resumability" to DocSpec, told SpicyDocs not to migrate "append-only failure ledgers, directory rescans, skip-if-file-exists state", and named no owner for a run driver. `spicy-docs/tools/source_native_campaign.py` was written into that gap.

Conventions follow Decision 0001: every obligation is a checkable rule against a
named file and line; **What this does not decide** makes an absent rule a
recorded absence.

## Decision

The accretive model the owner asked for is already built, twice, and wired to a
constant in both places. Nobody builds a third.

DocSpec owns execution and already consumes the spicy-docs wheel through a port.
Acquisition therefore runs as a DocSpec run, on DocSpec's backend, under
DocSpec's failure taxonomy and disposition ledger. SpicyDocs keeps the source
knowledge — profiles, parsers, collapse rules, refusals, schemas — and deletes
the runner, the blob store, the retry constants, and the partition writer it
duplicated. It imports nothing; the arrow already points the other way.

SpicyDocs keeps exactly one rule of its own: a failure during *enumeration*
still aborts, because a gap you never enumerated cannot be named.

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

## Status ledger, 2026-09-02

This record was reviewed twice after it was written: once against the full week
of platform documentation, once against the two codebases. Both reviews changed
it. What follows is the current state of each obligation, not a plan, because a
plan that does not say which steps are done decays into fiction within days —
the exact failure this record documents elsewhere.

| Rule | State |
|---|---|
| 8 — Federal Register collapse | **diagnosed; remedy deferred to its own record.** Does not block the crawl; blocks on the census |
| 6 — S3 retry patience | **landed**, `b13a1de`; one gap survives in `s3_client()` |
| 9 — key/body refusal into the replay parser | open, one function call |
| 10 — policy digest at admission | open, with two version bumps owed |
| 11 — seam profile coverage and wheel re-pin | open |
| 7 — a writer for `supersedes` | open |
| 4 — phase-conditional `failedRecordCount` | open; owes the spec an amendment, see below |
| 1, 2, 3, 5 — the acquisition topology | **deliberately deferred**, see below |

**Why 1 through 3 are deferred rather than open.** They change the topology of
the machinery producing the platform's only data right now:
`spicy-docs/tools/source_native_campaign.py`, the runner Rule 1 says to delete,
is mid fan-out on the source-supply plan's Phase A as this is written. Rule 2
concedes on its own terms that the change is "a design step rather than a
refactor". Inserting a design step between here and the first minted catalog is
how a platform gets neither. They resume after catalog-A and catalog-B compose.

The severable rules — 6 through 11 — are each a bounded change on a live path
and depend on nothing in the topology work. That is why they are numbered after
it and sequenced before it.

**A debt this record has not paid.** Rule 4 supersedes the source-native spec's
`failedRecordCount` clause, and that supersession is recorded only here.
`docs/superpowers/specs/2026-08-25-source-native-release-spec.md` §5 still
carries the absolute sentence with no amendment block, while §3 and §4 of the
same file carry three dated 2026-09-02 amendments. Two current documents rule
differently on one clause. The amendment belongs at the point of the rule, and
until it is written there, treat the spec as authoritative for anyone who has not
read this file.

**A debt now paid.** This record was untracked until 2026-09-02 — the governing
decision for who runs acquisition existed only as an unstaged file and would
have vanished on a clean checkout. Recorded here rather than fixed silently,
because a decision that is not in the tree is not a decision.

## What is wrong today

### The habit: a gap vocabulary wired to a constant

Three independent implementations declare a way to say "these are missing" and
then make it structurally impossible to say it.

| Where | Declared | Wired to |
|---|---|---|
| `spicy-docs/src/spicy_docs/source_native.py:315` | `failedRecordCount` | literal `0` at `:1384` and `:2304` |
| `spicy-docs/src/spicy_docs/source_native.py:392` | ledger `failure` | type `{"type": "null"}`; emitted `None` at `:873` |
| `spicy-docs/src/spicy_docs/schemas/source_native_release/1.0/failure.schema.json` | `{code, evidenceDigest, stage}` | sealed into `releaseSchemaDigest`, never instantiated |
| `src/docspec/application/reconcile.py:262` | `coverage.sourceCatalog.complete` | literal `True`, one write site, no reader |
| `src/docspec/application/reconcile.py:265` | `unaccountedInputCount` | literal `0`, one write site, no reader |
| `src/docspec/domain/delivery.py:229-241` | the `failures` layer, written every release | `RunPlanner` never reads it back (`application/planner.py:550` scans only `source-items`) |
| `src/docspec/conformance/runner.py:479` | the report's `documentStores` block and all five `bytes` fields | hardcoded `0`, `planId` `None`, then sealed into `reportId` by `stable_urn` at `:487` |
| `src/docspec/conformance/runner.py:292-293` | the conformance verdict | a function of a hand-edited `status` string, not of whether the checks pass; CI recomputes it under `continue-on-error` into `$RUNNER_TEMP` (`.github/workflows/ci.yml:55-62`) and discards the result |

A completeness field that can only report completeness is a formatting
assertion, not a verification. `coverage.complete = True` is the clearest case:
it is named for the exact fact it cannot express.

The consequence in DocSpec is concrete. An `accepted-failure` entry still emits
a live `source-items` row (`domain/delivery.py:164-172`), so the next planner
classifies it `UNCHANGED` off the source digest alone
(`application/planner.py:597-600`) and drops it (`:350-351`). DocSpec records
its failures and never retries them either.

### The split that actually hurt

Of the three losses this week, only one is an execution problem, and the
strictness was right about the other two.

- **EPA, 85 minutes, one document of ~526,000.** Two observations at one instant
  differing only in `openForComment`. A real profile bug. Aborting was correct:
  a failure ledger would have hidden a misclassification of unknown extent.
  Fixed at `spicy-docs` `7105a79`.
- **FAA and OSHA, single unparseable dates.** Also real, also correctly fixed, as
  out-of-scope rather than corrupt (`5415de5`, `3cd9968`).
- **Four-way fan-out, 47 agencies.** S3 read timeouts. Not a data fact at all.
  The strictness bought nothing and cost 47 agencies.

DocSpec already draws that line: `transient external failure` and
`deterministic input failure` are separate classes with `retryable` derived from
the class, not chosen (`src/docspec/domain/jobs.py:58-77`,
`docs/superpowers/specs/2026-08-05-docspec-standalone-platform-implementation-spec.md`
§10.3). SpicyDocs has one bucket, and it is pinned to zero.

The conflation was visible in one docstring, which described
`iter_source_objects` as deliberately fail-fast on "a missing listing ETag,
changed object, failed GET, or incomplete body". Three of those four mean the
snapshot is genuinely unfaithful; "failed GET" means the network was busy.
**Resolved upstream in `b13a1de`**, which rewrote that docstring to draw the
line this record asked for: "A genuine transient transport failure ... is not
one of those — the network was busy, not wrong"
(`spicy-docs/src/spicy_docs/sources/mirrulations.py:650-652`). Quoted here in
the past tense because the diagnosis is what justifies Rule 3, and the fix is
what retires it.

### The taxonomy was re-derived per transport, and then converged

`spicy-docs` `168c6c4` rediscovered the transient/deterministic split inside an
httpx retry loop — "Only 429 and 5xx are retryable now... every other 4xx fails
on the first attempt" — four weeks after DocSpec specified it as a control-plane
obligation, and its own message named the rest of the defect: "the crawl has no
resume, so the whole run was lost." For a day that fix existed only in
`source_native_cli.py` while the S3 path that lost 47 agencies kept a
thirty-second read timeout and no per-key retry.

`b13a1de` closed the gap on the S3 side. **This section is retained as the
diagnosis behind Rule 3 and nothing more**; an earlier draft asserted the gap in
the present tense after Rule 6 had already recorded it closed, which is the
contradiction this record exists to catch and did not catch in itself. What
survives is the argument, not the defect: two transports each grew their own
retry policy because no shared control plane owned the classification, and a
third transport would grow a third.

## Obligations

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
decides whether an attempt is owed. This makes Rule 6 a configuration change
rather than a third retry loop.

**Rule 4 — `failedRecordCount` becomes phase-conditional, not absolute.**
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

**Rule 6 — S3 gets the patience `168c6c4` gave httpx. LANDED, `b13a1de`.**
`sources/mirrulations.py:84` now sets `read_timeout=120`, and
`_retry_transient` at `:355-390` gives each key up to `_MAX_TRANSIENT_ATTEMPTS
= 14` jittered attempts layered above botocore's five rather than instead of
them. An earlier draft of this rule said that path "is unchanged"; that was
true when written and is false now.

One gap survives and inherits this rule's number: `s3_client()`
(`sources/mirrulations.py:90-92`), used for agency discovery, still carries no
timeout and no retry configuration while every sibling path does.

**Rule 7 — `supersedes` gets a writer.**
`spicy-docs/src/spicy_docs/source_native.py:145` declares it and `:1469` threads
it to `build_artifact_root`, and no caller anywhere in `src/`, `tools/`, or
`tests/` ever sets it. Every release published today has `supersedes=None`.
Until a writer exists there is no lineage chain, so there is nothing for a retry
release to attach to.

**Rule 8 — the Federal Register collapse is a defect; the remedy is deferred to
its own record.** `federal_register_source_native.py:659` makes `sourceRecordId`
the bare `document_number`, and `source_native_profiles.py:96-107` collapses a
reused number to the newest `publication_date`. The code's own comment concedes
what that discards: `00-111` is "a 2000-01-18 filing and an older 2000-01-14
rule", two documents, not two observations of one. §4's collapse rule was
written for a record observed twice and is being applied to an identity
collision, so the older document never becomes a record, never gets a rendition
row, and never reaches a catalog — against Rule 5, that every member of the
requested universe gets a row. That diagnosis stands.

**An earlier draft of this rule prescribed the remedy and sequenced it first.
Both were wrong, and the review that produced them retracted them.**

The sequencing claim was that the collision census must wait because "a census
taken over this release shape measures its own filter". That is false.
`spicy-docs/tools/observation_census.py:2-6` replays the accepted traversal's
**acquisition-pages evidence**, not the published records, "to recover every
discarded observation too", and it already emits `sameNumberDifferentDateCount`
(`:118`) and `modernFormCollisions` (`:130`) as named fields. The discarded
document is in evidence and the tool reads evidence. The census is therefore not
circular, it does not block on this rule, and this rule blocks on it: nobody
currently knows whether this is two documents or twenty thousand.

The prescribed remedy — making the identity `(document_number, publication_date)`
— is also under-specified and more expensive than that draft claimed. It
contradicts spec §8 (`:461-462`), which states the identity and the
source-issued version as separate things and which the draft did not propose to
amend. It moves rather than resolves the collision: with no `observation_version`
under a composite identity, a same-date byte-identical repeat that publishes
cleanly today would abort through `_select_observations`' `count(*) > 1` branch.
And `sourceRecordId` is both the 64-bucket partition key and the `recordOrderKey`,
so the change moves every bucket assignment, `sourceStateDigest`, `logicalId` and
`artifactDigest`, and every Federal Register release published under the old
schema becomes unreadable by the new profile. Downstream,
`application/federal_register_catalog.py:224-227` keys `documentId` on
`document_number` alone, so two catalog rows would share a `documentId`.

**A cheaper option this record now prefers, without deciding it.** Keep
`sourceRecordId = document_number` and stop collapsing across dates — *refuse*
instead. Two observations of one number at different publication dates are not
repeat observations of one record, which is what §4's rule is for. Refusing
costs no schema version, no policy version, no partition-key move and no
downstream shape change; it surfaces the collision loudly and matches the
platform's refuse-or-record doctrine. Its cost is an aborted release, which is
precisely the cost Rule 4 exists to convert into a named disposition row. The
clean order is therefore Rule 4, then the crawl, then the census, then the
identity decision with a number in hand.

**This belongs in its own decision record, not as rule 8 of 11 here.** It
changes a sealed identity, it contradicts two clauses of a live spec, it is
contested between two agents, and it needs an owner's ruling rather than an
agent's. Burying a contested identity decision inside a record about execution
topology is how it gets executed without being decided.

**Rule 9 — the key/body identity refusal moves to the shared replay parser.**
It currently sits in `regulations_gov_source_native.py:1691`, inside
`_iter_pages`, which is the live acquisition path only. `_parse_page_response`
(`:1120-1198`) — the parser an independent verifier replays — never compares the
object key to `source_record_id(record)`. So `verify` cannot detect the defect
the refusal exists to prevent, and spec `:189-191` overstate what independent verification proves. A guard on
the acquisition path is an operational check; a guard in the shared parser is a
release property.

**Rule 10 — admission recomputes the acquisition policy digest, not its name.**
`verify_source_native_admission` compares `acquisitionPolicyId` and
`acquisitionPolicyVersion` against the installed profile
(`source_native.py:1709-1716`) and never recomputes `acquisitionPolicyDigest`,
which happens only in full replay (`:2116-2127`). Six lines later the source
schema is compared byte-for-byte and its digest recomputed (`:1844-1852`), so
the asymmetry looks deliberate and is not: two policy-content changes landed on
2026-09-02 under an unchanged `ACQUISITION_POLICY_VERSION = "1.0"` — the query
window widening carried at `regulations_gov_source_native.py:1603`, and Federal
Register's new `observationSelection` block. Recompute the digest at admission,
and bump both policy versions.

**Rule 11 — the seam covers what the producer publishes, or stops claiming to.**
`spicyregs_source_profile` (`adapters/spicyregs_source_native.py:73-81`) knows
four profile names; spicy-docs publishes six. Neither `gao-product-pages` nor
`spicy-regs-public-comments` can reach DocSpec, and the public-comments table is
the source-supply plan's declared first rung of supply — so the first rung is
currently unconsumable. The names also differ across the seam
(`regulations-documents` in the producer CLI against `regulations-gov-documents`
in the adapter), which is a live trap for an operator. Either extend the adapter
or delete the unreachable sources and say so.

Separately, the seam's only executable proof pins a producer wheel at
`4cf1e82` (`tests/test_source_catalog_installed_wheel.py:28`), six
behaviour-changing commits behind spicy-docs HEAD. Re-pin it.

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
  acquisition path — but only there. Rule 9 moves it into the shared replay
  parser, which is what a streaming collapse would need to rely on. Streaming
  collapse stays downstream of Rule 9, and this record still does not decide it.
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
