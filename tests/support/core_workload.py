"""Deterministic, streamed inputs for the C01 core-bulk-v1 workload."""

from collections.abc import Iterator
from copy import deepcopy
import hashlib
from typing import Any


CORE_MEMBER_COUNT = 1_048_576
CORE_BODY_BYTES = 8_192
_METADATA = (
    {}, {"field": None}, {"field": 1}, {"field": "1"}, {"field": False},
    {"field": {"nested": [1, "1", None, True]}}, {"field": "e\u0301😀"}, {"field": "\u001f"},
)


def core_value(ordinal: int) -> dict[str, Any]:
    """Return the frozen value for one ordinal, refusing a non-int or out-of-range one."""
    if type(ordinal) is not int or not 0 <= ordinal < CORE_MEMBER_COUNT:
        raise ValueError("ordinal outside core-bulk-v1")
    # First 1,024 pairs have equal complete values but distinct member/entity IDs.
    source = ordinal - 1 if ordinal < 2_048 and ordinal % 2 else ordinal
    body = "".join(
        hashlib.sha256(f"core-bulk-v1:{source}:{block}".encode("ascii")).hexdigest()
        for block in range(128)
    )
    return {
        "body": body, "url": f"https://example.invalid/{source}", "title": f"title-{source}",
        "position": source, "metadata": deepcopy(_METADATA[(source // 2 if source < 2_048 else source) % 8]),
    }


def core_members(count: int = CORE_MEMBER_COUNT) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield ``(member_key, occurrence_id, value)`` rows, refusing a non-int or out-of-range count."""
    if type(count) is not int or not 0 <= count <= CORE_MEMBER_COUNT:
        raise ValueError("member count outside core-bulk-v1")
    for ordinal in range(count):
        yield f"{ordinal:07d}", f"urn:docspec:fixture:occurrence:{ordinal}", core_value(ordinal)
