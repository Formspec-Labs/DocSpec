"""Local controls: small canonical control artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from docspec.adapters.storage.files import _read_exact, _storage_root, _verify_artifact_bytes, _write_once
from docspec.domain.identity import (
    canonical_json_file_bytes,
    parse_canonical_json,
    require_relative_path,
    require_text,
    sha256_digest,
    thaw_json,
)
from docspec.domain.references import ArtifactRef
from docspec.errors import IntegrityError, LimitExceededError


class LocalJsonControlRepository:
    """Persist small closed control artifacts as immutable canonical JSON."""

    def __init__(self, root: Path, *, max_artifact_bytes: int = 8 * 1024**2, create: bool = True) -> None:
        if max_artifact_bytes <= 0:
            raise ValueError("max_artifact_bytes must be positive")
        self.root = _storage_root(root, create=create)
        self.max_artifact_bytes = max_artifact_bytes

    def put(self, *, kind: str, artifact_id: str, value: Mapping[str, Any]) -> ArtifactRef:
        if "/" in kind or kind in {".", ".."}:
            raise ValueError("control artifact kind must be one path segment")
        require_text(kind, "control artifact kind")
        require_text(artifact_id, "control artifact_id")
        payload = canonical_json_file_bytes(
            {
                "format": "docspec-control-artifact",
                "formatVersion": "1.0",
                "kind": kind,
                "artifactId": artifact_id,
                "value": value,
            }
        )
        if len(payload) > self.max_artifact_bytes:
            raise LimitExceededError(f"control artifact exceeds the {self.max_artifact_bytes}-byte limit")
        digest = sha256_digest(payload)
        hexadecimal = digest.removeprefix("sha256:")
        locator = f"control/{kind}/{hexadecimal[:2]}/{hexadecimal}.json"
        _write_once(self.root, locator, payload)
        return ArtifactRef(artifact_id, locator, digest, "application/json", len(payload))

    def load(self, reference: ArtifactRef) -> dict[str, Any]:
        if reference.byte_size > self.max_artifact_bytes:
            raise LimitExceededError(f"control artifact exceeds the {self.max_artifact_bytes}-byte limit")
        payload = _read_exact(self.root, reference.locator, max_bytes=self.max_artifact_bytes)
        _verify_artifact_bytes(reference, payload)
        root = thaw_json(parse_canonical_json(payload, label=reference.artifact_id))
        expected = {"format", "formatVersion", "kind", "artifactId", "value"}
        if not isinstance(root, dict) or set(root) != expected:
            raise IntegrityError("control artifact root must be a JSON object")
        if root["format"] != "docspec-control-artifact" or root["formatVersion"] != "1.0":
            raise IntegrityError("control artifact root has an unknown format")
        if root["artifactId"] != reference.artifact_id:
            raise IntegrityError("control artifact identity differs from its reference")
        locator = PurePosixPath(require_relative_path(reference.locator, "control artifact locator"))
        if len(locator.parts) != 4 or locator.parts[:1] != ("control",) or locator.parts[1] != root["kind"]:
            raise IntegrityError("control artifact kind differs from its locator")
        value = root["value"]
        if not isinstance(value, dict):
            raise IntegrityError("control artifact value must be a JSON object")
        return value

    def verify(self, reference: ArtifactRef) -> None:
        self.load(reference)
