"""Strict JSON parsing and exact record-coordinate helpers."""

from __future__ import annotations

import json
import re
from typing import Any

from docspec.errors import IntegrityError

_WHITESPACE = re.compile(r"[ \t\r\n]*")


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError(f"non-finite number {value!r}")


def decoder() -> json.JSONDecoder:
    return json.JSONDecoder(object_pairs_hook=_closed_object, parse_constant=_constant)


def strict_json_value(text: str, *, label: str = "JSON") -> Any:
    """Parse one closed JSON value and reject duplicates, constants, and tails."""

    try:
        value, end = decoder().raw_decode(text, _skip_whitespace(text, 0))
    except (json.JSONDecodeError, ValueError) as error:
        raise IntegrityError(f"{label} is not valid closed JSON: {error}") from error
    if _skip_whitespace(text, end) != len(text):
        raise IntegrityError(f"{label} contains trailing content")
    return value


def record_char_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Return exact top-level JSON record ranges as half-open character spans."""

    value = strict_json_value(text)
    start = _skip_whitespace(text, 0)
    if not isinstance(value, list):
        parsed_end = decoder().raw_decode(text, start)[1]
        return ((start, parsed_end),)
    position = _skip_whitespace(text, start + 1)
    if position < len(text) and text[position] == "]":
        return ()
    ranges: list[tuple[int, int]] = []
    parser = decoder()
    while position < len(text):
        record_start = position
        try:
            _, record_end = parser.raw_decode(text, record_start)
        except (json.JSONDecodeError, ValueError) as error:
            raise IntegrityError(f"JSON record is invalid: {error}") from error
        ranges.append((record_start, record_end))
        position = _skip_whitespace(text, record_end)
        if position >= len(text):
            raise IntegrityError("JSON array has no closing bracket")
        if text[position] == "]":
            return tuple(ranges)
        if text[position] != ",":
            raise IntegrityError("JSON array records must be separated by commas")
        position = _skip_whitespace(text, position + 1)
    raise IntegrityError("JSON array has no closing bracket")


def _skip_whitespace(text: str, position: int) -> int:
    return _WHITESPACE.match(text, position).end()
