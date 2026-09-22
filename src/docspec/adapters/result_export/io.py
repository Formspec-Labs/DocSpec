"""Bounded access to exact members in a local result export."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import gettempdir
from typing import Any, BinaryIO

from rulespec_artifacts import ArtifactVerificationError, LocalMemberSource, MemberDescriptor, MemberSourceError

from docspec.adapters.record_workspace import LocalSqliteRecordWorkspaceFactory
from docspec.domain.identity import decode_canonical_json_value
from docspec.errors import IntegrityError, LimitExceededError

ROOT_BYTES = 1024**2
RECORD_BYTES = 8 * 1024**2
MANIFEST_BYTES = 64 * 1024**2
CHUNK_BYTES = 256 * 1024
INDEX_KEY = "export.json"
MANIFEST_KEY = "members.json"


def require_limit(value: int) -> int:
    """Require a positive integer output limit."""

    if type(value) is not int or value <= 0:
        raise ValueError("max_output_bytes must be a positive integer")
    return value


def scratch(max_bytes: int):
    """Create the temporary SQLite index workspace used while reading or writing one export."""

    return LocalSqliteRecordWorkspaceFactory(
        Path(gettempdir()).resolve(), max_spooled_bytes=max_bytes,
        max_record_bytes=RECORD_BYTES, read_batch_size=1,
    ).create()


@contextmanager
def verified_open(source: LocalMemberSource, descriptor: MemberDescriptor) -> Iterator[BinaryIO]:
    """Hash the same open local file before exposing any of its bytes.

    Rulespec pins the root and checks mutation after normal consumption.
    Early closure releases the file; it does not promise a final mutation
    check for an abandoned read. No retained copy is needed for local files.
    """
    if descriptor.object_key is None or descriptor.sha256 is None:
        raise IntegrityError("result exports require embedded local members")
    try:
        with source.open(descriptor.object_key) as stream:
            size = 0
            digest = hashlib.sha256()
            while block := stream.read(min(CHUNK_BYTES, descriptor.byte_size - size + 1)):
                size += len(block)
                if size > descriptor.byte_size:
                    raise IntegrityError("export member grew beyond its admitted descriptor")
                digest.update(block)
            if size != descriptor.byte_size or "sha256:" + digest.hexdigest() != descriptor.sha256:
                raise IntegrityError("export member bytes differ from its admitted descriptor")
            stream.seek(0)
            yield stream
    except (ArtifactVerificationError, MemberSourceError) as error:
        raise IntegrityError(f"export member is invalid: {error}") from error


def read_mapping(source: LocalMemberSource, descriptor: MemberDescriptor, *, max_bytes: int) -> dict[str, Any]:
    """Read one verified member as a canonical JSON object, refusing oversize metadata or a non-object value."""

    if descriptor.byte_size > max_bytes:
        raise LimitExceededError("export metadata exceeds its byte limit")
    with verified_open(source, descriptor) as stream:
        value = decode_canonical_json_value(stream.read(max_bytes + 1), label=descriptor.object_key)
    if not isinstance(value, dict):
        raise IntegrityError("export metadata must be a JSON object")
    return value
