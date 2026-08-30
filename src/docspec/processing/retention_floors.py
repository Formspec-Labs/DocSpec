"""Extraction retention floors: declared before a parser runs, or it does not run.

`docs/decisions/0001-document-release-2-0.md`, *Acceptance gates*: DocSpec today
counts and never refuses -- `processing/extraction.py:237-238` records an element
count and a visible codepoint count and no threshold reads them. 2.0 refuses. A
parse is refused when it is **below the declared floor**, when there is **no
declared floor for that parser and format**, and when the source's **visible text
could not be measured**. Undeclared is not "inherit a default".

The two invariants are the predecessor's, carried over unchanged from the
sibling regulations document pipeline's acquisition module at the commit
Decision 0001 cites:

* ``0 < value < 1`` -- outside that a floor either gates nothing or refuses
  everything;
* ``observed_minimum > value`` -- no margin under the lowest legitimate document
  is a future false refusal.

Fractions are decimal STRINGS, not floats. The canonical JSON encoder refuses a
binary float outright, and a floor that is written into a release must survive
the round trip it will be read back through. Comparison is therefore string
comparison over zero-padded decimals, never a float conversion.

Media types collapse onto one format key before lookup, so the same document is
not gated differently by which header the publisher sent: ``text/xml``,
``application/xml``, and every ``*+xml`` are one population.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from docspec.domain.identity import require_text
from docspec.errors import IntegrityError

# The unit every markup floor is measured in (Decision 0001, amendment B5):
# WHITESPACE-NORMALIZED retained representation bytes over WHITESPACE-NORMALIZED
# captured rendition bytes. Both sides are normalized by the same rule, and that
# is not decoration -- the HTML extractor lays out verbatim while the XML one
# lays out normalized, so an un-normalized numerator makes the two
# incommensurable and, measured, produces retention above 1.0 on real documents,
# which this floor's arithmetic cannot represent. Normalized on both sides, the
# ratio measures the share of the source's non-whitespace substance the parse
# kept: the publisher's indentation cancels, and a thin parse still craters it.
NORMALIZED_VISIBLE_TEXT_FRACTION = "normalized-visible-text-fraction"
# The predecessor spelling, over RAW captured bytes. It survives in the sealed
# conformance corpus and in the superseded first mint; nothing in the docspec
# generation may mint it again.
VISIBLE_TEXT_FRACTION = "visible-text-fraction"
# Text density for binary renditions, declared and unmeasured: no binary
# extractor is chosen, so no floor is calibrated in it.
TEXT_DENSITY = "text-density"
RETENTION_FLOOR_UNITS: frozenset[str] = frozenset(
    {NORMALIZED_VISIBLE_TEXT_FRACTION, VISIBLE_TEXT_FRACTION, TEXT_DENSITY}
)

# The refusal vocabulary, in the release's machine-legible `reasonCode` spelling.
FLOOR_UNDECLARED = "extraction.retention-floor-undeclared"
BELOW_FLOOR = "extraction.below-retention-floor"
UNMEASURABLE = "extraction.retention-unmeasurable"

_WHOLE_FRACTION = re.compile(r"^0\.[0-9]*[1-9]$")
# Every maximal run of ASCII whitespace, which is what a pretty-printer emits
# and what the normalized measurement collapses.
_ASCII_WHITESPACE_RUN = re.compile(rb"[ \t\n\r\f\v]+")


def is_whole_fraction(value: object) -> bool:
    """Whether one value is a decimal string strictly between 0 and 1."""

    return isinstance(value, str) and bool(_WHOLE_FRACTION.match(value))


def normalized_byte_size(payload: bytes) -> int:
    """The byte length of one payload with its whitespace runs collapsed.

    The measurement rule both sides of the retention ratio are taken under
    (amendment B5): every maximal run of ASCII whitespace bytes becomes one
    space, and leading and trailing runs go. Defined on BYTES rather than on
    decoded text so it needs no parser, no encoding guess, and no vocabulary --
    a denominator that required parsing the source would be measuring the
    parser it is meant to gate.
    """

    return len(_ASCII_WHITESPACE_RUN.sub(b" ", payload).strip(b" "))


class RetentionFloorError(IntegrityError):
    """A parse is refused: below its floor, unmeasurable, or ungoverned."""

    def __init__(self, reason_code: str, reason: str) -> None:
        super().__init__(reason)
        self.reason_code = reason_code
        self.reason = reason


def format_key(media_type: str) -> str:
    """Collapse one media type onto the format key a floor is declared against.

    A floor is a property of a parser and a document family, not of a header.
    `text/xml`, `application/xml`, and `application/rss+xml` are read by one
    parser over one population, so they look up one floor.
    """

    base = media_type.partition(";")[0].strip().casefold()
    if base in {"text/xml", "application/xml"} or base.endswith("+xml"):
        return "application/xml"
    return base


def decimal_fraction(numerator: int, denominator: int, places: int = 4) -> str:
    """Write one ratio below 1 as the decimal string this format can carry.

    Truncated rather than rounded, so a measured minimum is never reported
    higher than it was, and stripped of trailing zeros so one ratio has one
    spelling. Binary floating point is refused outright by the canonicaliser,
    which is why this is a string at all.
    """

    if numerator <= 0 or numerator >= denominator:
        raise ValueError(f"retention measurement {numerator}/{denominator} is not a ratio below 1")
    scaled = f"{(numerator * 10**places) // denominator:0{places + 1}d}"
    value = (scaled[:-places] + "." + scaled[-places:]).rstrip("0")
    if value.endswith("."):
        raise ValueError(f"retention measurement {numerator}/{denominator} truncated to zero")
    return value


def greater(left: str, right: str) -> bool:
    """Compare two decimal-string fractions without going through a float."""

    width = max(len(left), len(right))
    return left.ljust(width, "0") > right.ljust(width, "0")


@dataclass(frozen=True, slots=True)
class RetentionFloor:
    """One declared floor: its value, its unit, and the population that measured it."""

    value: str
    unit: str
    observed_minimum: str
    population: str

    def __post_init__(self) -> None:
        for label, value in (
            ("value", self.value),
            ("unit", self.unit),
            ("observed_minimum", self.observed_minimum),
            ("population", self.population),
        ):
            require_text(value, f"retention floor {label}")
        if self.unit not in RETENTION_FLOOR_UNITS:
            raise ValueError(
                f"retention floor unit {self.unit!r} is not one this format declares"
            )
        for label, value in (("value", self.value), ("observed_minimum", self.observed_minimum)):
            if not _WHOLE_FRACTION.match(value):
                raise ValueError(
                    f"retention floor {label} {value!r} must be a decimal fraction strictly between 0 and 1"
                )
        if not greater(self.observed_minimum, self.value):
            raise ValueError(
                f"retention floor {self.value} has no margin under the observed minimum "
                f"{self.observed_minimum}: a floor at or above the lowest legitimate document "
                "is a future false refusal"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "observedMinimum": self.observed_minimum,
            "population": self.population,
            "unit": self.unit,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RetentionFloor:
        if not isinstance(value, Mapping) or set(value) != {
            "observedMinimum",
            "population",
            "unit",
            "value",
        }:
            raise ValueError("retention floor has an invalid closed shape")
        return cls(
            value=value["value"],
            unit=value["unit"],
            observed_minimum=value["observedMinimum"],
            population=value["population"],
        )

    def admits(self, measured: str) -> bool:
        return greater(measured, self.value) or measured == self.value


@dataclass(frozen=True, slots=True)
class RetentionFloorRegistry:
    """Every floor this producer may run under, keyed by text kind and format.

    Per-kind is not optional: a `document-body` HTML extractor and a `comment`
    PDF extractor are different code with different floors, and one table keyed
    by both is what keeps them from borrowing each other's margin.
    """

    floors: Mapping[tuple[str, str], RetentionFloor]

    def floor_for(self, text_kind: str, media_type: str) -> RetentionFloor:
        key = (text_kind, format_key(media_type))
        floor = self.floors.get(key)
        if floor is None:
            raise RetentionFloorError(
                FLOOR_UNDECLARED,
                f"no retention floor is declared for {text_kind} {format_key(media_type)}, "
                "so no parse of it may be admitted",
            )
        return floor

    def admit(self, text_kind: str, media_type: str, *, retained: int, source: int) -> str:
        """Measure one parse against its floor and return the measured fraction.

        ``retained`` and ``source`` are both WHITESPACE-NORMALIZED byte counts
        (`normalized_byte_size`, amendment B5). Passing raw byte counts here
        would measure the publisher's indentation and refuse documents that
        extracted completely, which is exactly what the first mint did to nine
        Federal Register rules.

        Raises rather than returning a verdict: a refused parse must not be able
        to continue by ignoring a boolean.
        """

        floor = self.floor_for(text_kind, media_type)
        if source <= 0 or retained <= 0:
            raise RetentionFloorError(
                UNMEASURABLE,
                f"the source's visible text could not be measured ({retained} retained of {source} bytes)",
            )
        if retained >= source:
            # Retention at or above 1 means nothing was stripped, which for a
            # markup parser means the parse did not happen. Measured, not
            # excused: `decimal_fraction` refuses it and so does this.
            raise RetentionFloorError(
                UNMEASURABLE,
                f"retention {retained}/{source} is not a fraction below 1, so no markup was stripped",
            )
        measured = decimal_fraction(retained, source)
        if not floor.admits(measured):
            raise RetentionFloorError(
                BELOW_FLOOR,
                f"visible text retained {measured} of the captured bytes, below the declared "
                f"floor {floor.value} for {text_kind} {format_key(media_type)}",
            )
        return measured


__all__ = [
    "BELOW_FLOOR",
    "FLOOR_UNDECLARED",
    "NORMALIZED_VISIBLE_TEXT_FRACTION",
    "RETENTION_FLOOR_UNITS",
    "TEXT_DENSITY",
    "UNMEASURABLE",
    "VISIBLE_TEXT_FRACTION",
    "RetentionFloor",
    "RetentionFloorError",
    "RetentionFloorRegistry",
    "decimal_fraction",
    "format_key",
    "greater",
    "is_whole_fraction",
    "normalized_byte_size",
]
