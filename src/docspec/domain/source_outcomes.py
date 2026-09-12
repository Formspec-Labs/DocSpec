"""Bound provider-reported collection evidence and DocSpec's acceptance choice."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from docspec.domain.identity import canonical_json_bytes, freeze_json
from docspec.errors import LimitExceededError


RECORD_OUTCOMES = frozenset({"empty", "no-record-rejections", "partial-rejection", "total-rejection"})
DEFAULT_ACCEPTED_RECORD_OUTCOMES = frozenset({"empty", "no-record-rejections"})
MAX_SOURCE_DESCRIPTION_BYTES = 1024 * 1024


def accepted_record_outcomes(values: Iterable[str]) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise TypeError("accepted record outcomes must be a collection of outcome names")
    selected = frozenset(values)
    if not selected <= RECORD_OUTCOMES:
        raise ValueError("accepted record outcomes contain an unsupported outcome")
    return selected


def collection_outcome(value: object, *, source_state_scope: str) -> Mapping[str, Any] | None:
    """Preserve reported evidence, without repeating the provider's admission.

    The original provider owns count arithmetic, traversal acceptance, and
    evidence membership. Only these fields govern DocSpec's input decision.
    """

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("source collection outcome must be an object or null")
    if value.get("recordOutcome") not in RECORD_OUTCOMES:
        raise ValueError("source collection outcome has an unsupported recordOutcome")
    if value.get("sourceStateScope") != source_state_scope:
        raise ValueError("source collection outcome differs from its source state scope")
    frozen = freeze_json(value, label="source collection outcome")
    assert isinstance(frozen, Mapping)
    if len(canonical_json_bytes(frozen)) > MAX_SOURCE_DESCRIPTION_BYTES:
        raise LimitExceededError("source collection outcome exceeds its metadata byte limit")
    return frozen


def require_accepted_outcome(value: Mapping[str, Any] | None, accepted: frozenset[str]) -> None:
    # No provider observation is claimed for caller-supplied/unreported input.
    if value is not None and value["recordOutcome"] not in accepted:
        raise ValueError(f"source collection record outcome {value['recordOutcome']!r} is not accepted")


__all__ = ["DEFAULT_ACCEPTED_RECORD_OUTCOMES", "RECORD_OUTCOMES"]
