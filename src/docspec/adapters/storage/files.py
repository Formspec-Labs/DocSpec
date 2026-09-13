"""Local files: contained paths and immutable file publication."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from docspec.adapters.atomic_directory import publish_directory_no_replace
from docspec.domain.identity import (
    parse_canonical_json,
    require_relative_path,
    require_sha256,
    sha256_digest,
    thaw_json,
)
from docspec.domain.references import ArtifactRef
from docspec.errors import IntegrityError, LimitExceededError

_FILE_CHUNK_BYTES = 1024 * 1024


def _storage_root(path: Path, *, create: bool = True) -> Path:
    path = Path(path)
    if path.is_symlink():
        raise IntegrityError(f"storage root must not be a symlink: {path}")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    elif not path.is_dir():
        raise IntegrityError(f"storage root must be an existing directory: {path}")
    if path.is_symlink():
        raise IntegrityError(f"storage root must not be a symlink: {path}")
    return path.resolve(strict=True)


def _contained(root: Path, locator: str, *, create_parents: bool = False) -> Path:
    relative = require_relative_path(locator, "locator")
    parts = PurePosixPath(relative).parts
    cursor = root
    for part in parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise IntegrityError(f"storage locator traverses a symlink: {relative}")
        if create_parents:
            cursor.mkdir(exist_ok=True)
        if cursor.exists() and (not cursor.is_dir() or cursor.is_symlink()):
            raise IntegrityError(f"storage locator parent is not a directory: {relative}")
    candidate = root.joinpath(*parts)
    try:
        candidate.parent.resolve(strict=False).relative_to(root)
    except ValueError as error:
        raise IntegrityError(f"storage locator escapes its root: {relative}") from error
    if candidate.is_symlink():
        raise IntegrityError(f"storage locator is a symlink: {relative}")
    return candidate


def _read_exact(root: Path, locator: str, *, max_bytes: int | None = None) -> bytes:
    path = _contained(root, locator)
    if not path.is_file() or path.is_symlink():
        raise IntegrityError(f"storage member is missing or not a regular file: {locator}")
    if max_bytes is None:
        return path.read_bytes()
    if path.stat().st_size > max_bytes:
        raise LimitExceededError(f"storage member exceeds the {max_bytes}-byte limit")
    with path.open("rb") as stream:
        payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise LimitExceededError(f"storage member exceeds the {max_bytes}-byte limit")
    return payload


def _write_once(
    root: Path,
    locator: str,
    payload: bytes,
    *,
    staging_locator: str | None = None,
) -> Path:
    path = _contained(root, locator, create_parents=True)
    staging = (
        path.parent
        if staging_locator is None
        else _contained(root, f"{staging_locator}/placeholder", create_parents=True).parent
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=staging,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise IntegrityError(f"refusing to replace conflicting immutable member: {locator}") from None
        return path
    finally:
        temporary.unlink(missing_ok=True)


def publish_directory_exclusive(root: Path, working: Path, locator: str) -> Path:
    """Atomically publish one directory without replacing an existing result."""

    relative = PurePosixPath(require_relative_path(locator))
    destination = _contained(root, relative.as_posix())
    parent_probe = (relative.parent / "placeholder").as_posix()
    _contained(root, parent_probe, create_parents=True)
    publish_directory_no_replace(working, destination)
    return destination


def _verify_artifact_bytes(reference: ArtifactRef, payload: bytes, *, media_type: str = "application/json") -> None:
    if reference.media_type != media_type:
        raise IntegrityError(f"artifact has unexpected media type: {reference.media_type}")
    if len(payload) != reference.byte_size or sha256_digest(payload) != reference.digest:
        raise IntegrityError("artifact bytes differ from their immutable reference")


def sha256_file(path: Path, *, chunk_size: int = _FILE_CHUNK_BYTES) -> tuple[str, int]:
    """Hash one regular file in bounded memory and return digest plus size.

    Public because it is the repository's one file digest. A caller that wants
    only the digest takes the first element; a second loop that reads the same
    bytes to produce the same string is not a second implementation, it is a
    second thing to keep correct.
    """

    if chunk_size <= 0:
        raise ValueError("file hash chunk_size must be positive")
    digest = hashlib.sha256()
    byte_size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            byte_size += len(chunk)
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}", byte_size


def _sync_file(path: Path) -> None:
    """Flush a previously closed staging file before immutable publication."""

    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verified_member_path(
    root: Path,
    member: Mapping[str, Any],
    *,
    media_type: str,
    schema_id: str | None = None,
    extra_fields: frozenset[str] = frozenset(),
) -> Path:
    expected = {"path", "mediaType", "byteSize", "digest", "recordCount"}
    if schema_id is not None:
        expected.add("schemaId")
    expected.update(extra_fields)
    if set(member) != expected:
        raise IntegrityError("distribution member has an invalid closed shape")
    if member["mediaType"] != media_type or (schema_id is not None and member["schemaId"] != schema_id):
        raise IntegrityError("distribution member has an unexpected media type or schema")
    if any(
        not isinstance(member[name], int) or isinstance(member[name], bool) or member[name] < 0
        for name in ("byteSize", "recordCount")
    ):
        raise IntegrityError("distribution member counts must be non-negative integers")
    require_sha256(member["digest"], "member digest")
    require_relative_path(member["path"], "member path")
    path = _contained(root, member["path"])
    if not path.is_file() or path.is_symlink() or path.stat().st_size != member["byteSize"]:
        raise IntegrityError("distribution member size or storage type differs from its description")
    digest, byte_size = sha256_file(path)
    if byte_size != member["byteSize"] or digest != member["digest"]:
        raise IntegrityError("distribution member bytes differ from their description")
    return path


def _iter_canonical_json_lines(
    path: Path,
    *,
    label: str,
    max_line_bytes: int = 8 * 1024**2,
) -> Iterator[dict[str, Any]]:
    if max_line_bytes <= 0:
        raise ValueError("max_line_bytes must be positive")
    with path.open("rb") as handle:
        number = 0
        while complete_line := handle.readline(max_line_bytes + 2):
            number += 1
            if len(complete_line) > max_line_bytes + 1:
                raise LimitExceededError(f"{label} line exceeds the {max_line_bytes}-byte limit")
            if not complete_line.endswith(b"\n"):
                raise IntegrityError(f"{label} must end at a complete JSON line")
            line = complete_line[:-1]
            if not line:
                raise IntegrityError(f"{label} contains an empty JSON line")
            value = thaw_json(parse_canonical_json(line, label=f"{label} line {number}", file_form=False))
            if not isinstance(value, dict):
                raise IntegrityError(f"{label} line {number} must be a JSON object")
            yield value
