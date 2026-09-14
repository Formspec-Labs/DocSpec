"""Shared ownership and bounded staging for caller-supplied streams."""

import hashlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from docspec.domain.streams import owned_iterator as owned_iterator
from docspec.domain.identity import require_sha256
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.record_storage import BATCH_BYTES as BATCH_BYTES, BATCH_ROWS as BATCH_ROWS



@contextmanager
def staged_bytes(chunks, *, directory: Path | None, limit: int, max_bytes=None, expected_digest=None, expected_size=None):
    """Hash and sync exact input bytes once; remove staging on every exit path."""
    if expected_digest is not None:
        require_sha256(expected_digest, "expected blob digest")
    if expected_size is not None and (type(expected_size) is not int or expected_size < 0):
        raise ValueError("expected_size must be a non-negative integer")
    if type(limit) is not int or limit < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    if max_bytes is not None:
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        limit = min(limit, max_bytes)
    descriptor, name = tempfile.mkstemp(prefix="docspec-blob-", dir=directory)
    temporary = Path(name)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as output, owned_iterator(chunks) as source:
            for chunk in source:
                if not isinstance(chunk, bytes):
                    raise TypeError("blob chunks must be bytes")
                size += len(chunk)
                if size > limit:
                    raise LimitExceededError(f"blob exceeds the {limit}-byte write limit")
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        actual = f"sha256:{digest.hexdigest()}"
        if expected_digest is not None and actual != expected_digest:
            raise IntegrityError("downloaded bytes differ from the expected digest")
        if expected_size is not None and size != expected_size:
            raise IntegrityError("downloaded bytes differ from the expected size")
        yield temporary, actual, size
    finally:
        temporary.unlink(missing_ok=True)
