# DocSpec

DocSpec turns immutable source-native releases into a verified `SourceCatalog`,
then turns selected source files into verified document state. It publishes the
product-owned kinds `docspec-source-catalog`, `docspec-processing-plan`, and
`docspec-document-release` through Rulespec's one generic artifact container.
Other software can trust those installed schemas and immutable files without
trusting this repository or a running DocSpec service.

The idea in four steps:

1. **Catalog** — bounded adapters read sealed source-native releases, preserve
   source facts, apply one explicit catalog policy, and publish the normative
   `SourceCatalog` and `SourceItem` model.
2. **Process** — the sealed catalog selects candidate files. Format-neutral
   adapters capture and normalize each selection (PDF, XML,
   JSON, HTML) into document nodes, structural segments, and evidence
   coordinates that point back to exact byte ranges in the source.
3. **Publish** — the catalog, plan, and document release each use the same
   portable Rulespec container: closed membership, exact digests, and
   product-owned semantic verification. No DocSpec-specific exchange root or
   structural verifier exists beside it.
4. **Serve nothing** — consumers (chiefly SpicySearch) verify the release's
   digests at admission and open, then read it directly. No DocSpec service,
   no database, no sibling checkout.

The seam is `source-native → SourceCatalog → DocumentRelease`. REF-048 in
`RefSpec/docs/decisions.md` assigns catalog ownership to DocSpec and supersedes
REF-024's older SpicyRegs/DocSpec catalog row; REF-024's other ownership rows and
the file-exchange rule remain. Products trade published, digest-pinned files,
never source trees.

DocSpec's application functions depend on small injected ports for source,
catalog, blob, task, and result access. They run locally, in external processes,
or through a maintained distributed executor. Dagster is an optional adapter at
that edge; it does not define document or catalog meaning.

## Quick start

```sh
uv sync --python 3.12
uv run pytest          # offline, standalone
uv run ruff check .
uv run docspec --help  # the one CLI
```

## Where things are

| | |
| --- | --- |
| Domain model (documents, segments, evidence) | `src/docspec/domain/` |
| Format adapters + source access | `src/docspec/adapters/` |
| Processing / segmentation | `src/docspec/processing/` |
| Release pipeline | `src/docspec/application/` |
| Document profiles (per source kind) | `profiles/` |
| Conformance fixtures | `conformance/`, `fixtures/` |
| Active plan | `docs/plans/` (newest date wins) |

## Boundaries

DocSpec owns `SourceCatalog`, `SourceItem`, catalog policy, document capture,
normalization, identity, segmentation, and evidence addresses. Rulespec owns
only the generic artifact bytes and structural checks. RefSpec owns governed
reference resources. SpicyRegs owns faithful source-native acquisition and
public raw-data publication. SpicySearch owns search, ranking, composition, and
serving. A consumer needs the published artifacts and installed packages—not a
sibling checkout.

**Status:** internal, unpublished; no license selected.
