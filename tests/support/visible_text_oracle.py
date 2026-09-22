"""Visible-text extraction: markup in, one searchable Unicode representation out.

`docs/decisions/0001-document-release-2-0.md` requires a `document-body`'s
`representation` to be `text/plain; charset=utf-8` -- "Markup is not search text:
this is visible text extracted before segmentation" (`documents.schema.json`,
`$defs/representation`). The extractors in `processing/extraction.py` are
source-native passthroughs whose representation blob IS the captured file, which
is the wrong answer for a search corpus; this module is the missing half and does
not replace them -- an XML file keeps its source-native representation and gains
a visible-text one.

XML goes through `xml.parsers.expat`, whose `CurrentByteIndex` gives every run of
character data the exact captured byte range it came from; HTML goes through
`html.parser.HTMLParser`, whose `getpos()` plus a byte-offset table gives the
same fact with character references folded. A **block** is any element that
directly owns non-whitespace character data, found top-down, and the one declared
vocabulary -- which tags are headings -- lives in the extractor's configuration
so it rides inside `extractorDigest`. XML is laid out **normalized** (whitespace
collapsed, `#` headings) because that is the input `processing/bounded_segmentation.py`
expects; HTML is **verbatim**, because the pinned corpus's `<pre>` paragraphs are
already blank-line separated. Every byte written that did not come from the
source belongs to no run, so `rendition_range` never invents a coordinate.
"""

from __future__ import annotations

import re
import xml.parsers.expat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from docspec.domain.identity import identity_digest
from docspec.errors import IntegrityError
from docspec.processing.artifacts import decode_utf8, utf8_byte_offsets

XML_VISIBLE_TEXT_EXTRACTOR_ID = "docspec.xml-visible-text/v1"
HTML_VISIBLE_TEXT_EXTRACTOR_ID = "docspec.html-visible-text/v1"

REPRESENTATION_MEDIA_TYPE = "text/plain; charset=utf-8"
BLOCK_SEPARATOR = "\n\n"
MAX_HEADING_LEVEL = 6

# Refusal reason codes, in the release's machine-legible spelling.
UNPARSEABLE = "extraction.unparseable-source"
NO_VISIBLE_TEXT = "extraction.no-visible-text"
MEDIA_TYPE_CONFLICT = "extraction.media-type-conflict"

# The heading vocabulary of the Federal Register XML this corpus carries, and
# the only tag list in this module. `SUBJECT` is the document's own subject line
# and sits above the preamble labels; `HD` carries its level in `SOURCE`, where
# `HED` labels a preamble section and `HD1`..`HD3` nest beneath it. Declared
# here so it rides inside `extractorDigest` and cannot drift under an unchanged
# extractor id.
XML_HEADING_LEVELS: Mapping[str, int] = {
    "SUBJECT": 1,
    "HD:HED": 2,
    "HD:HD1": 3,
    "HD:HD2": 4,
    "HD:HD3": 5,
    "HD": 3,
}

HTML_HEADING_TAGS: Mapping[str, int] = {f"h{level}": level for level in range(1, MAX_HEADING_LEVEL + 1)}
# Elements whose character data a reader never sees. `head` goes with them: a
# `<title>` is document metadata that the body does not render, and admitting it
# would put the same sentence in the corpus twice.
HTML_SUPPRESSED_TAGS = frozenset({"script", "style", "template", "noscript", "head"})
# Elements HTML closes implicitly; tracked so a stray unclosed tag cannot swallow
# the rest of a document into one block.
HTML_VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)

_WHITESPACE = re.compile(r"\s+")
_ATX_HEADING = re.compile(r"^(#{1,6})[ \t]+(\S.*)$")


class VisibleTextError(IntegrityError):
    """A captured rendition cannot produce a visible-text representation."""

    def __init__(self, reason_code: str, reason: str) -> None:
        super().__init__(reason)
        self.reason_code = reason_code
        self.reason = reason


@dataclass(frozen=True, slots=True)
class TextRun:
    """One run of source character data, and where it landed in the representation.

    A run is **exact** when it occupies the same number of bytes in both, which
    means the parser copied it through without collapsing whitespace and without
    folding a character reference. Inside an exact run a representation offset
    resolves to one captured byte; inside any other run it resolves only to the
    run, and the honest answer is the whole run rather than an interpolation the
    bytes do not support.
    """

    representation_start: int
    representation_end: int
    rendition_start: int
    rendition_end: int

    @property
    def exact(self) -> bool:
        return (self.representation_end - self.representation_start) == (
            self.rendition_end - self.rendition_start
        )

    def resolve(self, start: int, end: int) -> tuple[int, int]:
        """The captured range one interval of this run came from."""

        if not self.exact:
            return self.rendition_start, self.rendition_end
        low = self.rendition_start + max(0, start - self.representation_start)
        high = self.rendition_start + min(self.representation_end, end) - self.representation_start
        return low, max(high, low + 1)


@dataclass(frozen=True, slots=True)
class VisibleTextBlock:
    """One text-owning element, as it was laid out in the representation."""

    kind: str
    level: int | None
    representation_start: int
    representation_end: int


@dataclass(frozen=True, slots=True)
class VisibleText:
    """One extracted representation: its bytes, its layout, and its source map."""

    content: bytes
    blocks: tuple[VisibleTextBlock, ...]
    runs: tuple[TextRun, ...]
    rendition_byte_size: int
    extractor_id: str
    configuration_digest: str
    metadata: Mapping[str, Any]

    def rendition_range(self, start: int, end: int) -> tuple[int, int]:
        """The captured byte range one representation interval was extracted from.

        The union of every run the interval touches, resolved inside each run as
        far as that run's bytes allow, so a segment cites the smallest span of
        captured bytes that certainly contains its text. A boundary that falls
        in injected bytes -- a separator, a `#` prefix -- borrows the
        neighbouring run rather than inventing a coordinate.
        """

        if end <= start:
            raise ValueError("a rendition range requires end > start")
        resolved = [
            run.resolve(start, end)
            for run in self.runs
            if run.representation_start < end and start < run.representation_end
        ]
        if not resolved:
            raise VisibleTextError(
                NO_VISIBLE_TEXT,
                f"representation range [{start}, {end}) came from no captured byte",
            )
        # Clamped to the captured file: an evidence coordinate that ran past the
        # bytes it names would be a coordinate nothing can check.
        return (
            max(0, min(low for low, _ in resolved)),
            min(self.rendition_byte_size, max(high for _, high in resolved)),
        )


class _Writer:
    """Accumulates the representation while recording where each run landed."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self.length = 0
        self.runs: list[TextRun] = []

    def write(self, text: str) -> tuple[int, int]:
        start = self.length
        self.length += len(text.encode("utf-8"))
        self._parts.append(text)
        return start, self.length

    def write_run(self, text: str, rendition_start: int, rendition_end: int) -> None:
        start, end = self.write(text)
        if end > start:
            self.runs.append(TextRun(start, end, rendition_start, max(rendition_end, rendition_start + 1)))

    def content(self) -> bytes:
        return "".join(self._parts).encode("utf-8")


@dataclass(frozen=True, slots=True)
class _Piece:
    """One run of character data as the parser delivered it."""

    text: str
    start: int
    end: int


@dataclass
class _Node:
    """One element, with its character data and children in document order."""

    tag: str
    key: str
    parts: list[Any]

    @property
    def owns_text(self) -> bool:
        return any(isinstance(part, _Piece) and part.text.strip() for part in self.parts)

    def pieces(self) -> list[_Piece]:
        collected: list[_Piece] = []
        stack: list[Any] = [self]
        while stack:
            current = stack.pop()
            if isinstance(current, _Piece):
                collected.append(current)
            else:
                stack.extend(reversed(current.parts))
        return collected


def _normalized_pieces(pieces: Sequence[_Piece]) -> list[_Piece | None]:
    """Collapse a block's whitespace, keeping one run per source piece.

    ``None`` marks an injected separating space: a byte the reader needs and no
    captured byte produced, so no run may claim it.
    """

    emitted: list[_Piece | None] = []
    pending_space = False
    started = False
    for piece in pieces:
        normalized = _WHITESPACE.sub(" ", piece.text)
        core = normalized.strip(" ")
        if not core:
            pending_space = pending_space or (started and bool(normalized))
            continue
        if started and (pending_space or normalized.startswith(" ")):
            emitted.append(None)
        emitted.append(_Piece(core, piece.start, piece.end))
        started = True
        pending_space = normalized.endswith(" ")
    return emitted


def _write_block(
    writer: _Writer,
    blocks: list[VisibleTextBlock],
    pieces: Sequence[_Piece],
    *,
    level: int | None,
    normalize: bool,
) -> None:
    """Lay one block out, separated from its predecessor by a blank line."""

    if blocks:
        writer.write(BLOCK_SEPARATOR)
    start = writer.length
    if level is not None or normalize:
        prepared = _normalized_pieces(pieces)
        if not any(item is not None for item in prepared):
            return
        if level is not None:
            writer.write("#" * min(level, MAX_HEADING_LEVEL) + " ")
        for item in prepared:
            if item is None:
                writer.write(" ")
            else:
                writer.write_run(item.text, item.start, item.end)
    else:
        if not any(piece.text.strip() for piece in pieces):
            return
        for piece in pieces:
            writer.write_run(piece.text, piece.start, piece.end)
    if writer.length == start:
        return
    kind = "heading" if level is not None else "paragraph"
    blocks.append(VisibleTextBlock(kind, level, start, writer.length))


def _lay_out(
    nodes: Iterable[tuple[_Node, int | None]],
    *,
    normalize: bool,
) -> tuple[bytes, tuple[VisibleTextBlock, ...], tuple[TextRun, ...]]:
    """Lay every block out in order, separated by blank lines, and return content, blocks and runs."""
    writer = _Writer()
    blocks: list[VisibleTextBlock] = []
    for node, level in nodes:
        _write_block(writer, blocks, node.pieces(), level=level, normalize=normalize)
    return writer.content(), tuple(blocks), tuple(writer.runs)


def _walk_blocks(root: _Node, heading_level: Any) -> list[tuple[_Node, int | None]]:
    """Every text-owning element, outermost first, with its heading level or None."""

    found: list[tuple[_Node, int | None]] = []
    stack: list[_Node] = [root]
    while stack:
        node = stack.pop()
        if node.owns_text:
            found.append((node, heading_level(node)))
            continue
        stack.extend(reversed([part for part in node.parts if isinstance(part, _Node)]))
    return found


class XmlVisibleTextExtractor:
    """Visible text and source-derived headings from one XML rendition."""

    extractor_id = XML_VISIBLE_TEXT_EXTRACTOR_ID

    def __init__(self, heading_levels: Mapping[str, int] = XML_HEADING_LEVELS) -> None:
        self.heading_levels = dict(heading_levels)
        self.configuration = {
            "headingLevels": dict(sorted(self.heading_levels.items())),
            "layout": "normalized",
            "parser": "expat",
            "unit": "visible-text",
        }
        self.configuration_digest = identity_digest(self.configuration)

    def extract(self, source_bytes: bytes) -> VisibleText:
        """Return the visible-text representation; a capture with no visible text is refused."""
        root = _parse_xml(source_bytes)
        blocks = _walk_blocks(root, self._level)
        content, laid_out, runs = _lay_out(blocks, normalize=True)
        if not content.strip():
            raise VisibleTextError(
                NO_VISIBLE_TEXT, "the captured XML carries no visible text to search"
            )
        return VisibleText(
            content=content,
            blocks=laid_out,
            runs=runs,
            rendition_byte_size=len(source_bytes),
            extractor_id=self.extractor_id,
            configuration_digest=self.configuration_digest,
            metadata={
                "rootTag": next(
                    (part.tag for part in root.parts if isinstance(part, _Node)), None
                ),
                "blockCount": len(laid_out),
                "headingCount": sum(1 for block in laid_out if block.kind == "heading"),
            },
        )

    def _level(self, node: _Node) -> int | None:
        return self.heading_levels.get(node.key, self.heading_levels.get(node.tag))


class HtmlVisibleTextExtractor:
    """Visible text from one HTML rendition, copied rather than reflowed."""

    extractor_id = HTML_VISIBLE_TEXT_EXTRACTOR_ID

    def __init__(self, heading_tags: Mapping[str, int] = HTML_HEADING_TAGS) -> None:
        self.heading_tags = dict(heading_tags)
        self.configuration = {
            "headingLevels": dict(sorted(self.heading_tags.items())),
            "layout": "verbatim",
            "parser": "html.parser",
            "suppressed": sorted(HTML_SUPPRESSED_TAGS),
            "unit": "visible-text",
        }
        self.configuration_digest = identity_digest(self.configuration)

    def extract(self, source_bytes: bytes) -> VisibleText:
        """Return the visible-text representation; a capture with no visible text is refused."""
        root, element_count = _parse_html(source_bytes)
        blocks = _walk_blocks(root, self._level)
        content, laid_out, runs = _lay_out(blocks, normalize=False)
        if not content.strip():
            raise VisibleTextError(
                NO_VISIBLE_TEXT, "the captured HTML carries no visible text to search"
            )
        return VisibleText(
            content=content,
            blocks=laid_out,
            runs=runs,
            rendition_byte_size=len(source_bytes),
            extractor_id=self.extractor_id,
            configuration_digest=self.configuration_digest,
            metadata={
                "elementCount": element_count,
                "blockCount": len(laid_out),
                "headingCount": sum(1 for block in laid_out if block.kind == "heading"),
            },
        )

    def _level(self, node: _Node) -> int | None:
        return self.heading_tags.get(node.tag)


def _parse_xml(source_bytes: bytes) -> _Node:
    """Build the element tree with exact captured-byte ranges for every text run."""

    root = _Node("#document", "#document", [])
    stack = [root]
    pending: list[_Piece] = []
    parser = xml.parsers.expat.ParserCreate()

    def close_pending() -> None:
        if pending:
            piece = pending.pop()
            stack[-1].parts.append(_Piece(piece.text, piece.start, parser.CurrentByteIndex))

    def start_element(name: str, attributes: dict[str, str]) -> None:
        close_pending()
        source = attributes.get("SOURCE")
        node = _Node(name, f"{name}:{source}" if source else name, [])
        stack[-1].parts.append(node)
        stack.append(node)

    def end_element(_name: str) -> None:
        close_pending()
        if len(stack) > 1:
            stack.pop()

    def characters(data: str) -> None:
        if pending:
            close_pending()
        pending.append(_Piece(data, parser.CurrentByteIndex, parser.CurrentByteIndex))

    def other(*_arguments: Any) -> None:
        close_pending()

    parser.StartElementHandler = start_element
    parser.EndElementHandler = end_element
    parser.CharacterDataHandler = characters
    parser.CommentHandler = other
    parser.ProcessingInstructionHandler = other
    try:
        parser.Parse(source_bytes, True)
    except xml.parsers.expat.ExpatError as error:
        raise VisibleTextError(UNPARSEABLE, f"captured XML cannot be parsed: {error}") from error
    if pending:
        piece = pending.pop()
        stack[-1].parts.append(_Piece(piece.text, piece.start, len(source_bytes)))
    return root


class _HtmlTreeBuilder(HTMLParser):
    """Collect visible character data with the captured byte range it came from."""

    def __init__(self, offsets: Sequence[int] | None) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document", "#document", [])
        self.element_count = 0
        self._offsets = offsets
        self._stack: list[_Node] = [self.root]
        self._suppressed = 0
        self._line_starts: list[int] = [0]
        self._pending: _Piece | None = None

    def prepare(self, text: str) -> None:
        # Split on "\n" alone: `HTMLParser` counts lines that way, and
        # `splitlines` would also break on a form feed or a line separator and
        # desync every position after it.
        for line in text.split("\n"):
            self._line_starts.append(self._line_starts[-1] + len(line) + 1)

    def _byte_position(self) -> int:
        line, column = self.getpos()
        index = self._line_starts[min(line - 1, len(self._line_starts) - 1)] + column
        if self._offsets is None:
            return index
        return self._offsets[min(index, len(self._offsets) - 1)]

    def _close_pending(self) -> None:
        if self._pending is not None:
            piece = self._pending
            self._pending = None
            self._stack[-1].parts.append(_Piece(piece.text, piece.start, self._byte_position()))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        self._close_pending()
        self.element_count += 1
        normalized = tag.casefold()
        if normalized in HTML_VOID_TAGS:
            return
        if normalized in HTML_SUPPRESSED_TAGS:
            self._suppressed += 1
            return
        node = _Node(normalized, normalized, [])
        if not self._suppressed:
            self._stack[-1].parts.append(node)
        self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        self._close_pending()
        self.element_count += 1

    def handle_endtag(self, tag: str) -> None:
        self._close_pending()
        normalized = tag.casefold()
        if normalized in HTML_VOID_TAGS:
            return
        if normalized in HTML_SUPPRESSED_TAGS:
            self._suppressed = max(0, self._suppressed - 1)
            return
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == normalized:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self._suppressed:
            self._close_pending()
            return
        self._close_pending()
        self._pending = _Piece(data, self._byte_position(), self._byte_position())

    def finish(self, source_size: int) -> None:
        if self._pending is not None:
            piece = self._pending
            self._pending = None
            self._stack[-1].parts.append(_Piece(piece.text, piece.start, source_size))


def _parse_html(source_bytes: bytes) -> tuple[_Node, int]:
    """Decode the capture, build the element tree and count elements, mapping positions to byte offsets."""
    text = decode_utf8(source_bytes, label="captured HTML")
    offsets = None if text.isascii() else utf8_byte_offsets(text)
    builder = _HtmlTreeBuilder(offsets)
    builder.prepare(text)
    try:
        builder.feed(text)
        builder.close()
    except (AssertionError, ValueError) as error:
        raise VisibleTextError(UNPARSEABLE, f"captured HTML cannot be parsed: {error}") from error
    builder.finish(len(source_bytes))
    return builder.root, builder.element_count


def is_atx_heading(line: str) -> bool:
    """Whether one representation line reads as a heading to the bounded segmenter."""

    return bool(_ATX_HEADING.match(line))


def heading_level_and_title(line: str) -> tuple[int, str]:
    """Split one ATX heading line into its level and its title."""

    match = _ATX_HEADING.match(line)
    if match is None:
        raise ValueError(f"{line!r} is not an ATX heading line")
    return len(match.group(1)), match.group(2).strip()


DEFAULT_VISIBLE_TEXT_EXTRACTORS: Mapping[str, Any] = {
    "application/xml": XmlVisibleTextExtractor(),
    "text/html": HtmlVisibleTextExtractor(),
}


__all__ = [
    "BLOCK_SEPARATOR",
    "DEFAULT_VISIBLE_TEXT_EXTRACTORS",
    "HTML_HEADING_TAGS",
    "HTML_SUPPRESSED_TAGS",
    "HTML_VISIBLE_TEXT_EXTRACTOR_ID",
    "MEDIA_TYPE_CONFLICT",
    "NO_VISIBLE_TEXT",
    "REPRESENTATION_MEDIA_TYPE",
    "UNPARSEABLE",
    "XML_HEADING_LEVELS",
    "XML_VISIBLE_TEXT_EXTRACTOR_ID",
    "HtmlVisibleTextExtractor",
    "TextRun",
    "VisibleText",
    "VisibleTextBlock",
    "VisibleTextError",
    "XmlVisibleTextExtractor",
    "heading_level_and_title",
    "is_atx_heading",
]
