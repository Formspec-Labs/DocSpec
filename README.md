# DocSpec

DocSpec turns bulk regulatory documents — Federal Register issues,
regulations.gov dockets, and similar public-sector sources — into **one
verified, immutable data product** (a `DocumentRelease`) that other software
can trust without trusting this repository.

The idea in four steps:

1. **Consume** — a sealed `SourceCatalogRelease` names exactly which source
   files exist, pinned by digest. DocSpec never decides what to fetch; it
   verifies and captures what the catalog declares.
2. **Process** — format-neutral adapters normalize each capture (PDF, XML,
   JSON, HTML) into document nodes, structural segments, and evidence
   coordinates that point back to exact byte ranges in the source.
3. **Publish** — everything is sealed into one portable, immutable
   `DocumentRelease`: closed membership, every member digest-pinned,
   coverage accounted in both directions (every source claim lands
   somewhere; every published claim traces to a source).
4. **Serve nothing** — consumers (chiefly SpicySearch) verify the release's
   digests at admission and open, then read it directly. No DocSpec service,
   no database, no sibling checkout.

The seam is `SourceCatalogRelease → DocumentRelease`. Ownership rows and the
exchange rule live in REF-024 (`RefSpec/docs/decisions.md`) — products trade
published, digest-pinned files, never source trees.

## Quick start

```sh
uv sync --python 3.12
uv run pytest          # offline, standalone
uv run ruff check .
uv run docspec --help  # the one CLI
```

## Where things are

| | |
|---|---|
| Domain model (documents, segments, evidence) | `src/docspec/domain/` |
| Format adapters + source access | `src/docspec/adapters/` |
| Processing / segmentation | `src/docspec/processing/` |
| Release pipeline | `src/docspec/application/` |
| Document profiles (per source kind) | `src/docspec/profiles/` |
| Conformance fixtures | `conformance/`, `fixtures/` |
| Active plan | `docs/plans/` (newest date wins) |

## Boundaries

DocSpec owns document capture, normalization, identity, segmentation, and
evidence addresses. It does not own vocabularies (RefSpec), semantic
contracts (Rulespec), or search/ranking/serving (SpicySearch). A consumer
needs the published `DocumentRelease` and the installed `docspec` package —
nothing else from this repository.

**Status:** internal, unpublished; no license selected.
