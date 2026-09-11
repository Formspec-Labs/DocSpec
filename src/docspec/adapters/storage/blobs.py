"""Local blobs: content-addressed source bytes."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

from docspec.adapters.storage.files import _FILE_CHUNK_BYTES, _contained, _storage_root, sha256_file
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
    ) -> None:
        if min(max_blob_bytes, stream_chunk_bytes) <= 0:
            raise ValueError("blob store limits must be positive")
        self.root = _storage_root(root)
        self.max_blob_bytes = max_blob_bytes
        self.stream_chunk_bytes = stream_chunk_bytes
        self._staging = _contained(self.root, ".staging/blob", create_parents=True).parent
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
        require_text(media_type, "blob media_type")
        if expected_digest is not None:
            require_sha256(expected_digest, "expected blob digest")
        if expected_size is not None and expected_size < 0:
            raise ValueError("expected_size must be non-negative")
        limit = self.max_blob_bytes if max_bytes is None else min(self.max_blob_bytes, max_bytes)
        if limit < 0:
            raise ValueError("max_bytes must be non-negative")
        descriptor, temporary_name = tempfile.mkstemp(prefix="blob-", dir=self._staging)
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        byte_size = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("blob chunks must be bytes")
                    byte_size += len(chunk)
                    if byte_size > limit:
                        raise LimitExceededError(f"blob exceeds the {limit}-byte write limit")
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            actual_digest = f"sha256:{digest.hexdigest()}"
            if expected_digest is not None and actual_digest != expected_digest:
                raise IntegrityError("downloaded bytes differ from the expected digest")
            if expected_size is not None and byte_size != expected_size:
                raise IntegrityError("downloaded bytes differ from the expected size")
            locator = self._locator(actual_digest)
            destination = _contained(self.root, locator, create_parents=True)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if destination.is_symlink() or not destination.is_file():
                    raise IntegrityError(f"blob destination is not a regular immutable object: {locator}") from None
                existing = BlobRef(locator, actual_digest, byte_size, media_type)
                self.verify(existing)
            return BlobRef(locator, actual_digest, byte_size, media_type)
        finally:
            temporary.unlink(missing_ok=True)

    def stat(self, reference: BlobRef) -> BlobRef:
        self.verify(reference)
        return reference

    def read(
        self,
        reference: BlobRef,
        *,
        chunk_size: int | None = None,
        max_bytes: int | None = None,
    ) -> Iterator[bytes]:
        effective_chunk_size = self.stream_chunk_bytes if chunk_size is None else chunk_size
        if effective_chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if max_bytes is not None and reference.byte_size > max_bytes:
            raise LimitExceededError(f"blob exceeds the {max_bytes}-byte read limit")
        path = _contained(self.root, reference.locator)
        if not path.is_file() or path.is_symlink() or path.stat().st_size != reference.byte_size:
            raise IntegrityError("blob size or storage type differs from its reference")
        digest = hashlib.sha256()
        seen = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(effective_chunk_size), b""):
                seen += len(chunk)
                digest.update(chunk)
                yield chunk
        if seen != reference.byte_size or f"sha256:{digest.hexdigest()}" != reference.digest:
            raise IntegrityError("blob bytes differ from their immutable reference")

    def read_range(self, reference: BlobRef, *, start: int, end: int) -> bytes:
        if start < 0 or end < start or end > reference.byte_size:
            raise ValueError("blob range must be a contained half-open interval")
        self.verify(reference)
        path = _contained(self.root, reference.locator)
        with path.open("rb") as handle:
            handle.seek(start)
            return handle.read(end - start)

    def materialize(self, reference: BlobRef, root: Path, relative_path: str) -> Path:
        self.verify(reference)
        destination_root = _storage_root(root)
        destination = _contained(destination_root, relative_path, create_parents=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise IntegrityError(f"refusing to replace materialized file: {relative_path}") from error
        try:
            with os.fdopen(descriptor, "wb") as handle:
                for chunk in self.read(reference):
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return destination

    def verify(self, reference: BlobRef) -> None:
        expected_locator = self._locator(reference.digest)
        if reference.locator != expected_locator:
            raise IntegrityError("blob locator does not match its digest")
        path = _contained(self.root, reference.locator)
        if not path.is_file() or path.is_symlink() or path.stat().st_size != reference.byte_size:
            raise IntegrityError("blob size or storage type differs from its reference")
        digest, byte_size = sha256_file(path, chunk_size=self.stream_chunk_bytes)
        if byte_size != reference.byte_size or digest != reference.digest:
            raise IntegrityError("blob bytes differ from their immutable reference")
