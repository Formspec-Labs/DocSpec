"""Use the existing HTML/XML visible-text parsers in an ordinary dataset run."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from docspec.domain.content import CapturedFile, EvidenceCoordinate, EvidenceMapping, Representation
from docspec.domain.identity import identity_digest, require_text
from docspec.errors import IntegrityError
from docspec.processing.artifacts import (
    DerivedEvidenceResolver,
    RepresentationPayload,
    SegmentPayload,
    build_segment,
    content_blob_ref,
)
from docspec.processing.extraction import ExtractionReceipt, ExtractionResult, _verify_captured_bytes
from docspec.processing.visible_text import (
    HTML_HEADING_TAGS,
    NO_VISIBLE_TEXT,
    REPRESENTATION_MEDIA_TYPE,
    UNPARSEABLE,
    XML_HEADING_LEVELS,
    HtmlVisibleTextExtractor,
    VisibleText,
    VisibleTextError,
    XmlVisibleTextExtractor,
)

VISIBLE_TEXT_BLOCK_TRANSFORM = "docspec-visible-text-block/v1"
_VISIBLE_TEXT_KIND = "visible-text"
_Parser = HtmlVisibleTextExtractor | XmlVisibleTextExtractor


def _headings(values: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    for name, level in values.items():
        require_text(name, "heading name")
        if type(level) is not int or not 1 <= level <= 6:
            raise ValueError("heading levels must be integers from 1 to 6")
    return tuple(sorted(values.items()))


def _parser_identity(parser: _Parser) -> tuple[str, str]:
    return parser.extractor_id.replace("/v1", "-blocks/v1"), identity_digest({
        "parserId": parser.extractor_id,
        "parserConfigurationDigest": parser.configuration_digest,
        "mapping": VISIBLE_TEXT_BLOCK_TRANSFORM,
        "inputEncoding": "utf-8",
    })


@dataclass(frozen=True, slots=True, init=False)
class VisibleTextExtractor:
    """Extract HTML/XML words with one enclosing source span per complete block.

    Heading maps are copied into immutable settings. Each call creates the
    existing parser from those settings, so later changes to caller dictionaries
    cannot silently change a pinned implementation. Other media types refuse.
    """

    _html_headings: tuple[tuple[str, int], ...]
    _xml_headings: tuple[tuple[str, int], ...]
    extractor_id = "docspec.visible-text/v1"

    def __init__(
        self,
        *,
        html_heading_tags: Mapping[str, int] | None = None,
        xml_heading_levels: Mapping[str, int] | None = None,
    ) -> None:
        object.__setattr__(self, "_html_headings", _headings(
            HTML_HEADING_TAGS if html_heading_tags is None else html_heading_tags,
        ))
        object.__setattr__(self, "_xml_headings", _headings(
            XML_HEADING_LEVELS if xml_heading_levels is None else xml_heading_levels,
        ))

    @property
    def configuration_digest(self) -> str:
        return identity_digest({
            "html": _parser_identity(HtmlVisibleTextExtractor(dict(self._html_headings))),
            "xml": _parser_identity(XmlVisibleTextExtractor(dict(self._xml_headings))),
        })

    def _parser(self, captured: CapturedFile) -> _Parser:
        media_type = captured.media_type.split(";", 1)[0].strip().lower()
        if media_type == "text/html":
            return HtmlVisibleTextExtractor(dict(self._html_headings))
        if media_type in {"application/xml", "text/xml"} or media_type.endswith("+xml"):
            return XmlVisibleTextExtractor(dict(self._xml_headings))
        raise ValueError("visible-text extraction requires captured HTML or XML")

    def selected_identity(self, captured_file: CapturedFile) -> tuple[str, str]:
        return _parser_identity(self._parser(captured_file))

    def _read(self, captured: CapturedFile, source_bytes: bytes) -> tuple[_Parser, VisibleText]:
        _verify_captured_bytes(captured, source_bytes)
        source_bytes.decode("utf-8")
        parser = self._parser(captured)
        try:
            visible = parser.extract(source_bytes)
        except VisibleTextError as error:
            # These are source-input refusals. A bad captured digest or an
            # impossible derived mapping remains an integrity failure.
            if error.reason_code in {UNPARSEABLE, NO_VISIBLE_TEXT}:
                raise ValueError(f"{error.reason_code}: {error.reason}") from error
            raise
        return parser, visible

    def extract(self, captured: CapturedFile, source_bytes: bytes) -> ExtractionResult:
        parser, visible = self._read(captured, source_bytes)
        extractor_id, configuration_digest = _parser_identity(parser)
        representation = Representation.create(
            source_item_id=captured.source_item_id,
            file_id=captured.file_id,
            file_digest=captured.blob.digest,
            kind=_VISIBLE_TEXT_KIND,
            blob=content_blob_ref(visible.content, REPRESENTATION_MEDIA_TYPE),
            extractor_id=extractor_id,
            configuration_digest=configuration_digest,
            evidence_mappings=_block_mappings(captured, visible),
        )
        return ExtractionResult(
            RepresentationPayload(representation, visible.content),
            ExtractionReceipt(
                extractor_id=extractor_id,
                configuration_digest=configuration_digest,
                file_id=captured.file_id,
                input_digest=captured.blob.digest,
                representation_id=representation.representation_id,
                output_digest=representation.blob.digest,
                output_byte_size=representation.blob.byte_size,
                kind=representation.kind,
                metadata={
                    **visible.metadata,
                    "parserId": parser.extractor_id,
                    "parserConfigurationDigest": parser.configuration_digest,
                    "mapping": VISIBLE_TEXT_BLOCK_TRANSFORM,
                },
            ),
        )

    def evidence_resolver(self, captured: CapturedFile, source_bytes: bytes) -> DerivedEvidenceResolver:
        """Reparse once for the shared representation/segment evidence verifier.

        Each requested mapping must exactly match a regenerated complete block,
        including its source span. Sub-block coordinates cannot be inferred.
        """
        _, visible = self._read(captured, source_bytes)
        blocks = {
            (mapping.representation_start, mapping.representation_end): mapping
            for mapping in _block_mappings(captured, visible)
        }

        def resolve(mapping: EvidenceMapping, actual_source: bytes) -> bytes:
            if actual_source != source_bytes:
                raise IntegrityError("visible-text resolver received different captured bytes")
            bounds = mapping.representation_start, mapping.representation_end
            if blocks.get(bounds) != mapping:
                raise IntegrityError("visible-text evidence differs from the regenerated block")
            return visible.content[bounds[0]:bounds[1]]

        return resolve


def _block_mappings(captured: CapturedFile, visible: VisibleText) -> tuple[EvidenceMapping, ...]:
    mappings = []
    for block in visible.blocks:
        start, end = visible.rendition_range(block.representation_start, block.representation_end)
        mappings.append(EvidenceMapping(
            representation_start=block.representation_start,
            representation_end=block.representation_end,
            evidence=EvidenceCoordinate(
                coordinate_system="utf8-byte-range",
                source_digest=captured.blob.digest,
                start=start,
                end=end,
            ),
            transformation=VISIBLE_TEXT_BLOCK_TRANSFORM,
        ))
    return tuple(mappings)


class VisibleTextBlockSegmenter:
    """Keep complete visible-text block boundaries and their enclosing source spans."""

    segmenter_id = "docspec.visible-text-block/v1"
    policy_digest = identity_digest({
        "policy": "declared-visible-text-blocks",
        "mapping": VISIBLE_TEXT_BLOCK_TRANSFORM,
        "includeBlockSeparators": False,
    })

    def selected_identity(self, representation: Representation) -> tuple[str, str]:
        if representation.kind != _VISIBLE_TEXT_KIND:
            raise IntegrityError("visible-text block segmentation requires a visible-text representation")
        return self.segmenter_id, self.policy_digest

    def segment(self, representation: RepresentationPayload) -> tuple[SegmentPayload, ...]:
        self.selected_identity(representation.representation)
        segments = []
        for ordinal, mapping in enumerate(representation.representation.evidence_mappings):
            evidence = mapping.evidence
            if (
                mapping.transformation != VISIBLE_TEXT_BLOCK_TRANSFORM
                or evidence.coordinate_system != "utf8-byte-range"
                or evidence.start is None
                or evidence.end is None
                or evidence.page is not None
                or evidence.region is not None
            ):
                raise IntegrityError("visible-text representation has a non-block evidence mapping")
            segments.append(build_segment(
                representation,
                ordinal=ordinal,
                kind="visible-text-block",
                start=mapping.representation_start,
                end=mapping.representation_end,
                segmenter_id=self.segmenter_id,
                policy_digest=self.policy_digest,
                derivation=(VISIBLE_TEXT_BLOCK_TRANSFORM,),
            ))
        return tuple(segments)
