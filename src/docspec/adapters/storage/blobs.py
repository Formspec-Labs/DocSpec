"""Local blobs: content-addressed source bytes."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from pathlib import Path

from docspec.adapters.streams import staged_bytes
from docspec.adapters.storage.files import (
    _FILE_CHUNK_BYTES, _contained, _link_content, _storage_root, _sync_file, _sync_parents, delete_content, materialize_bytes, sha256_file,
)
from docspec.domain.identity import (
    require_sha256,
    require_text,
)
from docspec.domain.references import BlobRef
from docspec.errors import IntegrityError, LimitExceededError


class LocalContentAddressedBlobStore:
    """Store exact bytes under their SHA-256 digest with conditional creation."""

    def __init__(
        self,
        root: Path,
        *,
        max_blob_bytes: int = 8 * 1024**3,
        stream_chunk_bytes: int = _FILE_CHUNK_BYTES,
        create: bool = True,
    ) -> None:
        if min(max_blob_bytes, stream_chunk_bytes) <= 0:
            raise ValueError("blob store limits must be positive")
        self.root = _storage_root(root, create=create)
        self.max_blob_bytes = max_blob_bytes
        self.stream_chunk_bytes = stream_chunk_bytes
        self._staging = _contained(self.root, ".staging/blob", create_parents=create).parent
        if create:
            self._staging.mkdir(exist_ok=True)

    @staticmethod
    def _locator(digest: str) -> str:
        hexadecimal = require_sha256(digest).removeprefix("sha256:")
        return f"objects/sha256/{hexadecimal[:2]}/{hexadecimal}"

    def put_if_absent(
        self,
        chunks: Iterable[bytes],
        *,
        media_type: str,
        expected_digest: str | None = None,
        expected_size: int | None = None,
        max_bytes: int | None = None,
    ) -> BlobRef:
        """Stage, hash and link exact bytes under their digest, returning their immutable reference."""

        require_text(media_type, "blob media_type")
        with staged_bytes(chunks, directory=self._staging, limit=self.max_blob_bytes, max_bytes=max_bytes,
                          expected_digest=expected_digest, expected_size=expected_size) as (temporary, actual_digest, byte_size):
            locator = self._locator(actual_digest)
            _link_content(self.root, temporary, locator, actual_digest, byte_size)
            return BlobRef(locator, actual_digest, byte_size, media_type)

    def stat(self, reference: BlobRef) -> BlobRef:
        """Verify the locator and size and return the same reference."""

        self._path(reference)
        return reference

    def ensure_ready(self, reference: BlobRef) -> None:
        """Verify the retained file and persist it through its parent directories."""

        self.verify(reference)
        path = _contained(self.root, reference.locator)
        _sync_file(path)
        _sync_parents(self.root, path)

    def delete(self, reference: BlobRef) -> bool:
        """Policy-owner primitive; its exclusive protection must cover this call."""
        if reference.locator != self._locator(reference.digest):
            raise IntegrityError("blob locator does not match its digest")
        return delete_content(self.root, reference)

    def read(
        self,
        reference: BlobRef,
        *,
        chunk_size: int | None = None,
        max_bytes: int | None = None,
    ) -> Iterator[bytes]:
        """Stream and hash the retained bytes, refusing a size or digest mismatch."""

        effective_chunk_size = self.stream_chunk_bytes if chunk_size is None else chunk_size
        if effective_chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if max_bytes is not None and reference.byte_size > max_bytes:
            raise LimitExceededError(f"blob exceeds the {max_bytes}-byte read limit")
        path = self._path(reference)
        digest = hashlib.sha256()
        seen = 0
        with path.open("rb") as handle:
            while chunk := handle.read(min(effective_chunk_size, reference.byte_size - seen + 1)):
                seen += len(chunk)
                if max_bytes is not None and seen > max_bytes:
                    raise LimitExceededError(f"blob exceeds the {max_bytes}-byte read limit")
                if seen > reference.byte_size:
                    raise IntegrityError("blob grew beyond its immutable reference")
                digest.update(chunk)
                yield chunk
        if seen != reference.byte_size or f"sha256:{digest.hexdigest()}" != reference.digest:
            raise IntegrityError("blob bytes differ from their immutable reference")

    def read_range(self, reference: BlobRef, *, start: int, end: int) -> bytes:
        """Read one contained half-open byte interval after verifying the whole object."""

        if start < 0 or end < start or end > reference.byte_size:
            raise ValueError("blob range must be a contained half-open interval")
        self.verify(reference)
        path = _contained(self.root, reference.locator)
        with path.open("rb") as handle:
            handle.seek(start)
            return handle.read(end - start)

    def materialize(self, reference: BlobRef, root: Path, relative_path: str) -> Path:
        return materialize_bytes(root, relative_path, self.read(reference))

    def _path(self, reference: BlobRef) -> Path:
        expected_locator = self._locator(reference.digest)
        if reference.locator != expected_locator:
            raise IntegrityError("blob locator does not match its digest")
        path = _contained(self.root, reference.locator)
        if not path.is_file() or path.is_symlink() or path.stat().st_size != reference.byte_size:
            raise IntegrityError("blob size or storage type differs from its reference")
        return path

    def verify(self, reference: BlobRef) -> None:
        """Re-hash the retained file and refuse any difference from its reference."""

        path = self._path(reference)
        digest, byte_size = sha256_file(path, chunk_size=self.stream_chunk_bytes)
        if byte_size != reference.byte_size or digest != reference.digest:
            raise IntegrityError("blob bytes differ from their immutable reference")
