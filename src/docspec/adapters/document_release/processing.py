"""Validate processing policies, structure, segments, and evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from docspec.adapters.document_release.coverage import _interval_union, _spans_by_body
from docspec.adapters.document_release.diagnostics import VerificationIssue, _issue
from docspec.processing.retention_floors import (
    RETENTION_FLOOR_UNITS,
    greater,
    is_whole_fraction,
)


def _validate_processing_policies(
    content: Mapping[str, Any],
    documents: Sequence[Mapping[str, Any]],
    attachments: Sequence[Mapping[str, Any]],
    comments: Sequence[Mapping[str, Any]],
    issues: list[VerificationIssue],
) -> None:
    """Check the declared floors, and that every text body had one to clear.

    Amendment B4's third diagnostic. Two arms, both readable from bundle bytes
    with no extractor re-run:

    * a declared floor that violates its own invariants -- ``0 < value < 1``,
      ``observedMinimum > value``, a unit this format declares. A floor with no
      margin under the lowest legitimate document is a future false refusal, and
      a floor at or above 1 refuses everything; both are defects in the RECORD,
      which is what a wire gate can see.
    * a text body whose ``(textKind, mediaType)`` has no governing policy at
      all. That is the checkable half of "an undeclared floor fails closed": a
      body extracted under no declared floor is visible in the release without
      re-running anything.

    Amendment C1: the pairing is matched **literally**, on the media type the
    capture record carries. This check used to collapse both sides onto the
    retention floor's format key first, and collapsing is what made it blind to
    the defect it exists to catch -- a release declaring its policy under
    ``application/xml`` while all 6,408 of its rows carry ``text/xml`` passed
    here and was refused by the first consumer to join the two. A floor is
    looked up by format key because a floor is a property of a parser; a policy
    ROW is declared by media type because it is a table a consumer joins its
    rows against, and a table keyed on a value no row holds is a table nobody
    can join. Where two spellings of one family are both carried, the release
    declares two rows.
    """

    policies = content.get("processingPolicies")
    if not isinstance(policies, list):
        return
    governed: set[tuple[Any, Any]] = set()
    for position, policy in enumerate(policies):
        if not isinstance(policy, Mapping):
            continue
        path = f"release.json/content/processingPolicies/{position}"
        governed.add((policy.get("textKind"), policy.get("mediaType")))
        floor = policy.get("retentionFloor")
        if not isinstance(floor, Mapping):
            _issue(issues, "invalid.retention-floor", path, "a policy declares a retention floor")
            continue
        value, minimum = floor.get("value"), floor.get("observedMinimum")
        if not is_whole_fraction(value):
            _issue(
                issues,
                "invalid.retention-floor",
                f"{path}/retentionFloor/value",
                "a floor is a decimal fraction strictly between 0 and 1",
            )
        if not is_whole_fraction(minimum):
            _issue(
                issues,
                "invalid.retention-floor",
                f"{path}/retentionFloor/observedMinimum",
                "an observed minimum is a decimal fraction strictly between 0 and 1",
            )
        if is_whole_fraction(value) and is_whole_fraction(minimum) and not greater(minimum, value):
            _issue(
                issues,
                "invalid.retention-floor",
                f"{path}/retentionFloor/observedMinimum",
                f"floor {value} has no margin under the observed minimum {minimum}",
            )
        if floor.get("unit") not in RETENTION_FLOOR_UNITS:
            _issue(
                issues,
                "invalid.retention-floor",
                f"{path}/retentionFloor/unit",
                f"{floor.get('unit')!r} is not a unit this format declares",
            )

    def govern(kind: Any, media_type: Any, where: str) -> None:
        if not isinstance(media_type, str):
            return
        if (kind, media_type) in governed:
            return
        _issue(
            issues,
            "invalid.retention-floor",
            where,
            f"no processing policy governs {kind!r} {media_type!r}, "
            "so this body was extracted under no declared floor",
        )

    for position, document in enumerate(documents):
        capture = document.get("capture")
        if isinstance(capture, Mapping):
            govern(
                document.get("textKind"),
                capture.get("mediaType"),
                f"data/documents.jsonl/{position}/capture/mediaType",
            )
    for position, comment in enumerate(comments):
        capture = comment.get("capture")
        if isinstance(capture, Mapping):
            govern(
                comment.get("textKind"),
                capture.get("mediaType"),
                f"data/comments.jsonl/{position}/capture/mediaType",
            )
    for position, attachment in enumerate(attachments):
        renditions = attachment.get("renditions")
        for order, rendition in enumerate(renditions if isinstance(renditions, list) else ()):
            if not isinstance(rendition, Mapping):
                continue
            if rendition.get("attachmentDisposition") != "text-captured":
                continue
            govern(
                attachment.get("textKind"),
                rendition.get("mediaType"),
                f"data/attachments.jsonl/{position}/renditions/{order}/mediaType",
            )


def _validate_structure(
    nodes: Sequence[Mapping[str, Any]],
    sizes: Mapping[str, int],
    object_key: str,
    key: str,
    issues: list[VerificationIssue],
) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for index, node in enumerate(nodes):
        path = f"{object_key}/{index}"
        node_id = node.get("structuralNodeId")
        if isinstance(node_id, str):
            if node_id in by_id:
                _issue(
                    issues,
                    "invalid.duplicate-identity",
                    f"{path}/structuralNodeId",
                    f"duplicate structuralNodeId {node_id}",
                )
            else:
                by_id[node_id] = dict(node)
    sibling_ordinals: dict[tuple[str, Any], list[int]] = {}
    for index, node in enumerate(nodes):
        path = f"{object_key}/{index}"
        version_id = node.get(key)
        start, end = node.get("representationStart"), node.get("representationEnd")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if end < start:
            _issue(issues, "invalid.structure", f"{path}/representationEnd", "range is inverted")
        size = sizes.get(str(version_id))
        if size is None:
            _issue(
                issues,
                "invalid.structure",
                f"{path}/{key}",
                "node names no text body in this release",
            )
        elif end > size:
            _issue(
                issues,
                "invalid.structure",
                f"{path}/representationEnd",
                f"range exceeds the {size}-byte representation",
            )
        parent_id = node.get("structuralParentId")
        if parent_id is not None:
            parent = by_id.get(str(parent_id))
            if parent is None:
                _issue(
                    issues,
                    "invalid.structure",
                    f"{path}/structuralParentId",
                    f"parent {parent_id!r} does not resolve",
                )
            else:
                if parent.get(key) != version_id:
                    _issue(
                        issues,
                        "invalid.structure",
                        f"{path}/structuralParentId",
                        "parent belongs to a different text body",
                    )
                if isinstance(parent.get("depth"), int) and node.get("depth") != parent["depth"] + 1:
                    _issue(
                        issues,
                        "invalid.structure",
                        f"{path}/depth",
                        f"expected {parent['depth'] + 1}",
                    )
                p_start, p_end = parent.get("representationStart"), parent.get("representationEnd")
                if isinstance(p_start, int) and isinstance(p_end, int) and not (
                    p_start <= start and end <= p_end
                ):
                    _issue(
                        issues,
                        "invalid.structure",
                        f"{path}/representationStart",
                        "range is not contained in its parent",
                    )
        elif node.get("depth") != 0:
            _issue(issues, "invalid.structure", f"{path}/depth", "a root node has depth 0")
        ordinal = node.get("ordinal")
        if isinstance(ordinal, int):
            sibling_ordinals.setdefault((str(version_id), parent_id), []).append(ordinal)
    for (version_id, parent_id), ordinals in sorted(
        sibling_ordinals.items(), key=lambda item: (item[0][0], str(item[0][1]))
    ):
        if sorted(ordinals) != list(range(len(ordinals))):
            _issue(
                issues,
                "invalid.structure",
                f"{object_key}:{version_id}:{parent_id}",
                f"sibling ordinals must be dense and zero-based, found {sorted(ordinals)}",
            )
    return by_id


def _validate_segments(
    segments: Sequence[Mapping[str, Any]],
    nodes: Mapping[str, Mapping[str, Any]],
    renditions: Mapping[Any, Mapping[str, Any]],
    sizes: Mapping[str, int],
    object_key: str,
    key: str,
    issues: list[VerificationIssue],
) -> None:
    seen: set[str] = set()
    ordinals: dict[str, list[int]] = {}
    for index, segment in enumerate(segments):
        path = f"{object_key}/{index}"
        segment_id = segment.get("segmentId")
        if isinstance(segment_id, str):
            if segment_id in seen:
                _issue(
                    issues,
                    "invalid.duplicate-identity",
                    f"{path}/segmentId",
                    f"duplicate segmentId {segment_id}",
                )
            seen.add(segment_id)
        version_id = str(segment.get(key))
        start, end = segment.get("representationStart"), segment.get("representationEnd")
        size = sizes.get(version_id)
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if end <= start:
            _issue(
                issues,
                "invalid.segment",
                f"{path}/representationEnd",
                "range is empty or inverted",
            )
        if size is None:
            _issue(
                issues,
                "invalid.segment",
                f"{path}/{key}",
                "segment names no text body in this release",
            )
        elif end > size:
            _issue(
                issues,
                "invalid.segment",
                f"{path}/representationEnd",
                f"range exceeds the {size}-byte representation",
            )
        parent = nodes.get(str(segment.get("structuralParentId")))
        if parent is None:
            _issue(
                issues,
                "invalid.segment",
                f"{path}/structuralParentId",
                "structural parent does not resolve",
            )
        else:
            if parent.get(key) != segment.get(key):
                _issue(
                    issues,
                    "invalid.segment",
                    f"{path}/structuralParentId",
                    "structural parent belongs to a different text body",
                )
            p_start, p_end = parent.get("representationStart"), parent.get("representationEnd")
            if isinstance(p_start, int) and isinstance(p_end, int) and not (
                p_start <= start and end <= p_end
            ):
                _issue(
                    issues,
                    "invalid.segment",
                    f"{path}/representationStart",
                    "range is not contained in its structural parent",
                )
            expected_path = _heading_path(parent, nodes)
            if segment.get("headingPath") != expected_path:
                _issue(
                    issues,
                    "invalid.segment",
                    f"{path}/headingPath",
                    f"expected {expected_path}",
                )
        evidence = segment.get("evidence")
        capture = renditions.get(segment.get(key))
        if isinstance(evidence, Mapping) and isinstance(capture, Mapping):
            if evidence.get("renditionSha256") != capture.get("sha256"):
                _issue(
                    issues,
                    "invalid.segment",
                    f"{path}/evidence/renditionSha256",
                    "evidence names bytes that are not this document's captured rendition",
                )
            e_start, e_end = evidence.get("start"), evidence.get("end")
            rendition_size = capture.get("byteSize")
            if isinstance(e_start, int) and isinstance(e_end, int):
                if e_end <= e_start:
                    _issue(
                        issues,
                        "invalid.segment",
                        f"{path}/evidence/end",
                        "evidence range is empty or inverted",
                    )
                elif isinstance(rendition_size, int) and e_end > rendition_size:
                    _issue(
                        issues,
                        "invalid.segment",
                        f"{path}/evidence/end",
                        f"evidence exceeds the {rendition_size}-byte rendition",
                    )
        if isinstance(segment.get("ordinal"), int):
            ordinals.setdefault(version_id, []).append(segment["ordinal"])
    for version_id, values in sorted(ordinals.items()):
        if sorted(values) != list(range(len(values))):
            _issue(
                issues,
                "invalid.segment",
                f"{object_key}:{version_id}",
                f"segment ordinals must be dense and zero-based, found {sorted(values)}",
            )


def _heading_path(
    node: Mapping[str, Any], nodes: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """Heading text from the document root down to ``node``, outermost first."""

    chain: list[str] = []
    current: Mapping[str, Any] | None = node
    guard = 0
    while current is not None and guard < 4096:
        guard += 1
        text = current.get("headingText")
        if isinstance(text, str) and text:
            chain.append(text)
        parent_id = current.get("structuralParentId")
        current = nodes.get(str(parent_id)) if parent_id is not None else None
    chain.reverse()
    return chain


def _validate_coverage(
    documents: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    sizes: Mapping[str, int],
    object_key: str,
    key: str,
    issues: list[VerificationIssue],
) -> None:
    """Every visible-text byte is segmented or explicitly excluded, never both."""

    spans = _spans_by_body(segments, key)
    for index, document in enumerate(documents):
        path = f"{object_key}/{index}"
        version_id = document.get(key)
        size = sizes.get(str(version_id))
        if size is None:
            continue
        segment_ranges = list(spans.get(version_id, ()))
        excluded_ranges = [
            (item["start"], item["end"])
            for item in document.get("excludedRanges") or []
            if isinstance(item, Mapping)
            and isinstance(item.get("start"), int)
            and isinstance(item.get("end"), int)
        ]
        if not segment_ranges:
            _issue(
                issues,
                "invalid.coverage",
                f"{path}/documentVersionId",
                "every document requires at least one search segment",
            )
        segment_cover = _interval_union(segment_ranges)
        excluded_cover = _interval_union(excluded_ranges)
        for start, end in excluded_cover:
            for other_start, other_end in segment_cover:
                if start < other_end and other_start < end:
                    _issue(
                        issues,
                        "invalid.coverage",
                        f"{path}/excludedRanges",
                        f"excluded range [{start}, {end}) overlaps a search segment",
                    )
                    break
        combined = _interval_union([*segment_ranges, *excluded_ranges])
        if combined != ([(0, size)] if size else []):
            _issue(
                issues,
                "invalid.coverage",
                f"{path}/representation",
                f"segments plus exclusions must tile [0, {size}); found {combined}",
            )
