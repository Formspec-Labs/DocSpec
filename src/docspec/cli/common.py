"""DocSpec command common: artifact receipts and shared request validation."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from rulespec_artifacts import ArtifactVerificationError, Producer

from docspec.adapters.storage import (
    LocalJsonControlRepository,
)
from docspec.cli_io import (
    MAX_JSON_BYTES as _MAX_JSON_BYTES,
)
from docspec.cli_io import (
    CliError,
    read_object,
)
from docspec.cli_io import (
    existing_root as _existing_root,
)
from docspec.cli_io import (
    read_bytes as _read_bytes,
)
from docspec.cli_io import (
    read_object as _read_json_object,
)
from docspec.domain.identity import (
    canonical_json_file_bytes,
    parse_canonical_json,
    sha256_digest,
    stable_urn,
    thaw_json,
)
from docspec.domain.references import ArtifactRef, DocumentReleaseRef
from docspec.domain.release import DocumentRelease
from docspec.errors import DocSpecError
from docspec.profile_registry import ProfileRegistry, RegisteredProfile


def _producer_record(value: object, *, label: str) -> Producer:
    try:
        return Producer.from_dict(value, path=f"$/{label}")
    except ArtifactVerificationError as error:
        raise CliError(f"{label} is invalid: {error}") from error


def _document_release_producer(
    implementation_id: str,
    verifier_implementation_id: str,
) -> Producer:
    return _producer_record(
        {
            "product": "docspec",
            "implementationId": implementation_id,
            "verifierId": "urn:docspec:verifier:document-release",
            "verifierVersion": "1.0.0",
            "verifierImplementationId": verifier_implementation_id,
        },
        label="document-release producer",
    )


def _read_canonical_object(path: Path, *, label: str) -> dict[str, Any]:
    return read_object(path, label=label, canonical=True)


def _write_new(path: Path, payload: bytes, *, label: str) -> None:
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise CliError(f"refusing to replace existing {label}: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise CliError(f"refusing to replace existing {label}: {path}") from error
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _write_failure_receipt(args: argparse.Namespace, error: Exception) -> None:
    """Best-effort write-once failure evidence for mutating operator commands."""

    receipt_value = getattr(args, "receipt", None)
    if receipt_value is None:
        return
    receipt_path = Path(receipt_value)
    destination_value = getattr(args, "destination", None)
    if destination_value is not None:
        destination_path = Path(destination_value).resolve(strict=False)
        resolved_receipt = receipt_path.resolve(strict=False)
        if (
            destination_path == resolved_receipt
            or destination_path in resolved_receipt.parents
            or resolved_receipt in destination_path.parents
        ):
            return
    if receipt_path.exists() or receipt_path.is_symlink():
        return
    request_digest: str | None = None
    request_value = getattr(args, "request", None)
    if request_value is not None:
        request_path = Path(request_value)
        try:
            request_digest = sha256_digest(_read_bytes(request_path, label="failed operation request"))
        except (DocSpecError, OSError):
            # Failure evidence must not mask the original refusal if its input
            # disappeared, became invalid, or grew beyond the input limit.
            pass
    content = {
        "operation": getattr(args, "operation", getattr(args, "command", "unknown")),
        "requestDigest": request_digest,
        "errorType": type(error).__name__,
        "diagnosticCode": f"DOCSPEC-CLI-{type(error).__name__.upper()}",
        "verdict": "failed",
    }
    receipt = {
        "format": "docspec-operation-failure-receipt",
        "formatVersion": "1.0",
        "receiptId": stable_urn("operation-failure-receipt", content),
        **content,
    }
    try:
        _write_new(
            receipt_path,
            canonical_json_file_bytes(receipt),
            label="operation failure receipt",
        )
    except (DocSpecError, OSError):
        return


def _require_new_output_paths(destination: Path, receipt_path: Path) -> None:
    destination = Path(destination)
    receipt_path = Path(receipt_path)
    if destination.resolve(strict=False) == receipt_path.resolve(strict=False):
        raise CliError("artifact destination and receipt path must differ")
    for path, label in ((destination, "artifact"), (receipt_path, "operation receipt")):
        if path.exists() or path.is_symlink():
            raise CliError(f"refusing to replace existing {label}: {path}")


def _artifact_receipt(
    *,
    operation: str,
    request_digest: str,
    artifact_id: str,
    destination: Path,
    payload: bytes,
) -> dict[str, Any]:
    content = {
        "operation": operation,
        "requestDigest": request_digest,
        "artifact": {
            "artifactId": artifact_id,
            "locator": destination.resolve(strict=False).as_posix(),
            "digest": sha256_digest(payload),
            "mediaType": "application/json",
            "byteSize": len(payload),
        },
        "verdict": "completed",
    }
    return {
        "format": "docspec-operation-receipt",
        "formatVersion": "1.0",
        "receiptId": stable_urn("operation-receipt", content),
        **content,
    }


def _write_artifact_and_receipt(
    *,
    operation: str,
    request_path: Path,
    destination: Path,
    receipt_path: Path,
    artifact_id: str,
    payload: bytes,
) -> dict[str, Any]:
    destination = Path(destination)
    receipt_path = Path(receipt_path)
    _require_new_output_paths(destination, receipt_path)
    request_digest = sha256_digest(_read_bytes(request_path, label="operation request"))
    receipt = _artifact_receipt(
        operation=operation,
        request_digest=request_digest,
        artifact_id=artifact_id,
        destination=destination,
        payload=payload,
    )
    _write_new(destination, payload, label="artifact")
    _write_new(receipt_path, canonical_json_file_bytes(receipt), label="operation receipt")
    return receipt


def _registered_profile(path: Path) -> RegisteredProfile:
    path = Path(path)
    _read_bytes(path, label="storage profile")
    return ProfileRegistry.from_file(path)


def _release_reference(path: Path) -> DocumentReleaseRef:
    return DocumentReleaseRef.from_dict(_read_json_object(path, label="document release reference"))


def _load_receipt_value(
    path: Path,
    *,
    control_root: Path | None,
    label: str,
    parser: Any,
    inline_format: str,
) -> Any:
    """Read a receipt directly or resolve the ArtifactRef emitted by a lifecycle command."""

    value = _read_canonical_object(path, label=label)
    if value.get("format") == inline_format:
        return parser(value)
    try:
        reference = ArtifactRef.from_dict(value)
    except (TypeError, ValueError) as error:
        raise CliError(f"{label} is neither an inline receipt nor an ArtifactRef: {error}") from error
    if control_root is None:
        raise CliError(f"{label} ArtifactRef requires --control-root")
    controls = object.__new__(LocalJsonControlRepository)
    controls.root = _existing_root(control_root, label="control repository root")
    controls.max_artifact_bytes = _MAX_JSON_BYTES
    controls.verify(reference)
    return parser(controls.load(reference))


def _load_release(path: Path) -> tuple[DocumentRelease, bytes]:
    payload = _read_bytes(path, label="document release")
    value = thaw_json(parse_canonical_json(payload, label="document release"))
    if not isinstance(value, dict):
        raise CliError("document release must be a JSON object")
    return DocumentRelease.from_dict(value), payload
