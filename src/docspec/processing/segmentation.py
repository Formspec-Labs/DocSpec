"""Evidence-preserving deterministic segmenters for common representations."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from docspec.domain.identity import identity_digest, require_text
from docspec.errors import IntegrityError
from docspec.processing.artifacts import (
    PDF_PAGE_TEXT_TRANSFORM,
    RepresentationPayload,
    SegmentPayload,
    build_segment,
    decode_utf8,
    utf8_byte_offsets,
)
from docspec.processing.bounded_segmentation import BOUNDED_TEXT_KINDS, BoundedSegmenter
from docspec.processing.json_tools import record_char_ranges

PARAGRAPH_SEGMENTER_ID = "docspec.paragraph/v1"
PAGE_SEGMENTER_ID = "docspec.pdf-page/v1"
RECORD_SEGMENTER_ID = "docspec.json-record/v1"
WHOLE_IMAGE_SEGMENTER_ID = "docspec.whole-image/v1"
DEFAULT_SEGMENTER_REGISTRY_ID = "docspec.default-segmenters/v1"
SEGMENTATION_RECEIPT_FORMAT = "docspec-segmentation-receipt"
SEGMENTATION_RECEIPT_FORMAT_VERSION = "1.0"

_PARAGRAPH_GAP = re.compile(r"(?:\r?\n)[ \t]*(?:\r?\n)")


@dataclass(frozen=True, slots=True)
class SegmentationReceipt:
    """Recomputable evidence for one segmenter invocation."""

    representation_id: str
    segmenter_id: str
    segment_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        require_text(self.representation_id, "segmentation receipt representation_id")
        require_text(self.segmenter_id, "segmentation receipt segmenter_id")
        if not isinstance(self.segment_ids, tuple):
            raise ValueError("segmentation receipt segment_ids must be an immutable tuple")
        for segment_id in self.segment_ids:
            require_text(segment_id, "segmentation receipt segment_id")
        if len(set(self.segment_ids)) != len(self.segment_ids):
            raise ValueError("segmentation receipt segment identities must be distinct")

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": SEGMENTATION_RECEIPT_FORMAT,
            "formatVersion": SEGMENTATION_RECEIPT_FORMAT_VERSION,
            "representationId": self.representation_id,
            "segmenterId": self.segmenter_id,
            "segments": list(self.segment_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SegmentationReceipt:
        expected = {"format", "formatVersion", "representationId", "segmenterId", "segments"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("segmentation receipt has an invalid closed shape")
        if value["format"] != SEGMENTATION_RECEIPT_FORMAT:
            raise ValueError("segmentation receipt format is not supported")
        if value["formatVersion"] != SEGMENTATION_RECEIPT_FORMAT_VERSION:
            raise ValueError("segmentation receipt format version is not supported")
        segments = value["segments"]
        if not isinstance(segments, (list, tuple)):
            raise ValueError("segmentation receipt segments must be an array")
        return cls(
            representation_id=value["representationId"],
            segmenter_id=value["segmenterId"],
            segment_ids=tuple(segments),
        )

    @property
    def receipt_digest(self) -> str:
        return identity_digest(self.to_dict())


class ParagraphSegmenter:
    """Split UTF-8 source-native text at blank lines with exact byte offsets."""

    segmenter_id = PARAGRAPH_SEGMENTER_ID
    policy_digest = identity_digest(
        {
            "policy": "blank-line-paragraphs",
            "version": 1,
            "coordinates": "utf8-byte-range",
            "trimOuterWhitespace": True,
        }
    )

    def segment(self, representation: RepresentationPayload) -> tuple[SegmentPayload, ...]:
        text = decode_utf8(representation.content, label="paragraph representation")
        byte_offsets = utf8_byte_offsets(text)
        spans = _paragraph_char_ranges(text)
        return tuple(
            build_segment(
                representation,
                ordinal=ordinal,
                kind="paragraph",
                start=byte_offsets[start],
                end=byte_offsets[end],
                segmenter_id=self.segmenter_id,
                policy_digest=self.policy_digest,
                derivation=("source-native-text", "blank-line-paragraph"),
            )
            for ordinal, (start, end) in enumerate(spans)
        )


class PageSegmenter:
    """Create one stable segment for every declared PDF page boundary."""

    segmenter_id = PAGE_SEGMENTER_ID
    policy_digest = identity_digest(
        {
            "policy": "declared-pdf-pages",
            "version": 1,
            "includeEmptyPages": True,
        }
    )

    def segment(self, representation: RepresentationPayload) -> tuple[SegmentPayload, ...]:
        if representation.representation.kind != "pdf-text":
            raise IntegrityError("page segmentation requires a pdf-text representation")
        pages: list[SegmentPayload] = []
        for ordinal, mapping in enumerate(representation.representation.evidence_mappings):
            if mapping.transformation != PDF_PAGE_TEXT_TRANSFORM or mapping.evidence.page is None:
                raise IntegrityError("PDF text representation has a non-page evidence link")
            pages.append(
                build_segment(
                    representation,
                    ordinal=ordinal,
                    kind="page",
                    start=mapping.representation_start,
                    end=mapping.representation_end,
                    segmenter_id=self.segmenter_id,
                    policy_digest=self.policy_digest,
                    derivation=("pypdf-embedded-text", f"page:{mapping.evidence.page}"),
                )
            )
        return tuple(pages)


class RecordSegmenter:
    """Create exact source slices for top-level JSON array records."""

    segmenter_id = RECORD_SEGMENTER_ID
    policy_digest = identity_digest(
        {
            "policy": "top-level-json-records",
            "version": 1,
            "arrayMembers": "one-record-each",
            "otherRoots": "one-record",
        }
    )

    def segment(self, representation: RepresentationPayload) -> tuple[SegmentPayload, ...]:
        if representation.representation.kind != "json":
            raise IntegrityError("record segmentation requires a JSON representation")
        text = decode_utf8(representation.content, label="JSON representation")
        byte_offsets = utf8_byte_offsets(text)
        return tuple(
            build_segment(
                representation,
                ordinal=ordinal,
                kind="record",
                start=byte_offsets[start],
                end=byte_offsets[end],
                segmenter_id=self.segmenter_id,
                policy_digest=self.policy_digest,
                derivation=("source-native-json", "top-level-record"),
                media_type="application/json",
            )
            for ordinal, (start, end) in enumerate(record_char_ranges(text))
        )


class WholeImageSegmenter:
    """Treat one immutable raster file as one exact whole-image segment."""

    segmenter_id = WHOLE_IMAGE_SEGMENTER_ID
    policy_digest = identity_digest({"policy": "whole-image", "version": 1})

    def segment(self, representation: RepresentationPayload) -> tuple[SegmentPayload, ...]:
        if representation.representation.kind != "image":
            raise IntegrityError("whole-image segmentation requires an image representation")
        return (
            build_segment(
                representation,
                ordinal=0,
                kind="whole-image",
                start=0,
                end=len(representation.content),
                segmenter_id=self.segmenter_id,
                policy_digest=self.policy_digest,
                derivation=("source-image", "whole-image"),
            ),
        )


class DefaultSegmenterRegistry:
    """Select deterministic source-grounded segmentation from representation kind.

    Five segmenters are registered here. Four are exact and unbounded, and one
    is bounded: `BoundedSegmenter` needs an injected token counter, so it is
    supplied by the composition root rather than constructed here. When one is
    supplied it takes the text kinds it declares; when none is, the registry
    behaves exactly as it did before bounded segmentation existed.
    """

    segmenter_id = DEFAULT_SEGMENTER_REGISTRY_ID

    def __init__(self, *, bounded: BoundedSegmenter | None = None) -> None:
        self._paragraph = ParagraphSegmenter()
        self._page = PageSegmenter()
        self._record = RecordSegmenter()
        self._image = WholeImageSegmenter()
        self._bounded = bounded

    @property
    def registered_policy_digests(self) -> dict[str, str]:
        """Every registered segmenter identity beside its digested policy id."""

        registered = {
            self._paragraph.segmenter_id: self._paragraph.policy_digest,
            self._page.segmenter_id: self._page.policy_digest,
            self._record.segmenter_id: self._record.policy_digest,
            self._image.segmenter_id: self._image.policy_digest,
        }
        if self._bounded is not None:
            registered[self._bounded.segmenter_id] = self._bounded.policy_digest
        return registered

    def segment(self, representation: RepresentationPayload) -> tuple[SegmentPayload, ...]:
        kind = representation.representation.kind
        if self._bounded is not None and kind in BOUNDED_TEXT_KINDS:
            return self._bounded.segment(representation)
        if kind in {"text", "html", "xml"}:
            return self._paragraph.segment(representation)
        if kind == "pdf-text":
            return self._page.segment(representation)
        if kind == "json":
            return self._record.segment(representation)
        if kind == "image":
            return self._image.segment(representation)
        raise IntegrityError(f"no segmenter is registered for representation kind {kind!r}")


def _paragraph_char_ranges(text: str) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    start = 0
    for gap in _PARAGRAPH_GAP.finditer(text):
        trimmed = _trim_char_range(text, start, gap.start())
        if trimmed is not None:
            ranges.append(trimmed)
        start = gap.end()
    trimmed = _trim_char_range(text, start, len(text))
    if trimmed is not None:
        ranges.append(trimmed)
    return tuple(ranges)


def _trim_char_range(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None
