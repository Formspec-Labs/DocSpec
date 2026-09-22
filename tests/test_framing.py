"""Incremental catalog framing must produce Rulespec's framed-section bytes, including UTF-16 key order.

Unsupported record values are refused on add, and digesting after zero accepted rows raises IntegrityError.
"""

from __future__ import annotations

import pytest
from rulespec_artifacts import FramedSection, framed_section_digest

from docspec.adapters.framing import FramedSectionHasher
from docspec.errors import IntegrityError


def test_incremental_framing_matches_shared_bytes_and_utf16_key_order():
    rows = [{"a": i} for i in range(7)] + [{"\ue000": 1, "\U00010000": {"text": "café"}}]
    hasher = FramedSectionHasher("docspec-test/2", "records", len(rows))
    for row in rows:
        hasher.add(row)
    assert hasher.digest() == framed_section_digest(
        "docspec-test/2", (FramedSection("records", len(rows), iter(rows)),)
    )


@pytest.mark.parametrize("invalid", [2**53, -(2**53), 1.5, "\ud800"])
def test_incremental_framing_refuses_unsupported_record_values(invalid):
    hasher = FramedSectionHasher("docspec-test/2", "records", 1)
    with pytest.raises(ValueError):
        hasher.add({"nested": [invalid]})
    with pytest.raises(IntegrityError, match="yielded 0"):
        hasher.digest()
