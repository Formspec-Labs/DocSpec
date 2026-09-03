"""``canonical_json_bytes`` must encode exactly what freeze-then-thaw encoded.

The encoder used to build two throwaway trees per value -- ``freeze_json`` walked
the whole structure to validate it, sort its keys and wrap it immutably, and
``thaw_json`` walked that copy straight back to plain dicts and lists before
``json.dumps`` walked it a third time and sorted the keys again. A profiled
real-corpus catalog build spent roughly a quarter of its time in that pair.

These tests pin what the replacement may not change: the bytes, and the
refusals. They compare against the original composition rather than against
recorded literals, so the property stays checkable if the rules themselves
ever move.
"""

from __future__ import annotations

import json
import math

import pytest

from docspec.domain.identity import (
    canonical_json_bytes,
    freeze_json,
    thaw_json,
    trusted_json_input,
)


def _previous_encoding(value: object) -> bytes:
    """The freeze-then-thaw composition this encoder replaced."""

    return json.dumps(
        thaw_json(freeze_json(value)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


# Shapes the identity rules exist for: the escaping classes a canonical encoder
# has to agree on, plus the nesting and key-ordering the sort used to do twice.
EQUIVALENT_VALUES = [
    {}, [], "", 0, -1, True, False, None,
    {"b": 1, "a": 2},
    {"z": {"y": [1, {"x": "  "}]}},
    {"k": "control \x00\x01\x1f"},
    {"k": "non-bmp \U0001f600"},
    {"k": "combining é"},
    {"k": "rtl אב"},
    {"k": "separators   "},
    {"nested": [[[{"deep": [1, 2, {"a": None}]}]]]},
    {"unicode key \U0001f600": 1},
    {"": "empty key"},
    [{"a": 1}, {"a": 2}],
    {"big": 2**63, "negative": -(2**63)},
]


@pytest.mark.parametrize("value", EQUIVALENT_VALUES, ids=range(len(EQUIVALENT_VALUES)))
def test_encodes_the_same_bytes_as_freeze_then_thaw(value: object) -> None:
    assert canonical_json_bytes(value) == _previous_encoding(value)


@pytest.mark.parametrize("value", EQUIVALENT_VALUES, ids=range(len(EQUIVALENT_VALUES)))
def test_trusted_input_encodes_those_same_bytes(value: object) -> None:
    """The trusted fast path skips the checks, never changes the output."""

    expected = _previous_encoding(value)
    with trusted_json_input():
        assert canonical_json_bytes(value) == expected


REFUSED_VALUES = [
    1.5,
    float("nan"),
    float("inf"),
    -math.inf,
    {"a": 1.5},
    {1: "int key"},
    {"a": {2: "nested int key"}},
    object(),
    {"a": object()},
    [object()],
]


@pytest.mark.parametrize("value", REFUSED_VALUES, ids=range(len(REFUSED_VALUES)))
def test_refuses_exactly_what_freeze_refused(value: object) -> None:
    with pytest.raises(ValueError) as previous:
        _previous_encoding(value)
    with pytest.raises(ValueError) as current:
        canonical_json_bytes(value)
    assert str(current.value) == str(previous.value)


def test_labels_name_the_path_to_the_offending_value() -> None:
    """The label is how a refusal is diagnosable, so it survives the rewrite."""

    with pytest.raises(ValueError) as error:
        canonical_json_bytes({"outer": [{"inner": 1.5}]})
    assert "outer[].inner" in str(error.value)
