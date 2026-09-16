"""Source-byte parity against the frozen pre-port reader, with named changes."""

import json
import math
from hashlib import sha256
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from docspec.domain.identity import identity_digest
from docspec.errors import IntegrityError
from docspec.processing import reader_identity
from docspec.processing.artifacts import verify_representation_evidence, verify_segment_evidence
from docspec.processing.extraction import JsonExtractor, _passthrough_result
from docspec.processing.segmentation import RecordSegmenter
from docspec.processing.source_profiles import JSON_SOURCE_PROFILE
from tests.support import json_tools_oracle as old
from tests.support.processing import _captured


def _assert_parity(source):
    text = source.decode("utf-8")
    expected = old.strict_json_value(text)
    captured = _captured(source, "application/json")
    result = JsonExtractor().extract(captured, source)
    assert result.payload.content == source
    assert result.payload.representation.blob == captured.blob
    assert result.receipt.metadata == {
        "rootKind": "array" if isinstance(expected, list) else "object" if isinstance(expected, dict) else "scalar",
        "recordCount": len(expected) if isinstance(expected, list) else 1,
    }
    assert result.receipt.extractor_id == "docspec.json-source/v2"
    assert result.receipt.configuration_digest != identity_digest({"mode": "source-native-passthrough"})
    verify_representation_evidence(result.payload, source)
    segments = RecordSegmenter().segment(result.payload)
    expected_bounds = [
        (len(text[:start].encode("utf-8")), len(text[:end].encode("utf-8")))
        for start, end in old.record_char_ranges(text)
    ]
    assert [(s.segment.evidence.start, s.segment.evidence.end) for s in segments] == expected_bounds
    assert [s.content for s in segments] == [source[start:end] for start, end in expected_bounds]
    assert all(s.segment.segmenter_id == "docspec.json-record/v2" for s in segments)
    for segment in segments:
        verify_segment_evidence(segment, result.payload, source)
    assert RecordSegmenter().segment(result.payload) == segments
    return result


@pytest.mark.parametrize(
    "source",
    [
        b"[]",
        b" [ ] \n",
        b"{}",
        b" null\t",
        b" true ",
        b"false",
        b"-0",
        b"9007199254740993",
        b'"\\ud800"',
        b'[{"\\ud800":"\\udfff"}]',
        b"[1.00e+2,-0.0,0.1,1e-9999,-1e-9999,9007199254740993]",
        b'[{}, [], [1,2], {"x":null}, "escaped \\" and \\n", false]',
        ' \r\n[ {"café":"§ 🧪"},\n "\\u00e9", "literal é", {"unknown":[0]} ]\t'.encode(),
    ],
)
def test_source_values_and_record_bytes_match_frozen_reader(source):
    _assert_parity(source)


_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(10**30), max_value=10**30)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=20),
    lambda children: st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=10), children, max_size=5),
    max_leaves=20,
)


@given(_values, st.booleans())
@settings(max_examples=75, deadline=None)
def test_generated_unicode_and_nested_source_spans_match_frozen_reader(value, ascii_escapes):
    _assert_parity((" \n" + json.dumps(value, ensure_ascii=ascii_escapes, indent=1) + "\r\n").encode())


def _unchecked_json(source):
    """A retained representation from an earlier or caller-supplied extractor."""
    return _passthrough_result(
        _captured(source, "application/json"),
        source,
        "fixture-retained-json/v1",
        identity_digest({"fixture": True}),
        "json",
        {},
    ).payload


@pytest.mark.parametrize(
    "source",
    [
        b'{"a":1,"a":2}',
        b'{"a":1,"\\u0061":2}',
        b'[{}, {"nested":{"x":1,"x":2}}]',
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b"[0,NaN]",
        b"[1,]",
        b"[1 2]",
        b"[1",
        b"{} null",
        b"[1]junk",
        b"[01]",
        b"[.1]",
        b" ",
        b"\xef\xbb\xbf[]",
    ],
)
def test_ambiguous_or_incomplete_sources_still_refuse_in_both_stages(source):
    with pytest.raises(IntegrityError):
        old.strict_json_value(source.decode())
    with pytest.raises(IntegrityError):
        JsonExtractor().extract(_captured(source, "application/json"), source)
    with pytest.raises(IntegrityError):
        RecordSegmenter().segment(_unchecked_json(source))


@pytest.mark.parametrize("source", [b"1e999", b"-1e999", b"[0,1e999]", b'{"unknown":-1e999}'])
def test_named_overflow_refusal_replaces_old_infinity_acceptance(source):
    value = old.strict_json_value(source.decode())
    number = value[-1] if isinstance(value, list) else value["unknown"] if isinstance(value, dict) else value
    assert math.isinf(number)
    for action in (
        lambda: JsonExtractor().extract(_captured(source, "application/json"), source),
        lambda: RecordSegmenter().segment(_unchecked_json(source)),
    ):
        with pytest.raises(IntegrityError, match="unsupported number"):
            action()


def test_invalid_utf8_retains_integrity_error_class_with_owner_diagnostic():
    source = b'["\xff"]'
    with pytest.raises(IntegrityError, match="utf-8"):
        JsonExtractor().extract(_captured(source, "application/json"), source)
    with pytest.raises(IntegrityError, match="utf-8"):
        RecordSegmenter().segment(_unchecked_json(source))


@pytest.mark.parametrize(
    ("bound", "limit", "source"),
    [
        ("max_bytes", 4, b"[0,1]"),
        ("max_nodes", 3, b'{"unknown":[0,1]}'),
        ("max_depth", 1, b'{"unknown":[0]}'),
    ],
)
def test_source_bounds_apply_to_unknown_fields_and_both_stages(monkeypatch, bound, limit, source):
    from spicy_docs.reading import json_input

    for name in ("read_json_records", "load_bounded_json"):
        original = getattr(json_input, name)

        def bounded(raw, *, _read=original, **kwargs):
            assert kwargs[bound] == JSON_SOURCE_PROFILE[bound]
            return _read(raw, **{**kwargs, bound: limit})

        monkeypatch.setattr(json_input, name, bounded)
    for action in (
        lambda: JsonExtractor().extract(_captured(source, "application/json"), source),
        lambda: RecordSegmenter().segment(_unchecked_json(source)),
    ):
        with pytest.raises(IntegrityError, match=bound if bound == "max_bytes" else "max_nodes or max_depth"):
            action()


@pytest.mark.parametrize("change", ["version", "module-bytes", "missing"])
@pytest.mark.parametrize("operation", ["selected", "run", "configuration"])
@pytest.mark.parametrize("stage", ["extractor", "segmenter"])
def test_reader_drift_refuses_before_parsing(monkeypatch, change, operation, stage):
    from spicy_docs.reading import json_input

    source = b"[1]"
    captured = _captured(source, "application/json")
    retained = _unchecked_json(source)
    reader = JsonExtractor() if stage == "extractor" else RecordSegmenter()
    identity = reader_identity.installed_reader_identity(reader_identity.JSON_MODULES)
    assert identity is not None
    changed = {
        "version": ("different", identity[1]),
        "module-bytes": (identity[0], ((identity[1][0][0], "0" * 64),)),
        "missing": None,
    }[change]
    monkeypatch.setattr(reader_identity, "installed_reader_identity", lambda _: changed)
    for name in ("read_json_records", "load_bounded_json"):
        monkeypatch.setattr(json_input, name, lambda *a, **k: pytest.fail("parsed after reader drift"))
    actions = {
        "selected": lambda: reader.selected_identity(captured if stage == "extractor" else retained.representation),
        "run": lambda: reader.extract(captured, source) if stage == "extractor" else reader.segment(retained),
        "configuration": lambda: reader.configuration_digest if stage == "extractor" else reader.policy_digest,
    }
    with pytest.raises(IntegrityError, match="reader"):
        actions[operation]()


def test_json_segmentation_does_not_allocate_per_character_offsets(monkeypatch):
    import docspec.processing.segmentation as segmentation

    monkeypatch.setattr(segmentation, "utf8_byte_offsets", lambda _: pytest.fail("allocated character offsets"))
    _assert_parity('["§", "🧪", "\\ud800"]'.encode())


@pytest.mark.parametrize(
    ("name", "digest"),
    [
        ("fr-agencies-2026-08-15.json", "70dd0e8fa373a22d5c9577ac1f70ea736542f0e564f816c3caf28014bd05a92b"),
        ("ecfr-agencies.json", "766685f466d62fa558a504cdeac23eef1d41f3ea24a2f5a3f78b38f2bcd5365e"),
    ],
)
@pytest.mark.parametrize("mutation", ["original", "outer-whitespace", "crlf"])
def test_retained_publisher_records_match_frozen_reader(name, digest, mutation):
    source = (Path(__file__).parent / "fixtures/json" / name).read_bytes()
    assert sha256(source).hexdigest() == digest
    if mutation == "outer-whitespace":
        source = b" \n" + source + b"\t\r\n"
    elif mutation == "crlf":
        source = source.replace(b"\n", b"\r\n")
    _assert_parity(source)
