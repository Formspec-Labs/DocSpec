# Decision 0003: the Federal Register record identity is undecided, and the collapse is a defect

- Date: 2026-09-02
- Status: **open — needs an owner's ruling.** Nothing here is accepted.
- Evidence strengthened 2026-09-03: the loss is now demonstrated in a built
  release, not inferred from a comment. One measurement is still owed — see
  "The measurement this record still owes".
- Raised-by: agent, from a code review of spicy-docs against the source-native spec
- Was blocked on a measured collision count. **Unblocked 2026-09-03**: the census
  exists, and the numbers are in "What the census measured" below.
- Split from: `docs/decisions/0002-shared-execution-and-the-acquisition-gap-ledger.md`,
  where this was rule 8 of 11 and did not belong

## Why this is its own record

It changes a sealed identity, it contradicts two clauses of a live specification,
it is contested between two agents, and the remedy first proposed for it was
retracted within hours by the same review that raised it. Burying a contested
identity decision inside a record about execution topology is how it gets
executed without being decided.

The one thing this record does decide is that nobody should act on it yet. The
diagnosis is sound; the remedy is not settled; and the measurement that would
size the problem does not exist.

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
changes a sealed identity, it contradicts two clauses of a live spec, it is
contested between two agents, and it needs an owner's ruling rather than an
agent's. Burying a contested identity decision inside a record about execution
topology is how it gets executed without being decided.

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

### Two claims relayed with this finding that do not survive checking

**"The release is labelled complete-snapshot and silently is not."** False for
this profile. `FEDERAL_REGISTER_PROFILE` sets
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
already keyed on. Sizing it needs no fetch: the crawl observed these records, and
the question is only whether the pipeline still holds them.

Until that exists, the honest statement of cost is: **at least one** real
document is absent from a published release, and at most 474 are.

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
