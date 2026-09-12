"""The shared wheel's byte corpus governs DocSpec identities and JSON admission."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

import pytest
from rulespec_artifacts.resources import canonical_json_corpus

from docspec.domain.identity import (
    canonical_json_bytes,
    canonical_json_file_bytes,
    freeze_json,
    parse_canonical_json,
    trusted_json_input,
)
from docspec.errors import IntegrityError


CORPUS = canonical_json_corpus()


@pytest.mark.parametrize("case", CORPUS["encodeAccepted"], ids=lambda case: case["name"])
def test_doc_spec_emits_the_shared_corpus_bytes(case):
    expected = bytes.fromhex(case["canonicalHex"])
    assert canonical_json_bytes(case["value"]) == expected
    assert canonical_json_bytes(freeze_json(case["value"])) == expected
    assert canonical_json_bytes(parse_canonical_json(expected, file_form=False)) == expected
    with trusted_json_input():
        assert canonical_json_bytes(case["value"]) == expected


def _rejected_value(description):
    kind = description["kind"]
    if kind == "integer":
        return int(description["literal"])
    if kind == "float":
        return float(description["literal"])
    if kind == "lone-surrogate-string":
        return chr(int(description["codeUnit"], 16))
    if kind == "lone-surrogate-key":
        return {chr(int(description["codeUnit"], 16)): None}
    if kind == "non-string-key":
        return {1: "integer key"}
    if kind == "bytes":
        return b"exact bytes are not JSON values"
    raise AssertionError(f"unhandled shared corpus value kind: {kind}")


@pytest.mark.parametrize("case", CORPUS["encodeRejected"], ids=lambda case: case["name"])
def test_doc_spec_refuses_the_shared_corpus_values_even_when_trusted(case):
    value = _rejected_value(case["input"])
    with pytest.raises(ValueError):
        canonical_json_bytes(value)
    with trusted_json_input(), pytest.raises(ValueError):
        canonical_json_bytes(value)


@pytest.mark.parametrize("case", CORPUS["parseRejected"], ids=lambda case: case["name"])
def test_doc_spec_refuses_the_shared_corpus_bytes_with_local_context(case):
    with pytest.raises(IntegrityError):
        parse_canonical_json(bytes.fromhex(case["utf8Hex"]), label="source receipt", file_form=False)


def test_file_framing_adds_exactly_one_newline_to_shared_bytes():
    value = {"\ue000": 1, "\U00010000": 2}
    expected = '{"𐀀":2,"":1}'.encode()
    assert canonical_json_file_bytes(value) == expected + b"\n"
    assert canonical_json_bytes(parse_canonical_json(expected + b"\n")) == expected
    with pytest.raises(IntegrityError, match="not canonical"):
        parse_canonical_json(expected + b"\n\n")


class _Label(Enum):
    NOTE = "note"


@dataclass
class _Record:
    label: _Label
    coordinates: tuple[int, int]


def test_domain_conversion_precedes_shared_encoding():
    assert canonical_json_bytes(_Record(_Label.NOTE, (1, 2))) == b'{"coordinates":[1,2],"label":"note"}'


class _RepeatedKeyMapping(Mapping):
    def __len__(self):
        return 2

    def __iter__(self):
        return iter(("repeated", "repeated"))

    def __getitem__(self, key):
        return "value"


def test_custom_mapping_duplicate_keys_refuse_before_plain_conversion():
    with pytest.raises(ValueError, match="value.outer contains a duplicate key: repeated"):
        canonical_json_bytes({"outer": _RepeatedKeyMapping()})


@pytest.mark.parametrize("value", [1.5, float("nan"), object()])
def test_conversion_errors_keep_the_nested_domain_path(value):
    with pytest.raises(ValueError, match=r"value.outer\[\].inner"):
        canonical_json_bytes({"outer": [{"inner": value}]})
