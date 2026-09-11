"""Descriptor-pinned filesystem operations for local source catalogs."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from rulespec_artifacts import (
    ArtifactVerificationError,
    MemberSourceError,
    move_child_directory_no_replace,
    publish_child_directory_no_replace,
)

from docspec.errors import IntegrityError

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_IDENTITY = tuple[int, int]


@dataclass(frozen=True)
class _PinnedDirectory:
    path: Path
    descriptor: int
    identity: _IDENTITY


def _pin_directory(
    path: Path | str,
    *,
    label: str,
    parent: _PinnedDirectory | None = None,
    create: bool = False,
    parents: bool = False,
    missing_ok: bool = False,
    expected_identity: _IDENTITY | None = None,
    error_type: type[Exception] = IntegrityError,
) -> _PinnedDirectory | None:
    """Open one real directory, using a pinned parent for every internal child."""

    selected = Path(path)
    if parent is not None and (
        len(selected.parts) != 1 or selected.name in {"", ".", ".."}
    ):
        raise ValueError("pinned directory children must use one safe path component")
    selected_path = selected if parent is None else parent.path / selected.name
    open_path: str | Path = selected_path if parent is None else selected.name
    directory_fd = None if parent is None else parent.descriptor
    if create:
        try:
            if parent is None:
                selected_path.mkdir(parents=parents)
            else:
                os.mkdir(selected.name, dir_fd=directory_fd)
        except FileExistsError:
            pass
        except OSError as error:
            raise error_type(f"{label} cannot be created safely: {error}") from error
    try:
        descriptor = os.open(open_path, _DIRECTORY_FLAGS, dir_fd=directory_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise error_type(f"{label} is missing") from None
    except OSError as error:
        raise error_type(f"{label} must be a non-symlink directory") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise error_type(f"{label} must be a non-symlink directory")
        identity = (metadata.st_dev, metadata.st_ino)
        if expected_identity is not None and identity != expected_identity:
            raise error_type(f"{label} changed since admission")
        if parent is None:
            try:
                named = selected_path.lstat()
                resolved = selected_path.resolve(strict=True)
            except OSError as error:
                raise error_type(f"{label} cannot be resolved safely: {error}") from error
            if stat.S_ISLNK(named.st_mode) or (named.st_dev, named.st_ino) != identity:
                raise error_type(f"{label} changed during admission")
            selected_path = resolved
        return _PinnedDirectory(selected_path, descriptor, identity)
    except BaseException:
        os.close(descriptor)
        raise


def _create_random_directory(
    parent: _PinnedDirectory,
    *,
    prefix: str,
    label: str,
) -> tuple[str, _PinnedDirectory]:
    for _ in range(128):
        name = f"{prefix}{secrets.token_hex(16)}"
        try:
            os.mkdir(name, 0o700, dir_fd=parent.descriptor)
        except FileExistsError:
            continue
        except OSError as error:
            raise IntegrityError(f"{label} cannot be created safely: {error}") from error
        selected = _pin_directory(name, label=label, parent=parent)
        assert selected is not None
        return name, selected
    raise IntegrityError(f"{label} cannot allocate a unique local name")


def _create_random_file(directory: _PinnedDirectory, *, prefix: str) -> tuple[str, int]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _ in range(128):
        name = f"{prefix}{secrets.token_hex(16)}"
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=directory.descriptor)
        except FileExistsError:
            continue
        return name, descriptor
    raise IntegrityError("source-catalog pending blob cannot allocate a unique local name")


def _sync_directory_descriptor(directory: _PinnedDirectory) -> None:
    if os.name != "nt":
        os.fsync(directory.descriptor)


def _cleanup_session_at(
    staging_root: _PinnedDirectory,
    session_name: str,
    session_identity: _IDENTITY,
) -> None:
    try:
        metadata = os.stat(
            session_name,
            dir_fd=staging_root.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError as error:
        raise IntegrityError("source-catalog staging session changed before cleanup") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != session_identity
    ):
        raise IntegrityError("source-catalog staging session changed before cleanup")
    tombstone = f".cleanup-{secrets.token_hex(16)}"
    try:
        move_child_directory_no_replace(
            staging_root.descriptor,
            staging_root.descriptor,
            session_name,
            tombstone,
            expected_source_identity=session_identity,
        )
    except (ArtifactVerificationError, MemberSourceError, OSError, ValueError) as error:
        raise IntegrityError(
            f"source-catalog staging session cannot move to cleanup: {error}"
        ) from error
    moved = _pin_directory(
        tombstone,
        label="source-catalog cleanup tombstone",
        parent=staging_root,
        expected_identity=session_identity,
    )
    assert moved is not None
    try:
        _clear_directory_contents_at(moved)
        _require_child_identity(
            staging_root,
            tombstone,
            moved,
            label="source-catalog cleanup tombstone",
        )
        os.rmdir(tombstone, dir_fd=staging_root.descriptor)
        _sync_directory_descriptor(staging_root)
    finally:
        os.close(moved.descriptor)


def _clear_directory_contents_at(directory: _PinnedDirectory) -> None:
    """Clear only the directory represented by ``directory.descriptor``.

    Every child directory stays open until its named entry is checked and
    removed. If a same-name replacement appears, cleanup refuses it instead of
    following or recursively deleting it.
    """

    for name in os.listdir(directory.descriptor):
        try:
            metadata = os.stat(
                name,
                dir_fd=directory.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        identity = (metadata.st_dev, metadata.st_ino)
        if stat.S_ISDIR(metadata.st_mode):
            child = _pin_directory(
                name,
                label="source-catalog cleanup child",
                parent=directory,
                expected_identity=identity,
            )
            assert child is not None
            try:
                _clear_directory_contents_at(child)
                _require_child_identity(
                    directory,
                    name,
                    child,
                    label="source-catalog cleanup child",
                )
                os.rmdir(name, dir_fd=directory.descriptor)
            finally:
                os.close(child.descriptor)
            continue
        try:
            current = os.stat(
                name,
                dir_fd=directory.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        if (current.st_dev, current.st_ino) != identity:
            raise IntegrityError("source-catalog cleanup entry changed during use")
        try:
            os.unlink(name, dir_fd=directory.descriptor)
        except IsADirectoryError as error:
            raise IntegrityError("source-catalog cleanup entry changed during use") from error
    _sync_directory_descriptor(directory)


def _publish_directory_no_replace_at(
    source_parent: _PinnedDirectory,
    source: _PinnedDirectory,
    source_name: str,
    destination_parent: _PinnedDirectory,
    destination_name: str,
) -> None:
    _require_child_identity(
        source_parent,
        source_name,
        source,
        label="source-catalog artifact staging root",
    )
    try:
        publish_child_directory_no_replace(
            source_parent.descriptor,
            destination_parent.descriptor,
            source_name,
            destination_name,
            expected_source_identity=source.identity,
            wait_for_lock=True,
        )
    except FileExistsError as error:
        raise IntegrityError(
            f"refusing to replace immutable source-catalog directory: {destination_name}"
        ) from error
    except (ArtifactVerificationError, MemberSourceError, OSError, ValueError) as error:
        raise IntegrityError(
            f"source-catalog directory cannot publish safely: {error}"
        ) from error
    published = os.stat(
        destination_name,
        dir_fd=destination_parent.descriptor,
        follow_symlinks=False,
    )
    if (published.st_dev, published.st_ino) != source.identity:
        raise IntegrityError("source-catalog artifact changed during publication")


def _entry_exists(directory: _PinnedDirectory, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _require_child_identity(
    parent: _PinnedDirectory,
    name: str,
    child: _PinnedDirectory,
    *,
    label: str,
) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
    except OSError as error:
        raise IntegrityError(f"{label} changed during use") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != child.identity
    ):
        raise IntegrityError(f"{label} changed during use")


def _object_parts(object_key: str) -> tuple[str, ...]:
    key = PurePosixPath(object_key)
    if key.is_absolute() or not key.parts or any(part in {"", ".", ".."} for part in key.parts):
        raise ValueError("source-catalog object key must be a contained relative path")
    if "\\" in object_key:
        raise ValueError("source-catalog object key must use portable separators")
    return key.parts


def _duplicate_directory(directory: _PinnedDirectory) -> _PinnedDirectory:
    descriptor = os.open(".", _DIRECTORY_FLAGS, dir_fd=directory.descriptor)
    metadata = os.fstat(descriptor)
    identity = (metadata.st_dev, metadata.st_ino)
    if identity != directory.identity:
        os.close(descriptor)
        raise IntegrityError("pinned source-catalog directory changed identity")
    return _PinnedDirectory(directory.path, descriptor, identity)


def _member_parent(
    root: _PinnedDirectory,
    object_key: str,
    *,
    create: bool,
) -> tuple[_PinnedDirectory, str]:
    parts = _object_parts(object_key)
    current = _duplicate_directory(root)
    try:
        for part in parts[:-1]:
            child = _pin_directory(
                part,
                label="source-catalog artifact member parent",
                parent=current,
                create=create,
            )
            assert child is not None
            os.close(current.descriptor)
            current = child
        return current, parts[-1]
    except BaseException:
        os.close(current.descriptor)
        raise


def _open_member_at(root: _PinnedDirectory, object_key: str) -> int:
    parent, name = _member_parent(root, object_key, create=False)
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=parent.descriptor)
    finally:
        os.close(parent.descriptor)
    try:
        metadata = os.fstat(descriptor)
        if stat.S_ISREG(metadata.st_mode):
            return descriptor
    except BaseException:
        os.close(descriptor)
        raise
    os.close(descriptor)
    raise IntegrityError(f"source-catalog member is not a regular file: {object_key}")


def _verify_blob_at(
    directory: _PinnedDirectory,
    name: str,
    *,
    blob_ref: str,
    byte_size: int,
) -> None:
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory.descriptor)
    except FileNotFoundError as error:
        raise IntegrityError(f"source-catalog blob is missing: {blob_ref}") from error
    except OSError as error:
        raise IntegrityError(f"source-catalog blob cannot be opened safely: {blob_ref}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise IntegrityError(f"source-catalog blob is not a regular file: {blob_ref}")
        digest = hashlib.sha256()
        observed_size = 0
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            while block := stream.read(1024 * 1024):
                observed_size += len(block)
                digest.update(block)
    finally:
        os.close(descriptor)
    observed_digest = "sha256:" + digest.hexdigest()
    if observed_size != byte_size or observed_digest != blob_ref:
        raise IntegrityError(f"source-catalog blob differs from its content identity: {blob_ref}")


def _open_blob_file(blob_root: _PinnedDirectory, blob_ref: str) -> int:
    selected_sha = _pin_directory(
        "sha256",
        label="source-catalog SHA-256 root",
        parent=blob_root,
        missing_ok=True,
    )
    if selected_sha is None:
        raise FileNotFoundError(blob_ref)
    descriptor: int | None = None
    try:
        name = blob_ref.removeprefix("sha256:")
        descriptor = os.open(name, _READ_FLAGS, dir_fd=selected_sha.descriptor)
        _require_child_identity(
            blob_root,
            "sha256",
            selected_sha,
            label="source-catalog SHA-256 root",
        )
        metadata = os.fstat(descriptor)
        if stat.S_ISREG(metadata.st_mode):
            return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    finally:
        os.close(selected_sha.descriptor)
    assert descriptor is not None
    os.close(descriptor)
    raise IntegrityError(f"source-catalog blob is not a regular file: {blob_ref}")
