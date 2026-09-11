"""Check source captures and their text representations."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from docspec.adapters.document_release.body_index import TextBodyIndex, _indexed_bytes
from docspec.adapters.document_release.diagnostics import VerificationIssue, _issue


def _validate_capture(
    capture: Mapping[str, Any],
    member_paths: Mapping[str, Path],
    index: TextBodyIndex,
    body_id: Any,
    path: str,
    issues: list[VerificationIssue],
) -> None:
    """Check one captured rendition against the member bytes it names."""

    resolved = member_paths.get(str(capture.get("objectKey")))
    if resolved is None or not resolved.is_file():
        _issue(
            issues,
            "invalid.capture",
            f"{path}/objectKey",
            "captured rendition is not a declared member",
        )
    else:
        blob = _indexed_bytes(
            resolved,
            index,
            "blob",
            body_id,
            member=capture.get("objectKey"),
            digest=capture.get("sha256"),
        )
        actual = hashlib.sha256(blob).hexdigest()
        if actual != capture.get("sha256"):
            _issue(issues, "invalid.capture", f"{path}/sha256", f"captured bytes digest to {actual}")
        if len(blob) != capture.get("byteSize"):
            _issue(issues, "invalid.capture", f"{path}/byteSize", "captured byte size differs")
    expected = capture.get("expectedSha256")
    if isinstance(expected, str) and expected.removeprefix("sha256:") != capture.get("sha256"):
        _issue(
            issues,
            "invalid.capture",
            f"{path}/expectedSha256",
            "the catalog's expected digest does not match the captured bytes",
        )


def _validate_representation(
    representation: Mapping[str, Any],
    member_paths: Mapping[str, Path],
    index: TextBodyIndex,
    body_id: Any,
    path: str,
    issues: list[VerificationIssue],
) -> int | None:
    """Check one selected representation and return its byte length."""

    resolved = member_paths.get(str(representation.get("objectKey")))
    if resolved is None or not resolved.is_file():
        _issue(
            issues,
            "invalid.representation",
            f"{path}/objectKey",
            "representation is not a declared member",
        )
        return None
    raw = _indexed_bytes(
        resolved,
        index,
        "text",
        body_id,
        member=representation.get("objectKey"),
        digest=representation.get("sha256"),
    )
    if hashlib.sha256(raw).hexdigest() != representation.get("sha256"):
        _issue(issues, "invalid.representation", f"{path}/sha256", "representation digest differs")
    if len(raw) != representation.get("byteSize"):
        _issue(
            issues,
            "invalid.representation",
            f"{path}/byteSize",
            f"representation is {len(raw)} bytes",
        )
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        _issue(
            issues,
            "invalid.representation",
            path,
            f"representation is not valid UTF-8: {exc}",
        )
    return len(raw)
