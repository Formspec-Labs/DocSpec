# Decision 0003: the Federal Register record identity is undecided, and the collapse is a defect

- Date: 2026-09-02
- Status: **open — needs an owner's ruling.** Nothing here is accepted.
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
   option named above and it costs no schema move.
3. Whether spec §8's separation of identity from source-issued version
   (`docs/superpowers/specs/2026-08-25-source-native-release-spec.md:461-462`)
   is amended, if a composite is chosen. The first draft proposed to amend §4
   only, which would have left §8 contradicting the code.

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
