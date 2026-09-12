"""One source-catalog staging transaction and its blob publication lifetime."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import BinaryIO

from docspec.adapters.source_catalog_store.pinned_fs import (
    _cleanup_session_at,
    _create_random_file,
    _entry_exists,
    _member_parent,
    _object_parts,
    _open_blob_file,
    _open_member_at,
    _pin_directory,
    _PinnedDirectory,
    _publish_directory_no_replace_at,
    _require_child_identity,
    _sync_directory_descriptor,
    _verify_blob_at,
)
from docspec.domain.identity import require_sha256
from docspec.domain.references import SourceCatalogRef
from docspec.errors import IntegrityError
from docspec.ports.source_catalog import SourceCatalogBlobSource, SourceCatalogBlobWrite


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
        try:
            published_blob_root = _pin_directory(
                ".blobs",
                label="source-catalog published blob root",
                parent=self._store,
                create=True,
            )
        except BaseException:
            if staged_sha_root is not None:
                os.close(staged_sha_root.descriptor)
            raise
        assert published_blob_root is not None
        try:
            if staged_sha_root is None:
                # Empty catalogs still need a stable blob resolver when opened
                # read-only. Create its directory during publication, even
                # though this artifact references no payload blobs.
                _sync_directory_descriptor(published_blob_root)
                _sync_directory_descriptor(self._store)
                return
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
            if staged_sha_root is not None:
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
