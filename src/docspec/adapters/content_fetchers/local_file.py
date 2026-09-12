"""Contained local-file acquisition."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from docspec.domain.content import CandidateFile
from docspec.domain.identity import identity_digest, require_relative_path, require_text
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.content_fetcher import FetchMetadata, FetchStream
from docspec.adapters.storage.files import _contained, _storage_root




class LocalFileContentFetcher:
    """Stream only regular files contained below one configured local root."""

    downloader_id = "docspec.content-fetcher.local-file.v1"

    def __init__(self, root: Path, *, chunk_size: int = 1024 * 1024) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self.root = _storage_root(root)
        self.chunk_size = chunk_size

    @property
    def configuration_digest(self) -> str:
        return identity_digest(
            {"implementationId": self.downloader_id, "root": self.root.as_posix(), "chunkSize": self.chunk_size}
        )

    def fetch(
        self,
        candidate: CandidateFile,
        *,
        max_bytes: int,
        task_id: str,
        attempt_id: str,
    ) -> FetchStream:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        require_text(task_id, "task_id")
        require_text(attempt_id, "attempt_id")
        locator = require_relative_path(candidate.locator, "candidate locator")
        path = _contained(self.root, locator)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        initial = os.fstat(descriptor)
        os.close(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise IntegrityError("local acquisition candidate is not a regular file")
        if initial.st_size > max_bytes:
            raise LimitExceededError(f"candidate exceeds the {max_bytes}-byte acquisition limit")
        if candidate.expected_size is not None and candidate.expected_size != initial.st_size:
            raise IntegrityError("local acquisition candidate differs from its expected size")
        started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        version = candidate.transport_version or (
            f"local-stat:{initial.st_dev}:{initial.st_ino}:{initial.st_size}:{initial.st_mtime_ns}"
        )

        def chunks() -> Iterator[bytes]:
            seen = 0
            opened: int | None = os.open(path, flags)
            try:
                current = os.fstat(opened)
                if (
                    current.st_dev != initial.st_dev
                    or current.st_ino != initial.st_ino
                    or current.st_size != initial.st_size
                    or current.st_mtime_ns != initial.st_mtime_ns
                ):
                    raise IntegrityError("local acquisition candidate changed before it was read")
                handle = os.fdopen(opened, "rb")
                opened = None
                with handle:
                    for chunk in iter(lambda: handle.read(self.chunk_size), b""):
                        seen += len(chunk)
                        if seen > max_bytes:
                            raise LimitExceededError(f"candidate exceeds the {max_bytes}-byte acquisition limit")
                        yield chunk
                    final = os.fstat(handle.fileno())
                if (
                    seen != initial.st_size
                    or final.st_dev != initial.st_dev
                    or final.st_ino != initial.st_ino
                    or final.st_mtime_ns != initial.st_mtime_ns
                ):
                    raise IntegrityError("local acquisition candidate changed while it was read")
            finally:
                if opened is not None:
                    try:
                        os.close(opened)
                    except OSError:
                        pass

        return FetchStream(
            FetchMetadata(
                self.downloader_id,
                self.configuration_digest,
                version,
                started_at,
                task_id,
                attempt_id,
            ),
            chunks(),
        )
