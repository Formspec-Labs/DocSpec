# Two public surfaces SpicySearch needs, with the measurement behind them

2026-09-05. Recorded because the number lived only in a chat message, and a
measured cost that exists only in a conversation is one nobody can re-derive.
Requested by SpicySearch (branch `perf/admit-once` at `3e0b517`, unmerged) for a
future docspec version. **Not implemented; this is the record, not the change.**

## The measurement

Reading a 99,545-row catalog through the public surface cost **34.8M
`_freeze_checked` calls**, about 350 per row, and roughly **half** of
SpicySearch's 99k demo build. The cost is the domain round trip: `from_dict` →
freeze → `to_dict`, paid per row to produce values the caller immediately turns
back into plain dicts.

## What already exists, so the fix is a surface and not machinery

Two mechanisms in this repository already avoid that cost, and neither is
reachable from the public API:

1. `domain.identity.trusted_json_input()` — its own docstring names this exact
   case: "for values decoded from bytes a verifier has ALREADY admitted (a
   reader re-streaming a catalog it verified)". Used at
   `adapters/source_catalog_artifact.py:405` for the unvalidated re-stream,
   where it skips the checks canonical parsing already established and only
   makes the tree immutable.
2. `_iter_located_catalog_rows(..., as_dict=True)` — skips constructing
   `SourceCatalogItem` altogether, which is what the caller actually wants. It
   is module-private.

So SpicySearch is reaching past the public surface for something the module
already does correctly. That is migration debt on their side and a missing
export on ours; the remedy is ours.

## The two requests

1. **A public way to stream a snapshot's rows as validated plain dicts**,
   without constructing `SourceCatalogItem` per row. Today only the private
   `_iter_located_catalog_rows(..., as_dict=True)` does it.
2. **A way to build a `SourceCatalogSnapshot` from a caller-supplied
   `VerifiedArtifact`/`MemberSource` the caller already admitted**, rather than
   only from a `SourceCatalogRef` this package re-admits itself.
   `SourceCatalogArtifactVerifier` is exported from
   `adapters.source_catalog_artifact` but not from the `docspec.source_catalog`
   facade.

Both are consistent with the tiered-verification rule: admission and digest
checks are cheap and run every time; re-deriving what a verifier already
established is the work to remove. "Fewer validations, not faster validation."

## What is settled and should not be re-litigated

SpicySearch traced its own admission path and confirmed the build-gate verifier
never imports the application layer, so it never rebuilt a lookup index and
never reproduced the Federal Register zero-join shape internally. Admit-once is
therefore a **performance** change on their side, not a correctness one.
