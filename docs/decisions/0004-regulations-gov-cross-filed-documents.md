# Decision 0004: a cross-filed Regulations.gov document is one item with a recorded discard

- Date: 2026-09-03
- Status: **accepted and implemented**, 2026-09-03. Ruled by the product owner;
  landed on main as `cc3336e`, fixed at `45751e1`, and **measured against the
  full 671-input catalog-A build on 2026-09-04** — see "What the build
  measured".
- Raised-by: the full 671-input catalog-A build, which refused and could not
  complete until this landed
- Beside, not inside, `0003-federal-register-record-identity.md`: doc1 ruled that a
  different profile, a different mechanism and a different question deserve their
  own record rather than being folded into one titled for the Federal Register.

## The failure

`docspec source-catalog build` over the 671 catalog-A inputs aborted in about
ninety seconds with

> source-native inputs repeat a sourceRecordId for one policy selector

raised when `_CatalogPolicyInputs._load` stores a repeated key. **Since the
collapse landed, that message means something narrower**: the loader now asks
the policy to resolve a repeat, and refuses only when the policy cannot — an
unresolvable repeat, not any repeat. The string is deliberately unchanged, so
it stays greppable and this record stays citable.

Cited by symbol and by the quoted string rather than by line number: three
citations in this record have gone stale already, two of them within an hour,
because the file is under active change. `_load` predates the collapse and is
what actually aborts; the message is what a reader greps for.

Two records, out of 2,221,713:

| sourceRecordId | appears in |
| --- | --- |
| `DHS_FRDOC_0001-2737` | `regs-documents-CISA`, `regs-documents-DHS` |
| `DHS_FRDOC_0001-2740` | `regs-documents-DHS`, `regs-documents-USCIS` |

Measured independently three times, and the populations differ — name the one
you mean, because three counts circulate:

| count | population |
| --- | --- |
| **2,221,713** | every record in the 670 non-Federal-Register releases — 335 document releases and 335 docket releases, one pair per agency |
| **1,943,108** | document records only, from the 335 document releases |
| **1,797,201** | of those, the ones carrying both a `documentId` and a `docketId` — the population the owner rule can be evaluated over |

The two above are the **only** two documentIds appearing in more than one
release corpus-wide.

## This is not 0003's problem, and it is nearly its inverse

0003 is one identifier naming **two genuinely different documents**: the
publisher reused a Federal Register number, and the builder collapses to the
newest publication date, so the older document never becomes a record at all.

This is one document appearing as **two source records**, because the mirror
files it under two agency prefixes. `frDocNum` is identical within each pair —
`2025-23504` for -2737, `2025-23853` for -2740 — which establishes one document
rather than assuming it.

Opposite failure costs, in refb's framing: 0003 silently merges two things, this
silently splits one. Only 0003 is an identity question.

The loud refusal that surfaces both is **not** 0003's doing, and no ruling on
0003 could weaken it. It comes from the workspace's primary key over namespace
and ordered key, plus the explicit re-raise above.

## The owner is decided by measurement, not by judgement

RefSpec supplied a rule; it was re-run here over all 1,797,201 document records
over that 1,797,201. Two tests, **four exceptions corpus-wide**, and both
blocking records are among them — each caught by exactly one test and neither by
both:

| test | exceptions | catches |
| --- | --- | --- |
| `documentId` starts with `docketId + "-"` | 3 | `DHS_FRDOC_0001-2740`, whose docket is `USCIS-2025-0040` → **USCIS is the cross-file** |
| `docketId` starts with `agencyId` | 1 | `DHS_FRDOC_0001-2737`, docket `DHS_FRDOC_0001`, agencyId `CISA` → **CISA is the cross-file** |

Both resolve to **DHS**. The docket-prefix tiebreak is itself a measured rule
rather than a fallback: it has exactly one exception in 1,797,201 records, and
that exception is the record it is being used to decide.

**An independent cross-check that the populations are right.** 1,756,713
one-segment sequences plus 40,485 two-segment sequences is 1,797,198 — exactly
three short of 1,797,201, and three is the stated exception count for the
`documentId`-starts-with-`docketId` test. Those three numbers were measured
separately, in different passes, and they reconcile to the record. A wrong
population would not have closed.

The rule must be stated as **prefix containment**, not "docket plus one trailing
segment". 1,756,713 sequences are one segment and **40,485 are two** —
`DOT-OST-1995-125-0050-0001` in docket `DOT-OST-1995-125`. The narrow phrasing
survives a 917-record sample and reports 40,488 false violations at full scale.

The other two exceptions to the first test — `EPA_FRDOC_0001-3113` in docket
`EPA-HQ-OAR-2002-0064` and `OSHA_FRDOC_0001-0003` in docket `OSHA-2007-0034` —
are **not** cross-files. Each appears in exactly one release. They are left
deliberately unclaimed: two specimens cannot settle whether a publisher may file
a Federal Register document into a subject docket, and RefSpec would rather it
sit visibly unruled than take a thin ruling.

## The copies are not redundant, which decides the remedy

The two filings were compared field by field — the only pairs in the corpus that
have been.

**`DHS_FRDOC_0001-2737`**, 8 of 84 leaf fields differ. Same docket, same title,
same `frVolNum` `90 FR 59851`. Differs on `postedDate` by **17 days** (DHS
2025-12-22, CISA 2026-01-08), on `commentStartDate` (CISA only), and on
`displayProperties` (a page count, DHS only).

**`DHS_FRDOC_0001-2740`**, 6 of 90 differ. Differs on `docketId`
(`DHS_FRDOC_0001` against **`USCIS-2025-0040`**, a real distinct docket), on
`frVolNum` (`90 FR 60864`, USCIS only), on `commentStartDate`, and on title
typography only — "To File" against "to File", and a hyphen-minus against an
en-dash in H-1B.

So a plain discard destroys real filing metadata. The proportionality argument —
two records in 2.2 million — survives, but it cannot rest on the copies being
redundant, because they are not.

**A caveat the owner ruled with.** The corpus-wide figure of 13,328 clean
duplicate groups was measured over four fields (title, posted date, Federal
Register number, page count) out of roughly eighty-five. Both pairs above agree
on all four and still differ elsewhere. That number means "agree on four
fields", not "are identical".

## What was ruled

**1. Collapse to the measured owner, and retain the non-owner filing as a
recorded observation.** Not a discard. This is 0003's own remedy applied to an
easier case: 0003 must justify discarding a distinct document, while this
discards a redundant *filing* of the same one. It needs no exception from 0003
and never did.

**2. Where two filings differ only by dash typography, the ASCII hyphen wins.**
An en-dash and a hyphen-minus are different search tokens, so the surviving
title decides whether an exact-match query for `H-1B` finds the rule, and
normalization cannot undo it afterwards.

### Selection is not substitution

Ruling 2 selects **which filing's title the item carries**. It does not rewrite
either filing's bytes. The retained observation keeps its own spelling verbatim.

Implemented as a normalization that mutates source text it would break the
exact-evidence rule, and a served match would stop resolving to its pinned
source. The general principle is recorded in 0003; this is its first instance.

The test that pins it is **not** that the item's title is the ASCII spelling —
a normalization implemented by mistake passes that. It is that **the retained
observation's title bytes are unchanged**.

## Cost: no schema version — corrected

**An earlier revision of this record said the retained observation needs a new
field, that `catalogSchemaDigest` therefore moves, and that every existing
catalog would stop opening. That was wrong, and it was wrong in the expensive
direction.** The schema already accommodates it.

`schemas/source_catalog/1.0/source-item.schema.json` carries
`sourceObservations`: an array of `{observationKey, observationValue}` where
`observationKey` is any string of `minLength: 1` and **`observationValue` has no
type constraint at all**. The array item is `additionalProperties: false` over
exactly those two keys, so writing both satisfies it.

It is already used this way, with free-form namespaced keys and structured
values — `field-diagnostic/{index}` carrying `{code, field, value}`,
`comment/source-issued-version-policy`,
`comment/unparseable-comment-on-document-id`,
`unparseableFederalRegisterDocumentNumber`. There is no enum, no registry and no
code-level constraint on the key; the policies simply write them. A discarded
cross-filed filing is an observation about the source in exactly the sense the
slot already serves.

So the collapse:

- moves **no** schema version,
- moves **no** `catalogSchemaDigest`,
- leaves every existing catalog openable.

### The retained filing rides on the surviving row, and that is forced

Stated as a constraint rather than a preference, because written as a
preference a future reader proposes the alternative again — doc1 did, and found
it unbuildable.

A sibling row carrying the discarded filing *cannot* be written, and the
refusal that stops it is **not** the loader. `_CatalogPolicyInputs._load`
refuses a repeated `sourceRecordId` on *input*, and this decision deliberately
made it permissive — it now asks the policy first. What refuses a second
**catalog item** is `_CatalogRowPartitioner.stage`, which requires strictly
increasing `sourceItemId` and so rejects a duplicate as an ordering violation;
the release-side equivalent is the `duplicate sourceItemId` refusal in
`document_release_verify`.

Naming the loader here would be worse than a miscitation: a reader sent to it
would find it resolving duplicates and conclude the constraint had lapsed,
which is precisely the re-litigation this section exists to prevent. The dispositions schema is closed, so a new
top-level disposition kind is not available either. `sourceObservations` on the
surviving row is what remains.

Two things would have to change together before the sibling shape is even
discussable: the item key would have to admit more than `sourceItemId`, and the
dispositions schema would have to open. Either alone leaves it unbuildable.

### Retained filings are staged on both paths, for symmetry rather than for a hazard

The universe path and the lookup path both carry `discardedFilings`. The
universe path is the one that matters today.

The lookup path is currently unreachable, and the record should say so rather
than imply a live loss. `_index_rows` has one call site and it passes
`federal_register_input`, so the Federal Register index is the only lookup
input. A Federal Register native record is flat — `abstract`, `agencies`,
`document_number` — with **no `data` key at all**, so `_record_data(...,
expected_type="documents")` refuses it, both candidates fail the ownership
test, the resolver returns `None`, and the loader's refusal stands. Verified
against a real catalog-B record rather than reasoned from the schema.

It is staged anyway so the property holds structurally when a second lookup
input appears, and it is deliberately untested: the only test reads
`iter_universe_rows`, because asserting an unreachable path would be asserting
nothing.

**This corrects `cc3336e`'s merge message**, which claims the both-paths
staging prevents "exactly the silent loss this decision exists to prevent". It
does not, today; it prevents a loss that would exist if a second lookup input
were added. A merge message cannot be edited, so the correction lives here.

### Which unbundles the reason-code labelling

The previous revision bundled `dispositionCounts` reason-code labelling with
this change, on the reasoning that the schema cost was being paid once anyway
and it was cheaper to ride along. **That reasoning is void.** The receipt's
`dispositionCounts` is closed over five integer buckets, so labelling it by
reason code genuinely does move `catalogSchemaDigest` — and bundling it here
would now *introduce* a schema move into a change that otherwise has none,
rather than share one.

The labelling is still worth doing, and the defect it addresses is still real:
the receipt reports `failed: 5678` with no reason anywhere, and a `reasonsDigest`
that pins content which is **not a member of the distribution** — catalog-B's
manifest declares 66 members and none is a reasons file. But it is now a
separate decision with its own cost, and it does not block catalog-A.

## What the build measured

catalog-A completed 2026-09-04 from `5f7e26b`: `buildExit=0`, verdict `pass`,
55 m 14 s, 2,221,713 items.

**Collapsed 2 of 2** — `DHS_FRDOC_0001-2737` and `DHS_FRDOC_0001-2740`, the two
this record names. **State the dependency rather than call it confirmation:**
the projection took spicy9's census as its input, so this is one measurement
agreeing with a projection derived from it, not two independent findings. What
it establishes is that the build did not contradict the census — which is worth
having, and is less than it looks.

**Zero `cross-file-discard/*` on the lookup path**, as predicted here and by
spicy9 independently — this record's reason being that a Federal Register
record is flat and `_record_data(expected_type="documents")` refuses it, and
spicy9's being that the upstream eviction leaves nothing to collide. Both
discards are on the regulations.gov universe path.

**The retained filing survives into the artifact.** The surviving item carries
`sourceObservations[0]` keyed `cross-file-discard/0` holding the *whole*
discarded record — `data.attributes` with `agencyId: USCIS` intact — and its
renditions. Not a reference to it, the record. That is the byte-level property
this decision was written around, now observable in a real artifact rather than
in a fixture, and it is a working precedent for the "carry rather than name"
option 0003 still has open for the Federal Register case.

**The build is a measurement vehicle, not a publishable artifact.** It pins a
Federal Register release missing roughly 359–415 real documents to the identity
defect 0003 describes. Its pins are not to be handed to a serving path.

## What this does not do

- It does not make the mirror agency identity-bearing. RefSpec's position is that
  the agency is already inside the identifier — `DHS_FRDOC_0001-2737` decomposes
  as docket `DHS_FRDOC_0001` plus sequence, and every agency holds exactly one
  `AGENCY_FRDOC_0001` docket — so keying on mirror-agency plus document would put
  the agency in the key twice, and the second copy would be the crawl's agency
  rather than the document's.
- It does not re-key the item. `SourceInputSelector` carries five fields and
  agency is not among them, so agency-scoped keys would add a dimension the
  selector does not have: a format break, not a tightening.
- It does not rule on `EPA_FRDOC_0001-3113` or `OSHA_FRDOC_0001-0003`.
- It does not touch the two Federal Register questions 0003 owns.
