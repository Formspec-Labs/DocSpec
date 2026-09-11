# Generated wiki consolidation — 2026-09-11

The generated wiki was consolidated into maintained guides under `docs/` so
contributors have one current explanation and navigation tree. Useful operational
meaning was checked against current code owners. Generated class inventories,
method summaries, and stale module links were retired rather than copied into a
second current reference.

This record describes the consolidation, not a fresh generation, deployment, or
qualification result. The [documentation index](../documentation.md) owns current
navigation; the [decision index](../decisions/README.md) identifies accepted rules
and implementation gaps.

## Provenance retained

The [original metadata](2026-09-03-wiki-generation.json) is byte-for-byte unchanged.
It records generation at `2026-09-03T00:26:53.219277+00:00`, model `gpt-5.6-sol`,
generator version `1.0.1`, and source revision
`0a41ac7fb50f0f0044738b4d7fdf2f2e1b8b5cf7`. Its statistics describe 1,886
components, 288 leaf nodes, and depth 5. Those are generator-reported values,
not measurements of the refactored repository.

The [file inventory](2026-09-11-wiki-inventory.json) records byte counts and
SHA-256 hashes for all 32 files observed under `wiki/`: 28 Markdown pages, two
navigation trees, metadata, and one local dependency cache. It distinguishes
the 31 tracked files from the ignored cache and maps each retired page to the
maintained explanation that retains its useful meaning.

All 31 tracked files were byte-checked against revision
`b0878fdf4c0cdda1bc138c3e6e50627343cc76c6` before removal. Their exact historical
content remains available in Git, including navigation repairs made after the
recorded generation. For example, from a checkout containing that history:

```sh
git show b0878fdf4c0cdda1bc138c3e6e50627343cc76c6:wiki/processor_extension_model.md
```

The 4,319,676-byte dependency graph under `wiki/temp/` was an ignored local cache,
so that command cannot recover it. Its 1,886 entries all include extracted
`source_code`; its hash and cache status remain in the inventory. No checked-in
consumer of it or the navigation trees was found. The two navigation trees were
byte-identical. Neither the cache nor those trees contained independent runtime
evidence or a distinct maintained explanation.

The repository contains no checked-in command that reproduces the external wiki
generator. Retaining exact generation metadata does not establish reproducibility
of that generator. Historical source bytes and the tracked generated snapshot
remain the evidence available here.

## What was retained and where

| Former page family | Useful meaning retained | Current location |
| --- | --- | --- |
| Overview and the three area overviews | Inputs, processing flow, durable outputs, ownership, and verification boundaries | [Architecture](../architecture.md) and [documentation index](../documentation.md) |
| Source catalog pipeline, model/ports, policy execution, artifacts/storage | Full evidence rows versus processing items; universe and lookup roles; field provenance; bounded construction; reader admission versus complete verification | [Catalog and processing](../catalog-and-processing.md) |
| Acquisition, extraction, and segmentation | Capture verification; distinct extraction compositions; exact UTF-8 coordinates, segmentation bounds, and evidence | [Catalog and processing](../catalog-and-processing.md) |
| Processing plans, processor extension, and portable tasks | Declared inputs and identities; same-segment prerequisites; cache validation; profiles; portable task references; scheduling ownership | [Extensions](../extensions.md), [architecture](../architecture.md), and [operations](../operations.md) |
| Application planning, execution, delivery, and release | Explicit planned population; revision admission; checkpoints and budgets; delivery recovery; complete reconciliation; expected-current publication | [Operations](../operations.md) and [architecture](../architecture.md) |
| Result delivery, shared references, and storage adapters | Complete stream accounting; immutable references and verification depth; incremental partition retention; bounded workspaces | [Extensions](../extensions.md) and [operations](../operations.md) |
| Release artifacts | Distinct application and portable release representations; format authority; verification and schema ownership | [Architecture](../architecture.md), [schema maintenance](../schema-maintenance.md), and [decision index](../decisions/README.md) |
| Release maintenance and scale acceptance | Explicit retention roots and profile reachability; inventory-only garbage collection; compaction equivalence; qualification evidence limits | [Operations](../operations.md) |

The per-file inventory supplies the full 28-page mapping. Maintained pages link
to current implementations and focused tests instead of reproducing generated
member tables. The portable builder is a historical mint recipe; it is not an
installed generic build API, and the application lifecycle does not replace its
historical outputs byte-for-byte.

The old pages referred to retired monoliths such as `cli.py`, `storage.py`,
`source_catalog_artifact.py`, and `document_release_verify.py`. The new guides
point to the current responsibility owners. Later changes must update those
guides and their links directly; recreating the old wiki tree is unnecessary.
