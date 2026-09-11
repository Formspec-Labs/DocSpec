"""Counts and coverage derived from the logical rows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from docspec.adapters.document_release.rules import (
    CATALOG_DISPOSITIONS,
    DOCSPEC_GENERATION,
    PREDECESSOR_GENERATION,
    TEXT_BODY_KEYS,
    TEXT_KINDS,
)


def _interval_union(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge half-open intervals into a sorted, disjoint cover."""

    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _covered_bytes(ranges: Sequence[tuple[int, int]]) -> int:
    return sum(end - start for start, end in _interval_union(ranges))


def _text_bodies(
    documents: Sequence[Mapping[str, Any]],
    attachments: Sequence[Mapping[str, Any]],
    comments: Sequence[Mapping[str, Any]],
    *,
    key: str,
) -> list[tuple[Any, str, Any, Any]]:
    """Every text body this release carries, as ``(id, kind, representation, excluded)``.

    One text pipeline, three kinds: a document body, an attachment, and a
    comment each have captured bytes, one selected representation, and an
    exclusion ledger, so the byte accounting reads them through one projection
    rather than three. An attachment with a null ``textBodyId`` carries no text
    -- every rendition of it failed or was excluded -- and is not a text body;
    it is still enumerated, still accounted, and still counted nowhere here.
    """

    bodies: list[tuple[Any, str, Any, Any]] = [
        (
            document.get(key),
            document.get("textKind") or "document-body",
            document.get("representation"),
            document.get("excludedRanges"),
        )
        for document in documents
    ]
    for attachment in attachments:
        if isinstance(attachment.get("textBodyId"), str):
            bodies.append(
                (
                    attachment["textBodyId"],
                    "attachment",
                    attachment.get("representation"),
                    attachment.get("excludedRanges"),
                )
            )
    for comment in comments:
        bodies.append(
            (
                comment.get("textBodyId"),
                "comment",
                comment.get("representation"),
                comment.get("excludedRanges"),
            )
        )
    return bodies


def _spans_by_body(
    segments: Sequence[Mapping[str, Any]], key: str
) -> dict[Any, list[tuple[int, int]]]:
    """Group every well-formed segment range by the text body it names.

    The same selection `_body_totals` made one text body at a time, made once.
    A release with 8,000 bodies and 28,000 segments is 8,000 full scans of the
    segment stream otherwise, three times over, and the answer does not change
    between them.
    """

    grouped: dict[Any, list[tuple[int, int]]] = {}
    for segment in segments:
        start = segment.get("representationStart")
        end = segment.get("representationEnd")
        if isinstance(start, int) and isinstance(end, int):
            grouped.setdefault(segment.get(key), []).append((start, end))
    return grouped


def _body_totals(
    body: tuple[Any, str, Any, Any],
    spans: Mapping[Any, Sequence[tuple[int, int]]],
) -> tuple[int, int, int]:
    """One text body's representation, segmented, and excluded byte totals."""

    body_id, _kind, representation, excluded = body
    representation_bytes = (
        representation["byteSize"]
        if isinstance(representation, Mapping) and isinstance(representation.get("byteSize"), int)
        else 0
    )
    segmented = _covered_bytes(spans.get(body_id, ()))
    excluded_bytes = _covered_bytes(
        [
            (item["start"], item["end"])
            for item in excluded or []
            if isinstance(item, Mapping)
            and isinstance(item.get("start"), int)
            and isinstance(item.get("end"), int)
        ]
    )
    return representation_bytes, segmented, excluded_bytes


def derive_per_kind_counts(
    documents: Sequence[Mapping[str, Any]],
    attachments: Sequence[Mapping[str, Any]],
    comments: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    *,
    key: str,
) -> dict[str, dict[str, int]]:
    """Recompute ``counts.perKind`` from the members alone (amendment A2).

    Closed at the three text kinds and keyed by them, each carrying exactly
    ``textBodies``, ``segments``, and the three byte totals. Every kind is
    present even when the release carries none of it: a zero is written, never
    omitted, so a consumer reads absence as a measured zero rather than as a
    field somebody forgot.
    """

    per_kind = {
        kind: {
            "textBodies": 0,
            "segments": 0,
            "representationByteTotal": 0,
            "segmentedByteTotal": 0,
            "excludedByteTotal": 0,
        }
        for kind in TEXT_KINDS
    }
    for segment in segments:
        kind = segment.get("textKind") or "document-body"
        if kind in per_kind:
            per_kind[kind]["segments"] += 1
    spans = _spans_by_body(segments, key)
    for body in _text_bodies(documents, attachments, comments, key=key):
        kind = body[1]
        if kind not in per_kind:
            continue
        representation, segmented, excluded = _body_totals(body, spans)
        tally = per_kind[kind]
        tally["textBodies"] += 1
        tally["representationByteTotal"] += representation
        tally["segmentedByteTotal"] += segmented
        tally["excludedByteTotal"] += excluded
    return per_kind


# The four `attachmentDisposition` tokens, in the order `counts` declares them.
ATTACHMENT_ACCOUNTING_FIELDS: dict[str, str] = {
    "text-captured": "textCaptured",
    "text-excluded": "textExcluded",
    "source-unavailable": "sourceUnavailable",
    "extraction-failed": "extractionFailed",
}


def derive_attachment_accounting(
    attachments: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    """Recompute ``counts.attachmentAccounting`` from the attachment rows alone.

    Amendment B4. `COMPLETE-SEARCH-CORPUS` requires proving "complete attachment
    accounting", and a claim is proved from bundle bytes by declaring the tally
    and recomputing it. Every token is present even at zero: a reader must be
    able to tell "none were unavailable" from "nobody counted".
    """

    tally = {
        "attachmentRows": 0,
        "renditionRows": 0,
        **{name: 0 for name in ATTACHMENT_ACCOUNTING_FIELDS.values()},
    }
    for attachment in attachments:
        tally["attachmentRows"] += 1
        renditions = attachment.get("renditions")
        for rendition in renditions if isinstance(renditions, list) else ():
            if not isinstance(rendition, Mapping):
                continue
            tally["renditionRows"] += 1
            field = ATTACHMENT_ACCOUNTING_FIELDS.get(rendition.get("attachmentDisposition"))
            if field is not None:
                tally[field] += 1
    return tally


def derive_counts(
    dispositions: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    nodes: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    *,
    member_count: int,
    total_member_byte_size: int,
    attachments: Sequence[Mapping[str, Any]] = (),
    comments: Sequence[Mapping[str, Any]] = (),
    generation: str = PREDECESSOR_GENERATION,
) -> dict[str, Any]:
    """Recompute the diagnostic counts from the members alone.

    ``perKind`` is the docspec generation's field and only that generation's:
    the sealed corpus was minted before amendment A2 named those fields, and a
    recomputation that added them would rename all twenty bundles.
    """

    tally = {name: 0 for name in CATALOG_DISPOSITIONS}
    for row in dispositions:
        value = row.get("catalogDisposition")
        if value in tally:
            tally[value] += 1
    counts: dict[str, Any] = {
        "requestedUniverseCount": len(dispositions),
        "selectedCount": tally["selected"],
        "excludedCount": tally["excluded"],
        "deletedCount": tally["deleted"],
        "unavailableCount": tally["unavailable"],
        "failedCount": tally["failed"],
        "documentVersionCount": len(documents),
        "structuralNodeCount": len(nodes),
        "searchSegmentCount": len(segments),
        "memberCount": member_count,
        "totalMemberByteSize": total_member_byte_size,
    }
    if generation == DOCSPEC_GENERATION:
        counts["perKind"] = derive_per_kind_counts(
            documents, attachments, comments, segments, key=TEXT_BODY_KEYS[generation]
        )
        counts["attachmentAccounting"] = derive_attachment_accounting(attachments)
    return counts


def derive_coverage(
    dispositions: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    *,
    key: str = "documentVersionId",
    attachments: Sequence[Mapping[str, Any]] = (),
    comments: Sequence[Mapping[str, Any]] = (),
) -> dict[str, int]:
    """Recompute the accounting proof from the members alone.

    ``key`` is the field segments hang off in the generation being read --
    ``documentVersionId`` for the sealed corpus, ``textBodyId`` for the docspec
    generation. It defaults to the predecessor's so the sealed corpus keeps
    being derived exactly as it was sealed.

    ``coverage`` stays AGGREGATE (amendment A2): the byte totals span every text
    kind together, and the per-kind breakdown lives under ``counts.perKind``.
    ``documentsWithSegmentCount`` stays a fact about documents, because that is
    what it counts.
    """

    accounted = sum(
        1 for row in dispositions if row.get("catalogDisposition") in CATALOG_DISPOSITIONS
    )
    with_segment = {
        segment.get(key) for segment in segments if isinstance(segment.get(key), str)
    }
    representation_total = 0
    segmented_total = 0
    excluded_total = 0
    spans = _spans_by_body(segments, key)
    for body in _text_bodies(documents, attachments, comments, key=key):
        representation, segmented, excluded = _body_totals(body, spans)
        representation_total += representation
        segmented_total += segmented
        excluded_total += excluded
    return {
        "accountedCount": accounted,
        "unaccountedCount": len(dispositions) - accounted,
        "documentsWithSegmentCount": sum(
            1 for document in documents if document.get(key) in with_segment
        ),
        "representationByteTotal": representation_total,
        "segmentedByteTotal": segmented_total,
        "excludedByteTotal": excluded_total,
    }
