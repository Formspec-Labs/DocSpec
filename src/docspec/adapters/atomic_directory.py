"""Dependency-light atomic publication for immutable local paths."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
import tempfile
from pathlib import Path

from docspec.errors import IntegrityError, StateTransitionError


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
    """Atomically publish ``source`` while refusing every replacement race."""

    source = Path(source)
    destination = Path(destination)
    if not source.is_dir() or source.is_symlink():
        raise IntegrityError("immutable publication source must be a regular directory")
    if destination.exists() or destination.is_symlink():
        raise IntegrityError(f"refusing to replace immutable distribution: {destination}")
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise IntegrityError("immutable publication parent must be a regular directory")

    lock_path = destination.parent / f".{destination.name}.publish.lock"
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise StateTransitionError(
            f"publication is already in progress for {destination.name}"
        ) from error
    try:
        with os.fdopen(descriptor, "wb") as lock:
            lock.write(source.name.encode("utf-8"))
            lock.flush()
            os.fsync(lock.fileno())
        if destination.exists() or destination.is_symlink():
            raise IntegrityError(f"refusing to replace immutable distribution: {destination}")
        for directory in sorted(
            (path for path in source.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            sync_directory(directory)
        sync_directory(source)
        _rename_directory_no_replace(source, destination)
        sync_directory(destination.parent)
    finally:
        lock_path.unlink(missing_ok=True)


def sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_directory_no_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        try:
            os.rename(source, destination)
        except FileExistsError as error:
            raise IntegrityError(
                f"refusing to replace immutable distribution: {destination}"
            ) from error
        return

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(source_bytes, destination_bytes, 0x4)
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        result = rename(-100, source_bytes, -100, destination_bytes, 1)
    else:
        raise IntegrityError("this platform lacks an atomic no-replace directory rename")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise IntegrityError(f"refusing to replace immutable distribution: {destination}")
    raise OSError(error_number, os.strerror(error_number), destination)


__all__ = ["publish_directory_no_replace", "sync_directory", "write_file_no_replace"]
