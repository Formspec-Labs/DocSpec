# Catalog evidence and document processing

Use the [architecture guide](architecture.md) for the complete flow and
[CONTRIBUTING](../CONTRIBUTING.md) for setup. This guide explains where source
meaning, admission checks, and evidence belong when changing that flow.

## Keep selection decisions in the catalog

The source adapter supplies an immutable source-native release. A pinned policy
defines its document universe and interprets its rows. Supporting docket or
Federal Register lookups enrich that universe; they do not silently add documents
to it. The builder retains selected, excluded, deleted, unavailable, and failed
outcomes, with reasons and counts that can be reconciled against the source.

[`SourceCatalogItem`](../src/docspec/domain/source_catalog.py) is the catalog's
full evidence row. Its `to_processing_item()` method produces the smaller
`SourceItem` used for processing. Keep candidate selection, source paths, and
interpretations in the full row even when downstream processing needs only the
selected file. A failed interpretation must remain an accountable outcome.

Source policies own joins, normalization, selection, and provenance. In
particular, a field's source path identifies the observation used for that
field; a general item-level provenance label cannot replace it. Preserve raw
observations when interpreting malformed or ambiguous values. Changing selection
meaning or output identity requires reviewing the policy and schema pins, not
just changing a serializer.

The [catalog builder](../src/docspec/adapters/catalog_artifact/builder.py) uses
bounded staging and deterministic row ordering. Keep its one-pass source reads,
SQLite workspace, and digest framing intact when changing policy execution.
See [policy admission tests](../tests/test_source_catalog_policy.py),
[row tests](../tests/test_source_catalog_rows.py), and
[serial/worker equivalence tests](../tests/test_source_catalog_workers.py).

## Choose the verification depth deliberately

The [catalog reader](../src/docspec/adapters/catalog_artifact/reader.py) exposes
two operations. `open_snapshot()` admits pinned artifacts and their receipts,
then supplies rows with checks during iteration. `verify_snapshot()` also
rederives the complete sealed row evidence. It memoizes successful verification
for the exact catalog identity and digest within that reader instance.

A constructed reference proves that its fields have valid shapes. It does not
prove that the referenced bytes exist or satisfy their declared meaning. Use
the appropriate reader and full gate before relying on a catalog for publication.
The descriptor-pinned [catalog store](../src/docspec/adapters/source_catalog_store/)
owns directory identity checks, safe staging, and pointer advancement. Its
filesystem checks are part of the behavior; replacing them with generic path
helpers would weaken that behavior.

## Create a policy member through the CLI

`docspec source-catalog write-policy` builds a policy through its application
owner, checks its canonical round trip, and exclusively creates the output.
The parent output directory must exist. Existing files and symlinks are refused.
The command also prints the written member as JSON.

```sh
uv run --frozen --extra dagster docspec source-catalog write-policy \
  --policy regulations-gov --input fields.json --output member.json
```

For `regulations-gov`, the input object requires `document_input` and
`agency_names`. Each input selector has exactly `sourceSystemId`,
`sourceSystemVersion`, `scopeId`, `schemaName`, and `schemaVersion`, using the
identities in the source release. `agency_names` is either an object mapping
agency IDs to names or a JSON file path resolved relative to `fields.json`.
Optional fields are `docket_input`, `federal_register_input`, `comment_input`,
`sample`, `max_selected_items`, `language`, and `source_url_template`. Omitted
fields use the policy defaults. Selectors and samples use the policy owners'
closed shapes; see [the selector](../src/docspec/ports/source_catalog.py) and
[sample policy](../src/docspec/application/regulations_gov_catalog/sampling.py).

For `federal-register`, use `--policy federal-register` and supply
`expected_source_system_id` in the input object. These fields configure a policy;
the resulting member contains its sealed identity and settings. Use the
[CLI owner](../src/docspec/cli/catalog_policy.py) and
[command tests](../tests/test_catalog_policy_cli.py) when changing this interface.
Catalog build and verification commands live in
[`cli/source_catalog.py`](../src/docspec/cli/source_catalog.py).

## Preserve evidence across acquisition, extraction, and segmentation

A [content fetcher](../src/docspec/ports/content_fetcher.py) supplies a stream
and metadata. Execution writes the captured blob, checks declared byte count and
digest when present, and records capture evidence. The
[fetcher adapters](../src/docspec/adapters/content_fetchers/) separately own
local files, HTTPS, S3, and routing. Keep provider errors, stream cleanup, and
capture verification at their respective owners.

Extraction turns captured bytes into a representation and coordinates that
trace back to those bytes. See [representation choices](representations.md) for
supported defaults, visible-text experiments, PDF/image limits, and coordinate
inspection. There are two compositions in this repository:

| Composition | Extraction and segmentation |
| --- | --- |
| Application execution | `DefaultExtractorRegistry` selects text, HTML, XML, JSON, image, or lazy PDF extraction. An experiment can instead choose `VisibleTextExtractor` with `VisibleTextBlockSegmenter`; the executor uses the pinned implementations. |
| Historical portable mint recipe | `tools/build_document_release.py` selects visible-text extraction, retention-floor checks, and bounded text segmentation for its recorded campaign workflow. |

The portable recipe is a repository tool, not an installed generic build API.
Application release output does not replace historical portable mints
byte-for-byte; see the separate release representations in the architecture guide.

The default registry does not register `HtmlVisibleTextExtractor` or
`XmlVisibleTextExtractor`. Adding those to general execution requires explicit
adaptation to `ExtractionResult` and its evidence model. An import or registry
entry alone does not establish equivalent extraction.

Text segments use half-open UTF-8 byte ranges: the start is included and the end
is excluded. Preserve exact coordinate round trips for non-ASCII text, tokenizer
identity, segment limits, and coverage or exclusion evidence. The
[extraction implementations](../src/docspec/processing/extraction.py),
[visible-text extraction](../src/docspec/processing/visible_text.py), and
[bounded segmenter](../src/docspec/processing/bounded_segmentation.py) own these
rules. Check changes with [pipeline tests](../tests/test_processing_pipeline.py),
[visible-text tests](../tests/test_visible_text.py), and
[segmentation tests](../tests/test_bounded_segmentation.py).
