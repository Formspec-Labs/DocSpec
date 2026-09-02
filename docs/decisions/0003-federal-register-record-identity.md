# Decision 0003: the Federal Register record identity is undecided, and the collapse is a defect

- Date: 2026-09-02
- Status: **open — needs an owner's ruling.** Nothing here is accepted.
- Raised-by: agent, from a code review of spicy-docs against the source-native spec
- Blocks on: a measured collision count from `fr-full-collision-census.json`,
  which the Federal Register full-history crawl has not yet produced
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

## What has to happen before that ruling is proportionate

Restart the Federal Register full-history crawl and run
`spicy-docs/tools/observation_census.py` over the result. It replays
acquisition-pages evidence rather than published records, so it recovers the
discarded observations, and it already emits `sameNumberDifferentDateCount` and
`modernFormCollisions` as named fields. Nobody currently knows whether this is
two documents or twenty thousand, and every option above prices differently at
those two ends.
