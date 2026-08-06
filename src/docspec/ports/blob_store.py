"""Provider-neutral storage for immutable byte objects."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Protocol

from docspec.domain.references import BlobRef


class BlobStore(Protocol):
    """Store and retrieve exact bytes without exposing provider responses."""

    def put_if_absent(
        self,
        chunks: Iterable[bytes],
        *,
        media_type: str,
        expected_digest: str | None = None,
        expected_size: int | None = None,
        max_bytes: int | None = None,
    ) -> BlobRef: ...

    def stat(self, reference: BlobRef) -> BlobRef: ...

    def read(
        self,
        reference: BlobRef,
        *,
        chunk_size: int | None = None,
        max_bytes: int | None = None,
    ) -> Iterator[bytes]: ...

    def read_range(self, reference: BlobRef, *, start: int, end: int) -> bytes: ...

    def materialize(self, reference: BlobRef, root: Path, relative_path: str) -> Path: ...

    def verify(self, reference: BlobRef) -> None: ...
