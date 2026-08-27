"""Dependency-light atomic publication for immutable local paths."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from rulespec_artifacts import (
    ArtifactVerificationError,
    MemberSourceError,
    publish_directory_no_replace as publish_artifact_directory_no_replace,
)

from docspec.errors import IntegrityError


def write_file_no_replace(destination: Path, payload: bytes) -> None:
    """Publish one complete file atomically while refusing replacement."""

    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise IntegrityError(f"refusing to replace immutable file: {destination}")
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise IntegrityError("immutable file publication parent must be a regular directory")
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise IntegrityError(f"refusing to replace immutable file: {destination}") from error
        sync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def publish_directory_no_replace(source: Path, destination: Path) -> None:
    """Publish through the shared product-neutral immutable primitive."""

    try:
        publish_artifact_directory_no_replace(source, destination)
    except (FileExistsError, BlockingIOError) as error:
        raise IntegrityError(
            f"refusing to replace immutable distribution: {destination}"
        ) from error
    except (ArtifactVerificationError, MemberSourceError, OSError, ValueError) as error:
        raise IntegrityError(
            f"immutable distribution publication failed for {destination}: {error}"
        ) from error


def sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["publish_directory_no_replace", "sync_directory", "write_file_no_replace"]
