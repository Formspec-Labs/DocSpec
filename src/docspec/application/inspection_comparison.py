"""Compare saved work and document values through disposable bounded joins."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterator
from contextlib import closing
from itertools import groupby
from typing import TYPE_CHECKING, Any

from docspec.domain.identity import canonical_json_bytes, identity_digest
from docspec.ports.record_workspace import RecordWorkspace

from .inspection_evidence import entry_evidence

if TYPE_CHECKING:
    from .inspection import InspectionView


def _changes(older: Any, newer: Any, path: str = "") -> list[dict[str, Any]]:
    if older == newer:
        return []
    if isinstance(older, dict) and isinstance(newer, dict):
        return [
            change
            for key in sorted(older.keys() | newer.keys())
            for change in _changes(older.get(key), newer.get(key), f"{path}/{key}")
        ]
    return [{"path": path, "older": older, "newer": newer}]


def _work(view: InspectionView, workspace: RecordWorkspace, collection: str) -> None:
    with closing(view._work_stores()) as stores:
        for reference, store in stores:
            for entry in store.entries:
                evidence = entry_evidence(entry, view.plan, view._controls, sample_limit=0)
                value = {
                    "sourceItemId": entry.source_item.item_id,
                    "inputDigest": identity_digest(entry.source_item.to_dict()),
                    "requestedStages": entry.requested_stages.to_dict(),
                    "executionMode": entry.execution_mode.value,
                    "processorIdsToRun": list(entry.processor_ids_to_run),
                    "disposition": evidence["disposition"],
                    "counts": evidence["counts"],
                    "failureDigest": identity_digest([failure.to_dict() for failure in entry.failures]),
                    "store": reference.to_dict(),
                }
                workspace.add_record(
                    collection, identity=entry.source_item.item_id,
                    source_item_id=entry.source_item.item_id, record=value,
                )


def _content(kind: str, payload: dict[str, Any]) -> Any:
    """Document byte/value changes deliberately exclude delivery-only identity."""

    if kind == "files":
        return {key: payload[key] for key in ("candidateId", "mediaType")} | {
            "digest": payload["blob"]["digest"], "byteSize": payload["blob"]["byteSize"],
        }
    if kind == "representations":
        return {key: payload[key] for key in ("fileId", "kind", "evidenceMappings")} | {
            "digest": payload["blob"]["digest"], "byteSize": payload["blob"]["byteSize"],
        }
    if kind == "segments":
        return {key: payload[key] for key in ("fileId", "kind", "ordinal", "evidence", "representationStart", "representationEnd")} | {
            "digest": payload["content"]["digest"], "byteSize": payload["content"]["byteSize"],
        }
    if kind.startswith("derived:"):
        return {key: payload[key] for key in ("inputIds", "schemaId", "value", "disposition", "warnings")}
    return None


def _configuration(kind: str, payload: dict[str, Any]) -> Any:
    if kind == "dispositions":
        return payload["requestedStages"]
    return None


def _spool_result(view: InspectionView, workspace: RecordWorkspace, collection: str) -> None:
    for kind in view.layer_kinds:
        with closing(view.records(kind)) as records:
            for row in records:
                source = row["sourceItemId"]
                payload = row["payload"]
                group = "derived" if kind.startswith("derived:") else kind
                content = _content(kind, payload)
                configuration = _configuration(kind, payload)
                outcome = None
                if kind in {"dispositions", "failures"}:
                    outcome = {key: value for key, value in payload.items() if key not in {"entryId", "requestedStages"}}
                value = {
                    "sourceItemId": source, "group": group,
                    "input": identity_digest(payload) if kind == "source-items" else None,
                    "content": None if content is None else identity_digest(content),
                    "configuration": None if configuration is None else identity_digest(configuration),
                    "outcome": None if outcome is None else identity_digest(outcome),
                    "provenance": identity_digest({"layerKind": kind, "record": row}),
                }
                # Content-based ordering keeps equal values comparable when their
                # IDs change with configuration or delivery provenance.
                sort_key = value["content"] or value["input"] or value["configuration"] or value["outcome"] or ""
                # Hex preserves UTF-8 ordering and makes separators unambiguous
                # even when an external source identity contains punctuation.
                identity = "!".join(part.encode("utf-8").hex() for part in (source, group, sort_key, row["recordId"]))
                workspace.add_record(collection, identity=identity, source_item_id=source, record=value)


def _result_signatures(workspace: RecordWorkspace, collection: str) -> Iterator[dict[str, Any]]:
    rows = workspace.stream_records(collection)
    try:
        for source, group in groupby(rows, key=lambda row: row["sourceItemId"]):
            hashes = {name: hashlib.sha256() for name in ("input", "content", "configuration", "outcome", "provenance")}
            counts: Counter[str] = Counter()
            for row in group:
                counts[row["group"]] += 1
                for name, digest in hashes.items():
                    if row[name] is not None:
                        digest.update(canonical_json_bytes([row["group"], row[name]]))
                        digest.update(b"\n")
            yield {
                "sourceItemId": source,
                **{f"{name}Digest": f"sha256:{digest.hexdigest()}" for name, digest in hashes.items()},
                "recordCounts": dict(sorted(counts.items())),
            }
    finally:
        close = getattr(rows, "close", None)
        if close is not None:
            close()


def _compare_rows(older: Iterator[dict[str, Any]], newer: Iterator[dict[str, Any]], limit: int) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    sample: list[dict[str, Any]] = []
    try:
        old, new = next(older, None), next(newer, None)
        while old is not None or new is not None:
            if new is None or old is not None and old["sourceItemId"] < new["sourceItemId"]:
                item, before, after, status = old["sourceItemId"], old, None, "removed"
                old = next(older, None)
            elif old is None or new["sourceItemId"] < old["sourceItemId"]:
                item, before, after, status = new["sourceItemId"], None, new, "added"
                new = next(newer, None)
            else:
                item, before, after = old["sourceItemId"], old, new
                status = "unchanged" if old == new else "changed"
                old, new = next(older, None), next(newer, None)
            counts[status] += 1
            if status != "unchanged" and len(sample) < limit:
                changes = _changes(before, after)
                left, right = before or {}, after or {}
                explanation = {
                    f"{name}Changed": left.get(f"{name}Digest") != right.get(f"{name}Digest")
                    for name in ("input", "content", "configuration", "outcome", "provenance")
                    if f"{name}Digest" in left or f"{name}Digest" in right
                }
                sample.append({"sourceItemId": item, "change": status, **explanation, "differences": changes})
    finally:
        for rows in (older, newer):
            close = getattr(rows, "close", None)
            if close is not None:
                close()
    changed = counts["added"] + counts["removed"] + counts["changed"]
    return {"changeCount": changed, "counts": dict(sorted(counts.items())), "sample": sample, "sampleTruncated": changed > len(sample)}


def compare_views(older: InspectionView, newer: InspectionView, *, sample_limit: int = 20) -> dict[str, Any]:
    from .inspection import _sample_limit

    limit = _sample_limit(sample_limit)
    with older._workspace_factory.create() as workspace:
        _work(older, workspace, "old-work")
        _work(newer, workspace, "new-work")
        work = _compare_rows(workspace.stream_records("old-work"), workspace.stream_records("new-work"), limit)
        result = None
        if older.complete_active_state and newer.complete_active_state:
            _spool_result(older, workspace, "old-result")
            _spool_result(newer, workspace, "new-result")
            result = _compare_rows(
                _result_signatures(workspace, "old-result"), _result_signatures(workspace, "new-result"), limit,
            )
    def execution(view: InspectionView) -> dict[str, Any] | None:
        profile = view.execution_profile
        if profile is None:
            return None
        return {
            "profile": profile.to_dict(),
            "worker": view._controls.load(profile.worker_composition),
            "scheduler": view._controls.load(profile.scheduler_configuration),
        }
    return {
        "format": "docspec-inspection-comparison", "formatVersion": "1.0",
        "olderPhase": older.phase, "newerPhase": newer.phase,
        "configurationChanges": _changes(older.plan.identity_content(), newer.plan.identity_content()),
        "executionConfigurationChanges": _changes(execution(older), execution(newer)),
        "work": work, "result": result,
        "resultUnavailable": None if result is not None else "both sides must have a complete active result",
        "interpretation": {
            "inputDigest": "source item version, candidates, state, and metadata",
            "contentDigest": "bytes, values, coordinates, and recorded input associations; an input ID change counts even when values agree",
            "configurationDigest": "each source's recorded requested stage identities and effective configuration pins",
            "outcomeDigest": "recorded dispositions, warnings, and failures",
            "provenanceDigest": "exact rows including receipt and delivery identities",
            "causation": "differences are recorded evidence; semantic quality and unrecorded external causes are unavailable",
        },
    }
