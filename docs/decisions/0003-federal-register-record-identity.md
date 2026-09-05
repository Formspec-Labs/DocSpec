# Decision 0003: the Federal Register identity becomes composite, and the collapse is recorded until it does

- Date: 2026-09-02
- Status: **accepted 2026-09-04**, ruled by the product owner, and **executed
  2026-09-05** — the rebuild ran and met all three acceptance criteria; see "The
  rebuild ran on 2026-09-05". Everything after that is the record that produced
  the ruling, kept including the parts it corrected and the dissent it ruled
  against.
- Evidence: the loss is demonstrated in a built release, not inferred from a
  comment, and the owed measurement was taken on 2026-09-03.

## The ruling

**Ruled 2026-09-04 by the product owner**, accepting the overseer's revised
recommendation with this record's corrections folded in.

**1. Option 4 now: the discarded observation is *carried* on the surviving row,
not merely named, and scoped to what this release collapsed.** The mechanism
exists and has run. Catalog-A's build of 2026-09-03 emitted the surviving
Regulations.gov item with `sourceObservations[0]` at
`observationKey: cross-file-discard/0`, `reasonCode:
source.cross-filed-under-another-agency`, carrying the **whole** discarded
record including its `data.attributes` and its renditions — receipt
`~/Work/corpora/supply-2026-09-02/receipts/catalog-A-build-2026-09-04.md`. That
is decision 0004's remedy in production and it is the working precedent for this
one.

**2. Composite identity — `(document_number, publication_date)` — is the correct
Federal Register identity, to be adopted at the next release rebuild, whatever
triggers it.** The ruling is unconditional; only execution defers. If no rebuild
ever comes, option 4 stands and the ~359 remain unfindable-and-named. The
release-scoped reading of the carried field, specified below, governs the
transition: a rebuilt release collapses nothing, so it carries nothing, and no
cleanup step is needed.

**3. `correction_of` is authorized** as the companion addition to the Federal
Register acquisition field list, decided with the ruling rather than after it,
for the asymmetry this record states: decided now it makes the residual countable
at the rebuild; decided later the fetch has already happened without it.

**4. The basis conditions travel with the ruling.** A proven floor of **one**, a
heuristic centre near **359**, a ceiling of **415** under the assumption that the
68 identical-on-four records are re-observations — **483** without it — all
measured over four captured fields. **Re-derive before acting; do not inherit.**
The scan is re-runnable: `tools/fr_discarded_distinctness.py` in spicy-docs,
committed `9f8c7ee`, takes `--release-root` and `--blob-store`.

### The rebuild is happening, so the three changes land as one release

**Ruled 2026-09-04, after the execution note below.** The Federal Register
release will be rebuilt. That resolves the sequencing problem rather than
working around it: **composite identity, `correction_of`, and the carried
discard all land in one release**, one acquisition policy version move, rebuilt
from the retained 1.76 GB of acquisition evidence with **no refetch**.

**This is not the bundling this record rejected, and the reason is checkable
rather than asserted.** A bundle prices one change off another's cost, so it
collapses if the other change does not happen. These three are *each
independently forced into the same event by the format*: `correction_of` moves
`acquisitionPolicyDigest`, the carried discard moves
`sourceNativeSchemaSetDigest`, and composite identity moves `sourceRecordId`
itself — all three sit in or under `SPEC_FIELDS`, the derivation spec that mints
the release logical id, and each alone requires a republish. There is no version
of this where one rides free on another. The test to apply, if this is ever
questioned: remove any two and ask whether the third still needs a rebuild. It
does.

The release-scoped reading of the carried field, specified above, governs the
transition: a rebuilt release under composite identity collapses nothing, so it
carries nothing, and no cleanup step is needed.

**The rebuild is two changes, not three: the carried discard is moot.** Settled
2026-09-04 with spicy9 before the first commit.

Under composite identity two records differing in date are no longer the same
record, so both survive and there is nothing to discard. The only discards that
could remain are same-number **same-date** collisions, and there are none:

    evidenceRowsRead             1,007,639
    scanDistinctNumberDatePairs  1,007,639   -> every evidence row's (number, date) is unique
    474 on >1 date + 1,006,682 on one date  = 1,007,156 published

Every one of the 483 discards is different-date. Under composite the discard
count goes to zero, so no source-native record field is needed and no DocSpec
evidence path is needed. The execution note below stands as the analysis of what
carrying *would* have cost; it is not needed once identity moves.

**Cited to the distinctness scan, deliberately not to the collision census.**
`fr-full-collision-census.json` reports `sameNumberSameDateIdenticalDigestCount:
0` under `numberAndDateUniquelyIdentifyBasis` — *"the collapse groups
observations by (document_number, publication_date) and refuses to publish a pair
whose canonical record digests differ, so a release that published is itself the
proof"*. That basis is circular, and it describes a grouping the code does not do:
the collapse groups by `/document_number` alone. It is fatal for exactly the case
being relied on — an identical-digest same-date pair is silently kept-one and
trips no refusal, so publication is no evidence about it. The figures above come
instead from a direct enumeration over the 1,072 pre-collapse evidence members,
which is the population that could actually contain such a pair. **This record's
own retracted basis was cited in support of it once; it is named here so it is
not cited a third time.**

**The outcome is stronger than option 4's floor.** The ~359–415 come back as
**items** — retrievable, searchable, citable — not as carried discards. That is
the visibility-versus-availability distinction argued above, and the route ruled
delivers availability, which option 4 alone would not have. The bounds still
hold: at most 415, roughly 359 as a heuristic centre, at least one proven.

**`correction_of`'s justification moved and the record should say so.** It was
authorized to make the residual adjudicable. Under composite there is no residual
— nothing is discarded, so nothing needs adjudicating. Its remaining value is
prospective: telling a genuine correction from a reused number in future data,
and in any other profile that hits this. Real, but not the reason it was
authorized, and a justification that was true when made and is now hollow reads
as still-true later.

**`correction_of` is deferred out of this release.** Ruled 2026-09-04 by the
overseer on spicy9's measurement, reversing the companion authorized this
morning. Mike is told it reverses that authorization and can reverse it back.

Four reasons, in the order that decides them:

1. **Its justification dissolved under composite identity.** It was authorized to
   make the residual adjudicable; composite leaves no residual, because every
   discarded observation becomes a record.
2. **Keeping it needs a second sealed move, and the ruling budgeted one.**
   `FEDERAL_REGISTER_DOCUMENT_SCHEMA` is `additionalProperties: false` over
   **22 properties and does not include `correction_of`** — verified — so a
   release carrying the field would violate its own published schema, failing in
   whichever consumer validates against it, which is the worst of the three
   places to fail. Adding it moves `sourceNativeSchemaSetDigest`, which mints the
   release id; the ruling budgeted only the acquisition policy's move.
3. **No record in this rebuild would carry it anyway.** The rebuild replays
   evidence acquired before the field was ever requested.
4. **It is the exact shape that `dc5687b` turns into a silent drop.** Now that an
   unclassifiable record is recorded and the run continues rather than aborting,
   a schema-forbidden field stops being a loud failure and becomes a countable
   one — not today, but in the first later release that acquires it.

**The deferral costs nothing to make.** `correction_of` is in spicy9's working
tree and in no commit, so this is a back-out before landing rather than a revert.
`DOCUMENT_FIELDS` currently reads 23 with the field present and the schema reads
22 without it, which is precisely the inconsistency reason 2 describes.

**Reopen condition, so this is a deferral and not a quiet drop.** The field is
requested from the API and permitted by `classify_document`, and
`ACCEPTED_DOCUMENT_FIELD_SETS` already carries both `1.0` (22) and `1.1` (23). So
a future release adds it to `FEDERAL_REGISTER_DOCUMENT_SCHEMA` with its value
shape and names **both** digest moves — acquisition policy and source-native
schema set — in one small commit, whenever there is a reason to want it. The
"decided with the ruling, not after" asymmetry written for it above still holds;
what changed is that the thing it was deciding for no longer exists.

**Acceptance:** `discardedObservationCount` 0; record count rises by **483**, and
if it is not 483 the fault is upstream of the rebuild rather than in it; release
embeds `4c324165…`. **All three met on 2026-09-05** — see "The rebuild ran on
2026-09-05" for the measurements and for what they cannot see.

**One forward risk, named because it is not visible in the rebuild.** With
`publication_date` becoming part of the identity while
`federal_register_observation_version` still returns it, the observation version
can no longer order anything within an identity — every same-identity pair is a
tie by construction. That is coherent, and the rebuild is safe: the enumeration
above shows no same-identity pairs exist in the retained evidence, so no tie can
fire. But **the behaviour changes for future crawls**. Today a re-observation
under a later date wins silently; under composite, a re-observation with the same
number and date and a *differing* digest — a publisher editing a record in place
— meets `tieDisposition` and **refuses the release** rather than being absorbed.
Loud instead of silent is the right direction, but it converts a routine
publisher correction into a build-stopping event, and nobody will have seen it
happen before it happens.

**A prerequisite the ruling did not know about: adding a field breaks replay of
the evidence the rebuild depends on.** Found while preparing the change, verified
by spicy9, and it must land before anything else moves.

`federal_register_request_window` validates a stored acquisition page against
*current* code, in two places, and both fail once `DOCUMENT_FIELDS` gains a
member:

- `:234` compares the stored request's `fields[]` against `sorted(DOCUMENT_FIELDS)`.
- `:237` regenerates the canonical URL with `federal_register_documents_url` and
  demands **exact string equality** with the stored one — and that function takes
  only `query_scope` and `per_page`, reading the field list from the module
  constant at `:192`. The field list is baked into the regenerated URL too.

Fixing the first alone leaves the second failing on every page. The path is
`publish` → `_index_pages` → `profile.page_window(page.request_key)` over stored
evidence, with the profile wiring `page_window=federal_register_request_window`.
So the 1.76 GB stops being publishable by the code meant to consume it, the
moment the field is added.

**The obvious fix does not work and the reason is worth recording.** Keying the
expected field set to `acquisitionPolicyVersion` needs the *evidence* to declare
which version recorded it. It does not: `acquisitionPolicyVersion` lives in the
release **spec**, while acquisition ledger rows carry only `evidenceBlobRef`,
`failure`, `observationRef` and `sourceRecordId`. On a rebuild the evidence is
republished under the *new* policy, so there is no stamp to key from.

**What is buildable, because the field list is recoverable from the evidence
itself — it is in the stored request URL:**

1. Parse `fields[]` out of the stored request rather than assuming it.
2. Validate that parsed set against a small set of **accepted historical field
   sets** keyed by policy version — 1.0 the 22, and whichever future version
   first carries the 23. Unknown drift is still refused; only *known* drift is
   admitted.

   **Corrected 2026-09-05: that future version is 1.2, not 1.1, and reopening
   costs three digest moves rather than two.** Acquisition policy 1.1 was spent
   on the composite-identity release without `correction_of` — a sealed version
   absorbs a second change only until an artifact exists under it, and
   `fr-full-1994-2026-composite-2` exists. `ACCEPTED_DOCUMENT_FIELD_SETS`
   therefore carries only `"1.0"` today, verified. So the reopen path is
   acquisition policy **1.2**, moving the acquisition policy digest, the
   `sourceNativeSchemaSetDigest` (the schema is `additionalProperties: false`
   over 22 properties), and the release id those mint.
3. Give `federal_register_documents_url` an explicit `fields` argument and pass
   the parsed set, so canonicality is checked **for the field list the evidence
   declares** rather than for today's constant.

Replay then becomes a function of what the evidence says instead of what the code
currently wants, and any future field addition works the same way. **It should be
proven against real stored evidence before `correction_of` moves anything** —
replay one retained window end-to-end under the current 22-field policy first, so
the path is known good before what it validates changes.

**Sequencing.** The prerequisite above first, then the acquisition change
(spicy-docs, spicy9's lane), then the
release build, then DocSpec consumes. If the carried discard requires a DocSpec
evidence-reading path — `adapters/spicyregs_source_native.py` exposes only
`iter_records` and `iter_renditions` today — that path is DocSpec-side work and
comes last. Sequenced after the in-flight 1.2.0 catalog-A rebuild.

**What still does not lift on the day the ruling was made.** catalog-A stays
**not publishable** until the new Federal Register release exists *and* catalog-A
is rebuilt pinning it. Both, not either. Until then the ~359 remain unfindable
and unnamed, exactly as the execution note says. "Ruled" is still not "resolved".

**Not in scope, recorded so it is not re-proposed.** Inline comment bodies —
submissions whose whole content sits in a metadata field with no file — are a
search-side concern. Neither this record nor 0004 should propose a rendition for
them.

### Execution note, 2026-09-04: option 4 does not decompose for this profile

Written after the ruling, from the code, because the ruling's shape — "option 4
now, composite at the next rebuild" — reads as two separable steps and for the
Federal Register it is not two steps.

**Why 0004's remedy was cheap and this one is not.** On the Regulations.gov side
the collapse happens *inside DocSpec's catalog build*: `_CatalogPolicyInputs._load`
hits the repeat, asks the policy, and the discarded filing is in hand at catalog
time. It therefore rides `sourceObservations` on the catalog item, whose
`observationValue` is schema-unconstrained, and costs no version move. That is
the precedent the ruling cites.

The Federal Register collapse happens **at acquisition, in spicy-docs, before the
release exists**. The discarded record is written only to acquisition evidence.
And DocSpec cannot see it: `adapters/spicyregs_source_native.py` exposes
`iter_records` and `iter_renditions` and has no evidence path at all. **DocSpec
cannot carry what it never receives.**

So carrying a discarded Federal Register observation requires one of two things,
and both are release-level:

1. **The release carries it.** `source-native-record.schema.json` is built by
   `_closed_schema` over exactly seven properties — `fieldDiagnostics`, `record`,
   `schemaDigest`, `schemaName`, `schemaVersion`, `scopeId`, `sourceRecordId` —
   with `additionalProperties: false`, and every shape in that format is closed
   by construction through `_ClosedObjectShape`. There is no free-form slot. So
   this moves `sourceNativeSchemaSetDigest`, which sits in `SPEC_FIELDS`, the
   derivation spec that mints the release's logical id. New schema, new release
   identity, and the release must be republished to contain the records.
2. **DocSpec grows an evidence-reading path.** A new input the adapter does not
   have, reading the pre-collapse population directly.

Either way there is no version of option 4 for this profile that runs against the
release we already hold. **Option 4 here is itself a rebuild**, which is the thing
the ruling deferred.

**The same is true of the authorized companion.** Adding `correction_of` changes
the acquisition policy, and `acquisitionPolicyDigest` and
`acquisitionPolicyVersion` are also in `SPEC_FIELDS`. It too mints a new release
identity on the next build.

**What this does and does not change.** It does not reverse the ruling: option 4
may still be the right destination, and no existing artifact is invalidated —
published releases keep their ids and stay readable. What it changes is the
sequencing the ruling assumed. There is no cheap interim state for the Federal
Register in which the discards become auditable before a rebuild happens. The
honest statement is that **for this profile the whole ruling lands at the
rebuild**, and until then the ~359 stay both unfindable and unnamed.

**And this record should not talk itself into liking that.** It is uncomfortably
close to the bundling argument this record rejected two sections above: several
changes riding one identity move because the move is happening anyway. The
distinction is real but narrow — these are not being *priced off* each other,
they are each independently forced into the same event by the format — and a
reader should check that distinction rather than accept it because this record
asserts it.

### What the ruling does not do

**It does not lift catalog-A's not-publishable mark.** That catalog pins the
Federal Register release that loses these documents, and it stays not publishable
until a release exists under composite identity. "Ruled" is not "resolved": the
decision is made, the loss is still in the artifact.

**It does not recover anything yet.** Option 4 makes the loss auditable, not the
documents retrievable. Until the rebuild, a query that should return the
2000-01-14 rule returns the 2000-01-18 notice.

**It does not settle the residual.** How many of the 415 are genuinely distinct
documents needs the bodies, because `correction_of` is absent from this corpus
rather than null. Authorizing it fixes that forward, not backward.

### The dissent, which was ruled against and stays

This record recommended option 4 alone, on the ground that the composite's cost
scales with all 1,007,156 records while its benefit scales with ~359 — a 0.036%
benefit against a re-partitioning of the whole corpus. That argument was not
accepted and is not withdrawn. It is kept because whoever executes the rebuild
should meet the strongest case against it before paying its cost, and because if
the rebuild never happens this is the position the record falls back to.
- Raised-by: agent, from a code review of spicy-docs against the source-native spec
- Was blocked on a measured collision count. **Unblocked 2026-09-03**: the census
  exists, and the numbers are in "What the census measured" below.
- Split from: `docs/decisions/0002-shared-execution-and-the-acquisition-gap-ledger.md`,
  where this was rule 8 of 11 and did not belong

## The rebuild ran on 2026-09-05, and it met all three acceptance criteria

Receipt: `~/Work/corpora/supply-2026-09-02/receipts/fr-replay-2026-09-04.log`.
Run 01:12:35Z to 02:09:09Z, `replayExit=0`, producer
`18bc4039bc783a96ff50ef67f0e2da2c2b3b321d` identical at start and finish with
`dirtySrc=[]` — one code state, checked rather than assumed, on the editable
install that `catalog-A-build-2026-09-04.md` names as an open hole.

| | source release | composite release |
| --- | --- | --- |
| path | `releases/fr-full-1994-2026` | `releases/fr-full-1994-2026-composite-2` |
| digest | `sha256:6695546e…` | `sha256:6cf8cf44…` |
| logical id | `…5f35c56b…` | `…d3d0a062…` |
| published records | 1,007,156 | 1,007,639 |

**`sha256:d2d8b03b…` (`releases/fr-full-1994-2026-composite`, without the `-2`)
is the scrapped first run and must not be pinned.** It recorded
`verifierImplementationId ...@b590d867` while the code that ran was `18bc4039`,
from a hand-typed literal in the launch script — and `b590d867` provably could
not have produced it, because without `18bc403`'s accepted-policy-version fix
the run cannot admit the release it replays. catalog-A's accepted-verifier gate
refused it in 4.9 seconds and it was re-run with the id derived from
`git rev-parse HEAD`. Its records are **byte-identical** to `composite-2`'s — 0
changed, 0 missing, 0 added — so nothing about its content warns you off it; only
the absent verifier id does.

### The three criteria

| criterion | required | measured |
| --- | --- | --- |
| `discardedObservationCount` | 0 | **0** |
| record count rise | exactly 483 | **483** (1,007,639 − 1,007,156) |
| release embeds `4c324165…` | yes | **`spec/releaseSchemaDigest = sha256:4c32416532f3…`** |

`inputObservationCount` and `publishedRecordCount` are both 1,007,639: under
composite identity the release publishes its entire pre-collapse input, which is
what "the collapse is recorded until it does" was pointing at.

### Re-derived from the record blobs, not from the receipt

The receipt is written by the thing under test, so its counts were re-derived by
reading the 64 `records` payload blobs directly out of the blob store and
counting:

    record lines                  1,007,639
    distinct sourceRecordId       1,007,639
    distinct (number, date)       1,007,639
    distinct document_number      1,007,156   <- the old release's count, derived
    numbers used on >1 date             474
    extra rows from that reuse          483

The last three lines are the load-bearing ones. **1,007,156 and 474 were
predicted in this record from the pre-collapse evidence before the rebuild
existed** — see the `474 on >1 date + 1,006,682 on one date` arithmetic above —
and the rebuild landed on both. That is a prediction meeting a measurement from
the opposite direction, not a receipt agreeing with itself.

`sourceRecordId` now reads `00-1000@2000-01-18`. **The specimen is back**:
`00-111` is present twice, as the 2000-01-14 Rule *Compliance Monitoring and
Miscellaneous Issues Relating to the Low-Income Housing Tax Credit* and the
2000-01-18 Notice *Notice of Filing of Plat of an Island; Minnesota*. The
document this record was opened over is in the release.

Replay integrity, from the same receipt: 1,072 distinct request keys served
against 1,072 in the source ledger, 2,144 fetch calls against 2,144 source page
rows, `distinctRequestKeysMatchSource: true`. No refetch, as ruled.
`semanticVerdict: pass`; deterministic, transient and unclassed failure counts
all 0. Cost: 3,394 s wall, 238 MB peak RSS.

### What this check cannot see

**Content fidelity of the 483 is spot-checked, not proven.** The counts prove 483
rows exist that did not before, and `00-111` matches this record's specimen on all
four distinguishing fields. One specimen is not a proof for 483, and no count can
be, because a count cannot tell a faithfully restored record from a well-formed
wrong one.

**The residual is now adjudicable, and is still not adjudicated.** Composite
identity makes every discarded observation a readable record; it does not say
which of them are distinct documents. The 359–415 range and its basis conditions
stand exactly as written, and closing them is still body-level work.

**68 true re-observations are now 68 separate records.** The ceiling assumption
cuts both ways: if the 68 identical-on-four rows really are one document observed
twice, the release now publishes each twice under different composite ids. That
is the accepted cost of the ruling — a duplicate that can be found and collapsed
downstream beats a document that cannot be found at all — but it is a real
property of the new release and not a rounding error.

**The forward risk named below is now live, not hypothetical.** With
`publication_date` inside the identity, a publisher editing a record in place now
meets `tieDisposition` and refuses the release rather than being absorbed. Nothing
in this rebuild could trigger it; the first future crawl can.

### The release was rebuilt on 2026-09-05, and the logical id held

The path this section names, `releases/fr-full-1994-2026-composite`, no longer
exists: it was rebuilt the same day as `fr-full-1994-2026-composite-2`
(completed 05:30:24Z against the first build's 01:54:56Z). Found while preparing
an unrelated census, not by a check that was looking for it.

**The physical artifact moved and the identity did not.** A rebuild always moves
`artifactDigest`, because the receipts inside it carry timings and a completion
instant — `sha256:d2d8b03b…` became `sha256:6cf8cf44…`. But the logical id is
**identical**: `urn:spicy:artifact:spicyregs-source-native-release:d3d0a062…`
on both, and so are `acquisitionPolicyDigest`, `sourceNativeSchemaSetDigest`,
`releaseSchemaDigest` and `sourceStateDigest`.

That is worth more than a corrected pointer. Two independent builds from the
same retained evidence, hours apart, minted the same logical release id — so
the identity this record moved is reproducible rather than incidental to one
run, which is a stronger claim than the original acceptance made and one nobody
set out to test.

Re-verified on `composite-2` rather than assumed from the id: 1,007,639 records,
1,007,156 distinct document numbers, 474 numbers reused, 483 extra rows,
`sourceRecordId` of the form `00-1000@2000-01-18`, and `00-111` present twice as
the 2000-01-14 Rule and the 2000-01-18 Notice. Every figure in "The rebuild ran
on 2026-09-05" reproduces. The acceptance stands; only the path and the physical
digest above are superseded.

### Consequence: catalog-A is superseded

`catalog-A-build-2026-09-04.md` states its own supersession condition — *"any
remedy that recovers them moves the FR release digest and supersedes this
catalog"*. The digest moved. That receipt's not-publishable mark and its Phase C
pins are now spent, and a catalog that serves this corpus rebuilds against
`sha256:6cf8cf44…` (`composite-2`), never against the scrapped `d2d8b03b…`.
Both catalogs have since been rebuilt on it: catalog-A `sha256:1ac55e0c…`
(state `sha256:6a12f498…`) and catalog-B `sha256:0872a608…` (state
`sha256:88d481dc…`).

## Why this is its own record

It changed a sealed identity, it contradicted two clauses of a live
specification, it was contested between two agents, and the remedy first
proposed for it was retracted within hours by the same review that raised it.
Burying a contested identity decision inside a record about execution topology is
how it gets executed without being decided.

*Both paragraphs here were written while this was open. All three conditions they
describe are now discharged: the measurement was taken on 2026-09-03, the owner
ruled on 2026-09-04, and the rebuild ran on 2026-09-05. The second paragraph said
"the one thing this record does decide is that nobody should act on it yet" —
that was the right call for eight days and is the opposite of the position today.
Kept, in the past tense, because the reason for splitting the record still holds
and deleting it would remove the reason this record exists.*

The one thing this record decided, until the measurement existed, was that nobody
should act on it yet. The diagnosis was sound; the remedy was not settled; and the
measurement that would size the problem did not exist.

## The finding, carried over unchanged

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
changed a sealed identity, it contradicted two clauses of a live spec, it was
contested between two agents, and it needed an owner's ruling rather than an
agent's. Burying a contested identity decision inside a record about execution
topology is how it gets executed without being decided.

*Written while this was open, and left in place because the argument for
splitting it still holds. It was ruled on 2026-09-04 — see the header and "The
ruling" — so read this paragraph as the reason the record exists, not as a
statement that a decision is still pending.*

## What the census measured

`~/Work/RefSpec/research/evidence/fr-collision-census-2026-09-02/fr-full-collision-census.json`,
112,263 bytes, `sha256:427a68272f87225e45c7bc25376c73c2761e07a613c7d87a8a6cdaa73c73356c`.
Produced by the full-history crawl on 2026-09-02 and brought into RefSpec at
`d8a9d70f`. Digest and every field below re-verified from the file on 2026-09-03,
not accepted from a report.

| field | value |
|---|---|
| `coverage.distinctNumberCount` | 1,007,156 |
| `coverage.queryScope` | 1994-01-01 through 2026-09-02 |
| `sameNumberDifferentDateCount` | **474** |
| `modernFormCollisionCount` | 7 |
| `sameNumberSameDateDifferingDigestCount` | 0 |
| `sameNumberSameDateIdenticalDigestCount` | 0 |
| `totals.multiObservationIds` | 474 |
| `totals.discardedObservations` | 483 |
| `numberAndDateUniquelyIdentify` | true |
| `legacyFormAlsoParsesAsModernCount` | 29,148 |

So the answer to "two documents or twenty thousand" is **474 numbers covering 483
discarded observations, out of 1,007,156** — 0.047%. Seven are modern-form; the
rest are legacy. Zero same-date conflicts of either kind.

Three riders the file states about itself and this record inherits. The census
covers the **crawled** Register from 1994 and its own `coverage.caveat` says it
is not evidence about the printed Register before that. The
`numberAndDateUniquelyIdentify` warrant is stated rather than assumed — the
collapse refuses to publish a same-instant pair whose canonical digests differ,
so a release that published is the proof, and the file scopes that to "every pair
it covers". And `letterPrefixStripCollisions` records 2,382 further collisions
that appear **only if someone normalizes** by stripping a legacy number's letter
prefix; raw values are disjoint. Anyone adopting a qualified identity needs that
last one, and the 29,148 legacy numbers that also parse as modern, before
choosing a parser.

## What the number changes

It makes the composite identity disproportionate and it makes a fourth option
obvious.

Recovering 474 documents out of a million does not justify contradicting spec §8,
moving the 64-bucket partition key and the `recordOrderKey`, and rendering every
already-published Federal Register release unreadable. Refusing the collapse
instead, which this record previously preferred, means 474 build aborts spread
across the history — correct in principle and unusable in practice as an abort.

**The option this record did not previously consider: keep the collapse and record
each discarded observation as a named disposition row.** At 474 rows the
accounting is trivial, the documents stop being invisible, no identity moves, no
schema version changes, and it is exactly the machinery
`docs/decisions/0002-shared-execution-and-the-acquisition-gap-ledger.md` Rules 4
and 5 exist to build. The corpus then says "these 483 observations were
discarded, here is each one and why" instead of saying nothing.

## The loss is proven, not inferred — and two relayed claims about it are wrong

Added 2026-09-03, after spicy9 traced the mechanism end to end. The diagnosis
above was argued from the code's own comment; it is now argued from the built
release.

**The specimen.** The release keeps the 2000-01-18 Notice "Filing of Plat of an
Island; Minnesota" under `00-111`. The 2000-01-14 rule filed under the same
number — a genuinely different document, named in
`federal_register_observation_version`'s own docstring — is **not in the
release**. It was fetched and then discarded as a stale observation. So the loss
is not a hazard this record anticipates; it is a document that a build already
dropped.

**The eviction happens in spicy-docs, not here.** `federal_register_acquisition_policy`
returns an `observationSelection` of `groupBy: /document_number`,
`orderBy: /publication_date DESC NULLS LAST`, wired into `FEDERAL_REGISTER_PROFILE`
as `observation_version=federal_register_observation_version`. By the time
DocSpec sees a release the older document is already gone. DocSpec's own
`_item_from_row` sets `issued_version = publication_date`, which is a faithful
reading of identity-versus-version and is **not** the eviction site. Citing it as
the mechanism would send a reader to the wrong layer — the same miscitation
decision 0004 had to correct in its own constraint section.

**It was deliberate, and it mirrors the publisher.** The comment states the
intent: keep the newest `publication_date` "exactly as the API's own
`/documents/<number>` resolution does". Whoever wrote it knew `00-111` resolves
to two documents — they named both. What appears not to have been weighed is
that mirroring the publisher's *resolution* endpoint also inherits its inability
to represent the older document at all. That is a defensible ingest choice and an
indefensible corpus property, and those are different questions.

**The exposure is bounded to cross-date reuse.** A same-day collision under one
number does not collapse: `tieDisposition` is
`refuse-differing-record-digest-at-normalized-instant`, with
`refuse_equal_observation_versions=False`, so two same-instant records whose
digests differ refuse the release rather than picking one. This matters for the
ruling below — the refusal machinery option 2 asks for **already exists** and
already runs; extending it from same-day to cross-date is a narrower change than
this record previously implied.

### Two claims that came with this finding and do not survive checking

**"The release is labelled complete-snapshot and silently is not."** Made by
spicy9, who asked that it be recorded against them by name rather than as an
ownerless relay artifact, on the grounds that a failed claim without an author
teaches nobody which reader to distrust. False for this profile. `FEDERAL_REGISTER_PROFILE` sets
`source_state_scope="observed-crawl"`. Five sibling profiles are
`complete-snapshot`; the Federal Register one is not, and observed-crawl is the
honest label for what it does. The labelling defect this finding needs is not
there, and asserting it would put a wrong claim in front of the owner alongside
the right ones.

**The census's `numberAndDateUniquelyIdentify = True` proves nothing.** It was
justified on the grounds that "the collapse groups observations by
(number, date)". The code groups by `/document_number` alone and orders by date;
date is the tiebreak, not part of the key. So the check ran over a release
already reduced to one record per number and reported that numbers are unique —
which is true by construction and would have been true no matter how much was
discarded. Re-deriving from the raw release blobs gives 1,007,156 records and
1,007,156 distinct numbers for the same reason. This is the day's recurring
shape: a measurement over a population that cannot contain the thing being
measured, internally consistent and therefore indistinguishable from a clean
result.

### The measurement this record still owes

Up to 483 observations across 474 numbers were discarded. **How many are
distinct documents and how many are true re-observations of one document is
unmeasured**, and it is the number an owner needs, because it is the difference
between "474 documents are missing" and "474 numbers were re-observed and one of
them, `00-111`, hid a second document".

It cannot come from the release: the release is the thing that already did the
reducing. It has to come from the discarded observations themselves — comparing
each discarded record against the one that survived under the same number, on
fields that distinguish a different document from a later look at the same one
(`type`, `title`, `agencies`, `abstract`), not on the two fields the collapse
already keyed on.

**It needs no fetch, and the inputs are known to exist.** spicy9 confirms the
acquisition evidence is retained — 1,072 members, 1.76 GB, raw API page
responses under a `results` key — which is the pre-collapse population and the
only place the discarded records still are. So this is a bounded local scan, not
a re-crawl, and the reason it has not run is scheduling against a live build
rather than missing data.

A trap to carry into that scan: `E8-30793` is year-coded `E8` and published
2009-01-02. A year-family spilling into January is legitimate, so no identity may
be minted from the year code alone, and a scan that treats a code/date mismatch
as evidence of two documents will manufacture collisions.

**What the scan can and cannot settle, recorded before it runs so its result is
not overread.** Comparing a discarded record against the survivor on `type`,
`title`, `agencies` and `abstract` establishes that the two records *differ*. It
does not establish that the discarded one is a *different document*: a corrected
title or a revised abstract on the same document differs too. So the scan's N is
an **upper bound on distinct documents, not a count of them**, and it has to be
reported in that form.

This record thought it already knew what settles the residual, and the analogy
it reached for was wrong in a way that changes the remedy.

The seven modern-form collisions could not be adjudicated from metadata:
federalregister.gov's own `correction_of` answers null for all seven, including
the two that genuinely are self-corrections, and RefSpec resolved them from the
document bodies. An earlier version of this section said the scan "inherits that
limit exactly". **It does not — the two limits are different and have different
fixes**, and spicy9 established the difference by checking our evidence rather
than accepting the analogy.

`correction_of` is not null in our corpus. It is **absent**. The Federal Register
acquisition policy requests exactly 22 fields — `DOCUMENT_FIELDS` in
`federal_register_source_native.py`, verified here independently — and
`correction_of` is not among them, nor does the string appear anywhere in either
tree. So:

| limit | what it is | fix |
| --- | --- | --- |
| the field answers null (RefSpec, 7 documents) | present and uninformative | none; bodies are the only route, permanently |
| the field was never requested (here, all 483) | absent from the crawl | request it, which helps every future collision and recovers none of these without a refetch |

Which limit governs the 483 is **unmeasured, and this corpus cannot measure it**,
precisely because the field is not in it. RefSpec's evidence establishes the null
result for seven documents; generalising that to 483 by analogy is the same move
this record has had to retract twice already.

**The consequence for budgeting, which is the part that changes.** The residual
adjudication is body-based no matter how N lands — not because metadata is
uninformative, but because we do not hold the metadata. A small N therefore means
a small number of *body fetches*, not a cheap metadata pass. Much cheaper than
474, and a different kind of work than "narrows the range and hands back a
smaller set" implied.

**The recommended companion change, to be decided with the ruling rather than
discovered after it.** Add `correction_of` to the Federal Register acquisition
field list. It does not touch the identity question, moves no schema, and is
orthogonal to which of the four options is chosen; it costs an acquisition policy
version and applies from the next crawl forward.

It is a companion rather than a rider, and the difference matters given what this
record has already unbundled: nothing about it depends on another change
happening, and it is not being priced off one. It is recommended alongside the
ruling for a specific reason — if the eventual rebuild refetches rather than
replaying retained evidence, `correction_of` would be present, and the residual
this record cannot settle becomes **adjudicable instead of estimated**. The
415/359 heuristic would become a count at the moment it is finally acted on.
Decided now it improves the deferred work; discovered later it does not, because
by then the fetch has happened without it.

It recovers none of the 483 without a refetch, so it is not mitigation for this
loss and must not be presented as any part of the answer to the current question.

**Not authorized as of 2026-09-03.** It changes what the next crawl fetches,
which is an acquisition policy change and therefore the owner's call, not a
reversible tidy-up.

The gate that makes the scan trustworthy is the receipt equation above:
`inputObservationCount` 1,007,639 − `publishedRecordCount` 1,007,156 = 483, and
publication refuses unless it balances. An enumeration that does not land on 483
is wrong, and the distinctness split — computed over the same enumeration —
must not be reported at all when it does not, because a wrong enumeration
yields a confidently wrong ratio rather than a visible failure.

## The scan ran, and the loss is large

Measured 2026-09-03 by spicy9. Receipt:
`~/Work/corpora/supply-2026-09-02/receipts/fr-discarded-distinctness-2026-09-03.md`,
data alongside it. Method: stream all 1,072 acquisition-evidence members and
compare each discarded observation against the survivor under the same number on
`type`, `title`, `agencies`, `abstract` — deliberately not on number or date.

**The gate passed, so the split is reportable.** An independent enumeration of
the evidence produced 1,007,639 input observations and 483 discards; the
release receipt's equation gives the same 483 from
`inputObservationCount − publishedRecordCount`. Two separately computed
quantities agreeing. Of 483 discarded observations across 474 numbers:

| | count |
| --- | --- |
| differ from the survivor on at least one of the four fields | **415** |
| identical on all four | **68** |
| of the 415, one title is a prefix of the other | 56 |

Differing fields among the 415: title 373, agencies 364, abstract 264, type 167.

**State the number with what bounds it, because the receipt's own summary does
not.** The receipt says the range "is now roughly 359–415", three paragraphs
after establishing that 359 "is an estimate, not a floor". Both cannot hold. The
defensible statement is:

- **at most 415**, and that ceiling itself assumes the 68 identical-on-four
  records are re-observations rather than distinct documents that happen to
  match on all four captured fields — an assumption spicy9 flagged and the
  evidence cannot test. Without it the ceiling is 483.
- **roughly 359** as a heuristic mid-estimate, from subtracting the 56
  title-prefix pairs that look like republications (`00-12867` differs only by a
  trailing "; Republication"). A heuristic, not a bound: a genuinely different
  document can share a title prefix with the survivor.
- **at least one** proven — `00-111`, differing on all four, a rule and a notice
  under one number.

So: a proven floor of one, a heuristic centre near 359, a ceiling of 415 under a
stated assumption. Not a 359–415 interval. The distinction matters because the
loose end is the *bottom*, and the bottom is what a proportionality argument
leans on.

**What it means for the ruling.** This record said that if the number came back
near 474 the trade would need re-pricing. It came back near 474. The cost
arithmetic against composite identity does not change — several hundred of
1,007,156 still does not justify moving the partition key, the `recordOrderKey`
and spec §8, and stranding every published release. What changes is how option 4
*reads*: "we can prove which ones" is a different sentence at 359 than at 5.
Ruling for option 4 at this scale is ruling that several hundred real Federal
Register documents stay unfindable, with an auditable list of them. That is a
defensible call at 0.036% of the corpus and it should be made in those words.

The residual is body-level work regardless of which remedy is chosen, because
`correction_of` is absent here rather than null. Nothing we hold converts 415
into a hard count.

Until that adjudication happens, the honest statement of cost is: **at least one**
real document is absent from a published release, at most 415 are under a stated
assumption, and the best available estimate is several hundred.

## The four options with the measured count beside each

Written 2026-09-03 once the scan landed, because the count changes how the
options read even though it does not change the arithmetic. Throughout: the loss
is **at least 1 proven, ~359 by heuristic, at most 415 under a stated
assumption** — see the scan section. Corpus is 1,007,156 published records.

| option | what happens to the ~359 | what it costs |
| --- | --- | --- |
| 1. status quo | stay lost, uncounted | nothing, and the corpus lies by omission |
| 2. refuse cross-date collisions | stay lost, loudly | 474 build aborts; extends the live same-day `tieDisposition` |
| 4. keep the number, record the discards | stay unfindable, become auditable and possibly readable | a field on a closed row, or nothing if the release can already carry it — unresolved above |
| 3. composite `(number, date)` | become findable items | re-partitions and re-digests all 1,007,156 |

**The asymmetry that decides it, verified rather than asserted.**
`_identity_bucket_for_row` partitions on `_partition_id(sourceRecordId)`, a
sha256 of the identity modulo 64, and `sourceRecordId` is also the row-ordering
key. Changing the identity from `00-111` to a composite therefore re-buckets
**every** record, not the 474: all 64 partitions get new contents, new blobRefs
and new member digests, the release digest moves, and every catalog and consumer
pinning an existing Federal Register release breaks. The benefit scales with
~359; the cost scales with 1,007,156. That is a 0.036% benefit against a 100%
cost, and it is why this record has recommended against the composite at every
revision.

**The strongest argument for the composite, recorded because it is real.** The
acquisition evidence is retained — 1,072 members, 1.76 GB, the pre-collapse
population. So a composite rebuild recovers the ~359 **from what we already
hold, with no refetch**. This record previously implied recovery needed a
re-crawl; it does not. Anyone weighing the composite should weigh it at rebuild
cost, not re-acquisition cost.

**And the argument against the way it is usually proposed.** The recommendation
on the table is composite identity *bundled with the next sealed move*, on the
reasoning that the marginal cost is small if a digest is moving anyway. This
record has already unbundled one rider today for precisely that reason: the
reason-code labelling was bundled onto a schema cost that turned out not to
exist, and when the cost vanished the rider would have *introduced* a move rather
than shared one. A bundle is a promise about a future event. If the sealed move
does not come, the composite is paid at full price, and the decision will have
been made at the bundled price. Bundling is a scheduling convenience and should
never be part of why an option wins.

**One thing the composite does not do by itself.** It changes what future builds
admit and what a rebuild can represent. It does not, on its own, put the ~359
into any existing artifact — that still takes a rebuild and a re-pin of every
consumer. So "composite makes them findable" is accurate only as "composite plus
a full rebuild makes them findable", and the rebuild is the larger half.

**This record's position, unchanged by the count.** Option 4, with the discarded
record *carried* rather than merely named, and with the open question above
settled first: whether the release can carry it without opening a closed schema.
Ruling for it means ruling that **~359 real Federal Register documents stay
unfindable and we can name every one of them**. At 0.036% of the corpus that is
defensible, and it should be said in those words rather than reached by default.

**The dissent, recorded so the owner sees both.** The overseer recommends the
composite, on the grounds that our identity should match the source's actual
uniqueness, that several hundred real documents is not a rounding error, and that
rebuilding immutable releases is the platform's ordinary work rather than an
exceptional cost. Both positions are in front of the owner; the count does not
settle between them, and neither of us should pretend it does.

### The revised proposal, and why it is not the bundle this record rejected

The overseer withdrew "composite because it is cheap when bundled" and proposes
instead: **option 4 now with the discard carried, and 0003 rules that composite
is the correct identity, adopted at the next Federal Register release rebuild,
whatever triggers it.** If no rebuild comes, option 4 stands.

That is not a bundle and this record should not call it one. The objection above
is to choosing an option at a discounted price that depends on a future event.
Here option 4 is paid in full today and composite is paid in full whenever it
runs; no cost is being shared, so none can evaporate. The ruling is made
unconditionally and only execution is deferred.

Two things it does create, and the ruling has to say what happens to them.

**A double representation.** Under option 4 the discarded record is carried on
the surviving row. After a composite rebuild the same document is also its own
item. Unresolved, the corpus then asserts both — `00-111`'s 2000-01-14 rule
exists as an item *and* as a discard recorded against the 2000-01-18 notice. The
rebuild must either drop the carried discards it has promoted, or the carried
field must be defined as a record of what *this release* collapsed rather than a
standing claim about the document. The second is cheaper and more honest, and it
wants deciding now, while both halves are in one person's head.

**A standing obligation on someone not in this conversation.** A ruling executed
months later is executed by whoever runs the rebuild, from this record alone. So
this record must carry the conditions of its own basis: that ~359 is a heuristic
centre and not a floor, that 415 assumes the 68 identical-on-four records are
re-observations, and that both were measured over four captured fields. Whoever
executes should re-derive before acting, not inherit.

**One synergy worth taking now.** Adding `correction_of` to the acquisition field
list is orthogonal to this ruling and helps it: if the eventual rebuild refetches
rather than replaying retained evidence, the field would be present and the
residual could be *adjudicated* instead of estimated — converting the 415/359
heuristic into a count at the moment it is finally acted on. That is an argument
for doing the cheap forward-looking fix now rather than alongside the rebuild.

## Where document identity should be decided, and where it should not

An earlier draft of my thinking proposed that DocSpec consult RefSpec's qualified
identity space, on the reasoning that both of this week's incidents are one field
doing two jobs and RefSpec already carries a space where a composite is cheap.
**RefSpec refused it, and the refusal is right.**

Two reasons. It is the edge the product topology forbids: SpicySearch is the only
junction between DocSpec's documents and RefSpec's vocabulary, and there is
deliberately no DocSpec-to-RefSpec edge. And practically, RefSpec's spaces are
versioned and moving — three release candidates inside two days — so binding a
**partition key** to a vocabulary that bumps weekly means a third-party release
could move DocSpec's sealed identities with no DocSpec diff. That is the same
defect the platform closed in a receipt yesterday, reintroduced with far more
blast radius.

The shape instead: **DocSpec emits what document identity needs; it never
consumes a vocabulary to compute it.** Filing identity stays here, keyed as the
publisher keys it, because that is this layer's job and no other layer can do it.
Document identity is derived downstream, where a composite is cheap and a wrong
answer costs a re-derivation rather than a re-partition. The two are related by a
mapping someone else owns, not by an ingest-time lookup.

The remedy above is already the first instalment of that. A disposition row
carrying the number and the date emits exactly what a downstream needs to derive
document identity, without this layer ever deciding what a document is. That is
the whole reason the trade is acceptable even though the loss remains: it moves
DocSpec from silently choosing to explicitly reporting, which is the only role in
this that belongs to an ingest layer.

## What an owner needs to rule on

1. Whether the Federal Register profile keeps `document_number` as its record
   identity, or moves to a composite with `publication_date`.
2. If it keeps the bare number, whether a cross-date collision **refuses** the
   release rather than silently collapsing to the newest. This is the cheaper
   option named above and it costs no schema move — and it is cheaper again than
   this record first said, because the same-day refusal path already exists and
   runs (`tieDisposition`), so this extends a live mechanism rather than
   introducing one.
3. Whether spec §8's separation of identity from source-issued version
   (`docs/superpowers/specs/2026-08-25-source-native-release-spec.md:461-462`)
   is amended, if a composite is chosen. The first draft proposed to amend §4
   only, which would have left §8 contradicting the code.

## What was removed from this record, and why

Recorded so nobody restores it by reflex. Two things present in the first version
of this record are deliberately gone, both superseded by the census arriving on
2026-09-03:

- The `Blocks on:` line naming a collision count the crawl had not yet produced.
  It has been produced; the numbers are above.
- A section headed "What has to happen before that ruling is proportionate",
  which said to restart the full-history crawl and run `observation_census.py`
  over the result. That happened, and its output is the census cited above.

**Nothing else has been dropped.** In particular the paragraph on the seven
modern-form collisions — that `correction_of` answers null for all seven,
including the two that genuinely are self-corrections, so only the document
bodies could adjudicate them — is present and current, together with the later
measurement that four of the seven share one first publication date and are a
transition artifact rather than four independent events.

## Recommendation, now that the number exists

Take the fourth option: keep `document_number` as the record identity, keep the
collapse, and record every discarded observation on the surviving number's
disposition row. It costs one schema version and no policy version, no partition
move and no consumer re-pin,
and it converts a silent loss of 483 observations into 483 counted facts.

**Correction, same day: the remedy as first written is impossible.** "A named
disposition row per discarded observation" cannot be built. The disposition
ledger is one row per source item — `adapters/document_release_verify.py:1898`
refuses a `duplicate sourceItemId` — and for Federal Register the source item id
*is* the document number (`application/federal_register_catalog.py:224-227` over
`federal_register_source_native.py:659`). So `00-111` gets exactly one row, and
the discarded sibling has nowhere to be. Two rows per number would require a
composite disposition key, which is the composite identity arriving through the
back door with none of its honesty.

What is actually buildable, and it satisfies the condition below: **the surviving
row carries its discarded siblings.** One row per number, as the ledger requires,
with a field naming the observations that number also covered and why each lost.
That is reachable from the number by construction and does not smuggle in the
identity move.

**The carried field means "what this release collapsed", not "what this document
is".** This is part of option 4's specification, not a note on it, because it is
the clause that makes option 4 and a later composite compatible instead of
contradictory. Read as a standing claim about the document, the field survives a
composite rebuild and the corpus then asserts the same document twice — as its
own item, and as a discard recorded against the row that used to absorb it. Read
as a record of one release's collapse, it is simply absent from a release that
did not collapse anything, and the rebuild needs no cleanup step and no memory of
why. Define it this way now: it costs nothing today and is expensive to retrofit
once written the other way.

**Second correction, 2026-09-03: it costs a schema version after all.** An
earlier draft of this paragraph claimed the remedy needed "no schema version, no
policy version, no partition move and no consumer re-pin". The last three hold;
the first does not. `schemas/document_release/2.0/source-dispositions.schema.json`
declares `additionalProperties: false` over exactly eight properties, none of
which can carry a sibling list, so adding one moves `releaseSchemaDigest` and
therefore the release identity. Reusing `reason` as free text would avoid that
and would be an abuse of a closed 21-code vocabulary on a row whose disposition
is `selected`, which is worse than paying the version.

So the honest comparison is a **field addition to a closed row schema** against a
**partition-key and record-order-key move**. Both move a sealed digest. They are
not close in blast radius — a field addition leaves every existing release
readable and every partition where it is, while the composite does not — but this
record previously overstated the gap by claiming one side was free.

**Selecting is not normalizing, and the difference is load-bearing.** Whenever
two observations of one document disagree on text, something has to choose which
one the catalog item surfaces. That choice selects a filing; it does not rewrite
either filing's bytes. Both observations — the surviving one and the recorded
discard — keep their exact source text verbatim.

Implemented as a text normalization instead, the same rule would mutate source
and break exact-evidence resolution: a served match would stop resolving to its
pinned source bytes, which is the property this whole format exists to preserve.
So the remedy chooses a representative and never edits a record.

**The test that pins it, and it is not the obvious one.** Asserting that the
item's title is the ASCII spelling proves only that selection picked the right
candidate. The assertion that actually guarantees exact evidence is that **the
retained observation's title bytes are unchanged** — that the loser kept its own
spelling verbatim. Those are different claims, and a normalization implemented by
mistake would pass the first and fail the second. Write the second.

This arrived from the regulations.gov ruling on 2026-09-03, where two filings of
one document differed only in dash typography and the owner ruled the ASCII
hyphen wins because it is what users type. That case is easy because the two
texts mean the same thing. **The Federal Register case is harder and the same
distinction still holds**: there the two observations are genuinely different
documents, so choosing which one the number surfaces is a substantive loss rather
than a typographic preference, and it is exactly why the discarded observation
has to be recorded rather than merely deselected.

**Visibility is not availability, and the ruling must say which it buys.**
Recorded 2026-09-03 at spicy9's argument, which is right and was not stated
plainly enough above. Option 4 makes the loss *auditable*: a reader holding
`00-111` learns that a second document wore that number. It does not make the
2000-01-14 rule *retrievable* — its title, abstract and text are still outside
the corpus, so it cannot be searched, ranked or cited. For a product whose
purpose is search over these documents that distinction is the whole question,
and "recorded as a disposition" reads as "handled" to anyone who has not traced
what the row contains. Ruling for option 4 is ruling that **these documents stay
unfindable and we can now prove which ones**. That may well be right at this
scale; it should be chosen, not inherited.

Two things narrow the gap and belong in the same breath.

*The discard is already counted, so the remedy is smaller than a new concept.*
The source-native receipt carries `discardedObservationCount`, computed as
`inputObservationCount - publishedRecordCount`, and the release refuses unless
`inputObservationCount == publishedRecordCount + discardedObservationCount`.
That conservation check is why 483 is knowable at all. The increment option 4
asks for is not "start accounting for discards" but "give an already-counted
quantity its members" — and the existing equation is a non-tautological check on
any list produced, because it compares two independently computed quantities
rather than a filter against itself.

*A named discard and a carried discard are different remedies.* 0004 settled the
adjacent case by carrying the whole discarded record and its renditions into the
surviving row's `sourceObservations`, whose values are unconstrained — and the
Federal Register policy already writes that slot (`field-diagnostic/{index}`).
Carrying rather than naming would put the discarded title and abstract inside the
corpus, which converts spicy9's objection from "absent" to "present but not
independently indexed". That is a materially better position and it is not free:
the catalog item is downstream of the release, and the release is the layer that
drops the record, so the content has to survive the release first. Whether the
release can carry it without opening a closed schema is **unanswered here** and
is the first thing to establish if an owner leans toward option 4 — it decides
whether the remedy costs a field on a closed row or nothing at all.

**The scan's result is an input to this ruling, not a footnote to it.** If most
of the 483 are true re-observations, option 4 is plainly proportionate. If most
are distinct documents, the population of permanently unfindable rules is larger
than "474" sounds and the trade needs re-pricing. So "nobody should act on this
yet" holds for a reason beyond authority: the number that decides it does not
exist.

**The condition that decides whether this works, and it is not optional.** The
loss still happens under this remedy: the 2000-01-14 rule still never becomes a
record. The only thing separating "counted" from "silently lost" is whether a
consumer holding `00-111` can discover that a second document wore that number.
So the disposition row MUST be reachable **from the number**, not only from a run
receipt or a build log. A row that can be found solely by whoever reads the
output of the run that wrote it is a counted fact in a place nobody looks, which
is the exact failure mode this record's sibling catalogues seven times over. If
the row is queryable by number, the trade is clean. If it is not, this remedy is
the status quo with better paperwork.

**Recorded for whoever faces this next: the composite key would have worked.**
`numberAndDateUniquelyIdentify` is true and `sameNumberSameDateDifferingDigestCount`
is 0, so `(document_number, publication_date)` is a clean key over this corpus.
It is the cost that rules it out here, not the design. An owner facing different
costs — a consumer that must address one of the 474 directly, or a corpus where
the count is not 0.048% — should find that distinction in this record rather than
concluding the idea was unsound.

Reconsider the composite identity only if a consumer appears that must address
one of those 474 documents directly. At 0.047% that is a real possibility rather
than a certainty, and it is cheaper to answer it then, for the documents that
actually matter, than to move a sealed identity for all of them now.

The seven modern-form collisions are the exception worth watching. RefSpec
adjudicated them from the document bodies, because federalregister.gov's own
`correction_of` field answers null for all seven, including the two that really
are self-corrections. Metadata cannot make that call.

Two corrections to an earlier draft of this paragraph, both from RefSpec. The
five are refusals **to mint an identity**, not findings that the documents do not
exist; the shape layer still reads all five, and only the minter refuses. And the
seven are not seven independent events: four of them share the first publication
date 2010-01-06 and reappear across one week that December, which is a single
publisher reusing a block of numbers. The real population is one 2010 batch
artifact, one 2010 pair, and two 2015 self-corrections.

**Measured, 2026-09-03, replacing both earlier triggers.** The shared-first-date
story was an inference and I adopted it as though it were a measurement. RefSpec
then measured it, and the cause is better than either of us guessed. Evidence:
`~/Work/RefSpec/research/evidence/fr-collision-census-2026-09-02/addendum/`,
committed `24357cbc`. Re-derived here from the day listings rather than accepted.

2010-01-06 is the first day the modern form exists, and that issue carries three
spellings at once: 52 `E9-`, one `E10-`, and seven `2010-`, over 60 documents.
Three of the seven are the fresh sequence — tails 8, 20 and 38. The other four
are the collision members, tails 31094, 31384, 31396 and 31415.

**The argument that settles all four is counter position.** The fresh `2010-`
counter stood at 8, 20 and 38 that day, so no document published 2010-01-06 can
carry a fresh-counter value of 31094. Those four values have to come from the
other counter, and their December halves are the fresh counter reaching the same
values honestly months later. This holds for all four and needs nothing beyond
the day's three fresh tails.

Neighbour density corroborates three of the four and should not be leaned on for
the fourth. `31384`, `31396` and `31415` are wedged into gaps of 4, 2 and 2 in a
dense contiguous legacy run, where the reading is close to forced. `31094` sits
in the sparse straggler tail with neighbours at 31004 and 31150, a gap of 146,
which is descriptively a gap and evidentially very little. An earlier draft of
this paragraph rested on range and absence rather than on counter position, which
made a 3-of-4 argument look like a 4-of-4 one.

So the cause is **two counters sharing one namespace for one transition year**,
and it cannot recur, because after 2010 there is no second counter.

**The trigger that follows is neither a count nor a date.** Reopen when a
collision appears whose two observations are **both explainable by the single
modern counter**. The four transition collisions fail that test by roughly 31,000
against their own day's range of 8 to 38. The other three pass it, which is what
makes them the interesting ones. A count moving from seven to eight because
somebody re-crawled 2010 is not signal, and neither is a shared first date, which
would let three genuinely new collisions hide behind an old one.

**And the seven are not seven of a kind, which strengthens the proportionality
argument above.** For the four transition collisions the discarded observation is
the transitional *spelling* of a document the legacy run already accounts for,
not a distinct rule lost. `2010-517` is the one where the discarded observation
is genuinely a different document from a different agency. If this record wants
to say the loss is small, it can now say so about a named case rather than about
seven averaged together.

## The consumers this shape move broke, and the list that should carry the next one

Recorded 2026-09-05, after the fact rather than before it, which is the point.

Making `sourceRecordId` composite is correct and this record stands. What it
also did was break every consumer that had been keying on it — silently, because
the composite form is a well-shaped string that simply matches nothing. The
producer change landed alone; the consumer changes did not travel with it. Each
was found by a separate failure hours apart, and the last one refused a
62-minute composition build.

**Known consumers of the pre-composite shape, all now fixed:**

| where | what it keyed on | how it failed | fixed in |
| --- | --- | --- | --- |
| DocSpec `regulations_gov_catalog._index_rows` | index key was `sourceRecordId` | Federal Register join coverage 430,323 → **0** across 499,238 eligible; build still reported `pass` | DocSpec `3880818` |
| DocSpec `regulations_gov_catalog`, exact-join guard | compared returned `sourceRecordId` to the bare `frDocNum` | could never agree once composite; never fired only because the lookup was already returning `None` for every row | DocSpec `3880818` |
| SpicySearch, native-fact decision | compared subject id to bare `document_number` | refused release two after 62 minutes | spicysearch `2b25860` |
| SpicySearch, exact-join guard | `matchedSourceRecordId` | same shape | spicysearch `2b25860` |
| SpicySearch tools, frozen-query runner | join key | same shape | spicysearch `2b25860` |

**The rule this buys.** A join or lookup key must name a stable *field*
(`record.document_number`), never a record's identity. An identity is allowed to
change shape — that is what this decision is — so any key riding on one inherits
every move it makes. When the two are currently the same string, that is not
safety, it is the absence of a signal: nothing in a test, a type, or a review
will name the coupling, because there is nothing yet to distinguish.

**Before the next identity shape moves,** grep every consuming repository for
the identity field and land those changes in the same commit as the producer's.
This table is the starting list, not the finished one — it was assembled from
three separate outages, so treat an empty grep as unfinished work rather than as
evidence of none.

**A second defect, independent of the key.** The zeroed join reported verdict
`pass`. A catalog with no joins is structurally identical to one whose documents
genuinely reference nothing, so nothing downstream could tell. `3880818` adds a
build-time backstop — a join with 10,000 or more eligible rows matching none of
them refuses — and labels it as a backstop: a build cannot know what its coverage
ought to be, and only comparison against the catalog it succeeds can say coverage
fell from 86% to zero. That check belongs in succession and does not exist yet.


## The succession record cannot be written retroactively, and why

The source-native release spec (spicy-docs
`docs/superpowers/specs/2026-08-25-source-native-release-spec.md`) says a
successor "records the exact superseded logical ID and artifact digest only as
publication evidence in the Rulespec root `supersedes` record". Checked
2026-09-05: **`supersedes` is absent from both roots**, and both carry
`inputs: []`. So the record this decision's rebuild owed was never written.

**It cannot be added now.** `artifactDigest` covers the root, so writing a
`supersedes` field changes the root's bytes and moves that digest — and that
digest is the value catalog-A (`sha256:1ac55e0c…`) and catalog-B
(`sha256:0872a608…`) pin, and that both admit against. Adding the record would
supersede the very release the record describes, and invalidate two catalogs and
a day of builds to document a lineage that is already fully written down here and
in `receipts/fr-composite-replay-2026-09-05.md`. The cure would cost more than
the defect and would itself need a succession record.

**What the next release does.** The next Federal Register release published from
this producer writes `supersedes` naming `composite-2`'s logical id
(`…d3d0a062…`) and artifact digest (`sha256:6cf8cf44…`) at publish time, when it
is free. Until then the lineage lives in this record, which is where a reader
looking for it will now find it. Note what the missing field does and does not
cost: logical identity already carries the lineage correctly — `composite-2`'s
`logicalId` moved from the source release's `…5f35c56b…` because the source
state changed, exactly as the spec says it should. What is missing is the
publication evidence naming the predecessor, not the identity relationship
itself.

**Where the gap actually is — corrected within the hour, because the first
version of this paragraph diagnosed it without checking.** It said the producer
writes no `supersedes` field and the fix belongs in the publisher. Wrong on
both counts. `SourceNativeReleaseBuild.supersedes` exists
(spicy-docs `source_native.py:146`) and is passed straight through to the
artifact writer (`:1683`); the dataclass docstring states the design
deliberately — *"Publication evidence supplied by the caller, not inferred from
a worktree."* The publisher has supported succession all along.

**The callers never supply it.** Neither
`tools/replay_federal_register_release.py` nor `tools/source_native_campaign.py`
passes `supersedes`, so it defaults to `None` and every release published by
either has silently omitted it. The replay tool is the clearest case: it exists
precisely to republish one release from another's evidence, so it always knows
its predecessor and has never recorded it.

So the fix is two lines in the callers, plus the question of whether a
republish that omits a known predecessor should be refused rather than
defaulted. That is a real choice — a first release legitimately has no
predecessor, and DocSpec's own document-release path already draws that line
(`platform_artifact.py:462`: an initial release *must not* declare
`supersedes`), which is the working model to copy rather than invent.

**And the lesson is the one this record keeps relearning:** I named a plausible
cause — "the producer does not support it" — and wrote it into a decision
record without opening the producer. Same error as the receipt that attributed
639 absent citations to pre-1994 scope and a parser defect, where the real cause
was leading zeros and both named candidates were wrong. A cause that sounds
right and is untested is worse in a decision record than in a message, because
the record is what the next reader trusts.
