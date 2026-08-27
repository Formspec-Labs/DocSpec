"""Local atomic storage for immutable DocSpec source catalogs."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import secrets
import stat
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from rulespec_artifacts import (
    ArtifactVerificationError,
    MemberSource,
    MemberSourceError,
    PinnedLocalDirectory,
    move_child_directory_no_replace,
    publish_child_directory_no_replace,
)

from docspec.domain.identity import (
    canonical_json_file_bytes,
    parse_canonical_json,
    require_sha256,
    require_text,
    thaw_json,
)
from docspec.domain.references import SourceCatalogRef
from docspec.errors import IntegrityError, StaleBaseError, StateTransitionError
from docspec.ports.source_catalog import (
    ImmutableSourceCatalogReader,
    SourceCatalogBlobSource,
    SourceCatalogBlobWrite,
    SourceCatalogCurrentPointer,
    SourceCatalogSnapshotSummary,
    SourceCatalogStaging,
)

_CURRENT_FORMAT = "docspec-source-catalog-current"
_CURRENT_FORMAT_VERSION = "1.0"
_MAX_CURRENT_BYTES = 64 * 1024
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


def _object_path(root: Path, object_key: str) -> Path:
    return root.joinpath(*_object_parts(object_key))


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


class _LayeredBlobSource:
    def __init__(self, staged_root: _PinnedDirectory, store_root: _PinnedDirectory) -> None:
        self._staged_root = staged_root
        self._store_root = store_root

    @contextmanager
    def open(self, blob_ref: str) -> Iterator[BinaryIO]:
        require_sha256(blob_ref, "source catalog blob_ref")
        try:
            descriptor = _open_blob_file(self._staged_root, blob_ref)
        except FileNotFoundError:
            published_root = _pin_directory(
                ".blobs",
                label="source-catalog published blob root",
                parent=self._store_root,
            )
            assert published_root is not None
            try:
                descriptor = _open_blob_file(published_root, blob_ref)
            finally:
                os.close(published_root.descriptor)
        with os.fdopen(descriptor, "rb") as stream:
            yield stream


class LocalSourceCatalogStaging:
    """One exclusive staging directory with atomic publication."""

    def __init__(
        self,
        store_root: _PinnedDirectory,
        staging_root: _PinnedDirectory,
        session_name: str,
        session: _PinnedDirectory,
        shared_blob_root: _PinnedDirectory | None,
    ) -> None:
        artifact = _pin_directory(
            "artifact",
            label="source-catalog artifact staging root",
            parent=session,
            create=True,
        )
        assert artifact is not None
        try:
            blob_staging = _pin_directory(
                "blobs",
                label="source-catalog blob staging root",
                parent=session,
                create=True,
            )
        except BaseException:
            os.close(artifact.descriptor)
            raise
        assert blob_staging is not None
        self._store = store_root
        self._staging_root = staging_root
        self._session_name = session_name
        self._session = session
        self._session_identity = session.identity
        self._artifact = artifact
        self._blob_staging = blob_staging
        self._store_root = store_root.path
        self._session_path = session.path
        self._path = artifact.path
        self._published_blob_root = store_root.path / ".blobs"
        self._shared_blob_root = shared_blob_root
        self._committed = False
        self._closed = False

    @staticmethod
    def _keys(
        directory: _PinnedDirectory,
        prefix: tuple[str, ...],
    ) -> Iterator[str]:
        for name in sorted(os.listdir(directory.descriptor)):
            key = "/".join((*prefix, name))
            _object_parts(key)
            metadata = os.stat(
                name,
                dir_fd=directory.descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(metadata.st_mode):
                raise IntegrityError(f"source-catalog artifact contains a symlink: {key}")
            if stat.S_ISDIR(metadata.st_mode):
                child = _pin_directory(
                    name,
                    label="source-catalog artifact member directory",
                    parent=directory,
                )
                assert child is not None
                try:
                    yield from LocalSourceCatalogStaging._keys(child, (*prefix, name))
                finally:
                    os.close(child.descriptor)
            elif stat.S_ISREG(metadata.st_mode):
                yield key
            else:
                raise IntegrityError(f"source-catalog artifact contains a special file: {key}")

    def keys(self) -> Iterable[str]:
        return self._keys(self._artifact, ())

    @contextmanager
    def open(self, object_key: str) -> Iterator[BinaryIO]:
        descriptor = _open_member_at(self._artifact, object_key)
        with os.fdopen(descriptor, "rb") as stream:
            yield stream

    def write(self, object_key: str, chunks: Iterable[bytes]) -> None:
        parent, name = _member_parent(self._artifact, object_key, create=True)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(name, flags, 0o666, dir_fd=parent.descriptor)
        except FileExistsError as error:
            raise IntegrityError(
                f"source-catalog staging member already exists: {object_key}"
            ) from error
        finally:
            os.close(parent.descriptor)
        with os.fdopen(descriptor, "wb") as stream:
            try:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("source-catalog writes require bytes")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                raise

    @staticmethod
    def _verified_blob(
        blob_root: _PinnedDirectory,
        *,
        name: str,
        blob_ref: str,
        byte_size: int,
        label: str,
    ) -> bool:
        sha_root = _pin_directory(
            "sha256",
            label=label,
            parent=blob_root,
            missing_ok=True,
        )
        if sha_root is None:
            return False
        try:
            if not _entry_exists(sha_root, name):
                return False
            _verify_blob_at(sha_root, name, blob_ref=blob_ref, byte_size=byte_size)
            _require_child_identity(
                blob_root,
                "sha256",
                sha_root,
                label=label,
            )
            return True
        finally:
            os.close(sha_root.descriptor)

    def put_blob(
        self,
        blob_ref: str,
        byte_size: int,
        chunks: Iterable[bytes],
    ) -> SourceCatalogBlobWrite:
        """Write absent CAS bytes without consuming chunks for verified reuse."""

        require_sha256(blob_ref, "source catalog blob_ref")
        if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size < 0:
            raise ValueError("source catalog blob byte_size must be a non-negative integer")
        name = blob_ref.removeprefix("sha256:")
        published_blob_root = _pin_directory(
            ".blobs",
            label="source-catalog published blob root",
            parent=self._store,
            missing_ok=True,
        )
        if published_blob_root is not None:
            try:
                if self._verified_blob(
                    published_blob_root,
                    name=name,
                    blob_ref=blob_ref,
                    byte_size=byte_size,
                    label="source-catalog published SHA-256 root",
                ):
                    return SourceCatalogBlobWrite(blob_ref, byte_size, True)
            finally:
                os.close(published_blob_root.descriptor)
        if self._verified_blob(
            self._blob_staging,
            name=name,
            blob_ref=blob_ref,
            byte_size=byte_size,
            label="source-catalog staged SHA-256 root",
        ):
            return SourceCatalogBlobWrite(blob_ref, byte_size, True)
        if self._shared_blob_root is not None:
            shared_sha_root = _pin_directory(
                "sha256",
                label="shared source-catalog SHA-256 root",
                parent=self._shared_blob_root,
                missing_ok=True,
            )
            if shared_sha_root is not None:
                try:
                    if _entry_exists(shared_sha_root, name):
                        _verify_blob_at(
                            shared_sha_root,
                            name,
                            blob_ref=blob_ref,
                            byte_size=byte_size,
                        )
                        staged_sha_root = _pin_directory(
                            "sha256",
                            label="source-catalog staged SHA-256 root",
                            parent=self._blob_staging,
                            create=True,
                        )
                        assert staged_sha_root is not None
                        try:
                            try:
                                os.link(
                                    name,
                                    name,
                                    src_dir_fd=shared_sha_root.descriptor,
                                    dst_dir_fd=staged_sha_root.descriptor,
                                    follow_symlinks=False,
                                )
                            except FileExistsError:
                                _verify_blob_at(
                                    staged_sha_root,
                                    name,
                                    blob_ref=blob_ref,
                                    byte_size=byte_size,
                                )
                            except OSError as error:
                                raise IntegrityError(
                                    "source-catalog shared CAS cannot link verified reuse into "
                                    "the destination"
                                ) from error
                            _require_child_identity(
                                self._blob_staging,
                                "sha256",
                                staged_sha_root,
                                label="source-catalog staged SHA-256 root",
                            )
                            _require_child_identity(
                                self._shared_blob_root,
                                "sha256",
                                shared_sha_root,
                                label="shared source-catalog SHA-256 root",
                            )
                            _verify_blob_at(
                                staged_sha_root,
                                name,
                                blob_ref=blob_ref,
                                byte_size=byte_size,
                            )
                        finally:
                            os.close(staged_sha_root.descriptor)
                        return SourceCatalogBlobWrite(blob_ref, byte_size, True)
                finally:
                    os.close(shared_sha_root.descriptor)

        pending_root = _pin_directory(
            ".pending",
            label="source-catalog pending blob root",
            parent=self._blob_staging,
            create=True,
        )
        assert pending_root is not None
        pending_name: str | None = None
        try:
            pending_name, descriptor = _create_random_file(pending_root, prefix="blob-")
            _require_child_identity(
                self._blob_staging,
                ".pending",
                pending_root,
                label="source-catalog pending blob root",
            )
            with os.fdopen(descriptor, "wb") as stream:
                digest = hashlib.sha256()
                observed_size = 0
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("source-catalog blob writes require bytes")
                    stream.write(chunk)
                    digest.update(chunk)
                    observed_size += len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            observed_ref = "sha256:" + digest.hexdigest()
            if observed_size != byte_size or observed_ref != blob_ref:
                raise IntegrityError("source-catalog blob write differs from its declared receipt")
            staged_sha_root = _pin_directory(
                "sha256",
                label="source-catalog staged SHA-256 root",
                parent=self._blob_staging,
                create=True,
            )
            assert staged_sha_root is not None
            try:
                try:
                    os.link(
                        pending_name,
                        name,
                        src_dir_fd=pending_root.descriptor,
                        dst_dir_fd=staged_sha_root.descriptor,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    _verify_blob_at(
                        staged_sha_root,
                        name,
                        blob_ref=blob_ref,
                        byte_size=byte_size,
                    )
                    return SourceCatalogBlobWrite(blob_ref, byte_size, True)
                _require_child_identity(
                    self._blob_staging,
                    "sha256",
                    staged_sha_root,
                    label="source-catalog staged SHA-256 root",
                )
                _verify_blob_at(
                    staged_sha_root,
                    name,
                    blob_ref=blob_ref,
                    byte_size=byte_size,
                )
                return SourceCatalogBlobWrite(blob_ref, byte_size, False)
            finally:
                os.close(staged_sha_root.descriptor)
        finally:
            if pending_name is not None:
                try:
                    os.unlink(pending_name, dir_fd=pending_root.descriptor)
                except FileNotFoundError:
                    pass
            os.close(pending_root.descriptor)

    def blob_source(self) -> SourceCatalogBlobSource:
        return _LayeredBlobSource(self._blob_staging, self._store)

    @staticmethod
    def _publish_blob(
        staged_sha_root: _PinnedDirectory,
        root: _PinnedDirectory,
        *,
        name: str,
        blob_ref: str,
        byte_size: int,
    ) -> None:
        published_sha_root = _pin_directory(
            "sha256",
            label="source-catalog published SHA-256 root",
            parent=root,
            create=True,
        )
        assert published_sha_root is not None
        try:
            if _entry_exists(published_sha_root, name):
                _verify_blob_at(
                    published_sha_root,
                    name,
                    blob_ref=blob_ref,
                    byte_size=byte_size,
                )
                _require_child_identity(
                    root,
                    "sha256",
                    published_sha_root,
                    label="source-catalog published SHA-256 root",
                )
                return
            try:
                os.link(
                    name,
                    name,
                    src_dir_fd=staged_sha_root.descriptor,
                    dst_dir_fd=published_sha_root.descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                _verify_blob_at(
                    published_sha_root,
                    name,
                    blob_ref=blob_ref,
                    byte_size=byte_size,
                )
                return
            _require_child_identity(
                root,
                "sha256",
                published_sha_root,
                label="source-catalog published SHA-256 root",
            )
            _verify_blob_at(
                published_sha_root,
                name,
                blob_ref=blob_ref,
                byte_size=byte_size,
            )
        finally:
            os.close(published_sha_root.descriptor)

    def _publish_blobs(self) -> None:
        """Persist verified CAS bytes before the artifact root becomes visible.

        A blob without a referring artifact root is reusable store state, not a
        published catalog. Retaining it after a failed root publication also
        avoids deleting bytes that a concurrent artifact may already reference.
        """

        staged_sha_root = _pin_directory(
            "sha256",
            label="source-catalog staged SHA-256 root",
            parent=self._blob_staging,
            missing_ok=True,
        )
        if staged_sha_root is None:
            return
        try:
            published_blob_root = _pin_directory(
                ".blobs",
                label="source-catalog published blob root",
                parent=self._store,
                create=True,
            )
        except BaseException:
            os.close(staged_sha_root.descriptor)
            raise
        assert published_blob_root is not None
        try:
            for name in sorted(os.listdir(staged_sha_root.descriptor)):
                metadata = os.stat(
                    name,
                    dir_fd=staged_sha_root.descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(metadata.st_mode):
                    raise IntegrityError("source-catalog staged blob must be a regular file")
                blob_ref = "sha256:" + name
                byte_size = metadata.st_size
                _verify_blob_at(
                    staged_sha_root,
                    name,
                    blob_ref=blob_ref,
                    byte_size=byte_size,
                )
                self._publish_blob(
                    staged_sha_root,
                    published_blob_root,
                    name=name,
                    blob_ref=blob_ref,
                    byte_size=byte_size,
                )
                _require_child_identity(
                    self._store,
                    ".blobs",
                    published_blob_root,
                    label="source-catalog published blob root",
                )
                if self._shared_blob_root is not None:
                    self._publish_blob(
                        staged_sha_root,
                        self._shared_blob_root,
                        name=name,
                        blob_ref=blob_ref,
                        byte_size=byte_size,
                    )
            for root in (published_blob_root, self._shared_blob_root):
                if root is None:
                    continue
                selected_sha_root = _pin_directory(
                    "sha256",
                    label="source-catalog published SHA-256 root",
                    parent=root,
                )
                assert selected_sha_root is not None
                try:
                    _sync_directory_descriptor(selected_sha_root)
                    _sync_directory_descriptor(root)
                finally:
                    os.close(selected_sha_root.descriptor)
            _sync_directory_descriptor(self._store)
        finally:
            os.close(published_blob_root.descriptor)
            os.close(staged_sha_root.descriptor)

    def commit(self, reference: SourceCatalogRef) -> SourceCatalogRef:
        if self._committed:
            raise IntegrityError("source-catalog staging transaction is already committed")
        digest_name = reference.digest.removeprefix("sha256:")
        expected_locator = f"{digest_name}/artifact.json"
        if reference.locator != expected_locator:
            raise IntegrityError("source-catalog locator differs from its exact artifact digest")
        try:
            descriptor = _open_member_at(self._artifact, "artifact.json")
        except OSError:
            raise IntegrityError("source-catalog staging transaction has no artifact root")
        else:
            os.close(descriptor)
        self._publish_blobs()
        try:
            _publish_directory_no_replace_at(
                self._session,
                self._artifact,
                "artifact",
                self._store,
                digest_name,
            )
        except IntegrityError as error:
            if _entry_exists(self._store, digest_name):
                raise IntegrityError("source-catalog artifact already exists") from error
            raise
        self._committed = True
        return reference

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self._artifact.descriptor)
        os.close(self._blob_staging.descriptor)
        try:
            _require_child_identity(
                self._store,
                ".staging",
                self._staging_root,
                label="source-catalog staging root",
            )
        except IntegrityError as error:
            staging_error: IntegrityError | None = error
        else:
            staging_error = None
        _cleanup_session_at(
            self._staging_root,
            self._session_name,
            self._session_identity,
        )
        if staging_error is not None:
            raise staging_error


class LocalSourceCatalogPublication:
    """Stage and conditionally publish one complete local catalog destination."""

    def __init__(self, destination: Path) -> None:
        selected = Path(destination)
        if selected.name in {"", ".", ".."}:
            raise ValueError("source-catalog destination must name one directory")
        parent = _pin_directory(
            selected.parent,
            label="source-catalog destination parent",
            error_type=ValueError,
        )
        assert parent is not None
        try:
            if _entry_exists(parent, selected.name):
                raise IntegrityError(
                    f"refusing to replace immutable source-catalog directory: {selected.name}"
                )
            session_name, session = _create_random_directory(
                parent,
                prefix=f".{selected.name}.",
                label="source-catalog command staging directory",
            )
        except BaseException:
            os.close(parent.descriptor)
            raise
        self.destination = parent.path / selected.name
        self.root = session.path
        self._parent = parent
        self._session_name = session_name
        self._session = session
        self._published = False
        self._closed = False

    def _require_named_parent(self) -> None:
        selected = _pin_directory(
            self._parent.path,
            label="source-catalog destination parent",
            expected_identity=self._parent.identity,
        )
        assert selected is not None
        os.close(selected.descriptor)

    def store(
        self,
        *,
        shared_blob_root: Path | None = None,
    ) -> LocalSourceCatalogStore:
        """Open the staged root through its exact directory identity."""

        return LocalSourceCatalogStore(
            self.root,
            shared_blob_root=shared_blob_root,
            _expected_root_identity=self._session.identity,
        )

    def remove_empty_directory(self, name: str) -> None:
        selected = _pin_directory(
            name,
            label="source-catalog staged internal directory",
            parent=self._session,
        )
        assert selected is not None
        try:
            if os.listdir(selected.descriptor):
                raise IntegrityError(
                    f"source-catalog staged internal directory is not empty: {name}"
                )
            _require_child_identity(
                self._session,
                name,
                selected,
                label="source-catalog staged internal directory",
            )
            os.rmdir(name, dir_fd=self._session.descriptor)
            _sync_directory_descriptor(self._session)
        finally:
            os.close(selected.descriptor)

    def write_file(self, name: str, payload: bytes) -> None:
        if len(Path(name).parts) != 1 or name in {"", ".", ".."}:
            raise ValueError("source-catalog root member must use one safe path component")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=self._session.descriptor)
        except FileExistsError as error:
            raise IntegrityError(
                f"refusing to replace source-catalog root member: {name}"
            ) from error
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _sync_directory_descriptor(self._session)
        except BaseException:
            try:
                os.unlink(name, dir_fd=self._session.descriptor)
            except FileNotFoundError:
                pass
            raise

    def publish(self) -> None:
        if self._published:
            raise IntegrityError("source-catalog destination is already published")
        self._require_named_parent()
        try:
            _publish_directory_no_replace_at(
                self._parent,
                self._session,
                self._session_name,
                self._parent,
                self.destination.name,
            )
        except BaseException:
            try:
                published = os.stat(
                    self.destination.name,
                    dir_fd=self._parent.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                self._published = (
                    published.st_dev,
                    published.st_ino,
                ) == self._session.identity
            raise
        self._published = True
        self._require_named_parent()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if not self._published:
                _cleanup_session_at(
                    self._parent,
                    self._session_name,
                    self._session.identity,
                )
        finally:
            os.close(self._session.descriptor)
            os.close(self._parent.descriptor)

    def __enter__(self) -> LocalSourceCatalogPublication:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()


class LocalSourceCatalogStore:
    """Resolve immutable catalogs below one explicit local destination."""

    def __init__(
        self,
        root: Path,
        *,
        create: bool = True,
        shared_blob_root: Path | None = None,
        _expected_root_identity: _IDENTITY | None = None,
    ) -> None:
        selected = Path(root)
        selected_root = _pin_directory(
            selected,
            label="source-catalog root",
            create=create,
            parents=create,
            expected_identity=_expected_root_identity,
            error_type=ValueError,
        )
        assert selected_root is not None
        try:
            pinned_reader_root = PinnedLocalDirectory(
                selected_root.path,
                expected_identity=selected_root.identity,
            )
        except MemberSourceError as error:
            os.close(selected_root.descriptor)
            raise ValueError(f"source-catalog root cannot be pinned safely: {error}") from error
        self.root = selected_root.path
        self._root_identity = selected_root.identity
        self._pinned_reader_root = pinned_reader_root
        os.close(selected_root.descriptor)
        self._staging_root = self.root / ".staging"
        self._blob_root = self.root / ".blobs"
        self._shared_blob_root: Path | None = None
        self._shared_blob_identity: _IDENTITY | None = None
        if shared_blob_root is not None:
            selected_blob_root = Path(shared_blob_root)
            resolved_blob_root = _pin_directory(
                selected_blob_root,
                label="shared source-catalog blob root",
                create=True,
                parents=True,
                error_type=ValueError,
            )
            assert resolved_blob_root is not None
            if resolved_blob_root.path != self._blob_root.resolve(strict=False):
                if resolved_blob_root.identity[0] != self._root_identity[0]:
                    os.close(resolved_blob_root.descriptor)
                    raise ValueError(
                        "shared source-catalog blob root must use the artifact filesystem"
                    )
                self._shared_blob_root = resolved_blob_root.path
                self._shared_blob_identity = resolved_blob_root.identity
            os.close(resolved_blob_root.descriptor)

    @contextmanager
    def stage(self) -> Iterator[SourceCatalogStaging]:
        store_root = _pin_directory(
            self.root,
            label="source-catalog root",
            expected_identity=self._root_identity,
        )
        assert store_root is not None
        try:
            staging_root = _pin_directory(
                ".staging",
                label="source-catalog staging root",
                parent=store_root,
                create=True,
            )
        except BaseException:
            os.close(store_root.descriptor)
            raise
        assert staging_root is not None
        shared_blob_root = None
        try:
            if self._shared_blob_root is not None:
                shared_blob_root = _pin_directory(
                    self._shared_blob_root,
                    label="shared source-catalog blob root",
                    expected_identity=self._shared_blob_identity,
                )
                assert shared_blob_root is not None
            session_name, session = _create_random_directory(
                staging_root,
                prefix="catalog-",
                label="source-catalog staging session",
            )
        except BaseException:
            if shared_blob_root is not None:
                os.close(shared_blob_root.descriptor)
            os.close(staging_root.descriptor)
            os.close(store_root.descriptor)
            raise
        staging: LocalSourceCatalogStaging | None = None
        try:
            staging = LocalSourceCatalogStaging(
                store_root,
                staging_root,
                session_name,
                session,
                shared_blob_root,
            )
            yield staging
        finally:
            try:
                if staging is None:
                    _cleanup_session_at(
                        staging_root,
                        session_name,
                        session.identity,
                    )
                else:
                    staging.close()
            finally:
                os.close(session.descriptor)
                if shared_blob_root is not None:
                    os.close(shared_blob_root.descriptor)
                os.close(staging_root.descriptor)
                os.close(store_root.descriptor)

    def source_for(self, reference: SourceCatalogRef) -> MemberSource:
        digest_name = reference.digest.removeprefix("sha256:")
        if reference.locator != f"{digest_name}/artifact.json":
            raise IntegrityError("source-catalog locator differs from its exact artifact digest")
        return self._pinned_reader_root.member_source(digest_name)

    def blob_source(self) -> SourceCatalogBlobSource:
        return self._pinned_reader_root.blob_source(".blobs")


class LocalSourceCatalogCurrentPointer(SourceCatalogCurrentPointer):
    """Atomically address one fully admitted current root per catalog series."""

    def __init__(self, root: Path, *, reader: ImmutableSourceCatalogReader) -> None:
        selected_root = _pin_directory(
            Path(root),
            label="source-catalog pointer root",
            create=True,
            parents=True,
            error_type=ValueError,
        )
        assert selected_root is not None
        self.root = selected_root.path
        self._root_identity = selected_root.identity
        os.close(selected_root.descriptor)
        self._reader = reader

    @staticmethod
    def _series_key(catalog_id: str) -> str:
        selected = require_text(catalog_id, "source catalog series catalog_id")
        return hashlib.sha256(selected.encode("utf-8")).hexdigest()

    def _open_root(self) -> _PinnedDirectory:
        selected = _pin_directory(
            self.root,
            label="source-catalog pointer root",
            expected_identity=self._root_identity,
        )
        assert selected is not None
        return selected

    def _require_named_root(self) -> None:
        selected = self._open_root()
        os.close(selected.descriptor)

    @staticmethod
    def _pointer_parent(
        root: _PinnedDirectory,
        *,
        create: bool,
    ) -> _PinnedDirectory | None:
        parent = _pin_directory(
            "current",
            label="source-catalog current-pointer parent",
            parent=root,
            create=create,
            missing_ok=not create,
        )
        if parent is not None and create:
            _sync_directory_descriptor(root)
        return parent

    def _read_pointer(
        self,
        parent: _PinnedDirectory,
        catalog_id: str,
    ) -> tuple[SourceCatalogRef, SourceCatalogRef | None] | None:
        name = f"{self._series_key(catalog_id)}.json"
        try:
            descriptor = os.open(name, _READ_FLAGS, dir_fd=parent.descriptor)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise IntegrityError(
                f"source-catalog current pointer cannot be opened safely: {error}"
            ) from error
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise IntegrityError("source-catalog current pointer must be a regular file")
            if metadata.st_size > _MAX_CURRENT_BYTES:
                raise IntegrityError("source-catalog current pointer exceeds its byte limit")
            payload = stream.read(_MAX_CURRENT_BYTES + 1)
            final_metadata = os.fstat(stream.fileno())
        if (
            len(payload) != metadata.st_size
            or (final_metadata.st_dev, final_metadata.st_ino) != (metadata.st_dev, metadata.st_ino)
            or final_metadata.st_size != metadata.st_size
            or final_metadata.st_mtime_ns != metadata.st_mtime_ns
            or final_metadata.st_ctime_ns != metadata.st_ctime_ns
        ):
            raise IntegrityError("source-catalog current pointer changed while it was read")
        value = thaw_json(parse_canonical_json(payload, label="source catalog current pointer"))
        if not isinstance(value, Mapping) or set(value) != {
            "format",
            "formatVersion",
            "catalogId",
            "catalog",
            "previous",
        }:
            raise IntegrityError("source-catalog current pointer has an invalid closed shape")
        if value["format"] != _CURRENT_FORMAT or value["formatVersion"] != _CURRENT_FORMAT_VERSION:
            raise IntegrityError("source-catalog current pointer has an unknown format")
        if value["catalogId"] != catalog_id:
            raise IntegrityError("source-catalog current pointer names a different series")
        try:
            reference = SourceCatalogRef.from_dict(value["catalog"])
            previous = (
                None
                if value["previous"] is None
                else SourceCatalogRef.from_dict(value["previous"])
            )
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"source-catalog current pointer is invalid: {error}") from error
        return reference, previous

    def _admit(
        self,
        catalog_id: str,
        reference: SourceCatalogRef,
    ) -> SourceCatalogSnapshotSummary:
        summary = self._reader.verify_snapshot(reference)
        if (
            summary.logical_id != reference.catalog_id
            or summary.artifact_digest != reference.digest
        ):
            raise IntegrityError("source-catalog admission differs from its pointer reference")
        if summary.catalog_id != catalog_id:
            raise IntegrityError("source-catalog candidate belongs to a different catalog series")
        return summary

    def _admitted_current(
        self,
        parent: _PinnedDirectory,
        catalog_id: str,
    ) -> tuple[SourceCatalogRef, SourceCatalogRef | None] | None:
        pointer = self._read_pointer(parent, catalog_id)
        if pointer is None:
            return None
        reference, previous = pointer
        summary = self._admit(catalog_id, reference)
        supersedes = summary.succession
        if previous is None:
            if supersedes is not None:
                raise IntegrityError("initial source-catalog pointer cannot name a successor root")
        else:
            self._admit(catalog_id, previous)
            if supersedes is None:
                raise IntegrityError("source-catalog successor root is missing supersedes")
            if (
                supersedes.logical_id != previous.catalog_id
                or supersedes.artifact_digest != previous.digest
            ):
                raise IntegrityError("source-catalog supersedes differs from the pointer predecessor")
            if not supersedes.reason.strip():
                raise IntegrityError("source-catalog supersedes reason must be nonempty")
        return reference, previous

    def current(self, catalog_id: str) -> SourceCatalogRef | None:
        require_text(catalog_id, "source catalog series catalog_id")
        root = self._open_root()
        try:
            parent = self._pointer_parent(root, create=False)
            if parent is None:
                self._require_named_root()
                return None
            try:
                pointer = self._admitted_current(parent, catalog_id)
                _require_child_identity(
                    root,
                    "current",
                    parent,
                    label="source-catalog current-pointer parent",
                )
                self._require_named_root()
                return None if pointer is None else pointer[0]
            finally:
                os.close(parent.descriptor)
        finally:
            os.close(root.descriptor)

    @staticmethod
    def _require_lock_identity(
        parent: _PinnedDirectory,
        name: str,
        identity: _IDENTITY,
    ) -> None:
        try:
            metadata = os.stat(
                name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise IntegrityError("source-catalog pointer lock changed during use") from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) != identity
        ):
            raise IntegrityError("source-catalog pointer lock changed during use")

    @contextmanager
    def _series_lock(
        self,
        root: _PinnedDirectory,
        parent: _PinnedDirectory,
        catalog_id: str,
        candidate: SourceCatalogRef,
    ) -> Iterator[tuple[str, _IDENTITY]]:
        name = f".{self._series_key(catalog_id)}.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=parent.descriptor)
        except OSError as error:
            raise IntegrityError(
                f"source-catalog pointer lock cannot be opened safely: {error}"
            ) from error
        locked = False
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise IntegrityError("source-catalog pointer lock must be a regular file")
            if metadata.st_nlink != 1:
                raise IntegrityError("source-catalog pointer lock must have one filesystem link")
            identity = (metadata.st_dev, metadata.st_ino)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    raise StateTransitionError(
                        "another source-catalog pointer advance is in progress"
                    ) from error
                raise
            locked = True
            self._require_lock_identity(parent, name, identity)
            _require_child_identity(
                root,
                "current",
                parent,
                label="source-catalog current-pointer parent",
            )
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, (candidate.digest + "\n").encode("ascii"))
            os.fsync(descriptor)
            _sync_directory_descriptor(parent)
            yield name, identity
        finally:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _write_current(
        self,
        root: _PinnedDirectory,
        parent: _PinnedDirectory,
        catalog_id: str,
        candidate: SourceCatalogRef,
        previous: SourceCatalogRef | None,
    ) -> None:
        destination = f"{self._series_key(catalog_id)}.json"
        try:
            existing = os.stat(
                destination,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(existing.st_mode):
                raise IntegrityError("source-catalog current pointer must not be a symlink")
            if not stat.S_ISREG(existing.st_mode):
                raise IntegrityError("source-catalog current pointer must be a regular file")
        payload = canonical_json_file_bytes(
            {
                "format": _CURRENT_FORMAT,
                "formatVersion": _CURRENT_FORMAT_VERSION,
                "catalogId": catalog_id,
                "catalog": candidate.to_dict(),
                "previous": None if previous is None else previous.to_dict(),
            }
        )
        temporary, descriptor = _create_random_file(parent, prefix="current-")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _require_child_identity(
                root,
                "current",
                parent,
                label="source-catalog current-pointer parent",
            )
            os.replace(
                temporary,
                destination,
                src_dir_fd=parent.descriptor,
                dst_dir_fd=parent.descriptor,
            )
            _require_child_identity(
                root,
                "current",
                parent,
                label="source-catalog current-pointer parent",
            )
            _sync_directory_descriptor(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent.descriptor)
            except FileNotFoundError:
                pass

    def advance(
        self,
        catalog_id: str,
        candidate: SourceCatalogRef,
        *,
        expected_current: SourceCatalogRef | None,
    ) -> SourceCatalogRef:
        require_text(catalog_id, "source catalog series catalog_id")
        summary = self._admit(catalog_id, candidate)
        root = self._open_root()
        try:
            parent = self._pointer_parent(root, create=True)
            assert parent is not None
            try:
                with self._series_lock(root, parent, catalog_id, candidate) as lock:
                    pointer = self._admitted_current(parent, catalog_id)
                    current = None if pointer is None else pointer[0]
                    if current == candidate:
                        result = candidate
                    else:
                        if current != expected_current:
                            raise StaleBaseError(
                                "source-catalog current root differs from the expected root"
                            )
                        supersedes = summary.succession
                        if current is None:
                            if supersedes is not None:
                                raise IntegrityError(
                                    "initial source-catalog candidate must not declare supersedes"
                                )
                        else:
                            if supersedes is None:
                                raise IntegrityError(
                                    "source-catalog successor candidate is missing supersedes"
                                )
                            if (
                                supersedes.logical_id != current.catalog_id
                                or supersedes.artifact_digest != current.digest
                            ):
                                raise IntegrityError(
                                    "source-catalog successor does not supersede the current root"
                                )
                            if not supersedes.reason.strip():
                                raise IntegrityError(
                                    "source-catalog supersedes reason must be nonempty"
                                )
                        self._require_lock_identity(parent, *lock)
                        self._write_current(root, parent, catalog_id, candidate, current)
                        readback = self._admitted_current(parent, catalog_id)
                        if readback is None or readback[0] != candidate:
                            raise IntegrityError(
                                "source-catalog current pointer readback differs after replacement"
                            )
                        result = candidate
                    self._require_lock_identity(parent, *lock)
                    _require_child_identity(
                        root,
                        "current",
                        parent,
                        label="source-catalog current-pointer parent",
                    )
                    self._require_named_root()
                    return result
            finally:
                os.close(parent.descriptor)
        finally:
            os.close(root.descriptor)


__all__ = [
    "LocalSourceCatalogCurrentPointer",
    "LocalSourceCatalogPublication",
    "LocalSourceCatalogStore",
]
