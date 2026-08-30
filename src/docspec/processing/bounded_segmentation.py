"""Bounded segmentation: a token budget that refuses, overlap, and a coverage sweep.

Reused with adoption under REF-048 §4.4 from the sibling regulations document
pipeline's `docpipeline/segments.py` at source commit
fc24e06ade915ead1209483733b1aec5cd824d1c, where it implements the
`structure-overlap-1800` policy the bounded fair comparison selected on
2026-07-24. Every boundary rule, budget rule, refusal, and coverage number
below is that module's; what changed is named under "What was adapted".

Why DocSpec needs it
--------------------
The four segmenters beside this one in `segmentation.py` are exact and
deterministic and have no size bound at all: a paragraph, a page, a record, or
an image is whatever the source made it. `docs/decisions/0001-document-release-2-0.md`
requires the opposite for `data/search-segments.jsonl` -- **bounded** segments,
a declared `maxSegmentBytes`, a `segmenterDigest` beside the `segmenterId`, and
the coverage identity `segmentedByteTotal + excludedByteTotal ==
representationByteTotal` per text body. That is this module.

The rules kept, and where each one lives
----------------------------------------
* **A region within budget stays whole.** Only a region that is itself
  oversized is split, into leaves of `BoundedSegmentSettings.leaf_budget`
  tokens (`_leaf_spans`). Each later leaf reaches backward at most
  `overlap_tokens` tokens and never past the start of its own region
  (`_overlap_start`) -- that is what "limited overlap only when one structural
  element is itself oversized" means.
* **A split leaf occupies its own segment.** Whole regions pack greedily
  against the hard budget (`_pack`). There is no same-field break.
* **The budget refuses rather than truncates.** A built segment over
  `max_tokens` raises `BoundedSegmentationError`; so does a budget that cannot
  hold one source character. This policy never drops text to fit.
* **The token counter is injected.** This module names no tokenizer package.
  A provider counter lives in `adapters/`, and settings that name a different
  tokenizer than the counter doing the counting are refused
  (`_require_matching_counter`).
* **Context is not evidence.** Heading text lives on `SegmentContext`. It is
  not in a segment's byte range, not in its token count, and not in its
  identity.
* **Coverage is recomputed, never remembered.** `_coverage` sweeps the emitted
  spans and the excluded ledger and reports covered, duplicated, excluded, and
  uncovered bytes, so a check cannot pass by trusting a number that no longer
  describes the segments beside it.

What was adapted
----------------
* **Byte coordinates, not character coordinates.** The source addresses Python
  codepoints. DocSpec addresses UTF-8 bytes, and ADR 0001 binds every span with
  `end > start` on a UTF-8 character boundary. The boundary machinery still
  runs in character space -- a token counter counts text, and `rfind("\\n\\n")`
  is a character operation -- and every emitted coordinate is converted once,
  at the edge, through `utf8_byte_offsets`, whose entries are character
  boundaries by construction. `_char_index` converts the other way and refuses
  a byte offset that is not one.
* **One contiguous range per segment, not a group of slices.** A DocSpec
  `Segment` is one exact half-open representation range. A packed group of
  adjacent whole regions therefore becomes the one range that spans them, so
  the separator bytes between them are inside the segment rather than lost, and
  the budget is measured on the bytes actually emitted rather than on a
  newline-join of the parts.
* **Heading regions are excluded, not carried.** The source keeps a heading
  region in its processing text for frozen-baseline parity and marks the slice
  context-only, recording the removal as a follow-up. With one contiguous range
  per segment there is no context-only slice to mark, so the follow-up is taken
  here: a heading is an `ExcludedRegion`, its bytes stay readable in the
  representation, and `SegmentContext.headings` carries it as context for the
  segments beneath it.
* **Regions come from the representation.** The source consumes a
  source-native region stream with real structure. DocSpec has no structural
  record yet -- ADR 0001 deviation row 7 adds `structuralNode`, and this port
  does not pre-empt it -- so regions here are blank-line blocks, and a heading
  is the one unambiguous form: an ATX line (`#` through `######`). When the
  structural record lands, `_regions` is the one function that changes.
* **Identity-mapped representations only.** `Segment` resolves its evidence
  through `Representation.evidence_for_range`, and a derived mapping (PDF page
  text) resolves only at its own declared boundary. A bounded segment inside
  such a page has no reversible coordinate, so this segmenter refuses a
  representation carrying one instead of minting a segment that cannot be
  proven.

Left behind deliberately: the parquet table and its column list, the artifact
and fragment vocabulary, the two-identity scheme (`segment_id` plus
`content_digest` -- DocSpec's `Segment.create` already mints one identity from
the same semantic inputs), the `contains_span` gold-containment helpers, and
the run-level `CheckResult` reporting, which belongs to that pipeline's runtime
rather than to a segmenter.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from docspec.domain.identity import identity_digest, require_sha256, require_text
from docspec.errors import IntegrityError
from docspec.processing.artifacts import (
    IDENTITY_TRANSFORM,
    RepresentationPayload,
    SegmentPayload,
    build_segment,
    decode_utf8,
    utf8_byte_offsets,
)

BOUNDED_SEGMENTER_ID = "docspec.bounded-structure-overlap/v1"
BOUNDED_SEGMENT_KIND = "bounded-text"

# The arm the bounded fair comparison selected on 2026-07-24, carried over
# whole: the strongest lossless direct arm under the declared
# Recall@50 -> Recall@10 -> MRR ordering.
SELECTED_POLICY = "structure-overlap"
SEGMENT_POLICY_VERSION = "structure-overlap-v1"
SELECTED_MAX_TOKENS = 1_800
SELECTED_MIN_TOKENS = 720
SELECTED_OVERLAP_TOKENS = 80
SELECTED_TOKENIZER = "o200k_base"
BOUNDARY_METHOD = "source-native-oversized-overlap"

# Representation kinds whose bytes are UTF-8 text this policy can bound.
# `pdf-text` is excluded by its evidence mappings, not by its kind; `json` has
# an exact record segmenter and `image` is not text at all.
BOUNDED_TEXT_KINDS = frozenset({"text", "html", "xml"})

# The exclusion reason vocabulary, ported token for token under DocSpec's
# dotted `reasonCode` spelling (ADR 0001 deviation row 8). `reason` beside it
# is prose fit to show a reader; both are required of every excluded range.
EXCLUDED_NOT_EVIDENCE_ELIGIBLE = "segmentation.region-not-evidence-eligible"
EXCLUDED_EMPTY = "segmentation.region-empty"
EXCLUDED_REASON_CODES = frozenset({EXCLUDED_NOT_EVIDENCE_ELIGIBLE, EXCLUDED_EMPTY})

HEADING_EXCLUSION_REASON = "heading text is segment context, never segment evidence"
EMPTY_EXCLUSION_REASON = "the region carries no text between its neighbours"

BOUNDED_SEGMENTATION_RECEIPT_FORMAT = "docspec-bounded-segmentation-receipt"
BOUNDED_SEGMENTATION_RECEIPT_FORMAT_VERSION = "1.0"

_REASON_CODE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9-]*)+$")
_PARAGRAPH_GAP = re.compile(r"(?:\r?\n)[ \t]*(?:\r?\n)")
_ATX_HEADING = re.compile(r"^(#{1,6})[ \t]+(\S.*)$")


class BoundedSegmentationError(IntegrityError):
    """One of this module's own invariants did not hold."""


class TokenCounter(Protocol):
    """The whole tokenizer contract this step needs: a name, a version, a count.

    Injected, never constructed here. A provider counter lives in `adapters/`;
    a test supplies an exact one.
    """

    name: str
    version: str

    def count(self, text: str) -> int: ...


@dataclass(frozen=True, slots=True)
class BoundedSegmentSettings:
    """The complete, versioned identity of one bounded segmentation.

    Everything that can move a boundary or change a recorded fact is here, and
    `policy_digest` covers all of it. Two runs that agree on that digest
    selected the same segments from the same representation.
    """

    policy: str = SELECTED_POLICY
    policy_version: str = SEGMENT_POLICY_VERSION
    max_tokens: int = SELECTED_MAX_TOKENS
    min_tokens: int = SELECTED_MIN_TOKENS
    overlap_tokens: int = SELECTED_OVERLAP_TOKENS
    tokenizer: str = SELECTED_TOKENIZER
    tokenizer_version: str = ""
    boundary_method: str = BOUNDARY_METHOD

    def __post_init__(self) -> None:
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if self.min_tokens <= 0 or self.min_tokens > self.max_tokens:
            raise ValueError("min_tokens must be positive and no larger than max_tokens")
        if self.overlap_tokens < 0:
            raise ValueError("overlap_tokens cannot be negative")
        if self.max_tokens - self.overlap_tokens <= 0:
            raise ValueError("the overlap budget leaves no room for evidence")
        for name, value in (
            ("policy", self.policy),
            ("policy_version", self.policy_version),
            ("tokenizer", self.tokenizer),
            ("tokenizer_version", self.tokenizer_version),
            ("boundary_method", self.boundary_method),
        ):
            require_text(value, f"bounded segmentation {name}")

    @classmethod
    def for_counter(cls, counter: TokenCounter, **overrides: Any) -> BoundedSegmentSettings:
        """Settings that name the counter really doing the counting."""

        return cls(tokenizer=counter.name, tokenizer_version=counter.version, **overrides)

    @property
    def leaf_budget(self) -> int:
        """The budget one leaf of an oversized region may fill, overlap reserved."""

        return self.max_tokens - self.overlap_tokens

    def identity(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "policyVersion": self.policy_version,
            "maxTokens": self.max_tokens,
            "minTokens": self.min_tokens,
            "overlapTokens": self.overlap_tokens,
            "tokenizer": self.tokenizer,
            "tokenizerVersion": self.tokenizer_version,
            "boundaryMethod": self.boundary_method,
            "coordinates": "utf8-byte-range",
        }

    @property
    def policy_digest(self) -> str:
        """The digested policy id every segment this run mints carries."""

        return identity_digest(self.identity())


@dataclass(frozen=True, slots=True)
class ExcludedRegion:
    """One representation range this step did not segment, and why.

    An exclusion is a search exclusion, never a redaction: these bytes stay in
    the representation and a reader slices them out by offset.
    """

    start: int
    end: int
    reason_code: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.start, int) or not isinstance(self.end, int):
            raise ValueError("an excluded range must carry integer byte offsets")
        if self.start < 0 or self.end <= self.start:
            raise ValueError("an excluded range must satisfy end > start")
        if not _REASON_CODE.match(self.reason_code):
            raise ValueError(f"excluded range reasonCode {self.reason_code!r} is not machine-legible")
        require_text(self.reason, "excluded range reason")

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "reasonCode": self.reason_code,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SegmentContext:
    """Everything a consumer may use as context and may never cite as evidence.

    Kept beside the segment, never inside it: context is not a source range, so
    it changes no boundary, no token count, and no identity.
    """

    headings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"headings": list(self.headings)}


@dataclass(frozen=True, slots=True)
class SegmentCoverage:
    """What one representation's bytes were covered by, and what they were not."""

    representation_bytes: int
    covered_bytes: int
    duplicated_bytes: int
    excluded_bytes: int
    uncovered_bytes: int
    segment_count: int

    @property
    def identity_holds(self) -> bool:
        """ADR 0001: `segmentedByteTotal + excludedByteTotal == representationByteTotal`."""

        return not self.uncovered_bytes and (
            self.covered_bytes + self.excluded_bytes == self.representation_bytes
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "representationByteTotal": self.representation_bytes,
            "segmentedByteTotal": self.covered_bytes,
            "duplicatedByteTotal": self.duplicated_bytes,
            "excludedByteTotal": self.excluded_bytes,
            "uncoveredByteTotal": self.uncovered_bytes,
            "segmentCount": self.segment_count,
        }


@dataclass(frozen=True, slots=True)
class BoundedSegment:
    """One bounded segment: its exact payload, its context, its measured size."""

    payload: SegmentPayload
    context: SegmentContext
    token_count: int


@dataclass(frozen=True, slots=True)
class TextSpan:
    """One bounded segment as a half-open UTF-8 byte range and its heading path."""

    start: int
    end: int
    headings: tuple[str, ...]
    token_count: int


@dataclass(frozen=True, slots=True)
class HeadingRegion:
    """One heading the region tiling found: its bytes, its level, its title.

    Reported beside the exclusion ledger rather than folded into it. A heading
    IS an excluded range -- its bytes are context, never evidence -- but a
    caller building a structural record needs the level and the title the
    tiling already parsed, and re-deriving them from the slice would be a second
    implementation of the same rule.
    """

    start: int
    end: int
    level: int
    title: str


@dataclass(frozen=True, slots=True)
class BoundedTextSegmentation:
    """One representation's boundaries, headings, exclusions, and coverage.

    The policy without the record plumbing. `segment_bounded` builds DocSpec
    `Segment` records on top of exactly this, and a producer that mints its own
    record shape -- a `DocumentRelease` builder, say -- reads it directly rather
    than minting an internal representation record it does not need. One
    boundary implementation either way: nothing here re-decides where a segment
    ends.
    """

    spans: tuple[TextSpan, ...]
    headings: tuple[HeadingRegion, ...]
    excluded: tuple[ExcludedRegion, ...]
    coverage: SegmentCoverage


@dataclass(frozen=True, slots=True)
class BoundedSegmentationReceipt:
    """Recomputable evidence for one bounded segmenter invocation."""

    representation_id: str
    segmenter_id: str
    policy_digest: str
    segment_ids: tuple[str, ...]
    excluded: tuple[ExcludedRegion, ...]
    coverage: SegmentCoverage

    def __post_init__(self) -> None:
        require_text(self.representation_id, "segmentation receipt representation_id")
        require_text(self.segmenter_id, "segmentation receipt segmenter_id")
        require_sha256(self.policy_digest, "segmentation receipt policy_digest")
        if not isinstance(self.segment_ids, tuple):
            raise ValueError("segmentation receipt segment_ids must be an immutable tuple")
        for segment_id in self.segment_ids:
            require_text(segment_id, "segmentation receipt segment_id")
        if len(set(self.segment_ids)) != len(self.segment_ids):
            raise ValueError("segmentation receipt segment identities must be distinct")

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": BOUNDED_SEGMENTATION_RECEIPT_FORMAT,
            "formatVersion": BOUNDED_SEGMENTATION_RECEIPT_FORMAT_VERSION,
            "representationId": self.representation_id,
            "segmenterId": self.segmenter_id,
            "policyDigest": self.policy_digest,
            "segments": list(self.segment_ids),
            "excludedRanges": [item.to_dict() for item in self.excluded],
            "coverage": self.coverage.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BoundedSegmentationReceipt:
        expected = {
            "format",
            "formatVersion",
            "representationId",
            "segmenterId",
            "policyDigest",
            "segments",
            "excludedRanges",
            "coverage",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("bounded segmentation receipt has an invalid closed shape")
        if value["format"] != BOUNDED_SEGMENTATION_RECEIPT_FORMAT:
            raise ValueError("bounded segmentation receipt format is not supported")
        if value["formatVersion"] != BOUNDED_SEGMENTATION_RECEIPT_FORMAT_VERSION:
            raise ValueError("bounded segmentation receipt format version is not supported")
        segments = value["segments"]
        excluded = value["excludedRanges"]
        coverage = value["coverage"]
        if not isinstance(segments, (list, tuple)) or not isinstance(excluded, (list, tuple)):
            raise ValueError("bounded segmentation receipt arrays must be arrays")
        if not isinstance(coverage, Mapping):
            raise ValueError("bounded segmentation receipt coverage must be an object")
        return cls(
            representation_id=value["representationId"],
            segmenter_id=value["segmenterId"],
            policy_digest=value["policyDigest"],
            segment_ids=tuple(segments),
            excluded=tuple(
                ExcludedRegion(
                    start=item["start"],
                    end=item["end"],
                    reason_code=item["reasonCode"],
                    reason=item["reason"],
                )
                for item in excluded
            ),
            coverage=SegmentCoverage(
                representation_bytes=coverage["representationByteTotal"],
                covered_bytes=coverage["segmentedByteTotal"],
                duplicated_bytes=coverage["duplicatedByteTotal"],
                excluded_bytes=coverage["excludedByteTotal"],
                uncovered_bytes=coverage["uncoveredByteTotal"],
                segment_count=coverage["segmentCount"],
            ),
        )

    @property
    def receipt_digest(self) -> str:
        return identity_digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class BoundedSegmentation:
    """One representation's bounded segmentation, with its complete account."""

    representation_id: str
    segmenter_id: str
    settings: BoundedSegmentSettings
    segments: tuple[BoundedSegment, ...]
    excluded: tuple[ExcludedRegion, ...]
    coverage: SegmentCoverage

    @property
    def payloads(self) -> tuple[SegmentPayload, ...]:
        return tuple(item.payload for item in self.segments)

    @property
    def receipt(self) -> BoundedSegmentationReceipt:
        return BoundedSegmentationReceipt(
            representation_id=self.representation_id,
            segmenter_id=self.segmenter_id,
            policy_digest=self.settings.policy_digest,
            segment_ids=tuple(item.payload.segment.segment_id for item in self.segments),
            excluded=self.excluded,
            coverage=self.coverage,
        )


# ---------------------------------------------------------------------------
# boundary machinery
#
# Behaviour carried over from the selected path. These four functions decide
# where an oversized region is cut, and a change to any of them moves every
# boundary the selected policy was measured on.
# ---------------------------------------------------------------------------


def _sentence_break(text: str, *, lower: int, upper: int) -> int | None:
    for index in range(upper - 1, lower - 1, -1):
        if text[index] in ".!?" and index + 1 < len(text) and text[index + 1].isspace():
            return index + 1
    return None


def _last_break(text: str, *, start: int, lower: int, upper: int) -> int:
    """The latest structural break in ``[lower, upper)``, preferring larger units."""

    paragraph = text.rfind("\n\n", lower, upper)
    if paragraph >= lower:
        return paragraph + 2
    line = text.rfind("\n", lower, upper)
    if line >= lower:
        return line + 1
    sentence = _sentence_break(text, lower=lower, upper=upper)
    if sentence is not None:
        return sentence
    for index in range(upper - 1, lower - 1, -1):
        if text[index].isspace():
            return index + 1
    if upper <= start:
        raise BoundedSegmentationError("segment boundary did not advance")
    return upper


def _largest_end_within_budget(text: str, *, start: int, max_tokens: int, counter: TokenCounter) -> int:
    """The furthest character boundary whose slice still fits the budget.

    BPE counts are nearly monotone but can change at a new suffix, so the
    exponential probe avoids tokenizing the whole tail of a large document, the
    binary search finds a candidate, and the final loop proves the returned
    slice itself fits.
    """

    if start >= len(text):
        return len(text)
    window = max(64, max_tokens * 4)
    high = min(len(text), start + window)
    safe = start
    while True:
        if counter.count(text[start:high]) <= max_tokens:
            safe = high
            if high == len(text):
                return high
            window *= 2
            high = min(len(text), start + window)
            continue
        break
    low = safe + 1
    while low <= high:
        middle = (low + high) // 2
        if counter.count(text[start:middle]) <= max_tokens:
            safe = middle
            low = middle + 1
        else:
            high = middle - 1
    while safe > start and counter.count(text[start:safe]) > max_tokens:
        safe -= 1
    if safe == start:
        first_end = min(len(text), start + 1)
        raise BoundedSegmentationError(
            f"max_tokens cannot contain one source character ({counter.count(text[start:first_end])} tokens required)"
        )
    return safe


def _smallest_end_at_budget(text: str, *, start: int, upper: int, min_tokens: int, counter: TokenCounter) -> int:
    if min_tokens <= 1:
        return min(start + 1, upper)
    low = start + 1
    high = upper
    result = upper
    while low <= high:
        middle = (low + high) // 2
        if counter.count(text[start:middle]) >= min_tokens:
            result = middle
            high = middle - 1
        else:
            low = middle + 1
    return result


def _leaf_spans(text: str, *, max_tokens: int, min_tokens: int, counter: TokenCounter) -> list[tuple[int, int]]:
    """Split one oversized region's text into gap-free leaves within the budget."""

    if max_tokens <= 0:
        raise BoundedSegmentationError("max_tokens must be positive")
    if min_tokens <= 0 or min_tokens > max_tokens:
        raise BoundedSegmentationError("min_tokens must be positive and no larger than max_tokens")
    if not text:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        upper = _largest_end_within_budget(text, start=start, max_tokens=max_tokens, counter=counter)
        if upper == len(text):
            end = upper
        else:
            lower = _smallest_end_at_budget(text, start=start, upper=upper, min_tokens=min_tokens, counter=counter)
            end = _last_break(text, start=start, lower=lower, upper=upper)
            while end > start and counter.count(text[start:end]) > max_tokens:
                end -= 1
        spans.append((start, end))
        start = end
    return spans


def _overlap_start(text: str, *, lower: int, end: int, overlap_tokens: int, counter: TokenCounter) -> int:
    """The earliest start at or after ``lower`` whose tail still fits the overlap."""

    low = lower
    high = end
    while low < high:
        middle = (low + high) // 2
        if counter.count(text[middle:end]) <= overlap_tokens:
            high = middle
        else:
            low = middle + 1
    start = low
    while start < end and counter.count(text[start:end]) > overlap_tokens:
        start += 1
    while start > lower and counter.count(text[start - 1 : end]) <= overlap_tokens:
        start -= 1
    return start


# ---------------------------------------------------------------------------
# regions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Region:
    """One tiling piece of the representation text, in character coordinates."""

    window: int
    kind: str
    start_char: int
    end_char: int
    text: str

    @property
    def evidence_eligible(self) -> bool:
        return self.kind == "paragraph"


def _blocks(text: str, lower: int, upper: int) -> list[tuple[int, int]]:
    """The blank-line separated blocks of ``[lower, upper)``, gaps included as gaps."""

    pieces: list[tuple[int, int]] = []
    start = lower
    for gap in _PARAGRAPH_GAP.finditer(text, lower, upper):
        pieces.append((start, gap.start()))
        pieces.append((gap.start(), gap.end()))
        start = gap.end()
    pieces.append((start, upper))
    return pieces


def _regions(text: str, windows: Sequence[tuple[int, int]]) -> tuple[_Region, ...]:
    """Tile every window of the text into paragraph, heading, and empty regions.

    The tiling is exact and gap-free: consecutive regions abut, the first
    begins at its window's start, and the last ends at its window's end. That
    is what makes the coverage identity checkable rather than asserted.
    """

    regions: list[_Region] = []
    for index, (lower, upper) in enumerate(windows):
        for block_start, block_end in _blocks(text, lower, upper):
            if block_start == block_end:
                continue
            trimmed_start, trimmed_end = block_start, block_end
            while trimmed_start < trimmed_end and text[trimmed_start].isspace():
                trimmed_start += 1
            while trimmed_end > trimmed_start and text[trimmed_end - 1].isspace():
                trimmed_end -= 1
            if trimmed_start == trimmed_end:
                regions.append(_Region(index, "empty", block_start, block_end, text[block_start:block_end]))
                continue
            body = text[trimmed_start:trimmed_end]
            kind = "heading" if "\n" not in body and _ATX_HEADING.match(body) else "paragraph"
            if block_start < trimmed_start:
                regions.append(_Region(index, "empty", block_start, trimmed_start, text[block_start:trimmed_start]))
            regions.append(_Region(index, kind, trimmed_start, trimmed_end, body))
            if trimmed_end < block_end:
                regions.append(_Region(index, "empty", trimmed_end, block_end, text[trimmed_end:block_end]))
    return tuple(regions)


def _heading_path(stack: Sequence[tuple[int, str]]) -> tuple[str, ...]:
    return tuple(title for _, title in stack)


def _push_heading(stack: list[tuple[int, str]], body: str) -> tuple[int, str]:
    match = _ATX_HEADING.match(body)
    if match is None:  # pragma: no cover - only a heading region reaches here
        raise BoundedSegmentationError("a heading region must carry a heading")
    level = len(match.group(1))
    while stack and stack[-1][0] >= level:
        stack.pop()
    entry = (level, match.group(2).strip())
    stack.append(entry)
    return entry


# ---------------------------------------------------------------------------
# units and packing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _HeldBack:
    """One region the stream held back, in character coordinates, and why.

    A heading also carries the level and title `_push_heading` just parsed out
    of it. That fact is free here and unrecoverable later: an exclusion ledger
    entry says only that bytes were held back, and a caller building a
    structural record from it would have to re-parse the slice to learn what
    the segmenter already decided.
    """

    start_char: int
    end_char: int
    reason_code: str
    reason: str
    level: int | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class _Unit:
    """One packable piece: a whole region, or one leaf of an oversized region."""

    start_char: int
    end_char: int
    headings: tuple[str, ...]
    break_key: tuple[int, int]
    split_region: bool


def _units(
    regions: Sequence[_Region],
    *,
    settings: BoundedSegmentSettings,
    counter: TokenCounter,
) -> tuple[list[_Unit], list[_HeldBack]]:
    """Turn the region tiling into units, splitting only what does not fit.

    Returns the units and the held-back regions, both in character coordinates
    and in region order.
    """

    units: list[_Unit] = []
    held: list[_HeldBack] = []
    stack: list[tuple[int, str]] = []
    epoch = 0
    for region in regions:
        if region.kind == "heading":
            level, title = _push_heading(stack, region.text)
            epoch += 1
            held.append(
                _HeldBack(
                    region.start_char,
                    region.end_char,
                    EXCLUDED_NOT_EVIDENCE_ELIGIBLE,
                    HEADING_EXCLUSION_REASON,
                    level,
                    title,
                )
            )
            continue
        if not region.evidence_eligible:
            held.append(_HeldBack(region.start_char, region.end_char, EXCLUDED_EMPTY, EMPTY_EXCLUSION_REASON))
            continue
        headings = _heading_path(stack)
        break_key = (region.window, epoch)
        if counter.count(region.text) <= settings.max_tokens:
            units.append(_Unit(region.start_char, region.end_char, headings, break_key, False))
            continue
        spans = _leaf_spans(
            region.text,
            max_tokens=settings.leaf_budget,
            min_tokens=min(settings.min_tokens, settings.leaf_budget),
            counter=counter,
        )
        for index, (leaf_start, leaf_end) in enumerate(spans):
            # ``lower=0`` is the region's own start: the overlap reaches back
            # into this region and never into the one before it.
            relative_start = leaf_start
            if index:
                relative_start = _overlap_start(
                    region.text,
                    lower=0,
                    end=leaf_start,
                    overlap_tokens=settings.overlap_tokens,
                    counter=counter,
                )
            units.append(
                _Unit(
                    region.start_char + relative_start,
                    region.start_char + leaf_end,
                    headings,
                    break_key,
                    True,
                )
            )
    return units, held


def _pack(
    text: str,
    units: Sequence[_Unit],
    *,
    settings: BoundedSegmentSettings,
    counter: TokenCounter,
) -> list[list[_Unit]]:
    """Group units into segments: split leaves alone, whole regions greedily.

    A group never crosses an evidence-mapping window or a heading, so every
    group is one contiguous range with one heading path.
    """

    groups: list[list[_Unit]] = []
    current: list[_Unit] = []
    for unit in units:
        if current and current[-1].break_key != unit.break_key:
            groups.append(current)
            current = []
        if unit.split_region:
            if current:
                groups.append(current)
                current = []
            groups.append([unit])
            continue
        if current and counter.count(text[current[0].start_char : unit.end_char]) > settings.max_tokens:
            groups.append(current)
            current = []
        current.append(unit)
    if current:
        groups.append(current)
    return groups


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------


def _merge(values: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(values):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _remainder(start: int, end: int, covered: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """The pieces of ``[start, end)`` that no merged covered span already holds.

    The excluded ledger reports only bytes no segment carries. A separator
    between two regions of one packed group is inside that group's segment, so
    it is segmented rather than excluded, and the coverage identity stays an
    exact partition rather than an arithmetic coincidence.
    """

    pieces: list[tuple[int, int]] = []
    position = start
    for span_start, span_end in covered:
        if span_end <= position:
            continue
        if span_start >= end:
            break
        if span_start > position:
            pieces.append((position, min(span_start, end)))
        position = max(position, span_end)
        if position >= end:
            return pieces
    if position < end:
        pieces.append((position, end))
    return pieces


def _union_length(values: Sequence[tuple[int, int]]) -> int:
    return sum(end - start for start, end in _merge(values))


def _coverage(
    representation_bytes: int,
    spans: Sequence[tuple[int, int]],
    excluded: Sequence[ExcludedRegion],
) -> SegmentCoverage:
    """Sweep the emitted spans, counting covered, duplicated, excluded, and missing.

    Overlap duplicates bytes on purpose, so duplication is reported rather than
    treated as a defect. An uncovered byte is a real gap.
    """

    events: list[tuple[int, int]] = []
    for start, end in spans:
        events.append((start, 1))
        events.append((end, -1))
    covered = 0
    duplicated = 0
    active = 0
    previous = 0
    for position, delta in sorted(events, key=lambda value: (value[0], -value[1])):
        width = position - previous
        if active:
            covered += width
            if active > 1:
                duplicated += width * (active - 1)
        active += delta
        previous = position
    excluded_spans = [(item.start, item.end) for item in excluded]
    accounted = _union_length([*spans, *excluded_spans])
    return SegmentCoverage(
        representation_bytes=representation_bytes,
        covered_bytes=covered,
        duplicated_bytes=duplicated,
        excluded_bytes=accounted - _union_length(list(spans)),
        uncovered_bytes=representation_bytes - accounted,
        segment_count=len(spans),
    )


# ---------------------------------------------------------------------------
# coordinate conversion
# ---------------------------------------------------------------------------


def _char_index(offsets: Sequence[int], byte_offset: int, *, label: str) -> int:
    """Convert one byte offset to its character index, refusing a split character."""

    index = bisect_left(offsets, byte_offset)
    if index >= len(offsets) or offsets[index] != byte_offset:
        raise BoundedSegmentationError(f"{label} does not fall on a UTF-8 character boundary")
    return index


def _identity_windows(representation: RepresentationPayload, offsets: Sequence[int]) -> tuple[tuple[int, int], ...]:
    """The character windows of the representation's reversible evidence mappings."""

    mappings = representation.representation.evidence_mappings
    if not mappings:
        raise BoundedSegmentationError("bounded segmentation requires at least one evidence mapping")
    windows: list[tuple[int, int]] = []
    for mapping in mappings:
        if mapping.transformation != IDENTITY_TRANSFORM:
            raise BoundedSegmentationError(
                "bounded segmentation requires reversible identity byte-slice evidence; "
                f"this representation carries a {mapping.transformation!r} mapping"
            )
        windows.append(
            (
                _char_index(offsets, mapping.representation_start, label="an evidence mapping start"),
                _char_index(offsets, mapping.representation_end, label="an evidence mapping end"),
            )
        )
    return tuple(windows)


# ---------------------------------------------------------------------------
# the segmenter
# ---------------------------------------------------------------------------


class BoundedSegmenter:
    """Bounded, overlapping, heading-aware segmentation of one UTF-8 representation.

    Conforms to `docspec.ports.segmenter.Segmenter`: `segment` returns exactly
    the segment payloads. `segment_bounded` returns the same segments together
    with their heading context, the excluded ledger, and the recomputed
    coverage the release needs.
    """

    segmenter_id = BOUNDED_SEGMENTER_ID

    def __init__(self, counter: TokenCounter, *, settings: BoundedSegmentSettings | None = None) -> None:
        resolved = settings if settings is not None else BoundedSegmentSettings.for_counter(counter)
        _require_matching_counter(resolved, counter)
        self._counter = counter
        self._settings = resolved

    @property
    def settings(self) -> BoundedSegmentSettings:
        return self._settings

    @property
    def policy_digest(self) -> str:
        return self._settings.policy_digest

    def segment(self, representation: RepresentationPayload) -> tuple[SegmentPayload, ...]:
        return self.segment_bounded(representation).payloads

    def segment_text(self, content: bytes, *, label: str = "representation") -> BoundedTextSegmentation:
        """Bound one UTF-8 text body: boundaries, headings, exclusions, coverage.

        The whole body is one window, because a caller holding text alone has no
        evidence mapping to cut it at. Everything else -- the region tiling, the
        budget, the overlap, the heading rule, the coverage sweep -- is the same
        code `segment_bounded` runs.
        """

        text = decode_utf8(content, label=f"bounded segmentation {label}")
        offsets = utf8_byte_offsets(text)
        return self._bound(text, offsets, ((0, len(text)),), len(content), label)

    def segment_bounded(self, representation: RepresentationPayload) -> BoundedSegmentation:
        settings = self._settings
        kind = representation.representation.kind
        if kind not in BOUNDED_TEXT_KINDS:
            raise BoundedSegmentationError(f"bounded segmentation does not accept representation kind {kind!r}")

        text = decode_utf8(representation.content, label="bounded segmentation representation")
        offsets = utf8_byte_offsets(text)
        windows = _identity_windows(representation, offsets)
        representation_id = representation.representation.representation_id
        bounded = self._bound(text, offsets, windows, len(representation.content), representation_id)

        segments = [
            BoundedSegment(
                payload=build_segment(
                    representation,
                    ordinal=ordinal,
                    kind=BOUNDED_SEGMENT_KIND,
                    start=span.start,
                    end=span.end,
                    segmenter_id=self.segmenter_id,
                    policy_digest=settings.policy_digest,
                    derivation=("source-native-text", f"bounded:{settings.policy}"),
                ),
                context=SegmentContext(headings=span.headings),
                token_count=span.token_count,
            )
            for ordinal, span in enumerate(bounded.spans)
        ]
        return BoundedSegmentation(
            representation_id=representation_id,
            segmenter_id=self.segmenter_id,
            settings=settings,
            segments=tuple(segments),
            excluded=bounded.excluded,
            coverage=bounded.coverage,
        )

    def _bound(
        self,
        text: str,
        offsets: Sequence[int],
        windows: Sequence[tuple[int, int]],
        content_length: int,
        label: str,
    ) -> BoundedTextSegmentation:
        """The one implementation of this policy's boundaries and its accounting."""

        settings = self._settings
        counter = self._counter
        _require_matching_counter(settings, counter)
        regions = _regions(text, windows)
        units, held = _units(regions, settings=settings, counter=counter)
        groups = _pack(text, units, settings=settings, counter=counter)

        spans: list[TextSpan] = []
        byte_spans: list[tuple[int, int]] = []
        char_spans: list[tuple[int, int]] = []
        for ordinal, group in enumerate(groups):
            start_char = group[0].start_char
            end_char = group[-1].end_char
            token_count = counter.count(text[start_char:end_char])
            if token_count > settings.max_tokens:
                raise BoundedSegmentationError(
                    f"segment {ordinal} of {label} needs {token_count} tokens, "
                    f"over the hard budget of {settings.max_tokens}"
                )
            start = offsets[start_char]
            end = offsets[end_char]
            spans.append(TextSpan(start, end, group[0].headings, token_count))
            byte_spans.append((start, end))
            char_spans.append((start_char, end_char))

        covered = _merge(char_spans)
        byte_excluded = tuple(
            ExcludedRegion(
                start=offsets[start],
                end=offsets[end],
                reason_code=item.reason_code,
                reason=item.reason,
            )
            for item in held
            for start, end in _remainder(item.start_char, item.end_char, covered)
        )
        coverage = _coverage(content_length, byte_spans, byte_excluded)
        if coverage.uncovered_bytes:
            raise BoundedSegmentationError(
                f"{coverage.uncovered_bytes} representation bytes reached neither a segment nor the excluded ledger"
            )
        headings = tuple(
            HeadingRegion(offsets[item.start_char], offsets[item.end_char], item.level, item.title)
            for item in held
            if item.level is not None and item.title is not None
        )
        return BoundedTextSegmentation(
            spans=tuple(spans),
            headings=headings,
            excluded=byte_excluded,
            coverage=coverage,
        )


def _require_matching_counter(settings: BoundedSegmentSettings, counter: TokenCounter) -> None:
    """Refuse settings that name a different tokenizer than the one counting."""

    if settings.tokenizer != counter.name or settings.tokenizer_version != counter.version:
        raise BoundedSegmentationError(
            f"settings name tokenizer {settings.tokenizer}@{settings.tokenizer_version} "
            f"but the counter is {counter.name}@{counter.version}"
        )
