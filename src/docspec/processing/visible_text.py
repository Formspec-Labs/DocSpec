"""Lay shared markup observations out as searchable text with source byte ranges.

SpicyDocs reads XML/HTML syntax and locates source text. DocSpec chooses blocks,
headings and suppression: XML whitespace is normalized; HTML body text retains
its existing spacing. Inserted separators and heading prefixes claim no source
bytes. Only byte-identical runs support exact subrange interpolation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from docspec.domain.identity import identity_digest
from docspec.errors import IntegrityError
from docspec.processing.reader_identity import (
    MARKUP_MODULES,
    installed_reader_identity,
    reader_configuration,
    require_reader_identity,
)

XML_VISIBLE_TEXT_EXTRACTOR_ID = "docspec.xml-visible-text/v2"
HTML_VISIBLE_TEXT_EXTRACTOR_ID = "docspec.html-visible-text/v2"

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
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
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
    """Output text and its captured span; exact only when their bytes match."""

    representation_start: int
    representation_end: int
    rendition_start: int
    rendition_end: int
    exact: bool

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

    def __init__(self, source_bytes: bytes) -> None:
        self._source_bytes = source_bytes
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
            self.runs.append(
                TextRun(
                    start,
                    end,
                    rendition_start,
                    rendition_end,
                    text.encode("utf-8") == self._source_bytes[rendition_start:rendition_end],
                )
            )

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
    source_bytes: bytes,
) -> tuple[bytes, tuple[VisibleTextBlock, ...], tuple[TextRun, ...]]:
    writer = _Writer(source_bytes)
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
        self._reader_identity = installed_reader_identity(MARKUP_MODULES)
        self.configuration = {
            **reader_configuration(self._reader_identity),
            "headingLevels": dict(sorted(self.heading_levels.items())),
            "layout": "normalized",
            "parser": "expat",
            "allowExternalDoctype": True,
            "unit": "visible-text",
        }
        self.configuration_digest = identity_digest(self.configuration)

    def extract(self, source_bytes: bytes) -> VisibleText:
        require_reader_identity(self._reader_identity, MARKUP_MODULES)
        root = _parse_xml(source_bytes)
        blocks = _walk_blocks(root, self._level)
        content, laid_out, runs = _lay_out(blocks, normalize=True, source_bytes=source_bytes)
        if not content.strip():
            raise VisibleTextError(NO_VISIBLE_TEXT, "the captured XML carries no visible text to search")
        return VisibleText(
            content=content,
            blocks=laid_out,
            runs=runs,
            rendition_byte_size=len(source_bytes),
            extractor_id=self.extractor_id,
            configuration_digest=self.configuration_digest,
            metadata={
                "rootTag": next((part.tag for part in root.parts if isinstance(part, _Node)), None),
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
        self._reader_identity = installed_reader_identity(MARKUP_MODULES)
        self.configuration = {
            **reader_configuration(self._reader_identity),
            "headingLevels": dict(sorted(self.heading_tags.items())),
            "layout": "verbatim",
            "parser": "html.parser",
            "suppressed": sorted(HTML_SUPPRESSED_TAGS),
            "unit": "visible-text",
        }
        self.configuration_digest = identity_digest(self.configuration)

    def extract(self, source_bytes: bytes) -> VisibleText:
        require_reader_identity(self._reader_identity, MARKUP_MODULES)
        root, element_count = _parse_html(source_bytes)
        blocks = _walk_blocks(root, self._level)
        content, laid_out, runs = _lay_out(blocks, normalize=False, source_bytes=source_bytes)
        if not content.strip():
            raise VisibleTextError(NO_VISIBLE_TEXT, "the captured HTML carries no visible text to search")
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
    from spicy_docs.reading.markup import read_xml_events

    try:
        observed = read_xml_events(source_bytes, allow_external_doctype=True)
    except ValueError as error:
        raise VisibleTextError(UNPARSEABLE, f"captured XML cannot be parsed: {error}") from error
    root = _Node("#document", "#document", [])
    stack = [root]
    for event in observed.events:
        if event.kind in {"start", "empty"}:
            source = dict(event.attributes).get("SOURCE")
            node = _Node(event.name, f"{event.name}:{source}" if source else event.name, [])
            stack[-1].parts.append(node)
            if event.kind == "start":
                stack.append(node)
        elif event.kind == "end":
            stack.pop()
        elif event.kind == "text":
            stack[-1].parts.append(_Piece(event.text, event.byte_start, event.byte_end))
    return root


def _parse_html(source_bytes: bytes) -> tuple[_Node, int]:
    from spicy_docs.reading.markup import read_html_events

    try:
        observed = read_html_events(source_bytes)
    except ValueError as error:
        raise VisibleTextError(UNPARSEABLE, f"captured HTML cannot be parsed: {error}") from error
    root = _Node("#document", "#document", [])
    stack = [root]
    positions: dict[str, list[int]] = {}
    suppressed = 0
    for event in observed.events:
        if event.kind == "start":
            if event.name in HTML_VOID_TAGS:
                continue
            if event.name in HTML_SUPPRESSED_TAGS:
                suppressed += 1
                continue
            node = _Node(event.name, event.name, [])
            if not suppressed:
                stack[-1].parts.append(node)
            positions.setdefault(event.name, []).append(len(stack))
            stack.append(node)
        elif event.kind == "end":
            if event.name in HTML_VOID_TAGS:
                continue
            if event.name in HTML_SUPPRESSED_TAGS:
                suppressed = max(0, suppressed - 1)
                continue
            matches = positions.get(event.name)
            if matches:
                index = matches[-1]
                while len(stack) > index:
                    removed = stack.pop()
                    positions[removed.tag].pop()
        elif event.kind == "text" and not suppressed:
            stack[-1].parts.append(_Piece(event.text, event.byte_start, event.byte_end))
    return root, observed.element_count


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
