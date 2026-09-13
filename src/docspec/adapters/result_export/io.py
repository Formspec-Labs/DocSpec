"""Bounded access to exact members in a local result export."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import gettempdir
from typing import Any, BinaryIO

from rulespec_artifacts import ArtifactVerificationError, LocalMemberSource, MemberDescriptor, MemberSourceError

from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.domain.identity import parse_canonical_json, thaw_json
from docspec.errors import IntegrityError, LimitExceededError

ROOT_BYTES = 1024**2
RECORD_BYTES = 8 * 1024**2
ITEM_BYTES = 64 * 1024**2
EVIDENCE_BYTES = 64 * 1024**2
MANIFEST_BYTES = 64 * 1024**2
CHUNK_BYTES = 256 * 1024
INDEX_KEY = "export.json"
INDEX_SCHEMA = "urn:docspec:result-export-index:1.0"
ROW_SCHEMA = "urn:docspec:delivery-record:1.0"
CONTROL_SCHEMA = "urn:docspec:control-artifact:1.0"
MANIFEST_KEY = "members.json"
ADMISSIONS = ("retained-evidence", "nonempty-text")


def require_limit(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("max_output_bytes must be a positive integer")
    return value


def scratch(max_bytes: int):
    return LocalSqliteReconciliationWorkspaceFactory(
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
    if descriptor.byte_size > max_bytes:
        raise LimitExceededError("export metadata exceeds its byte limit")
    with verified_open(source, descriptor) as stream:
        value = thaw_json(parse_canonical_json(stream.read(max_bytes + 1), label=descriptor.object_key))
    if not isinstance(value, dict):
        raise IntegrityError("export metadata must be a JSON object")
    return value


def rows(source: LocalMemberSource, descriptor: MemberDescriptor) -> Iterator[dict[str, Any]]:
    count = 0
    previous = None
    with verified_open(source, descriptor) as stream:
        while raw := stream.readline(RECORD_BYTES + 1):
            if len(raw) > RECORD_BYTES:
                raise LimitExceededError("export row exceeds its byte limit")
            value = thaw_json(parse_canonical_json(raw, label=descriptor.object_key))
            if not isinstance(value, dict):
                raise IntegrityError("export record must be a JSON object")
            identity = value.get("recordId")
            if not isinstance(identity, str) or not identity or (previous is not None and identity <= previous):
                raise IntegrityError("export records must have sorted distinct record identities")
            previous = identity
            count += 1
            if descriptor.record_count is None or count > descriptor.record_count:
                raise IntegrityError("export row count differs from its descriptor")
            yield value
    if count != descriptor.record_count:
        raise IntegrityError("export row count differs from its descriptor")
