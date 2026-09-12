# Inspect retained SpicyRegs comments before fetching documents

This example builds a DocSpec catalog from the community SpicyRegs comment
table, using SpicyDocs' existing source reader. It preserves the original table
fields, input pin and evidence, then filters the catalog by an exact docket ID.
Attachment links remain candidates; no attachment is fetched and no processing
run is created.

The checkout's development dependencies include the provider's optional
Parquet support for this fixture:

```sh
uv run --frozen python -m examples.spicyregs_comments --output /tmp/spicyregs-comments
```

Use a new absolute output directory. `--docket EPA-2026-0002` changes the exact,
case-sensitive metadata filter. `--invalid-row` demonstrates source refusal
when one row lacks its required comment identity. Both modes use three
synthetic rows in the provider's published column shape; neither makes a live
request or establishes current upstream availability.

Outside a development checkout, creating or replaying the Parquet fixture needs
the pinned provider wheel's `public-table` extra. DocSpec's core and the admitted
source reader do not require it.

| Input fact | What the catalog and report preserve |
| --- | --- |
| `See attached`, a Unicode title, and an attachment URL | Exact table text and one PDF candidate with declared size 123; its content digest remains unavailable. |
| Comment text and table-supplied `text_content`, without attachments | Exact fields remain readable. The item has no document candidate. The text is not promoted into a captured file, extracted representation or verified source span. |
| Null fields, an empty comment, malformed `attachments_json` | Nulls stay null, empty text stays empty, and the provider's field diagnostic accompanies the original malformed value. The row remains in the catalog. |
| A missing required `comment_id` in `--invalid-row` mode | The provider refuses the entire partition; no source release or catalog is published. The example keeps `input.parquet` and the refusal report. |

Read `spicyregs-comments.json` for the catalog reference, bounded public preview,
unavailable fields, attachment candidates and metadata matches. It separates
three decisions: provider record rejection, catalog availability for document
capture, and the caller's docket filter. In the valid fixture, there are zero
provider record rejections, one candidate-bearing catalog item and two items
unavailable for capture. Either unavailable item can still match a metadata
query. An invalid input produces a whole-source refusal, not an invented
one-row rejection ledger or partial catalog.

`source-result.json` is the unchanged provider publication/refusal report.
`source/` and `source-blobs/` retain the admitted source and its exact partition
evidence. `input.parquet` is the example's original synthetic input. Only
`dataset/sourceCatalog/` is used for the catalog; there are no document stores,
processing controls, captures, representations or segments.

The catalog's supplied metadata contains the original source description and
artifact pin, all 16 logical fields, each record's evidence reference, and the
provider's rendition rows with provider-declared source-field locations. The Hive agency
path supplies `agency_code`; the physical file has 15 columns. DocSpec uses a
fingerprint of the admitted record for its required version field, while the
publisher's `modify_date` remains unchanged, including null. An empty title is
normalized to unavailable; the original value stays in source facts.

This is a bounded caller mapping through `SuppliedRecordSource` and the existing
`SuppliedRecordCatalogPolicy`. Its own collection outcome is unreported; the
provider's original outcome remains explicitly nested in source metadata. It
does not turn a supplied-record snapshot into a new native source release.

The provider profile describes an `observed-crawl` with one observed traversal.
It starts at `part-0.parquet` for each requested agency and stops at the first
missing numbered part. Later parts are unrequested; missing-part responses are
not retained. It establishes neither all-agency coverage nor a single frozen
publisher-wide instant. These are community table observations, distinct from
origin Regulations.gov API records and Mirrulations object captures. A fresh
DocSpec catalog is a full chosen snapshot, not an instruction to append its rows
to an earlier catalog.

For a real bounded retained input, open `SpicyDocsSourceNativeAdapter` with the
public `SPICY_REGS_PUBLIC_COMMENT_PROFILE`, exact artifact pin and independently
accepted source verifier. Pass it and a `LocalWorkspace` to
[`build_comment_catalog`](../examples/spicyregs_comments.py), then use
`open_local_catalog`, `preview_local_catalog` or `inspect_comments`. The example
accepts at most ten records, forty candidates and 1 MiB of canonical supplied
metadata, with 8 MiB of catalog scratch. It accepts a nonempty source with no
record rejections. The existing [catalog APIs](catalog-inputs.md) support other
explicitly bounded policies; source acquisition and table publication remain
with their provider.

The [behavior tests](../tests/test_spicyregs_comments_example.py) compare exact
catalog fields with admitted provider records and retained Parquet bytes, check
nulls and diagnostics, preserve whole-input refusal, and prove repeated metadata
queries leave all saved files unchanged. The existing
[installed-provider gate](../tests/test_source_catalog_installed_wheel.py) runs
the same copied example and tests outside the checkout. It installs the Parquet
extra only after checking that the core reader needs no such dependency. These
checks qualify the bounded synthetic workflow, not live coverage or capacity.
