"""The bounded segmenter: budget refusal, overlap, context, and coverage.

These exercise what `docs/decisions/0001-document-release-2-0.md` requires of
`data/search-segments.jsonl` and the four unbounded segmenters cannot give it.
Every counter here is exact and injected, so a budget in this file is a budget
in bytes or words rather than whatever a tokenizer build happened to decide.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from docspec.domain.content import (
    CapturedFile,
    EvidenceCoordinate,
    EvidenceMapping,
    Representation,
)
from docspec.domain.identity import sha256_digest
from docspec.domain.references import BlobRef
from docspec.errors import IntegrityError
from docspec.processing import (
    EXCLUDED_EMPTY,
    EXCLUDED_NOT_EVIDENCE_ELIGIBLE,
    BoundedSegmentation,
    BoundedSegmentationError,
    BoundedSegmentationReceipt,
    BoundedSegmenter,
    BoundedSegmentSettings,
    DefaultExtractorRegistry,
    DefaultSegmenterRegistry,
    ExcludedRegion,
    RepresentationPayload,
    verify_segment_evidence,
    verify_segment_representation,
)
from docspec.processing.artifacts import (
    IDENTITY_TRANSFORM,
    PDF_PAGE_TEXT_TRANSFORM,
    content_blob_ref,
)


class CharacterCounter:
    """One token per character: an exact budget with no tokenizer in it."""

    name = "characters"
    version = "1"

    def count(self, text: str) -> int:
        return len(text)


class WordCounter:
    name = "words"
    version = "1"

    def count(self, text: str) -> int:
        return len(text.split())


class NonMonotoneCounter:
    """Characters, except that one exact string tokenizes far worse than its parts.

    A real BPE counter is not monotone in its input: a longer slice can end at
    a suffix that tokenizes worse than every prefix did. This counter makes
    that property exact, so the hard-budget refusal the policy keeps for it is
    reachable from a test instead of only from a corpus.
    """

    name = "characters"
    version = "1"

    def __init__(self, expensive: str, cost: int) -> None:
        self._expensive = expensive
        self._cost = cost

    def count(self, text: str) -> int:
        return self._cost if text == self._expensive else len(text)


class ExpensiveCharacterCounter:
    """Every character costs more than any budget this test declares."""

    name = "expensive"
    version = "1"

    def count(self, text: str) -> int:
        return len(text) * 100


def _captured(content: bytes, media_type: str) -> CapturedFile:
    blob = BlobRef(
        locator=f"fixture://{sha256_digest(content).removeprefix('sha256:')}",
        digest=sha256_digest(content),
        byte_size=len(content),
        media_type=media_type,
    )
    return CapturedFile.create(
        source_item_id="source:item-1",
        source_version="2026-08-30",
        candidate_id="primary",
        blob=blob,
        media_type=media_type,
        acquired_at="2026-08-30T12:01:00Z",
        downloader_id="fixture-downloader/v1",
        transport_version="fixture-v1",
        acquisition_started_at="2026-08-30T12:00:00Z",
        downloader_configuration_digest=sha256_digest(b"fixture-downloader-config"),
        task_id="fixture-task",
        attempt_id="fixture-attempt",
    )


def _representation(text: str, media_type: str = "text/plain") -> RepresentationPayload:
    source = text.encode("utf-8")
    return DefaultExtractorRegistry().extract(_captured(source, media_type), source).payload


def _bounded(
    text: str,
    counter: object,
    *,
    media_type: str = "text/plain",
    **overrides: int,
) -> tuple[RepresentationPayload, BoundedSegmentation]:
    payload = _representation(text, media_type)
    settings = BoundedSegmentSettings.for_counter(counter, **overrides)
    return payload, BoundedSegmenter(counter, settings=settings).segment_bounded(payload)


# One oversized paragraph of fixed-width ASCII words: with a character counter
# every boundary below is arithmetic rather than a tokenizer's opinion.
OVERSIZED = " ".join(f"w{index:03d}" for index in range(60))

# A budget small enough that these fixtures actually reach it.
SMALL = {"max_tokens": 8, "min_tokens": 2, "overlap_tokens": 2}


# ─── the budget refuses rather than truncates ──────────────────────────────


def test_a_budget_that_cannot_hold_one_source_character_refuses_the_representation() -> None:
    counter = ExpensiveCharacterCounter()
    with pytest.raises(BoundedSegmentationError) as failure:
        _bounded(OVERSIZED, counter, max_tokens=50, min_tokens=10, overlap_tokens=10)
    assert "cannot contain one source character" in str(failure.value)


def test_a_segment_over_the_hard_budget_refuses_instead_of_dropping_text() -> None:
    _, honest = _bounded(OVERSIZED, CharacterCounter(), max_tokens=40, min_tokens=10, overlap_tokens=10)
    expensive = honest.segments[1].payload.content.decode("utf-8")

    counter = NonMonotoneCounter(expensive, cost=41)
    with pytest.raises(BoundedSegmentationError) as failure:
        _bounded(OVERSIZED, counter, max_tokens=40, min_tokens=10, overlap_tokens=10)
    assert "over the hard budget of 40" in str(failure.value)
    assert BoundedSegmentationError.__mro__[1] is IntegrityError


def test_a_region_within_budget_is_never_split() -> None:
    _, bounded = _bounded("Alpha beta.\n\nGamma delta.", WordCounter(), max_tokens=10, min_tokens=2, overlap_tokens=1)
    assert [item.payload.content.decode("utf-8") for item in bounded.segments] == [
        "Alpha beta.\n\nGamma delta."
    ], "two small regions pack into one segment and neither is cut"


# ─── overlap ───────────────────────────────────────────────────────────────


def test_leaves_of_one_oversized_region_overlap_by_exactly_the_declared_budget() -> None:
    payload, bounded = _bounded(OVERSIZED, CharacterCounter(), max_tokens=40, min_tokens=10, overlap_tokens=10)
    segments = [item.payload.segment for item in bounded.segments]
    assert len(segments) > 2, "the fixture must actually be split"

    assert segments[0].representation_start == 0, "the first leaf never reaches before its own region"
    for previous, current in zip(segments, segments[1:], strict=False):
        assert current.representation_start == previous.representation_end - 10
        assert current.representation_end > current.representation_start
    assert segments[-1].representation_end == len(payload.content)

    for item in bounded.segments:
        assert item.token_count <= 40, "no leaf may leave the hard budget once its overlap is added"


def test_overlap_is_reported_as_duplication_rather_than_hidden() -> None:
    _, bounded = _bounded(OVERSIZED, CharacterCounter(), max_tokens=40, min_tokens=10, overlap_tokens=10)
    expected = 10 * (len(bounded.segments) - 1)
    assert bounded.coverage.duplicated_bytes == expected


def test_an_overlap_budget_that_leaves_no_room_for_evidence_is_refused() -> None:
    with pytest.raises(ValueError, match="leaves no room for evidence"):
        BoundedSegmentSettings(max_tokens=10, min_tokens=5, overlap_tokens=10)


# ─── context is not evidence ───────────────────────────────────────────────


HEADING_DOCUMENT = "# Alpha Aa\n\nOne two three.\n\n## Beta Bb\n\nFour five six.\n"
# Same shape, same byte lengths, different heading words: only the context moves.
RENAMED_DOCUMENT = "# Gamma Cc\n\nOne two three.\n\n## Zeta Zz\n\nFour five six.\n"


def test_heading_text_is_context_and_never_lands_in_a_segment() -> None:
    _, bounded = _bounded(HEADING_DOCUMENT, WordCounter(), max_tokens=8, min_tokens=2, overlap_tokens=2)

    assert [item.payload.content.decode("utf-8") for item in bounded.segments] == [
        "One two three.",
        "Four five six.",
    ]
    assert [item.context.headings for item in bounded.segments] == [
        ("Alpha Aa",),
        ("Alpha Aa", "Beta Bb"),
    ], "the heading path runs outermost first"

    headings = [item for item in bounded.excluded if item.reason_code == EXCLUDED_NOT_EVIDENCE_ELIGIBLE]
    assert [item.to_dict()["start"] for item in headings] == [0, 28]
    for item in bounded.segments:
        for heading in headings:
            assert not (
                item.payload.segment.representation_start < heading.end
                and item.payload.segment.representation_end > heading.start
            ), "no segment range may touch a heading range"


def test_a_heading_is_in_no_segment_identity_because_context_is_not_evidence() -> None:
    _, first = _bounded(HEADING_DOCUMENT, WordCounter(), max_tokens=8, min_tokens=2, overlap_tokens=2)
    _, second = _bounded(RENAMED_DOCUMENT, WordCounter(), max_tokens=8, min_tokens=2, overlap_tokens=2)

    def evidence_of(bounded: BoundedSegmentation) -> list[tuple[object, ...]]:
        return [
            (
                item.payload.segment.representation_start,
                item.payload.segment.representation_end,
                item.payload.segment.ordinal,
                item.payload.segment.kind,
                item.payload.segment.content.digest,
                item.payload.segment.policy_digest,
            )
            for item in bounded.segments
        ]

    assert evidence_of(first) == evidence_of(second), (
        "renaming every heading moves no boundary, no segment byte, and no policy digest"
    )
    assert [item.context.headings for item in first.segments] != [
        item.context.headings for item in second.segments
    ], "the context did change, and it is recorded beside the segment rather than inside it"
    for item in first.segments:
        body = item.payload.content.decode("utf-8")
        assert "Alpha Aa" not in body and "Beta Bb" not in body


def test_excluded_heading_bytes_stay_readable_in_the_representation() -> None:
    payload, bounded = _bounded(HEADING_DOCUMENT, WordCounter(), max_tokens=8, min_tokens=2, overlap_tokens=2)
    headings = [item for item in bounded.excluded if item.reason_code == EXCLUDED_NOT_EVIDENCE_ELIGIBLE]
    assert [payload.content[item.start : item.end].decode("utf-8") for item in headings] == [
        "# Alpha Aa",
        "## Beta Bb",
    ], "an exclusion is a search exclusion, never a redaction"


# ─── coverage ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "counter", "overrides"),
    [
        (HEADING_DOCUMENT, WordCounter(), {"max_tokens": 8, "min_tokens": 2, "overlap_tokens": 2}),
        (OVERSIZED, CharacterCounter(), {"max_tokens": 40, "min_tokens": 10, "overlap_tokens": 10}),
        ("  \n\n \t\n", WordCounter(), {"max_tokens": 8, "min_tokens": 2, "overlap_tokens": 2}),
        ("Alpha.\n\nBeta.\n\n# Only A\n", WordCounter(), {"max_tokens": 3, "min_tokens": 1, "overlap_tokens": 1}),
    ],
)
def test_segmented_and_excluded_bytes_account_for_the_whole_representation(
    text: str, counter: object, overrides: dict[str, int]
) -> None:
    payload, bounded = _bounded(text, counter, **overrides)
    coverage = bounded.coverage

    assert coverage.representation_bytes == len(payload.content)
    assert coverage.segment_count == len(bounded.segments)
    assert coverage.uncovered_bytes == 0
    assert coverage.covered_bytes + coverage.excluded_bytes == coverage.representation_bytes
    assert coverage.identity_holds


def test_the_excluded_ledger_and_the_segments_partition_the_representation() -> None:
    payload, bounded = _bounded(HEADING_DOCUMENT, WordCounter(), max_tokens=8, min_tokens=2, overlap_tokens=2)

    accounted = bytearray(len(payload.content))
    for item in bounded.segments:
        segment = item.payload.segment
        for offset in range(segment.representation_start, segment.representation_end):
            accounted[offset] += 1
    for item in bounded.excluded:
        for offset in range(item.start, item.end):
            accounted[offset] += 1
    assert min(accounted) == 1, "no representation byte is unaccounted for"
    assert max(accounted) == 1, "and none is claimed by both a segment and the excluded ledger"


def test_an_empty_representation_body_segments_nothing_and_loses_nothing() -> None:
    payload, bounded = _bounded("   \n\n\t\n", WordCounter(), max_tokens=8, min_tokens=2, overlap_tokens=2)
    assert bounded.segments == ()
    assert {item.reason_code for item in bounded.excluded} == {EXCLUDED_EMPTY}
    assert bounded.coverage.excluded_bytes == len(payload.content)


def test_every_excluded_range_carries_a_machine_legible_code_and_reader_prose() -> None:
    _, bounded = _bounded(HEADING_DOCUMENT, WordCounter(), max_tokens=8, min_tokens=2, overlap_tokens=2)
    for item in bounded.excluded:
        assert item.reason_code in {EXCLUDED_EMPTY, EXCLUDED_NOT_EVIDENCE_ELIGIBLE}
        assert item.reason.strip()
        assert item.end > item.start

    with pytest.raises(ValueError, match="not machine-legible"):
        ExcludedRegion(start=0, end=1, reason_code="Not A Code", reason="prose")
    with pytest.raises(ValueError, match="end > start"):
        ExcludedRegion(start=4, end=4, reason_code="segmentation.region-empty", reason="prose")


# ─── the injected counter ──────────────────────────────────────────────────


def test_settings_that_name_a_different_tokenizer_than_the_counter_are_refused() -> None:
    counter = WordCounter()
    with pytest.raises(BoundedSegmentationError) as failure:
        BoundedSegmenter(counter, settings=BoundedSegmentSettings(tokenizer="o200k_base", tokenizer_version="9.9"))
    assert "settings name tokenizer o200k_base@9.9" in str(failure.value)
    assert "but the counter is words@1" in str(failure.value)


def test_a_counter_at_a_new_version_moves_the_policy_digest() -> None:
    class Rebuilt(WordCounter):
        version = "2"

    first = BoundedSegmentSettings.for_counter(WordCounter())
    second = BoundedSegmentSettings.for_counter(Rebuilt())
    assert first.policy_digest != second.policy_digest
    assert first.policy_digest.startswith("sha256:")


# ─── UTF-8 byte coordinates ────────────────────────────────────────────────


MULTIBYTE = (
    "# Título §\n\n"
    "Ärger über größe Straßen — dreimal.\n\n"
    "日本語の文章をここに置く。もう一度置く。\n\n"
    "Emoji 🌍🌎🌏 and a tail.\n"
)


def test_every_emitted_coordinate_falls_on_a_utf8_character_boundary() -> None:
    payload, bounded = _bounded(MULTIBYTE, WordCounter(), max_tokens=6, min_tokens=2, overlap_tokens=1)
    content = payload.content
    text = content.decode("utf-8")
    assert len(content) > len(text), "the fixture must actually carry multibyte characters"

    boundaries = [
        offset
        for item in bounded.segments
        for offset in (item.payload.segment.representation_start, item.payload.segment.representation_end)
    ] + [offset for item in bounded.excluded for offset in (item.start, item.end)]
    for offset in boundaries:
        assert offset in {0, len(content)} or not 0x80 <= content[offset] < 0xC0, (
            "a coordinate may not fall inside a UTF-8 continuation sequence"
        )
        content[:offset].decode("utf-8")
        content[offset:].decode("utf-8")


def test_a_multibyte_segment_is_the_exact_representation_slice_it_names() -> None:
    payload, bounded = _bounded(MULTIBYTE, WordCounter(), max_tokens=6, min_tokens=2, overlap_tokens=1)
    for item in bounded.segments:
        segment = item.payload.segment
        exact = payload.content[segment.representation_start : segment.representation_end]
        assert item.payload.content == exact
        assert exact.decode("utf-8"), "every segment decodes on its own"
        verify_segment_representation(item.payload, payload)
        verify_segment_evidence(item.payload, payload, MULTIBYTE.encode("utf-8"))


def test_a_multibyte_boundary_is_stable_under_a_shifted_prefix() -> None:
    _, first = _bounded(MULTIBYTE, WordCounter(), max_tokens=6, min_tokens=2, overlap_tokens=1)
    payload, second = _bounded("🌍\n\n" + MULTIBYTE, WordCounter(), max_tokens=6, min_tokens=2, overlap_tokens=1)
    shift = len("🌍\n\n".encode("utf-8"))
    assert [item.payload.segment.representation_start for item in second.segments][1:] == [
        item.payload.segment.representation_start + shift for item in first.segments
    ], "a four-byte prefix shifts every later coordinate by four bytes, not by one character"


# ─── receipt and registry ──────────────────────────────────────────────────


def test_the_receipt_round_trips_and_names_the_policy_digest() -> None:
    _, bounded = _bounded(HEADING_DOCUMENT, WordCounter(), max_tokens=8, min_tokens=2, overlap_tokens=2)
    receipt = bounded.receipt

    assert receipt.policy_digest == bounded.settings.policy_digest
    assert receipt.segment_ids == tuple(item.payload.segment.segment_id for item in bounded.segments)
    assert BoundedSegmentationReceipt.from_dict(receipt.to_dict()) == receipt
    assert receipt.receipt_digest.startswith("sha256:")

    invalid = receipt.to_dict()
    del invalid["coverage"]
    with pytest.raises(ValueError, match="invalid closed shape"):
        BoundedSegmentationReceipt.from_dict(invalid)


def test_the_registry_carries_five_segmenters_each_with_its_digested_policy_id() -> None:
    counter = WordCounter()
    bounded = BoundedSegmenter(counter, settings=BoundedSegmentSettings.for_counter(counter, **SMALL))

    default = DefaultSegmenterRegistry()
    assert len(default.registered_policy_digests) == 4

    registry = DefaultSegmenterRegistry(bounded=bounded)
    digests = registry.registered_policy_digests
    assert len(digests) == 5
    assert bounded.segmenter_id in digests
    assert digests[bounded.segmenter_id] == bounded.policy_digest
    assert all(value.startswith("sha256:") for value in digests.values())
    assert len(set(digests.values())) == 5, "two segmenters may not share one policy digest"


@pytest.mark.parametrize("media_type", ["text/plain", "text/html", "application/xml"])
def test_the_registry_routes_text_kinds_to_the_bounded_segmenter_when_one_is_supplied(media_type: str) -> None:
    counter = WordCounter()
    tight = BoundedSegmentSettings.for_counter(counter, max_tokens=3, min_tokens=1, overlap_tokens=1)
    bounded = BoundedSegmenter(counter, settings=tight)
    body = {
        "text/plain": "Alpha beta gamma.\n\nDelta epsilon zeta.\n",
        "text/html": "<p>Alpha beta gamma.</p>\n\n<p>Delta epsilon zeta.</p>\n",
        "application/xml": "<root><a>Alpha beta gamma.</a>\n\n<b>Delta epsilon zeta.</b></root>\n",
    }[media_type]
    payload = _representation(body, media_type)

    assert [
        item.segment.segmenter_id for item in DefaultSegmenterRegistry(bounded=bounded).segment(payload)
    ] == [bounded.segmenter_id] * 2
    assert {item.segment.segmenter_id for item in DefaultSegmenterRegistry().segment(payload)} == {
        "docspec.paragraph/v1"
    }, "with no counter supplied the registry is exactly what it was"


def test_bounded_segmentation_is_deterministic() -> None:
    first = _bounded(HEADING_DOCUMENT, WordCounter(), max_tokens=8, min_tokens=2, overlap_tokens=2)[1]
    second = _bounded(HEADING_DOCUMENT, WordCounter(), max_tokens=8, min_tokens=2, overlap_tokens=2)[1]
    assert first.receipt.to_dict() == second.receipt.to_dict()
    assert [item.payload.segment.to_dict() for item in first.segments] == [
        item.payload.segment.to_dict() for item in second.segments
    ]


# ─── what it refuses to segment at all ─────────────────────────────────────


def _text_representation_with_mappings(text: str, mappings: tuple[EvidenceMapping, ...]) -> RepresentationPayload:
    """One `text` representation carrying exactly the evidence mappings given."""

    content = text.encode("utf-8")
    representation = Representation.create(
        source_item_id="source:item-1",
        file_id="urn:docspec:captured-file:v1:" + "0" * 64,
        file_digest=sha256_digest(content),
        kind="text",
        blob=content_blob_ref(content, "text/plain; charset=utf-8"),
        extractor_id="fixture.text/v1",
        configuration_digest=sha256_digest(b"fixture-configuration"),
        evidence_mappings=mappings,
    )
    return RepresentationPayload(representation, content)


def _segmenter() -> BoundedSegmenter:
    counter = WordCounter()
    return BoundedSegmenter(counter, settings=BoundedSegmentSettings.for_counter(counter, **SMALL))


def test_a_representation_whose_evidence_is_not_reversible_is_refused() -> None:
    body = "Alpha beta gamma.\n"
    digest = sha256_digest(body.encode("utf-8"))
    derived = EvidenceMapping(
        0,
        len(body.encode("utf-8")),
        EvidenceCoordinate(coordinate_system="page", source_digest=digest, page=1),
        PDF_PAGE_TEXT_TRANSFORM,
    )
    payload = _text_representation_with_mappings(body, (derived,))

    with pytest.raises(BoundedSegmentationError) as failure:
        _segmenter().segment_bounded(payload)
    assert "requires reversible identity byte-slice evidence" in str(failure.value)


def test_an_evidence_boundary_inside_a_character_is_refused_rather_than_split() -> None:
    body = "Grüße\n\nnoch mal\n"
    content = body.encode("utf-8")
    digest = sha256_digest(content)
    split = body.index("ü") + 1  # the byte between the two bytes of `ü`
    mappings = (
        EvidenceMapping(
            0,
            split,
            EvidenceCoordinate(coordinate_system="byte", source_digest=digest, start=0, end=split),
            IDENTITY_TRANSFORM,
        ),
        EvidenceMapping(
            split,
            len(content),
            EvidenceCoordinate(coordinate_system="byte", source_digest=digest, start=split, end=len(content)),
            IDENTITY_TRANSFORM,
        ),
    )
    payload = _text_representation_with_mappings(body, mappings)

    with pytest.raises(BoundedSegmentationError) as failure:
        _segmenter().segment_bounded(payload)
    assert "does not fall on a UTF-8 character boundary" in str(failure.value)


def test_a_packed_group_never_crosses_an_evidence_mapping_boundary() -> None:
    body = "Alpha beta.\n\nGamma delta.\n"
    content = body.encode("utf-8")
    digest = sha256_digest(content)
    split = body.index("Gamma")
    mappings = (
        EvidenceMapping(
            0,
            split,
            EvidenceCoordinate(coordinate_system="byte", source_digest=digest, start=0, end=split),
            IDENTITY_TRANSFORM,
        ),
        EvidenceMapping(
            split,
            len(content),
            EvidenceCoordinate(coordinate_system="byte", source_digest=digest, start=split, end=len(content)),
            IDENTITY_TRANSFORM,
        ),
    )
    bounded = _segmenter().segment_bounded(_text_representation_with_mappings(body, mappings))

    assert [item.payload.content.decode("utf-8") for item in bounded.segments] == ["Alpha beta.", "Gamma delta."], (
        "two regions that would have packed together stay apart across a mapping boundary"
    )
    assert bounded.coverage.identity_holds


@pytest.mark.parametrize(
    ("media_type", "body"),
    [("application/json", b'[{"a":1}]'), ("image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)],
)
def test_a_representation_that_is_not_utf8_text_is_refused(media_type: str, body: bytes) -> None:
    payload = DefaultExtractorRegistry().extract(_captured(body, media_type), body).payload
    counter = WordCounter()
    segmenter = BoundedSegmenter(counter, settings=BoundedSegmentSettings.for_counter(counter, **SMALL))
    with pytest.raises(BoundedSegmentationError, match="does not accept representation kind"):
        segmenter.segment_bounded(payload)


# ─── the optional provider counter ─────────────────────────────────────────

ROOT = Path(__file__).resolve().parents[1]


def _token_counters() -> object:
    return importlib.import_module("docspec.adapters.token_counters")


def test_selecting_the_provider_counter_loads_no_tokenizer_until_it_is_built() -> None:
    isolated = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; from docspec.adapters import TiktokenCounter; "
            "assert TiktokenCounter.__name__ == 'TiktokenCounter'; assert 'tiktoken' not in sys.modules",
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert isolated.returncode == 0, isolated.stderr


def test_the_provider_counter_names_its_encoding_and_its_pinned_build(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _token_counters()

    class FakeEncoding:
        def encode(self, text: str, *, disallowed_special: tuple[str, ...]) -> list[str]:
            assert disallowed_special == ()
            return text.split()

    provider = SimpleNamespace(get_encoding=lambda name: FakeEncoding())
    monkeypatch.setattr(
        module,
        "import_module",
        lambda name: provider if name == "tiktoken" else importlib.import_module(name),
    )
    monkeypatch.setattr(module, "distribution_version", lambda name: "9.9.9")

    counter = module.TiktokenCounter()
    assert counter.name == "o200k_base"
    assert counter.version == "9.9.9"
    assert counter.count("alpha beta gamma") == 3

    settings = BoundedSegmentSettings.for_counter(counter, **SMALL)
    assert settings.tokenizer == "o200k_base"
    assert settings.tokenizer_version == "9.9.9"
    segmenter = BoundedSegmenter(counter, settings=settings)
    assert segmenter.policy_digest == settings.policy_digest, "the counter build is inside the policy digest"


def test_the_provider_counter_says_which_extra_installs_it(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _token_counters()

    def missing(name: str) -> object:
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(module, "import_module", missing)
    with pytest.raises(RuntimeError, match="'tokens' extra"):
        module.TiktokenCounter()


# ─── the text-level entry point ────────────────────────────────────────────
#
# `segment_text` is the same policy without the record plumbing, for a producer
# that mints its own record shape. These prove it is the SAME policy: identical
# boundaries, identical exclusions, identical coverage, and the heading facts the
# region tiling already knew reported rather than left to be re-parsed.


HEADED = "# Alpha\n\nOne two three.\n\n## Beta\n\nFour five six.\n\nSeven eight."


def test_segment_text_reaches_the_same_boundaries_as_the_record_path() -> None:
    counter = WordCounter()
    payload, bounded = _bounded(HEADED, counter, max_tokens=10, min_tokens=2, overlap_tokens=1)
    settings = BoundedSegmentSettings.for_counter(counter, max_tokens=10, min_tokens=2, overlap_tokens=1)
    text_only = BoundedSegmenter(counter, settings=settings).segment_text(payload.content)

    assert [(span.start, span.end) for span in text_only.spans] == [
        (item.payload.representation_start, item.payload.representation_end) for item in bounded.segments
    ]
    assert [span.headings for span in text_only.spans] == [
        item.context.headings for item in bounded.segments
    ]
    assert [span.token_count for span in text_only.spans] == [item.token_count for item in bounded.segments]
    assert text_only.excluded == bounded.excluded
    assert text_only.coverage == bounded.coverage


def test_segment_text_reports_the_heading_level_and_title_the_tiling_parsed() -> None:
    counter = WordCounter()
    settings = BoundedSegmentSettings.for_counter(counter, max_tokens=10, min_tokens=2, overlap_tokens=1)
    result = BoundedSegmenter(counter, settings=settings).segment_text(HEADED.encode("utf-8"))

    assert [(item.level, item.title) for item in result.headings] == [(1, "Alpha"), (2, "Beta")]
    body = HEADED.encode("utf-8")
    assert [body[item.start : item.end].decode("utf-8") for item in result.headings] == [
        "# Alpha",
        "## Beta",
    ]


def test_segment_text_accounts_for_every_byte_of_the_body_it_was_given() -> None:
    counter = WordCounter()
    settings = BoundedSegmentSettings.for_counter(counter, max_tokens=10, min_tokens=2, overlap_tokens=1)
    body = HEADED.encode("utf-8")
    result = BoundedSegmenter(counter, settings=settings).segment_text(body)

    covered: list[tuple[int, int]] = [(span.start, span.end) for span in result.spans]
    covered += [(item.start, item.end) for item in result.excluded]
    merged: list[tuple[int, int]] = []
    for start, end in sorted(covered):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    assert merged == [(0, len(body))], "segments plus exclusions must tile the whole body"
    assert result.coverage.identity_holds


def test_a_body_with_no_heading_carries_an_empty_heading_path() -> None:
    counter = WordCounter()
    settings = BoundedSegmentSettings.for_counter(counter, max_tokens=10, min_tokens=2, overlap_tokens=1)
    result = BoundedSegmenter(counter, settings=settings).segment_text(b"One two three.")
    assert result.headings == ()
    assert [span.headings for span in result.spans] == [()]
