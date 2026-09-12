"""The optional example processor pins its vocabulary and preserves literal evidence."""

from dataclasses import FrozenInstanceError, replace

import pytest

from docspec.application.processor_rules import validate_processor_result
from docspec.domain.identity import canonical_json_bytes, identity_digest, sha256_digest
from docspec.domain.policies import DataUsePolicy
from docspec.domain.processors import ProcessorItemLimits, ProcessorPayload, ProcessorRequest, ProcessorResourceIdentity, ProcessorResourceKind
from docspec.errors import IntegrityError, LimitExceededError
from docspec.processing import ParagraphSegmenter, TextExtractor
from examples.phrase_match_processor import PhraseMatchProcessor
from tests.helpers import artifact
from tests.support.processing import _captured


def _resource(*phrases, revision="1", terms=None):
    raw = canonical_json_bytes({"terms": terms or [{"id": "term", "label": "Term", "phrases": list(phrases)}]})
    return ProcessorResourceIdentity("urn:test:vocabulary", ProcessorResourceKind.REFERENCE_DATA, revision, sha256_digest(raw)), raw


def _invocation(processor, text):
    content = text.encode("utf-8")
    extracted = TextExtractor().extract(_captured(content, "text/plain"), content)
    segment = ParagraphSegmenter().segment(extracted.payload)[0]
    policy = DataUsePolicy.local_content()
    payload = ProcessorPayload.for_segment(segment.segment, segment.content, policy.allowed_fields)
    description = processor.description
    request = ProcessorRequest(artifact("plan"), description.processor_id, identity_digest(description.to_dict()),
        "source:item-1", (payload.input_record,), (), payload.allowed_fields, description.item_limits,
        description.cache_policy.key_schema_id, "invocation")
    return request, payload, segment, policy


def _result(processor, text):
    request, payload, segment, policy = _invocation(processor, text)
    result = processor.process(request, payload, ())
    validate_processor_result(result, request, processor.description, segment.segment, len(segment.content), (),
        data_use_policy=policy, require_current_request=True)
    return result.derived_records[0].value, segment


def test_case_matching_uses_original_unicode_quote_slices_and_enclosing_evidence():
    insensitive = PhraseMatchProcessor(*_resource("café"))
    sensitive = PhraseMatchProcessor(*_resource("café"), case_sensitive=True)
    text = "Préface: CAFÉ and café café; cafétéria _café café_."
    value, segment = _result(insensitive, text)
    assert [match["quote"] for match in value["matches"]] == ["CAFÉ", "café", "café"]
    assert value["enclosingSourceEvidence"] == segment.segment.evidence.to_dict()
    for match in value["matches"]:
        assert segment.content[match["segmentByteStart"]:match["segmentByteEnd"]].decode() == match["quote"]
        assert "sourceByteStart" not in match
    assert [match["quote"] for match in _result(sensitive, text)[0]["matches"]] == ["café", "café"]
    assert insensitive.description.processor_id != sensitive.description.processor_id


def test_overlapping_literals_preserve_each_start_and_deterministic_order():
    processor = PhraseMatchProcessor(*_resource("a", "a a"))
    matches = _result(processor, "a a a")[0]["matches"]
    assert [(match["segmentByteStart"], match["segmentByteEnd"], match["phrase"]) for match in matches] == [
        (0, 1, "a"), (0, 3, "a a"), (2, 3, "a"), (2, 5, "a a"), (4, 5, "a"),
    ]
    literal = PhraseMatchProcessor(*_resource("a.b"))
    assert [match["quote"] for match in _result(literal, "a.b axb")[0]["matches"]] == ["a.b"]


def test_no_match_is_successful_empty_output_and_resource_changes_identity():
    processor = PhraseMatchProcessor(*_resource("café"))
    changed = PhraseMatchProcessor(*_resource("tea", revision="2"))
    assert _result(processor, "Only tea here.")[0]["matches"] == []
    assert _result(changed, "Only tea here.")[0]["matches"][0]["quote"] == "tea"
    assert processor.description.processor_id != changed.description.processor_id
    assert processor.description.configuration_digest == changed.description.configuration_digest


def test_vocabulary_pin_and_immutable_settings_are_enforced():
    resource, raw = _resource("café")
    with pytest.raises(IntegrityError, match="resource pin"):
        PhraseMatchProcessor(resource, raw + b" ")
    with pytest.raises(TypeError, match="immutable bytes"):
        PhraseMatchProcessor(resource, bytearray(raw))
    with pytest.raises(ValueError, match="reference-data"):
        PhraseMatchProcessor(replace(resource, resource_kind=ProcessorResourceKind.MODEL), raw)
    processor = PhraseMatchProcessor(resource, raw)
    with pytest.raises(FrozenInstanceError):
        processor.description = processor.description
    assert _result(processor, "café")[0]["resource"] == resource.to_dict()


@pytest.mark.parametrize("terms", [
    [{"id": "one", "label": "One", "phrases": [""]}],
    [{"id": "one", "label": "One", "phrases": ["x", "x"]}],
    [{"id": "one", "label": "One", "phrases": ["x"]}, {"id": "one", "label": "Other", "phrases": ["y"]}],
    [{"id": "one", "label": "One", "phrases": ["x"], "extra": True}],
])
def test_malformed_vocabulary_refuses(terms):
    with pytest.raises(ValueError):
        PhraseMatchProcessor(*_resource(terms=terms))


def test_resource_input_and_output_bounds_refuse_without_truncation():
    resource, raw = _resource("x")
    oversized = b" " * (64 * 1024 + 1)
    with pytest.raises(LimitExceededError, match="vocabulary.*byte"):
        PhraseMatchProcessor(replace(resource, identity_digest=sha256_digest(oversized)), oversized)
    with pytest.raises(LimitExceededError, match="term-count"):
        PhraseMatchProcessor(*_resource(terms=[{"id": str(n), "label": "Term", "phrases": ["x"]} for n in range(65)]))
    small = PhraseMatchProcessor(resource, raw, item_limits=ProcessorItemLimits(1, 2, 1, 1024, 5))
    with pytest.raises(LimitExceededError, match="input.*byte"):
        _result(small, "long text")
    small = PhraseMatchProcessor(resource, raw, item_limits=ProcessorItemLimits(1, 4096, 1, 20, 5))
    with pytest.raises(LimitExceededError, match="output"):
        _result(small, "x")
    bounded = PhraseMatchProcessor(resource, raw, item_limits=ProcessorItemLimits(1, 4096, 1, 1024**2, 5))
    with pytest.raises(LimitExceededError, match="matches.*output"):
        _result(bounded, " ".join(["x"] * 1025))


def test_duration_and_wrong_invocation_refuse(monkeypatch):
    processor = PhraseMatchProcessor(*_resource("x"))
    request, payload, _, _ = _invocation(processor, "x")
    with pytest.raises(IntegrityError, match="pinned invocation"):
        processor.process(replace(request, processor_id="wrong"), payload, ())
    moments = iter((0, 6))
    monkeypatch.setattr("examples.phrase_match_processor.monotonic", lambda: next(moments))
    with pytest.raises(LimitExceededError, match="duration"):
        processor.process(request, payload, ())
