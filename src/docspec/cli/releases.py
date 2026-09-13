"""DocSpec command releases: release publication and inspection."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from docspec.application.commit import ReleaseCommitService
from docspec.application.maintenance import ReleaseCompactionService
from docspec.cli.common import (
    _load_release,
    _read_canonical_object,
    _require_new_output_paths,
    _write_artifact_and_receipt,
)
from docspec.cli.requests import _absolute_request_path, _local_storage_for_run_request
from docspec.cli_io import (
    CliError,
)
from docspec.cli_io import (
    emit as _emit,
)
from docspec.cli_io import (
    read_object as _read_json_object,
)
from docspec.domain.identity import (
    canonical_json_file_bytes,
    sha256_digest,
)
from docspec.domain.maintenance import ReleaseCompactionReceipt
from docspec.domain.plans import ProcessingPlan
from docspec.domain.references import ArtifactRef, DocumentReleaseRef


def _cmd_document_release_save(args: argparse.Namespace) -> int:
    _require_new_output_paths(args.destination, args.receipt)
    action = args.document_release_command
    label = f"document release {action} request"
    value = _read_json_object(args.request, label=label)
    fields = {"format", "formatVersion", "runRequest", "runReceipt", "baseRelease"}
    if set(value) != fields:
        raise CliError(f"{label} has an invalid closed shape")
    if value["format"] != f"docspec-local-release-{action}-request" or value["formatVersion"] != "1.0":
        raise CliError(f"{label} has an unknown format")
    run_request_path = _absolute_request_path(value["runRequest"], label="local run request")
    run_receipt_path = _absolute_request_path(value["runReceipt"], label="run receipt reference")
    with _local_storage_for_run_request(run_request_path) as (_, plan, controls, _, records, _, catalog):
        base_release = None if value["baseRelease"] is None else DocumentReleaseRef.from_dict(value["baseRelease"])
        if base_release != plan.base_release:
            raise CliError("release base differs from the processing plan")
        plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
        run_receipt = ArtifactRef.from_dict(_read_canonical_object(run_receipt_path, label="run receipt reference"))
        service = ReleaseCommitService(
            plan_ref=plan_ref,
            controls=controls,
            records=records,
            document_catalog=catalog,
        )
        save = service.retain_release if action == "retain" else service.commit_release
        reference = save(base_release, run_receipt)
    receipt = _write_artifact_and_receipt(
        operation=args.operation,
        request_path=args.request,
        destination=args.destination,
        receipt_path=args.receipt,
        artifact_id=reference.release_id,
        payload=canonical_json_file_bytes(reference.to_dict()),
    )
    _emit(receipt)
    return 0


def _local_release_compaction_request(path: Path) -> tuple[Path, DocumentReleaseRef]:
    value = _read_json_object(path, label="local release compaction request")
    fields = {"format", "formatVersion", "runRequest", "sourceRelease"}
    if set(value) != fields:
        raise CliError("local release compaction request has an invalid closed shape")
    if value["format"] != "docspec-local-release-compaction-request" or value["formatVersion"] != "1.0":
        raise CliError("local release compaction request has an unknown format")
    return (
        _absolute_request_path(value["runRequest"], label="local run request"),
        DocumentReleaseRef.from_dict(value["sourceRelease"]),
    )


def _cmd_document_release_compact(args: argparse.Namespace) -> int:
    _require_new_output_paths(args.destination, args.receipt)
    run_request_path, source_reference = _local_release_compaction_request(args.request)
    with _local_storage_for_run_request(run_request_path) as (
        request, plan, controls, stores, records, _, catalog,
    ):
        source = catalog.open(source_reference)
        try:
            source_plan = ProcessingPlan.from_dict(controls.load(source.processing_plan))
        except (TypeError, ValueError) as error:
            raise CliError(f"source release processing plan is invalid: {error}") from error
        if source_plan != plan or source.profiles != plan.profiles:
            raise CliError("compaction run composition differs from the source release")

        reference = ReleaseCompactionService(
            controls=controls,
            records=records,
            stores=stores,
            document_catalog=catalog,
            clock=lambda: request["completedAt"],
        ).compact(source_reference)
        compaction = ReleaseCompactionReceipt.from_dict(controls.load(reference))
        if compaction.receipt_id != reference.artifact_id or compaction.source_release != source_reference:
            raise CliError("saved compaction receipt differs from its immutable reference")
    receipt = _write_artifact_and_receipt(
        operation="document-release.compact",
        request_path=args.request,
        destination=args.destination,
        receipt_path=args.receipt,
        artifact_id=reference.artifact_id,
        payload=canonical_json_file_bytes(reference.to_dict()),
    )
    _emit(receipt)
    return 0


def _cmd_document_release_verify(args: argparse.Namespace) -> int:
    release, payload = _load_release(args.release)
    _emit(
        {
            "format": "docspec-document-release-verification",
            "formatVersion": "1.0",
            "releaseId": release.release_id,
            "artifactDigest": sha256_digest(payload),
            "logicalStateDigest": release.logical_state_digest,
            "activeLayerCount": len(release.active_layers),
            "blobRootCount": len(release.blob_roots),
            "verificationScope": "canonical-release-root",
            "verdict": "structurally-valid",
        }
    )
    return 0


def _cmd_document_release_diff(args: argparse.Namespace) -> int:
    older, _ = _load_release(args.older)
    newer, _ = _load_release(args.newer)
    old_layers = {item.layer_kind: item for item in older.active_layers}
    new_layers = {item.layer_kind: item for item in newer.active_layers}
    layer_changes: list[dict[str, Any]] = []
    for kind in sorted(set(old_layers) | set(new_layers)):
        old = old_layers.get(kind)
        new = new_layers.get(kind)
        change = "unchanged"
        if old is None:
            change = "added"
        elif new is None:
            change = "deleted"
        elif old.to_dict() != new.to_dict():
            change = "changed"
        layer_changes.append(
            {
                "layerKind": kind,
                "change": change,
                "older": None if old is None else old.to_dict(),
                "newer": None if new is None else new.to_dict(),
            }
        )
    count_deltas = {
        name: newer.counts.get(name, 0) - older.counts.get(name, 0)
        for name in sorted(set(older.counts) | set(newer.counts))
    }
    _emit(
        {
            "format": "docspec-document-release-diff",
            "formatVersion": "1.0",
            "olderReleaseId": older.release_id,
            "newerReleaseId": newer.release_id,
            "logicalStateEqual": older.logical_state_digest == newer.logical_state_digest,
            "layerChanges": layer_changes,
            "countDeltas": count_deltas,
            "declaredPreviousRelease": None
            if newer.previous_release is None
            else newer.previous_release.to_dict(),
            "verdict": "pass",
        }
    )
    return 0
