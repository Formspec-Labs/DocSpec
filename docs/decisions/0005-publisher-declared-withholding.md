# 0005 — Publisher-declared withholding is a reason code, and the receipt counts reasons

Accepted 2026-09-04 (Mike, relayed through the overseer session). Implemented in
the commit that adds this file. The policy-version move it implies lands in a
later commit together with spicy9's publisher-test-fixture exclusion, so that
one `1.1.0 → 1.2.0` and one catalog-A rebuild absorb both behaviour changes;
see "What moves" below for why they are not separable.

## What changed

- An `unavailable` row whose source record carries `restrictReasonType` now
  takes that value as its reason code, one code per publisher value and nothing
  else: `source.publisher-withheld.copyrighted`,
  `source.publisher-withheld.confidential-business-information`,
  `source.publisher-withheld.personally-identifiable-information`,
  `source.publisher-withheld.other`. The reason text carries the value verbatim
  and the record's `subtype` when present.
- A value outside those four is not inferred into a bucket. The row fails with
  `source.restrict-reason-unread` and the receipt shows the count.
- A row with no `restrictReasonType` keeps `source.no-candidate-rendition`,
  unchanged. A row that carries the field *and* offers a rendition stays
  `selected` (57 such rows in catalog-A): the field labels an absence, it does
  not create one.
- The policy configuration records the rule under `publisherWithholding` so a
  reader of the sealed policy member can see it without the code.
- The build receipt gains `reasonCounts`: one closed row per
  (disposition, reasonCode) that occurred, in UTF-16 order, reconciled to each
  `dispositionCounts` bucket by the bounded verifier at every open and
  recomputed by the producer gate before publish. `selected` rows carry no
  reason and never appear.

## Why now

[0004](0004-regulations-gov-cross-filed-documents.md) unbundled this labelling
because `dispositionCounts` is closed over five integers, so labelling by
reason moved `catalogSchemaDigest` while nothing else was moving. The
2026-09-04 campaign changed the arithmetic (receipt:
`~/Work/corpora/supply-2026-09-02/receipts/unavailable-web-search-campaign-2026-09-04.json`):

- 74,976 of catalog-A's 865,206 `unavailable` rows carry a declared reason
  (Copyrighted 53,580, Other 19,965, CBI 1,179, PII 252), and the field was
  already in our source records.
- No sampled row carrying one had a file at the publisher; doc1's independent
  probe found 0 of 45 Copyrighted and 0 of 3 CBI with a retrievable file.
- The remaining 790,230 carry no reason, and the disposition collapsed both
  populations into one bucket that read as a statement about the world.

Mike authorized the split on that evidence. The split is what the field says,
and only what the field says.

## What moves, and what does not

Moves in this commit: `catalogSchemaDigest` (the receipt shape). Moves in the
bump commit: `selectionPolicyVersion` and `selectionPolicyDigest`, and the
docspec package version, because SpicySearch admits catalogs through a vendored
wheel (`docspec==0.2.9`) whose verifier compares `catalogSchemaDigest` to its
own schema bundle. SpicySearch will refuse any catalog built after this until
that wheel is re-vendored. That is the fail-closed rule working as written, not
a defect; the re-pin is a SpicySearch change and Mike's call. Its
`tests/search/test_platform_source_catalog.py` constructs a summary and will
need `reason_counts` at that point.

Does not move: the five-value disposition enum every consumer switches on; the
item schema; the catalog format label `1.0`, which names the format family
while the digest is the identity.

Why one bump with spicy9's change: a sealed version absorbs a second behaviour
change only until an artifact exists under it. Two bumps an hour apart with a
rebuild between them would cost `1.3.0` and a second 55-minute build.

[0003](0003-federal-register-record-identity.md)'s preferred remedy costs no
catalog schema version and no policy version, so the Federal Register identity
rebuild does not move these digests again. That is a statement about 0003 as
written, not a promise that this is the last move.

## What is deliberately not inferred

The 790,230 rows with no declared reason stay `source.no-candidate-rendition`.
spicy9's coverage receipt (`unavailable-coverage-2026-09-04.md`, same
directory) shows 410,329 of them carry inline `comment` text and that the
`attachments` relationship is a stub present on every record, so it sizes
nothing. Surfacing inline text is a materialization question for its own
decision; it is not a disposition and this record does not touch it.
