"""Immutable catalog lookup and whole-destination publication."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from rulespec_artifacts import MemberSource, MemberSourceError, PinnedLocalDirectory

from docspec.adapters.source_catalog_store.pinned_fs import (
    _IDENTITY,
    _cleanup_session_at,
    _create_random_directory,
    _entry_exists,
    _pin_directory,
    _publish_directory_no_replace_at,
    _require_child_identity,
    _sync_directory_descriptor,
)
from docspec.adapters.source_catalog_store.staging import (
    LocalSourceCatalogStaging,
)
from docspec.domain.references import SourceCatalogRef
from docspec.errors import IntegrityError
from docspec.ports.source_catalog import SourceCatalogBlobSource, SourceCatalogStaging


class LocalSourceCatalogPublication:
    """Stage and conditionally publish one complete local catalog destination."""

    def __init__(self, destination: Path) -> None:
        """Create a randomly named staging session beside the destination, refusing to replace an existing directory."""

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
        """Remove one empty staged internal directory, refusing a non-empty one."""

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

    def publish(self) -> None:
        """Publish the staging session as the destination without replacing an existing directory."""

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
        """Clean up the staging session unless it was published."""

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
        """Pin the catalog root and an optional shared blob root, refusing a shared root on another filesystem."""

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
        """Yield one exclusive staging transaction, cleaning the session on every exit path."""

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
        """Return the pinned member source for a reference, refusing a locator that differs from its exact digest."""

        digest_name = reference.digest.removeprefix("sha256:")
        if reference.locator != f"{digest_name}/artifact.json":
            raise IntegrityError("source-catalog locator differs from its exact artifact digest")
        return self._pinned_reader_root.member_source(digest_name)

    def blob_source(self) -> SourceCatalogBlobSource:
        """Return the pinned blob source over the published CAS."""

        return self._pinned_reader_root.blob_source(".blobs")
