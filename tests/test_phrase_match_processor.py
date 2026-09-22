"""The optional example phrase processor pins its vocabulary resource and limits.

Each literal match keeps its original bytes and enclosing source evidence; malformed vocabularies refuse,
a whitespace-changed pin refuses, and input, output and duration bounds raise LimitExceededError without
truncation.
"""

from dataclasses import FrozenInstanceError

import pytest

from docspec.domain.identity import canonical_json_bytes, sha256_digest
from docspec.domain.processor_policy import ProcessorLimits
from docspec.domain import core
from docspec.errors import IntegrityError, LimitExceededError
from docspec.processing import ParagraphSegmenter, TextExtractor
from examples.phrase_match_processor import PhraseMatcher
from examples.dataset_example_support import phrase_processor
from tests.support.processing import _captured


def _resource(*phrases, revision="1", terms=None):
    """Build a vocabulary resource and its canonical bytes from phrase terms."""
    raw = canonical_json_bytes({"terms": terms or [{"id": "term", "label": "Term", "phrases": list(phrases)}]})
    return core.Resource(label="vocabulary", certainty="established", description={"resourceId": "urn:test:vocabulary", "revision": revision, "digest": sha256_digest(raw)}), raw


def _result(processor, text):
    """Extract, segment and run the processor on ``text``, returning its single output value and the segment."""
    content = text.encode("utf-8")
    extracted = TextExtractor().extract(_captured(content, "text/plain"), content)
    segment = ParagraphSegmenter().segment(extracted.payload)[0]
    result = processor({"content": segment.content, "evidence": segment.segment.evidence.to_dict()})
    return result.values[0], segment


def test_case_matching_uses_original_unicode_quote_slices_and_enclosing_evidence():
    insensitive = PhraseMatcher(*_resource("café"))
    sensitive = PhraseMatcher(*_resource("café"), case_sensitive=True)
    text = "Préface: CAFÉ and café café; cafétéria _café café_."
    value, segment = _result(insensitive, text)
    assert [match["quote"] for match in value["matches"]] == ["CAFÉ", "café", "café"]
    assert value["enclosingSourceEvidence"] == segment.segment.evidence.to_dict()
    for match in value["matches"]:
        assert segment.content[match["segmentByteStart"]:match["segmentByteEnd"]].decode() == match["quote"]
        assert "sourceByteStart" not in match
    assert [match["quote"] for match in _result(sensitive, text)[0]["matches"]] == ["café", "café"]
    assert phrase_processor("1", ("café",), resource_id="v").definition != phrase_processor("1", ("café",), resource_id="v", case_sensitive=True).definition


def test_overlapping_literals_preserve_each_start_and_deterministic_order():
    processor = PhraseMatcher(*_resource("a", "a a"))
    matches = _result(processor, "a a a")[0]["matches"]
    assert [(match["segmentByteStart"], match["segmentByteEnd"], match["phrase"]) for match in matches] == [
        (0, 1, "a"), (0, 3, "a a"), (2, 3, "a"), (2, 5, "a a"), (4, 5, "a"),
    ]
    literal = PhraseMatcher(*_resource("a.b"))
    assert [match["quote"] for match in _result(literal, "a.b axb")[0]["matches"]] == ["a.b"]


def test_no_match_is_successful_empty_output_and_resource_changes_identity():
    processor = PhraseMatcher(*_resource("café"))
    changed = PhraseMatcher(*_resource("tea", revision="2"))
    assert _result(processor, "Only tea here.")[0]["matches"] == []
    assert _result(changed, "Only tea here.")[0]["matches"][0]["quote"] == "tea"
    assert phrase_processor("1", ("café",), resource_id="v").definition != phrase_processor("2", ("tea",), resource_id="v").definition


def test_vocabulary_pin_and_immutable_settings_are_enforced():
    resource, raw = _resource("café")
    with pytest.raises(IntegrityError, match="resource pin"):
        PhraseMatcher(resource, raw + b" ")
    with pytest.raises(TypeError, match="immutable bytes"):
        PhraseMatcher(resource, bytearray(raw))
    processor = PhraseMatcher(resource, raw)
    with pytest.raises(FrozenInstanceError):
        processor.resource = resource
    assert _result(processor, "café")[0]["resource"] == resource.description


@pytest.mark.parametrize("terms", [
    [{"id": "one", "label": "One", "phrases": [""]}],
    [{"id": "one", "label": "One", "phrases": ["x", "x"]}],
    [{"id": "one", "label": "One", "phrases": ["x"]}, {"id": "one", "label": "Other", "phrases": ["y"]}],
    [{"id": "one", "label": "One", "phrases": ["x"], "extra": True}],
])
def test_malformed_vocabulary_refuses(terms):
    with pytest.raises(ValueError):
        PhraseMatcher(*_resource(terms=terms))


def test_resource_input_and_output_bounds_refuse_without_truncation():
    resource, raw = _resource("x")
    oversized = b" " * (64 * 1024 + 1)
    with pytest.raises(LimitExceededError, match="vocabulary.*byte"):
        PhraseMatcher(resource, oversized)
    with pytest.raises(LimitExceededError, match="term-count"):
        PhraseMatcher(*_resource(terms=[{"id": str(n), "label": "Term", "phrases": ["x"]} for n in range(65)]))
    bounded = PhraseMatcher(resource, raw, limits=ProcessorLimits(max_input_bytes=4096, max_output_bytes=1024**2))
    with pytest.raises(LimitExceededError, match="matches.*output"):
        _result(bounded, " ".join(["x"] * 1025))


def test_duration_refuses(monkeypatch):
    processor = PhraseMatcher(*_resource("x"))
    moments = iter((0, 61))
    monkeypatch.setattr("examples.phrase_match_processor.monotonic", lambda: next(moments))
    with pytest.raises(LimitExceededError, match="duration"):
        _result(processor, "x")
