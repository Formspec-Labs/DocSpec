"""Admitted current-catalog pointers with locked compare-and-swap advancement."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from docspec.adapters.locks import lock_descriptor
from docspec.adapters.source_catalog_store.pinned_fs import (
    _IDENTITY,
    _READ_FLAGS,
    _create_random_file,
    _pin_directory,
    _PinnedDirectory,
    _require_child_identity,
    _sync_directory_descriptor,
)
from docspec.domain.identity import canonical_json_file_bytes, parse_canonical_json, require_text, thaw_json
from docspec.domain.references import SourceCatalogRef
from docspec.errors import IntegrityError, StaleBaseError
from docspec.ports.source_catalog import (
    ImmutableSourceCatalogReader,
    SourceCatalogCurrentPointer,
    SourceCatalogSnapshotSummary,
)

_CURRENT_FORMAT = "docspec-source-catalog-current"
_CURRENT_FORMAT_VERSION = "1.0"
_MAX_CURRENT_BYTES = 64 * 1024


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
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise IntegrityError("source-catalog pointer lock must be a regular file")
            if metadata.st_nlink != 1:
                raise IntegrityError("source-catalog pointer lock must have one filesystem link")
            identity = (metadata.st_dev, metadata.st_ino)
            with lock_descriptor(descriptor, busy_message="another source-catalog pointer advance is in progress"):
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
