"""Disposition counts and join coverage shared by builders and derivations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from docspec.adapters.catalog_artifact.rules import _SELECTED_DISPOSITION, _utf16_key
from docspec.domain.source_catalog import (
    SOURCE_CATALOG_MAX_JOIN_IDS,
    CatalogDisposition,
)
from docspec.errors import IntegrityError, LimitExceededError


class _DispositionTally:
    """Count rows by disposition and by (disposition, reasonCode).

    One tally serves the build and both derivations, so the receipt's two count
    sections come from one rule; it pickles across the parallel derivation's
    worker boundary as a plain object.
    """

    def __init__(self) -> None:
        self.dispositions = {value.value: 0 for value in CatalogDisposition}
        self.reasons: dict[tuple[str, str], int] = {}

    def add(self, disposition: str, reason_code: str | None) -> None:
        self.dispositions[disposition] += 1
        if reason_code is not None:
            key = (disposition, reason_code)
            self.reasons[key] = self.reasons.get(key, 0) + 1

    def merge(self, other: _DispositionTally) -> None:
        for name, value in other.dispositions.items():
            self.dispositions[name] += value
        for key, value in other.reasons.items():
            self.reasons[key] = self.reasons.get(key, 0) + value

    def to_state(self) -> dict[str, object]:
        return {
            "dispositions": dict(self.dispositions),
            "reasons": [[d, c, n] for (d, c), n in sorted(self.reasons.items())],
        }

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> _DispositionTally:
        tally = cls()
        tally.dispositions = {str(k): int(v) for k, v in state["dispositions"].items()}
        tally.reasons = {(str(d), str(c)): int(n) for d, c, n in state["reasons"]}
        return tally

    def reason_counts(self) -> list[dict[str, object]]:
        """Return the receipt's ``reasonCounts`` rows in their sealed order."""

        return [
            {"disposition": disposition, "reasonCode": reason_code, "count": count}
            for (disposition, reason_code), count in sorted(
                self.reasons.items(),
                key=lambda item: (_utf16_key(item[0][0]), _utf16_key(item[0][1])),
            )
        ]


def _reconcile_reason_counts(rows: Sequence[Mapping[str, Any]], counts: Mapping[str, int]) -> None:
    """Require ordered, distinct reason rows that sum to every non-selected bucket.

    The schema already closes each row's shape, keeps ``selected`` out of the
    enum and requires a positive count; this is the cross-section arithmetic
    the schema cannot express.
    """

    previous: tuple[bytes, bytes] | None = None
    totals = {name: 0 for name in counts}
    for row in rows:
        key = (_utf16_key(row["disposition"]), _utf16_key(row["reasonCode"]))
        if previous is not None and key <= previous:
            raise IntegrityError("catalog build receipt reason counts must be ordered and distinct")
        previous = key
        totals[row["disposition"]] += row["count"]
    for name, total in totals.items():
        if name != _SELECTED_DISPOSITION and total != counts[name]:
            raise IntegrityError("catalog build receipt reason counts do not account for every non-selected row")


#: Above this many eligible rows, a join that matches none of them is a broken
#: key rather than a coverage story. Below it, zero matches is ordinary: a
#: fixture with one document and one non-matching docket is a legitimate test,
#: and a slice built from a one-day Federal Register release against documents
#: citing other days genuinely matches nothing. There is no threshold-free
#: version of this rule, so the number is stated rather than tuned: it exists to
#: catch a key mismatch across a real corpus, and 499,238-eligible-zero-matched
#: is the case it was written for.
_COLLAPSED_JOIN_ELIGIBLE_FLOOR: Final = 10_000


def _refuse_collapsed_joins(join_coverage: list[dict[str, Any]]) -> None:
    """Refuse a build whose large join had candidates and matched none of them.

    On 2026-09-05 the Federal Register join matched none of 499,238 eligible
    documents while the build still reported "pass", because the indexed
    identity became composite while the lookup passed a bare number. This is a
    backstop for the one shape that is never a real corpus -- eligible at or
    above ``_COLLAPSED_JOIN_ELIGIBLE_FLOOR`` with zero matched -- since only a
    comparison against the catalog it succeeds can judge real coverage.
    """
    for coverage in join_coverage:
        eligible = coverage.get("eligible", 0)
        if eligible >= _COLLAPSED_JOIN_ELIGIBLE_FLOOR and not coverage.get("matched", 0):
            raise IntegrityError(
                f"catalog join {coverage.get('joinId')!r} matched none of its "
                f"{eligible} eligible rows; the index key and the lookup key disagree"
            )


def _accumulate_join_coverage(
    counts: dict[str, dict[str, int]],
    record: Mapping[str, Any],
) -> None:
    """Add one join outcome to its per-join counts, refusing an unknown
    outcome, a non-text identity, or too many identities.
    """

    join_id = record["joinId"]
    if not isinstance(join_id, str):
        raise IntegrityError("catalog join identity must be text")
    if join_id not in counts and len(counts) >= SOURCE_CATALOG_MAX_JOIN_IDS:
        raise LimitExceededError("catalog join coverage exceeds its distinct-identity limit")
    selected = counts.setdefault(
        join_id,
        {"eligible": 0, "matched": 0, "unmatched": 0, "nullResult": 0},
    )
    outcome = record["outcome"]
    if outcome == "matched":
        selected["eligible"] += 1
        selected["matched"] += 1
    elif outcome == "no-match":
        selected["eligible"] += 1
        selected["unmatched"] += 1
    elif outcome == "not-stated":
        selected["nullResult"] += 1
    else:
        raise IntegrityError("catalog join outcome is not recognized")
