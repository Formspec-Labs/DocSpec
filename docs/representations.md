# Choose a document representation

DocSpec keeps a capture of the exact fetched bytes. Extraction adds a
representation for later work; segmentation identifies pieces of that
representation. A different extraction choice can reuse the capture without
changing what was downloaded.

Choose the form your next step needs. Keeping HTML or XML is useful for
inspecting source markup and attributes. Visible text is useful when a reader or
processor needs the document's words. PDF text and images have different source
coordinates, so their evidence should not be treated as character offsets into
an original file.

## Supported default routes

`DocumentPipeline` defaults to `DefaultExtractorRegistry` and
`DefaultSegmenterRegistry`, with no processors. The registries choose by media
type and representation kind. These are the current routes:

| Captured input | Retained representation | Default segments | Source coordinates |
| --- | --- | --- | --- |
| UTF-8 text, including other `text/*` types | Exact source bytes, kind `text` | Blank-line paragraphs | Half-open byte ranges in the captured file |
| HTML | Exact UTF-8 markup, kind `html` | Blank-line slices of the markup | Captured-file byte ranges |
| XML, including `+xml` types such as SVG | Exact UTF-8 markup, kind `xml` | Blank-line slices of the markup | Captured-file byte ranges |
| JSON, including `+json` types | Exact UTF-8 source, kind `json` | Top-level array records, or one record for another JSON root | Captured-file byte ranges |
| PDF with the optional PDF dependency | One UTF-8 text representation containing page outputs | One segment per page, including empty pages | Captured PDF digest and page number |
| Other `image/*` types | Exact image bytes, kind `image` | One whole-image segment | Captured-file byte range and a whole-image region |

The HTML extractor parses source markup and records an observed text count. It
does not turn the representation into visible text. Neither the HTML nor XML
paragraph segmenter understands document sections, removes tags, or renders a
page. XML parsing checks well-formedness; this is not validation against the
publisher's XML schema. Unsupported media types are refused.

Source captures and representations are logically separate records even when
they refer to identical bytes. Content-addressed storage can share those bytes.
Use `pipeline.run(..., extract=False, segment=False)` for capture only, or
`segment=False` to inspect extraction before choosing segments.
See [the Python lifecycle](python-runs.md).

## Pin the same choices that execute

For a known HTML input, make markup retention explicit:

```python
from docspec.processing import HtmlExtractor, ParagraphSegmenter

extractor = HtmlExtractor()
segmenter = ParagraphSegmenter()
pipeline = workspace.documents(fetcher=fetcher, extractor=extractor, segmenter=segmenter)
```

The selected objects become pinned Core operation definitions. Retained
representations and segments record the actual implementation that produced
them. For mixed-format inputs, use the registries rather than one single-format
implementation. A later run checks compatible retained stages for reuse.

## Visible text and source markup

Use `VisibleTextExtractor` with `VisibleTextBlockSegmenter` for an HTML/XML
experiment that needs words rather than source markup:

```python
from docspec.processing.visible_text_runtime import (
    VisibleTextBlockSegmenter,
    VisibleTextExtractor,
)

extractor = VisibleTextExtractor()
segmenter = VisibleTextBlockSegmenter()
pipeline = workspace.documents(fetcher=fetcher, extractor=extractor, segmenter=segmenter)
```

The extractor chooses the existing HTML or XML parser from the
captured media type and requires UTF-8 input. It retains a `visible-text`
representation and one source mapping per complete text block. The original HTML/XML capture remains
available independently.

The runtime adapter reuses `HtmlVisibleTextExtractor` and
`XmlVisibleTextExtractor` from
[`processing.visible_text`](../src/docspec/processing/visible_text.py):

- HTML copies character data and suppresses `head`, `script`, `style`, `template`
  and `noscript`. It is not a browser: it does not execute JavaScript, apply CSS,
  or determine visual visibility from layout.
- XML normalizes whitespace within text blocks. Its default heading vocabulary
  is the declared Federal Register mapping; another XML vocabulary needs
  explicit heading settings.
- Entity decoding, normalized spaces and inserted separators mean visible text
  is not a byte-for-byte source slice. A source range may contain markup or the
  complete entity that produced a shorter character in the representation.

Each segment covers one complete declared text block. Its source range encloses
the captured character-data runs that produced that block; it may also contain
intervening markup. This is a derived mapping, not a claim that the segment is
an exact slice of the captured bytes. Inserted block separators are not emitted
as segments. A large HTML `<pre>` block remains one segment, even when it
contains blank lines. Store limits still apply, but this choice does not make
each segment fit a token window.

The extractor snapshots heading settings when constructed, and its plan pin
includes those settings and the mapping rule. Configure XML headings for the
input vocabulary, for example `VisibleTextExtractor(xml_heading_levels={
"TITLE": 1, "SECTION": 2})`. Changing a heading map requires a new plan.

The underlying parser classes remain useful for direct coordinate inspection.
Their `extract(bytes) -> VisibleText` interface is separate from the runtime
adapter. For example:

```python
from docspec.processing.visible_text import HtmlVisibleTextExtractor

source = b"<p>A &amp; B</p>"
visible = HtmlVisibleTextExtractor().extract(source)
start = visible.content.index(b"&")
source_start, source_end = visible.rendition_range(start, start + 1)
assert b"&amp;" in source[source_start:source_end]
```

`VisibleTextBlockSegmenter` accepts only the adapter's named visible-text block
mappings. The ordinary paragraph and bounded segmenters cannot subdivide these
derived mappings. That restriction preserves honest source coordinates when
entities, normalized whitespace or inserted headings change byte positions.

Run [the representation example](../examples/representation_choices.py) to
build, retain and inspect the same synthetic input using either choice:

```sh
uv run --frozen --extra dagster python -m examples.representation_choices --representation visible-text --output /tmp/docspec-visible-text
uv run --frozen --extra dagster python -m examples.representation_choices --representation markup --output /tmp/docspec-markup
```

Each output directory must be new. The example uses local fixture bytes, a
one-document work limit, and the public Python lifecycle and inspection APIs.

## PDF limits

`LazyPypdfExtractor` reads embedded PDF text through `pypdf`. The `pdf` extra
supplies that dependency; the normal text and markup routes do not require it.
Configuration pins the installed `pypdf` version, page separator and whitespace
choice before execution. A missing dependency or a loaded version that differs
from the pin is refused.

```python
from docspec.processing import LazyPypdfExtractor, PageSegmenter

extractor = LazyPypdfExtractor(
    page_separator="\n\f\n",
    strip_page_whitespace=False,
)
segmenter = PageSegmenter()
```

This extracts embedded text, not optical character recognition (OCR). Empty
pages retain their page identity and generate warnings. Scanned pages can
therefore yield no useful text. Encrypted PDFs are refused; no password or
decryption profile is supplied by the default route. Text order comes from the
parser, not a verified reconstruction of reading order, tables or columns.

Page evidence identifies the original PDF and whole page. It does not claim
character positions or bounding boxes in the original PDF bytes. The default
segmenter preserves that page boundary. Arbitrary sub-page slicing cannot use
this evidence mapping, and the bounded text segmenter refuses it.

The extractor materializes PDF input and page text in the worker. Store limits
bound admitted work and recorded observations; they do not provide a hard
sandbox around the PDF parser's transient memory or CPU use. Use an execution
environment that supplies those controls when the input requires them.

## Images

`ImageExtractor` preserves image bytes and reports dimensions when recognizable
PNG, GIF or JPEG headers provide them. It does not fully decode or validate an
image. Another `image/*` input may have unknown format or dimensions. The
default registry treats SVG (`image/svg+xml`) as XML markup because the `+xml`
route takes precedence.

A whole-image segment is available to an explicitly chosen processor that
accepts that media type. There is no default OCR, image captioning, frame
extraction, object detection or region segmentation. Pixel dimensions, when
known, describe the whole-image region; they do not identify text locations.

## Bound text segments when a downstream processor needs it

Default paragraphs, pages, JSON records and images retain their natural size.
A store byte limit does not make each segment fit a model's token window.
For source-mapped text, HTML or XML, configure the existing bounded segmenter
with the actual tokenizer:

```python
from docspec.adapters.token_counters import TiktokenCounter
from docspec.processing import (
    BoundedSegmenter,
    BoundedSegmentSettings,
    DefaultSegmenterRegistry,
)

counter = TiktokenCounter("o200k_base")  # Requires the tokens extra.
settings = BoundedSegmentSettings.for_counter(
    counter, max_tokens=1800, min_tokens=720, overlap_tokens=80,
)
segmenter = DefaultSegmenterRegistry(
    bounded=BoundedSegmenter(counter, settings=settings),
)
```

The policy pins tokenizer name/version and boundary settings. It splits
oversized regions, limits overlap, and refuses impossible budgets instead of
truncating text. Heading context and exclusion/coverage details are available
through its lower-level `segment_bounded` result. Ordinary runtime execution
retains segment records and the selected-policy receipt; it does not persist
that richer result as another coverage report.

Applying this to HTML/XML markup bounds the markup bytes. It does not remove
tags. The current bounded runtime segmenter requires identity-mapped source
text and does not accept derived PDF page mappings or visible-text mappings.

## Inspect bytes and coordinates

Use the same [inspection view](inspection.md) for a run or retained result:

```python
from docspec.domain.references import BlobRef

for row in view.records("representations", source_item_id=source_item_id):
    representation = row["payload"]
    print(representation["kind"], representation["extractorId"])
    print(representation["evidenceMappings"])
    content = b"".join(view.read_blob(
        BlobRef.from_dict(representation["blob"]), max_bytes=1024 * 1024,
    ))

for row in view.records("segments", source_item_id=source_item_id):
    segment = row["payload"]
    print(segment["representationStart"], segment["representationEnd"])
    print(segment["evidence"])
```

Byte ranges include the start and exclude the end. They count UTF-8 bytes, not
Python characters. Inspect the coordinate system before interpreting a range:
source bytes, PDF pages and image regions answer different questions.

Representation identity and coordinates make a result traceable. They do not
establish that an extraction contains every meaningful sentence or that a
processor understood it. Compare the captured source, chosen representation
and resulting segments on examples that matter to the experiment.
