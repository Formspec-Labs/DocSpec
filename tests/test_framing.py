"""docspec.framing: the fast framer and record writer are Rulespec, byte for byte."""

from __future__ import annotations

from typing import Any

import pytest
from rulespec_artifacts import FramedSection, canonical_json_bytes, framed_section_digest

from docspec.errors import IntegrityError
from docspec.adapters.framing import (
    FramedSectionHasher,
    canonical_record_payload,
    framed_section_digest_fast,
    is_fast_canonical_safe,
)


def _sections() -> tuple[FramedSection, ...]:
    rows = [{"a": i, "s": "x" * i, "n": [i, {"d": i}]} for i in range(40)]
    return (
        FramedSection("members", 40, iter(rows)),
        FramedSection("empty", 0, iter(())),
        FramedSection("unicode", 2, iter([{"t": "naïve ✓ \U0001f600"}, {"t": "é"}])),
    )


def test_the_fast_multi_section_digest_equals_rulespec_byte_for_byte() -> None:
    assert framed_section_digest_fast("docspec-test/1", _sections()) == framed_section_digest(
        "docspec-test/1", _sections()
    )


def test_the_fast_digest_refuses_exactly_what_rulespec_refuses() -> None:
    with pytest.raises(IntegrityError, match="exceeds its declared count"):
        framed_section_digest_fast("d/1", (FramedSection("m", 1, iter([{"a": 1}, {"a": 2}])),))
    with pytest.raises(IntegrityError, match="declared 2 records but yielded 1"):
        framed_section_digest_fast("d/1", (FramedSection("m", 2, iter([{"a": 1}])),))
    with pytest.raises(IntegrityError, match="distinct"):
        framed_section_digest_fast("d/1", (FramedSection("m", 0, iter(())), FramedSection("m", 0, iter(()))))


def test_the_record_writer_falls_back_outside_its_proven_domain() -> None:
    in_domain: dict[str, Any] = {"k": "v", "nested": {"z": [1, "é", {"a": None}]}}
    assert is_fast_canonical_safe(in_domain)
    assert canonical_record_payload(in_domain) == canonical_json_bytes(in_domain)
    for outside in ({"naïve": 1}, {"ok": 1.5}, {"\U0001f600": 1}):
        assert not is_fast_canonical_safe(outside)
        if not any(isinstance(v, float) for v in outside.values()):
            assert canonical_record_payload(outside) == canonical_json_bytes(outside)


def test_the_incremental_hasher_matches_the_batch_function() -> None:
    rows = [{"a": i} for i in range(7)]
    hasher = FramedSectionHasher("docspec-test/2", "records", 7)
    for row in rows:
        hasher.add(row)
    assert hasher.digest() == framed_section_digest(
        "docspec-test/2", (FramedSection("records", 7, iter(rows)),)
    )
