# Shared source reading

DocSpec uses the pinned SpicyDocs core wheel to read JSON, XML, HTML and image headers.
The wheel is required; network acquisition and PDF decoding remain optional.
SpicyDocs returns source observations. DocSpec decides how those observations
become catalog metadata, searchable text, segments and verifiable evidence.

| Operation | SpicyDocs supplies | DocSpec keeps |
| --- | --- | --- |
| XML/HTML source representation | Ordered elements, attributes, text and source byte positions | Exact original file, metadata counts and representation identity |
| XML/HTML visible text | The same ordered observations | Heading vocabulary, whitespace, suppressed elements, blocks and source mappings |
| Image representation | PNG/GIF/JPEG header dimensions, when readable | Exact image bytes, metadata and whole-image evidence |
| JSON representation and records | Decoded values and exact top-level record positions | Exact original bytes, root counts, record segments and evidence |

Empty HTML retains an empty source representation with zero observations;
visible-text extraction refuses because there is no text to segment.

HTML source metadata includes head/title text; the visible-text profile excludes
it. XML text normalizes whitespace; HTML text preserves existing spacing. These
are explicit display policies, not changes to the source file.

The v2 text mappings exclude CDATA/comment/processing-instruction delimiters
from character spans. Only byte-identical runs support exact subrange mapping;
transformed text resolves to its full captured span. Literal runs do not depend
on parser feed sizes. Namespace-invalid XML and internal declarations/entities refuse. Inert external
DTDs, used by official bill XML, remain accepted without loading external files. Default bounds are 64 MiB of input, one million
events and depth 256; these are not total memory or runtime limits.

The public source-native and dataset-run paths still require UTF-8 bytes. The
native/lower-level HTML readers classify undecodable bytes as a source refusal;
code drift remains an integrity failure. XML
encoding declarations are honored when reading those original bytes. This fixes
the previous source-native path's use of decoded text, which could ignore a
conflicting declaration. Direct mapped XML reading can observe other encodings.

The v2 image reader requires the PNG IHDR framing and stops JPEG header scanning
at scan data or end-of-image. Dimensions are declarations, including zero values;
they establish neither successful decoding nor oriented display geometry.

The v2 JSON stages share one source-reading profile: finite binary floats,
64 MiB of input, one million JSON values and depth 256, with the root at depth
zero. Unknown fields count toward these bounds. Extraction reads values only;
segmentation reads each top-level array member once and uses its original byte
positions. Other roots form one record; an empty array forms none. Each stage
checks its own retained input without caching a second decoded dataset.

Source number spelling, Unicode escapes and whitespace remain in the original
bytes. Large integers and escaped surrogate code units remain accepted source
facts. Float rounding and underflow retain their previous behavior. Overflow
such as `1e999` now refuses instead of silently becoming infinity. Duplicate
keys, non-finite constants, trailing content and invalid UTF-8 still refuse as
`IntegrityError`; diagnostic wording comes from the shared reader. These source
rules are distinct from Rulespec's stricter canonical artifact rules.

Stage settings record the SpicyDocs version and hashes of the selected installed
reader files. Extraction and JSON segmentation refuse if those files change after configuration. The
shared XML scanner hash participates in markup identities. These hashes identify
installed source files; they do not attest to the entire loaded environment.

Frozen prior readers live only in test support. The shared-reader tests compare
retained fixtures and name deliberate mapping, malformed-header and refusal
changes. No legacy fallback parser remains in production.
