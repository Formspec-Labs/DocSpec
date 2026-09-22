"""Small source-independent helpers shared by DocSpec catalog policies."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

from docspec.domain.source_catalog import (CatalogNormalizationField, CatalogRenditionFamily, CatalogSelectionDecision, SourceCatalogCandidate, SourceCatalogSelection)
from docspec.errors import IntegrityError

_RIN = re.compile(r"^[0-9]{4}-[A-Z][A-Z0-9]{3}$")
_UTC_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def utf16_key(value: str) -> bytes:
    """Return the UTF-16 big-endian sort key for catalog policy text; a lone surrogate raises IntegrityError."""
    try:
        return value.encode("utf-16-be")
    except UnicodeEncodeError as error:
        raise IntegrityError("catalog policy text contains a lone Unicode surrogate") from error


def array_with_unparseable(value: object) -> tuple[list[Any], tuple[Any, ...]]:
    """Split an optional array: a list passes through, None is empty, any other value is rejected."""
    if value is None:
        return [], ()
    if isinstance(value, list):
        return value, ()
    return [], (value,)


def strings(value: object) -> tuple[list[str], tuple[Any, ...]]:
    """Return deduplicated nonempty strings sorted by UTF-16 key plus every rejected value."""
    values, rejected = array_with_unparseable(value)
    accepted: set[str] = set()
    unparseable = list(rejected)
    for item in values:
        if isinstance(item, str) and item:
            accepted.add(item)
        else:
            unparseable.append(item)
    return sorted(accepted, key=utf16_key), tuple(unparseable)


def text_value(value: object) -> tuple[str | None, tuple[Any, ...]]:
    """Return nonblank text, ``(None, ())`` for None and ``(None, (value,))`` for any other rejection."""
    if value is None:
        return None, ()
    if isinstance(value, str) and value.strip():
        return value, ()
    return None, (value,)


def iso_date(value: object) -> str | None:
    """Return the first ten characters when they form an ISO calendar date, else None."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    selected = value[:10]
    try:
        date.fromisoformat(selected)
    except ValueError:
        return None
    return selected


def date_value(value: object) -> tuple[str | None, tuple[Any, ...]]:
    """Return an ISO date, or ``(None, ())`` for None and ``(None, (value,))`` for an invalid value."""
    if value is None:
        return None, ()
    normalized = iso_date(value)
    return (normalized, ()) if normalized is not None else (None, (value,))


def utc_instant_date_value(value: object) -> tuple[str | None, tuple[Any, ...]]:
    """Read a canonical second-precision UTC instant as its calendar date, refusing any other form."""

    if value is None:
        return None, ()
    if not isinstance(value, str) or _UTC_INSTANT.fullmatch(value) is None:
        return None, (value,)
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return None, (value,)
    if parsed.isoformat().replace("+00:00", "Z") != value:
        return None, (value,)
    return parsed.date().isoformat(), ()


def normalized_rins(value: object) -> tuple[list[str], tuple[Any, ...]]:
    """Return unique NFKC-normalized uppercase RINs plus every raw value the syntax rejected."""
    accepted: set[str] = set()
    values, rejected = strings(value)
    unparseable = list(rejected)
    for raw in values:
        normalized = unicodedata.normalize("NFKC", raw.strip()).upper()
        if _RIN.fullmatch(normalized):
            accepted.add(normalized)
        else:
            unparseable.append(raw)
    return sorted(accepted, key=utf16_key), tuple(unparseable)


def http_url(value: object) -> str | None:
    """Return the value when it is an HTTP(S) URL with a netloc, else None."""
    if not isinstance(value, str) or not value:
        return None
    parsed = urlsplit(value)
    return value if parsed.scheme in {"http", "https"} and bool(parsed.netloc) else None


def http_url_value(value: object) -> tuple[str | None, tuple[Any, ...]]:
    """Return an HTTP(S) URL, or ``(None, ())`` for None and ``(None, (value,))`` for a rejection."""
    if value is None:
        return None, ()
    normalized = http_url(value)
    return (normalized, ()) if normalized is not None else (None, (value,))


def normalization_field(
    normalized_field: str,
    source_paths: tuple[str, ...],
    value: Any,
    *,
    value_source: str = "source",
    unparseable_values: tuple[Any, ...] = (),
    present: bool | None = None,
) -> CatalogNormalizationField:
    """Build one normalization outcome from a value and its rejected counterparts.

    The outcome is ``unparseable`` when anything was rejected, else ``normalized``
    when present, else ``absent``; presence defaults to the value's truthiness,
    and duplicate rejections are collapsed.
    """
    is_present = bool(value) if present is None else present
    outcome = "unparseable" if unparseable_values else "normalized" if is_present else "absent"
    distinct_unparseable: list[Any] = []
    for raw in unparseable_values:
        if not any(raw == existing for existing in distinct_unparseable):
            distinct_unparseable.append(raw)
    return CatalogNormalizationField(
        normalized_field,
        source_paths,
        value_source,
        outcome,
        value,
        tuple(distinct_unparseable),
    )


#: URN prefixes reserved for a concept registry this repository does not own,
#: so a publisher's raw vocabulary can never mint an id that registry owns (D6).
_RESERVED_TOPIC_NAMESPACES = ("urn:ref:", "urn:refspec:")


def observed_topics(
    value: object,
    *,
    scheme: str,
    identity_fields: tuple[str, ...],
    label_fields: tuple[str, ...],
) -> tuple[dict[str, str], ...]:
    """Return sorted, deduplicated observed topics, refusing any reserved concept namespace.

    Raises IntegrityError when the scheme or an identity starts with ``urn:ref:``
    or ``urn:refspec:``; a string becomes its own identity and label, while a
    mapping takes the first nonempty identity and label fields.
    """
    if scheme.startswith(_RESERVED_TOPIC_NAMESPACES):
        raise IntegrityError(f"observed topic scheme {scheme!r} claims a reserved concept namespace")
    result: dict[tuple[str, str], dict[str, str]] = {}
    values = value if isinstance(value, list) else []
    for raw in values:
        if isinstance(raw, str) and raw:
            identity = label = raw
        elif isinstance(raw, Mapping):
            label = next(
                (
                    raw.get(field)
                    for field in label_fields
                    if isinstance(raw.get(field), str) and raw.get(field)
                ),
                None,
            )
            identity = next(
                (
                    raw.get(field)
                    for field in identity_fields
                    if isinstance(raw.get(field), str) and raw.get(field)
                ),
                label,
            )
            if not isinstance(label, str) or not isinstance(identity, str):
                continue
        else:
            continue
        if identity.startswith(_RESERVED_TOPIC_NAMESPACES):
            raise IntegrityError(f"observed topic id {identity!r} claims a reserved concept namespace")
        result[(identity, label)] = {
            "observedTopicId": identity,
            "observedTopicScheme": scheme,
            "label": label,
        }
    return tuple(
        result[key]
        for key in sorted(result, key=lambda pair: tuple(utf16_key(part) for part in pair))
    )


def catalog_interpretations(
    pin: Mapping[str, Any],
    *,
    joins: Sequence[Mapping[str, Any]],
    normalization_fields: Sequence[Any],
    ordered_family_ids: Sequence[str],
    families: Sequence[CatalogRenditionFamily],
    selected_family_id: str | None,
    candidates: Sequence[SourceCatalogCandidate],
    sampling_result: Mapping[str, Any],
    selection: SourceCatalogSelection,
    decisions: Sequence[CatalogSelectionDecision],
    topic_source_field: str,
    topics: Sequence[Mapping[str, Any]] = (),
) -> tuple[dict[str, Any], ...]:
    """Record the six ordered interpretation forms without choosing policy rules."""

    return (
        {
            "interpretationKind": "exact-join",
            **pin,
            "result": {"joins": [dict(value) for value in joins]},
        },
        {
            "interpretationKind": "normalization",
            **pin,
            "result": {
                "fields": [field.to_dict() for field in normalization_fields]
            },
        },
        {
            "interpretationKind": "rendition-preference",
            **pin,
            "result": {
                "orderedFamilyIds": list(ordered_family_ids),
                "families": [family.to_dict() for family in families],
                "selectedFamilyId": selected_family_id,
                "selectedRenditionIds": [value.rendition_id for value in candidates],
            },
        },
        {
            "interpretationKind": "sampling",
            **pin,
            "result": dict(sampling_result),
        },
        {
            "interpretationKind": "selection",
            **pin,
            "result": {
                "decisions": [decision.to_dict() for decision in decisions],
                "finalDisposition": selection.disposition.value,
                "reasonCode": selection.reason_code,
                "reason": selection.reason,
            },
        },
        {
            "interpretationKind": "topic-recovery",
            **pin,
            "result": {
                "sourceField": topic_source_field,
                "outcome": "observed" if topics else "not-recovered",
                "evidenceDigest": None,
                "observedTopicIds": [value["observedTopicId"] for value in topics],
            },
        },
    )

def selection_failure(
    decisions: Sequence[CatalogSelectionDecision],
    decision_id: str,
    selection: SourceCatalogSelection,
) -> tuple[SourceCatalogSelection, tuple[CatalogSelectionDecision, ...]]:
    """Record the stopping decision after the caller has chosen its outcome."""
    return selection, (
        *decisions,
        CatalogSelectionDecision(
            decision_id,
            False,
            selection.disposition,
            selection.reason_code,
            selection.reason,
        ),
    )


__all__ = [
    "catalog_interpretations",
    "selection_failure",
    "array_with_unparseable",
    "date_value",
    "http_url",
    "http_url_value",
    "normalization_field",
    "normalized_rins",
    "observed_topics",
    "strings",
    "text_value",
    "utc_instant_date_value",
    "utf16_key",
]
