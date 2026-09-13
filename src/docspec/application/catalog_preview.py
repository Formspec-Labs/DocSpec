"""Explain catalog selection and succession using already admitted evidence."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from typing import Any

from docspec.domain.identity import canonical_json_bytes
from docspec.ports.source_catalog import SourceCatalogSnapshotSummary, SourceNativeDescription

from .catalog_policy import utf16_key
from .comparison import json_changes, json_equal, paired_rows


def validate_preview_limits(sample_limit: int, max_sample_bytes: int) -> None:
    for name, value in (("sample_limit", sample_limit), ("max_sample_bytes", max_sample_bytes)):
        if type(value) is not int or value < 0:
            raise ValueError(f"catalog preview {name} must be a non-negative integer")


def _summary(summary: SourceCatalogSnapshotSummary) -> dict[str, Any]:
    succession = summary.succession
    return {
        "logicalId": summary.logical_id, "artifactDigest": summary.artifact_digest,
        "catalogId": summary.catalog_id, "catalogStateDigest": summary.catalog_state_digest,
        "itemCount": summary.item_count,
        "selectionPolicy": dict(summary.selection_policy),
        "partitions": list(summary.partitions), "partitionPolicy": dict(summary.partition_policy),
        "sourceNativeInputs": [SourceNativeDescription.from_dict(value).to_dict() for value in summary.source_native_inputs],
        "acceptedRecordOutcomes": sorted(summary.accepted_record_outcomes),
        "catalogSelection": {
            "counts": dict(summary.disposition_counts),
            "reasons": [dict(value) for value in summary.reason_counts],
        },
        "supersedes": None if succession is None else {
            "logicalId": succession.logical_id, "artifactDigest": succession.artifact_digest,
            "reason": succession.reason,
        },
    }


def _append_sample(
    sample: list[dict[str, Any]], value: dict[str, Any], used: int, limit: int, maximum: int,
) -> int:
    if len(sample) < limit and used < maximum:
        size = len(canonical_json_bytes(value))
        if used + size <= maximum:
            sample.append(value)
            return used + size
    return used


def preview_catalog(
    summary: SourceCatalogSnapshotSummary,
    rows: Iterator[dict[str, Any]],
    *,
    previous_summary: SourceCatalogSnapshotSummary | None,
    previous_rows: Iterator[dict[str, Any]],
    sample_limit: int,
    max_sample_bytes: int,
) -> dict[str, Any]:
    """Stream complete counts while bounding samples, without fetching documents.

    Rows and summaries come from the existing admitted catalog reader. Each
    sample list separately bounds the sum of canonical entry bytes; summary
    metadata and surrounding report structure are outside that sample bound.
    """

    validate_preview_limits(sample_limit, max_sample_bytes)
    sample: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    sample_bytes = change_bytes = 0
    counts = {"added": 0, "removedFromCatalog": 0, "changed": 0, "unchanged": 0}
    with closing(paired_rows(previous_rows, rows, key=lambda row: utf16_key(row["sourceItemId"]))) as pairs:
        for before, after in pairs:
            if after is not None:
                sample_bytes = _append_sample(sample, after, sample_bytes, sample_limit, max_sample_bytes)
            if previous_summary is None:
                continue
            status = (
                "added" if before is None else "removedFromCatalog" if after is None
                else "unchanged" if json_equal(before, after) else "changed"
            )
            counts[status] += 1
            if status != "unchanged" and len(changes) < sample_limit and change_bytes < max_sample_bytes:
                left, right = before or {}, after or {}
                change = {
                    "sourceItemId": (before if before is not None else after)["sourceItemId"],
                    "change": status,
                    "changedFields": [name for name in sorted(left.keys() | right.keys())
                        if name not in left or name not in right or not json_equal(left[name], right[name])],
                    "differences": json_changes(before, after),
                }
                change_bytes = _append_sample(changes, change, change_bytes, sample_limit, max_sample_bytes)
    changed = sum(counts[name] for name in ("added", "removedFromCatalog", "changed"))
    return {
        "format": "docspec-catalog-preview", "formatVersion": "1.0",
        "catalog": _summary(summary),
        "sample": sample, "sampleBytes": sample_bytes, "sampleTruncated": summary.item_count > len(sample),
        "comparison": None if previous_summary is None else {
            "previousCatalog": _summary(previous_summary),
            "counts": counts, "changeCount": changed,
            "sample": changes, "sampleBytes": change_bytes, "sampleTruncated": changed > len(changes),
            "selectionPolicyChanges": json_changes(dict(previous_summary.selection_policy), dict(summary.selection_policy)),
        },
        "sampleLimits": {"itemsPerSample": sample_limit, "canonicalEntryBytesPerSample": max_sample_bytes},
        "interpretation": {
            "catalogSelection": "The catalog policy's selected, excluded, deleted, unavailable, and failed rows; samples retain candidates and reasons.",
            "runSelection": "Run filters are not applied by this catalog preview.",
            "plannedWork": "Prepare a run and inspect its saved work to see actual processing and reuse decisions; catalog differences do not predict cost.",
            "removedFromCatalog": "Absent from the newer full snapshot, not evidence of publisher deletion. An observed crawl does not preserve omitted items.",
            "sourceScope": "Input artifact pins identify the source evidence; this summary does not independently establish publisher-wide coverage.",
        },
    }
