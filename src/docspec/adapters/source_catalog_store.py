"""Local atomic storage for immutable DocSpec source catalogs."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from rulespec_artifacts import LocalMemberSource, MemberSource

from docspec.adapters.atomic_directory import publish_directory_no_replace
from docspec.domain.references import SourceCatalogRef
from docspec.errors import IntegrityError, StateTransitionError
from docspec.ports.source_catalog import SourceCatalogStaging


def _object_path(root: Path, object_key: str) -> Path:
    key = PurePosixPath(object_key)
    if key.is_absolute() or not key.parts or any(part in {"", ".", ".."} for part in key.parts):
        raise ValueError("source-catalog object key must be a contained relative path")
    if "\\" in object_key:
        raise ValueError("source-catalog object key must use portable separators")
    return root.joinpath(*key.parts)


class LocalSourceCatalogStaging:
    """One exclusive staging directory with atomic publication."""

    def __init__(self, store_root: Path, path: Path) -> None:
        self._store_root = store_root
        self._path = path
        self._committed = False

    def _source(self) -> LocalMemberSource:
        return LocalMemberSource(self._path)

    def keys(self) -> Iterable[str]:
        return self._source().keys()

    @contextmanager
    def open(self, object_key: str) -> Iterator[BinaryIO]:
        with self._source().open(object_key) as stream:
            yield stream

    def write(self, object_key: str, chunks: Iterable[bytes]) -> None:
        path = _object_path(self._path, object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("source-catalog writes require bytes")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as error:
            raise IntegrityError(f"source-catalog staging member already exists: {object_key}") from error

    def commit(self, reference: SourceCatalogRef) -> SourceCatalogRef:
        if self._committed:
            raise IntegrityError("source-catalog staging transaction is already committed")
        digest_name = reference.digest.removeprefix("sha256:")
        expected_locator = f"{digest_name}/artifact.json"
        if reference.locator != expected_locator:
            raise IntegrityError("source-catalog locator differs from its exact artifact digest")
        if not (self._path / "artifact.json").is_file():
            raise IntegrityError("source-catalog staging transaction has no artifact root")
        destination = self._store_root / digest_name
        if destination.exists():
            raise IntegrityError("source-catalog artifact already exists")
        try:
            publish_directory_no_replace(self._path, destination)
        except StateTransitionError as error:
            raise IntegrityError("source-catalog artifact publication is already in progress") from error
        except IntegrityError as error:
            if destination.exists():
                raise IntegrityError("source-catalog artifact already exists") from error
            raise
        self._committed = True
        return reference

    def close(self) -> None:
        if not self._committed and self._path.exists():
            shutil.rmtree(self._path)


class LocalSourceCatalogStore:
    """Resolve immutable catalogs below one explicit local destination."""

    def __init__(self, root: Path, *, create: bool = True) -> None:
        selected = Path(root)
        if create:
            selected.mkdir(parents=True, exist_ok=True)
            self.root = selected.resolve(strict=True)
        else:
            self.root = selected.resolve(strict=True)
            if not self.root.is_dir():
                raise ValueError("source-catalog root must be a directory")
        self._staging_root = self.root / ".staging"

    @contextmanager
    def stage(self) -> Iterator[SourceCatalogStaging]:
        self._staging_root.mkdir(exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix="catalog-", dir=self._staging_root))
        staging = LocalSourceCatalogStaging(self.root, path)
        try:
            yield staging
        finally:
            staging.close()

    def source_for(self, reference: SourceCatalogRef) -> MemberSource:
        digest_name = reference.digest.removeprefix("sha256:")
        if reference.locator != f"{digest_name}/artifact.json":
            raise IntegrityError("source-catalog locator differs from its exact artifact digest")
        return LocalMemberSource(self.root / digest_name)


__all__ = ["LocalSourceCatalogStore"]
