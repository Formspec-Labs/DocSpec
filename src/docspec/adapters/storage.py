"""Standard-library local storage profiles with immutable SHA-256 objects."""

from __future__ import annotations

import hashlib
import heapq
import os
import re
import shutil
import sqlite3
import tempfile
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from docspec.application.commit import DocumentReleaseVerifier
from docspec.domain.identity import (
    canonical_json_bytes,
    canonical_json_file_bytes,
    ordered_json_sequence_digest,
    parse_canonical_json,
    require_relative_path,
    require_sha256,
    require_text,
    sha256_digest,
    stable_urn,
    thaw_json,
)
from docspec.domain.jobs import DocumentEntry, DocumentStore, StoreState
from docspec.domain.release import DocumentRelease
from docspec.domain.references import ArtifactRef, BlobRef, DocumentReleaseRef, LayerRef, StoreRef
from docspec.domain.storage import partition_bucket
from docspec.domain.storage import PartitionPolicy, RecordSchema
from docspec.errors import IntegrityError, LimitExceededError, StaleBaseError, StateTransitionError
from docspec.ports.blob_store import BlobStore
from docspec.ports.control_repository import ControlRepository
from docspec.ports.document_store_repository import DocumentStoreRepository
from docspec.ports.record_storage import RecordStorage

_PROFILE_ID = "urn:docspec:profile:record-storage:local-jsonl:1"
_DOCUMENT_STORE_PROFILE_ID = "urn:docspec:profile:document-store-persistence:local-json:1"
_PLANNED_STORE_LAYER_KIND = "planned-document-stores"
_PLANNED_STORE_SCHEMA_ID = "docspec-planned-store-reference/1.0"
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_FILE_CHUNK_BYTES = 1024 * 1024


def _storage_root(path: Path) -> Path:
    path = Path(path)
    if path.is_symlink():
        raise IntegrityError(f"storage root must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise IntegrityError(f"storage root must not be a symlink: {path}")
    return path.resolve(strict=True)


def _contained(root: Path, locator: str, *, create_parents: bool = False) -> Path:
    relative = require_relative_path(locator, "locator")
    parts = PurePosixPath(relative).parts
    cursor = root
    for part in parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise IntegrityError(f"storage locator traverses a symlink: {relative}")
        if create_parents:
            cursor.mkdir(exist_ok=True)
        if cursor.exists() and (not cursor.is_dir() or cursor.is_symlink()):
            raise IntegrityError(f"storage locator parent is not a directory: {relative}")
    candidate = root.joinpath(*parts)
    try:
        candidate.parent.resolve(strict=False).relative_to(root)
    except ValueError as error:
        raise IntegrityError(f"storage locator escapes its root: {relative}") from error
    if candidate.is_symlink():
        raise IntegrityError(f"storage locator is a symlink: {relative}")
    return candidate


def _read_exact(root: Path, locator: str) -> bytes:
    path = _contained(root, locator)
    if not path.is_file() or path.is_symlink():
        raise IntegrityError(f"storage member is missing or not a regular file: {locator}")
    return path.read_bytes()


def _write_once(root: Path, locator: str, payload: bytes) -> Path:
    path = _contained(root, locator, create_parents=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise IntegrityError(f"refusing to replace conflicting immutable member: {locator}") from None
        return path
    finally:
        temporary.unlink(missing_ok=True)


def _publish_directory_once(root: Path, working: Path, locator: str) -> Path:
    relative = PurePosixPath(require_relative_path(locator))
    destination = _contained(root, relative.as_posix())
    parent_probe = (relative.parent / "placeholder").as_posix()
    _contained(root, parent_probe, create_parents=True)
    lock_path = destination.parent / f".{destination.name}.publish.lock"
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise StateTransitionError(f"publication is already in progress for {locator}") from error
    try:
        with os.fdopen(descriptor, "wb") as lock:
            lock.write(working.name.encode("utf-8"))
            lock.flush()
            os.fsync(lock.fileno())
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise IntegrityError(f"immutable distribution path is not a regular directory: {locator}")
            shutil.rmtree(working)
            return destination
        os.rename(working, destination)
        return destination
    finally:
        lock_path.unlink(missing_ok=True)


def _verify_artifact_bytes(reference: ArtifactRef, payload: bytes, *, media_type: str = "application/json") -> None:
    if reference.media_type != media_type:
        raise IntegrityError(f"artifact has unexpected media type: {reference.media_type}")
    if len(payload) != reference.byte_size or sha256_digest(payload) != reference.digest:
        raise IntegrityError("artifact bytes differ from their immutable reference")


def _sha256_file(path: Path, *, chunk_size: int = _FILE_CHUNK_BYTES) -> tuple[str, int]:
    """Hash one regular file in bounded memory and return digest plus size."""

    if chunk_size <= 0:
        raise ValueError("file hash chunk_size must be positive")
    digest = hashlib.sha256()
    byte_size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            byte_size += len(chunk)
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}", byte_size


def _sync_file(path: Path) -> None:
    """Flush a previously closed staging file before immutable publication."""

    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verified_member_path(
    root: Path,
    member: Mapping[str, Any],
    *,
    media_type: str,
    schema_id: str | None = None,
    extra_fields: frozenset[str] = frozenset(),
) -> Path:
    expected = {"path", "mediaType", "byteSize", "digest", "recordCount"}
    if schema_id is not None:
        expected.add("schemaId")
    expected.update(extra_fields)
    if set(member) != expected:
        raise IntegrityError("distribution member has an invalid closed shape")
    if member["mediaType"] != media_type or (schema_id is not None and member["schemaId"] != schema_id):
        raise IntegrityError("distribution member has an unexpected media type or schema")
    if any(
        not isinstance(member[name], int) or isinstance(member[name], bool) or member[name] < 0
        for name in ("byteSize", "recordCount")
    ):
        raise IntegrityError("distribution member counts must be non-negative integers")
    require_sha256(member["digest"], "member digest")
    require_relative_path(member["path"], "member path")
    path = _contained(root, member["path"])
    if not path.is_file() or path.is_symlink() or path.stat().st_size != member["byteSize"]:
        raise IntegrityError("distribution member size or storage type differs from its description")
    digest, byte_size = _sha256_file(path)
    if byte_size != member["byteSize"] or digest != member["digest"]:
        raise IntegrityError("distribution member bytes differ from their description")
    return path


class _DistinctTextIndex:
    """Disk-backed exact membership check with bounded process memory."""

    def __init__(self, directory: Path | None, *, label: str) -> None:
        self._directory = directory
        self._label = label
        self._path: Path | None = None
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> _DistinctTextIndex:
        descriptor, name = tempfile.mkstemp(prefix="distinct-", suffix=".sqlite3", dir=self._directory)
        os.close(descriptor)
        self._path = Path(name)
        self._connection = sqlite3.connect(self._path)
        self._connection.execute("PRAGMA journal_mode = OFF")
        self._connection.execute("PRAGMA synchronous = OFF")
        self._connection.execute("PRAGMA cache_size = -2048")
        self._connection.execute("CREATE TABLE members (value TEXT PRIMARY KEY) WITHOUT ROWID")
        return self

    def add(self, value: str) -> None:
        require_text(value, self._label)
        assert self._connection is not None
        try:
            self._connection.execute("INSERT INTO members(value) VALUES (?)", (value,))
        except sqlite3.IntegrityError as error:
            raise IntegrityError(f"{self._label} repeats {value!r}") from error

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._connection is not None:
            self._connection.close()
        if self._path is not None:
            self._path.unlink(missing_ok=True)


class _BoundedPartitionWriters:
    """Append to many partition shards while holding a fixed number of handles."""

    def __init__(self, directory: Path, *, max_open: int) -> None:
        if max_open <= 0:
            raise ValueError("max_open must be positive")
        self._directory = directory
        self._max_open = max_open
        self._handles: OrderedDict[tuple[int, int], BinaryIO] = OrderedDict()
        self.paths: dict[tuple[int, int], Path] = {}
        self.peak_open = 0

    def _close_oldest(self) -> None:
        _, handle = self._handles.popitem(last=False)
        handle.close()

    def _open(self, key: tuple[int, int]) -> BinaryIO:
        handle = self._handles.pop(key, None)
        if handle is not None:
            self._handles[key] = handle
            return handle
        if len(self._handles) >= self._max_open:
            self._close_oldest()
        partition, sequence = key
        path = self.paths.get(key)
        if path is None:
            descriptor, name = tempfile.mkstemp(
                prefix=f"records-{partition:05d}-{sequence:05d}-",
                dir=self._directory,
            )
            path = Path(name)
            self.paths[key] = path
            mode = "wb"
        else:
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
            mode = "ab"
        try:
            handle = os.fdopen(descriptor, mode)
        except BaseException:
            os.close(descriptor)
            raise
        self._handles[key] = handle
        self.peak_open = max(self.peak_open, len(self._handles))
        return handle

    def write(self, key: tuple[int, int], payload: bytes) -> None:
        self._open(key).write(payload)

    def close(self) -> None:
        while self._handles:
            self._close_oldest()


def _iter_canonical_json_lines(
    path: Path,
    *,
    label: str,
    max_line_bytes: int = 8 * 1024**2,
) -> Iterator[dict[str, Any]]:
    if max_line_bytes <= 0:
        raise ValueError("max_line_bytes must be positive")
    with path.open("rb") as handle:
        number = 0
        while complete_line := handle.readline(max_line_bytes + 2):
            number += 1
            if len(complete_line) > max_line_bytes + 1:
                raise LimitExceededError(f"{label} line exceeds the {max_line_bytes}-byte limit")
            if not complete_line.endswith(b"\n"):
                raise IntegrityError(f"{label} must end at a complete JSON line")
            line = complete_line[:-1]
            if not line:
                raise IntegrityError(f"{label} contains an empty JSON line")
            value = thaw_json(parse_canonical_json(line, label=f"{label} line {number}", file_form=False))
            if not isinstance(value, dict):
                raise IntegrityError(f"{label} line {number} must be a JSON object")
            yield value


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
        digest, byte_size = _sha256_file(path, chunk_size=self.stream_chunk_bytes)
        if byte_size != reference.byte_size or digest != reference.digest:
            raise IntegrityError("blob bytes differ from their immutable reference")


class LocalJsonControlRepository:
    """Persist small closed control artifacts as immutable canonical JSON."""

    def __init__(self, root: Path, *, max_artifact_bytes: int = 8 * 1024**2) -> None:
        if max_artifact_bytes <= 0:
            raise ValueError("max_artifact_bytes must be positive")
        self.root = _storage_root(root)
        self.max_artifact_bytes = max_artifact_bytes

    def put(self, *, kind: str, artifact_id: str, value: Mapping[str, Any]) -> ArtifactRef:
        if "/" in kind or kind in {".", ".."}:
            raise ValueError("control artifact kind must be one path segment")
        require_text(kind, "control artifact kind")
        require_text(artifact_id, "control artifact_id")
        payload = canonical_json_file_bytes(
            {
                "format": "docspec-control-artifact",
                "formatVersion": "1.0",
                "kind": kind,
                "artifactId": artifact_id,
                "value": value,
            }
        )
        if len(payload) > self.max_artifact_bytes:
            raise LimitExceededError(f"control artifact exceeds the {self.max_artifact_bytes}-byte limit")
        digest = sha256_digest(payload)
        hexadecimal = digest.removeprefix("sha256:")
        locator = f"control/{kind}/{hexadecimal[:2]}/{hexadecimal}.json"
        _write_once(self.root, locator, payload)
        return ArtifactRef(artifact_id, locator, digest, "application/json", len(payload))

    def load(self, reference: ArtifactRef) -> dict[str, Any]:
        if reference.byte_size > self.max_artifact_bytes:
            raise LimitExceededError(f"control artifact exceeds the {self.max_artifact_bytes}-byte limit")
        payload = _read_exact(self.root, reference.locator)
        _verify_artifact_bytes(reference, payload)
        root = thaw_json(parse_canonical_json(payload, label=reference.artifact_id))
        expected = {"format", "formatVersion", "kind", "artifactId", "value"}
        if not isinstance(root, dict) or set(root) != expected:
            raise IntegrityError("control artifact root must be a JSON object")
        if root["format"] != "docspec-control-artifact" or root["formatVersion"] != "1.0":
            raise IntegrityError("control artifact root has an unknown format")
        if root["artifactId"] != reference.artifact_id:
            raise IntegrityError("control artifact identity differs from its reference")
        locator = PurePosixPath(require_relative_path(reference.locator, "control artifact locator"))
        if len(locator.parts) != 4 or locator.parts[:1] != ("control",) or locator.parts[1] != root["kind"]:
            raise IntegrityError("control artifact kind differs from its locator")
        value = root["value"]
        if not isinstance(value, dict):
            raise IntegrityError("control artifact value must be a JSON object")
        return value

    def verify(self, reference: ArtifactRef) -> None:
        self.load(reference)


class LocalDocumentStoreRepository:
    """Save every DocumentStore transition as one immutable canonical revision."""

    def __init__(
        self,
        root: Path,
        *,
        max_revision_bytes: int = 64 * 1024**2,
        max_inline_bytes: int = 1024**2,
        max_plan_ledger_bytes: int = 4 * 1024**3,
        max_plan_record_bytes: int = 64 * 1024,
        max_plan_store_count: int = 10_000_000,
        verification_scratch: Path | None = None,
    ) -> None:
        if min(
            max_revision_bytes,
            max_inline_bytes,
            max_plan_ledger_bytes,
            max_plan_record_bytes,
            max_plan_store_count,
        ) <= 0:
            raise ValueError("document store byte limits must be positive")
        if max_inline_bytes > max_revision_bytes:
            raise ValueError("max_inline_bytes must not exceed max_revision_bytes")
        self.root = _storage_root(root)
        self.max_revision_bytes = max_revision_bytes
        self.max_inline_bytes = max_inline_bytes
        self.max_plan_ledger_bytes = max_plan_ledger_bytes
        self.max_plan_record_bytes = max_plan_record_bytes
        self.max_plan_store_count = max_plan_store_count
        self._plan_staging = _contained(self.root, ".staging/plans/placeholder", create_parents=True).parent
        self._verification_scratch = self._external_verification_scratch(verification_scratch)

    def _external_verification_scratch(self, path: Path | None) -> Path | None:
        if path is None:
            return None
        path = Path(path)
        if path.is_symlink():
            raise IntegrityError("verification scratch must not be a symlink")
        path.mkdir(parents=True, exist_ok=True)
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(self.root)
        except ValueError:
            return resolved
        raise IntegrityError("verification scratch must be outside the document-store root")

    @staticmethod
    def _store_key(store_id: str) -> str:
        require_text(store_id, "store_id")
        return hashlib.sha256(store_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _entry_member_locator(digest: str) -> str:
        hexadecimal = require_sha256(digest, "document store entry-member digest").removeprefix("sha256:")
        return f"document-store-members/sha256/{hexadecimal[:2]}/{hexadecimal}.jsonl"

    @staticmethod
    def _plan_key(plan_id: str) -> str:
        require_text(plan_id, "plan_id")
        return hashlib.sha256(plan_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _planned_member_locator(digest: str) -> str:
        hexadecimal = require_sha256(digest, "planned-store member digest").removeprefix("sha256:")
        return f"planned-store-members/sha256/{hexadecimal[:2]}/{hexadecimal}.jsonl"

    @classmethod
    def _planned_ledger_locator(cls, plan_id: str) -> str:
        return f"planned-store-ledgers/{cls._plan_key(plan_id)}/ledger.json"

    def _saved_payload(self, store: DocumentStore) -> bytes:
        document_store_payload = canonical_json_file_bytes(store.to_dict())
        if len(document_store_payload) > self.max_revision_bytes:
            raise LimitExceededError(f"document store revision exceeds the {self.max_revision_bytes}-byte limit")
        if len(document_store_payload) <= self.max_inline_bytes:
            return document_store_payload

        entry_payload = b"".join(canonical_json_bytes(entry.to_dict()) + b"\n" for entry in store.entries)
        if len(entry_payload) > self.max_revision_bytes:
            raise LimitExceededError(f"document store entry ledger exceeds the {self.max_revision_bytes}-byte limit")
        entry_digest = sha256_digest(entry_payload)
        entry_locator = self._entry_member_locator(entry_digest)
        header = store.to_dict()
        del header["entries"]
        root = {
            "format": "docspec-saved-document-store",
            "formatVersion": "1.0",
            "storeId": store.store_id,
            "revision": store.revision,
            "documentStoreDigest": sha256_digest(document_store_payload),
            "documentStore": header,
            "entriesMember": {
                "path": entry_locator,
                "mediaType": "application/x-ndjson",
                "byteSize": len(entry_payload),
                "digest": entry_digest,
                "recordCount": len(store.entries),
                "schemaId": "docspec-document-store-entry/1.0",
            },
        }
        root_payload = canonical_json_file_bytes(root)
        if len(root_payload) > self.max_inline_bytes:
            raise LimitExceededError(f"document store root exceeds the {self.max_inline_bytes}-byte inline limit")
        _write_once(self.root, entry_locator, entry_payload)
        return root_payload

    def save(self, store: DocumentStore) -> StoreRef:
        payload = self._saved_payload(store)
        digest = sha256_digest(payload)
        locator = f"document-stores/{self._store_key(store.store_id)}/revisions/{store.revision:020d}.json"
        try:
            _write_once(self.root, locator, payload)
        except IntegrityError as error:
            raise StateTransitionError(
                f"store {store.store_id} revision {store.revision} already has different immutable content"
            ) from error
        return StoreRef(store.store_id, store.revision, locator, digest)

    def load(self, reference: StoreRef) -> DocumentStore:
        path = _contained(self.root, reference.locator)
        if path.is_file() and path.stat().st_size > self.max_revision_bytes:
            raise LimitExceededError(f"document store revision exceeds the {self.max_revision_bytes}-byte limit")
        payload = _read_exact(self.root, reference.locator)
        if sha256_digest(payload) != reference.digest:
            raise IntegrityError("document store bytes differ from their reference")
        value = thaw_json(parse_canonical_json(payload, label=reference.store_id))
        if not isinstance(value, dict):
            raise IntegrityError("document store root must be a JSON object")
        if value.get("format") == "docspec-saved-document-store":
            value = self._expand_saved_root(value, reference)
        try:
            store = DocumentStore.from_dict(value)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"document store record is invalid: {error}") from error
        if store.store_id != reference.store_id or store.revision != reference.revision:
            raise IntegrityError("document store identity or revision differs from its reference")
        expected = f"document-stores/{self._store_key(store.store_id)}/revisions/{store.revision:020d}.json"
        if reference.locator != expected:
            raise IntegrityError("document store locator differs from its identity and revision")
        return store

    def _expand_saved_root(self, root: dict[str, Any], reference: StoreRef) -> dict[str, Any]:
        expected = {
            "format",
            "formatVersion",
            "storeId",
            "revision",
            "documentStoreDigest",
            "documentStore",
            "entriesMember",
        }
        if set(root) != expected or root["formatVersion"] != "1.0":
            raise IntegrityError("saved document store root has an unknown format or invalid closed shape")
        if root["storeId"] != reference.store_id or root["revision"] != reference.revision:
            raise IntegrityError("saved document store root identity differs from its reference")
        require_sha256(root["documentStoreDigest"], "saved document store digest")
        header = root["documentStore"]
        if not isinstance(header, dict) or "entries" in header:
            raise IntegrityError("saved document store header has an invalid shape")
        member = root["entriesMember"]
        if not isinstance(member, dict):
            raise IntegrityError("saved document store entry member has an invalid shape")
        member_size = member.get("byteSize")
        if not isinstance(member_size, int) or isinstance(member_size, bool) or member_size < 0:
            raise IntegrityError("saved document store entry-member size is invalid")
        if member_size > self.max_revision_bytes:
            raise LimitExceededError(
                f"document store entry ledger exceeds the {self.max_revision_bytes}-byte limit"
            )
        path = _verified_member_path(
            self.root,
            member,
            media_type="application/x-ndjson",
            schema_id="docspec-document-store-entry/1.0",
        )
        if member["path"] != self._entry_member_locator(member["digest"]):
            raise IntegrityError("document store entry-member locator differs from its digest")
        entries: list[dict[str, Any]] = []
        for value in _iter_canonical_json_lines(
            path,
            label="document store entry ledger",
            max_line_bytes=self.max_revision_bytes,
        ):
            try:
                entry = DocumentEntry.from_dict(value)
            except (TypeError, ValueError) as error:
                raise IntegrityError(f"document store entry ledger is invalid: {error}") from error
            entries.append(entry.to_dict())
        if len(entries) != member["recordCount"]:
            raise IntegrityError("document store entry count differs from its member")
        expanded = {**header, "entries": entries}
        if sha256_digest(canonical_json_file_bytes(expanded)) != root["documentStoreDigest"]:
            raise IntegrityError("expanded document store differs from its declared digest")
        return expanded

    def revisions(self, store_id: str) -> tuple[StoreRef, ...]:
        key = self._store_key(store_id)
        directory = _contained(self.root, f"document-stores/{key}/revisions/placeholder").parent
        if not directory.exists():
            return ()
        if directory.is_symlink() or not directory.is_dir():
            raise IntegrityError("document store revision path is not a regular directory")
        references: list[StoreRef] = []
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file() or not re.fullmatch(r"[0-9]{20}\.json", path.name):
                raise IntegrityError("document store revision directory contains an undeclared member")
            revision = int(path.stem)
            payload = path.read_bytes()
            reference = StoreRef(
                store_id,
                revision,
                path.relative_to(self.root).as_posix(),
                sha256_digest(payload),
            )
            self.load(reference)
            references.append(reference)
        return tuple(references)

    def latest(self, store_id: str) -> StoreRef | None:
        revisions = self.revisions(store_id)
        return revisions[-1] if revisions else None

    def _iter_planned_member(
        self,
        path: Path,
        *,
        plan_id: str,
        expected_count: int,
    ) -> Iterator[StoreRef]:
        count = 0
        with _DistinctTextIndex(self._verification_scratch, label="planned-store ledger store_id") as distinct:
            for row in _iter_canonical_json_lines(
                path,
                label="planned-store ledger",
                max_line_bytes=self.max_plan_record_bytes,
            ):
                if set(row) != {"ordinal", "store"} or row["ordinal"] != count:
                    raise IntegrityError("planned-store ledger ordinals must be complete and ordered")
                try:
                    reference = StoreRef.from_dict(row["store"])
                except (TypeError, ValueError) as error:
                    raise IntegrityError(f"planned-store ledger contains an invalid reference: {error}") from error
                if reference.revision != 0:
                    raise IntegrityError("planned-store ledger must contain initial planned revisions")
                distinct.add(reference.store_id)
                store = self.load(reference)
                if store.state != StoreState.PLANNED or store.plan_id != plan_id:
                    raise IntegrityError("planned-store ledger contains a non-planned store or a different plan")
                count += 1
                yield reference
        if count != expected_count:
            raise IntegrityError("planned-store ledger count differs from its root")

    def _planned_root(self, reference: LayerRef) -> tuple[dict[str, Any], Path]:
        if (
            reference.layer_kind != _PLANNED_STORE_LAYER_KIND
            or reference.schema_id != _PLANNED_STORE_SCHEMA_ID
            or reference.profile_id != _DOCUMENT_STORE_PROFILE_ID
        ):
            raise IntegrityError("planned-store ledger reference has an unknown logical profile")
        path = _contained(self.root, reference.state_ref)
        if path.is_file() and path.stat().st_size > self.max_inline_bytes:
            raise LimitExceededError(f"planned-store ledger root exceeds the {self.max_inline_bytes}-byte limit")
        payload = _read_exact(self.root, reference.state_ref)
        if sha256_digest(payload) != reference.digest:
            raise IntegrityError("planned-store ledger root differs from its reference")
        value = thaw_json(parse_canonical_json(payload, label=reference.layer_id))
        expected = {
            "format",
            "formatVersion",
            "ledgerId",
            "layerKind",
            "schemaId",
            "profileId",
            "planId",
            "orderPolicy",
            "member",
            "recordCount",
            "orderedStoreSetDigest",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or value["format"] != "docspec-planned-store-ledger"
            or value["formatVersion"] != "1.0"
            or value["layerKind"] != _PLANNED_STORE_LAYER_KIND
            or value["schemaId"] != _PLANNED_STORE_SCHEMA_ID
            or value["profileId"] != _DOCUMENT_STORE_PROFILE_ID
            or value["orderPolicy"] != "planner-emission-order"
        ):
            raise IntegrityError("planned-store ledger root has an unknown format or invalid closed shape")
        if not isinstance(value["recordCount"], int) or isinstance(value["recordCount"], bool) or value["recordCount"] < 0:
            raise IntegrityError("planned-store ledger count must be a non-negative integer")
        if value["recordCount"] > self.max_plan_store_count:
            raise LimitExceededError(f"planned-store ledger exceeds the {self.max_plan_store_count}-store limit")
        require_sha256(value["orderedStoreSetDigest"], "planned-store ordered-set digest")
        content = {
            "layerKind": value["layerKind"],
            "schemaId": value["schemaId"],
            "profileId": value["profileId"],
            "planId": value["planId"],
            "orderPolicy": value["orderPolicy"],
            "member": value["member"],
            "recordCount": value["recordCount"],
            "orderedStoreSetDigest": value["orderedStoreSetDigest"],
        }
        if value["ledgerId"] != stable_urn("planned-store-ledger", content):
            raise IntegrityError("planned-store ledger identity differs from its content")
        if (
            reference.layer_id != value["ledgerId"]
            or reference.state_ref != self._planned_ledger_locator(value["planId"])
            or reference.record_count != value["recordCount"]
        ):
            raise IntegrityError("planned-store ledger root differs from its reference fields")
        member = value["member"]
        if not isinstance(member, dict):
            raise IntegrityError("planned-store ledger member has an invalid shape")
        member_size = member.get("byteSize")
        if not isinstance(member_size, int) or isinstance(member_size, bool) or member_size < 0:
            raise IntegrityError("planned-store ledger member size is invalid")
        if member_size > self.max_plan_ledger_bytes:
            raise LimitExceededError(
                f"planned-store ledger member exceeds the {self.max_plan_ledger_bytes}-byte limit"
            )
        member_path = _verified_member_path(
            self.root,
            member,
            media_type="application/x-ndjson",
            schema_id=_PLANNED_STORE_SCHEMA_ID,
        )
        if member["path"] != self._planned_member_locator(member["digest"]):
            raise IntegrityError("planned-store member locator differs from its digest")
        if member["recordCount"] != value["recordCount"]:
            raise IntegrityError("planned-store member count differs from its root")
        return value, member_path

    def seal_planned_stores(self, plan_id: str, references: Iterable[StoreRef]) -> LayerRef:
        """Persist one complete ordered job population without retaining it in memory."""

        require_text(plan_id, "plan_id")
        descriptor, temporary_name = tempfile.mkstemp(prefix="planned-stores-", dir=self._plan_staging)
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        byte_count = 0
        store_count = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                with _DistinctTextIndex(self._plan_staging, label="planned-store population store_id") as distinct:
                    for reference in references:
                        if store_count >= self.max_plan_store_count:
                            raise LimitExceededError(
                                f"planned-store ledger exceeds the {self.max_plan_store_count}-store limit"
                            )
                        if reference.revision != 0:
                            raise IntegrityError("only initial planned revisions may enter a planned-store ledger")
                        distinct.add(reference.store_id)
                        store = self.load(reference)
                        if store.state != StoreState.PLANNED or store.plan_id != plan_id:
                            raise IntegrityError("planned-store population contains a non-planned store or another plan")
                        line = canonical_json_bytes({"ordinal": store_count, "store": reference.to_dict()}) + b"\n"
                        if len(line) > self.max_plan_record_bytes:
                            raise LimitExceededError(
                                f"planned-store record exceeds the {self.max_plan_record_bytes}-byte limit"
                            )
                        byte_count += len(line)
                        if byte_count > self.max_plan_ledger_bytes:
                            raise LimitExceededError(
                                f"planned-store ledger exceeds the {self.max_plan_ledger_bytes}-byte limit"
                            )
                        handle.write(line)
                        digest.update(line)
                        store_count += 1
                handle.flush()
                os.fsync(handle.fileno())

            member_digest = f"sha256:{digest.hexdigest()}"
            member_locator = self._planned_member_locator(member_digest)
            member = {
                "path": member_locator,
                "mediaType": "application/x-ndjson",
                "byteSize": byte_count,
                "digest": member_digest,
                "recordCount": store_count,
                "schemaId": _PLANNED_STORE_SCHEMA_ID,
            }
            destination = _contained(self.root, member_locator, create_parents=True)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
            _verified_member_path(
                self.root,
                member,
                media_type="application/x-ndjson",
                schema_id=_PLANNED_STORE_SCHEMA_ID,
            )
            ordered_digest = ordered_json_sequence_digest(
                reference.to_dict()
                for reference in self._iter_planned_member(
                    temporary,
                    plan_id=plan_id,
                    expected_count=store_count,
                )
            )
            content = {
                "layerKind": _PLANNED_STORE_LAYER_KIND,
                "schemaId": _PLANNED_STORE_SCHEMA_ID,
                "profileId": _DOCUMENT_STORE_PROFILE_ID,
                "planId": plan_id,
                "orderPolicy": "planner-emission-order",
                "member": member,
                "recordCount": store_count,
                "orderedStoreSetDigest": ordered_digest,
            }
            ledger_id = stable_urn("planned-store-ledger", content)
            root = {
                "format": "docspec-planned-store-ledger",
                "formatVersion": "1.0",
                "ledgerId": ledger_id,
                **content,
            }
            payload = canonical_json_file_bytes(root)
            if len(payload) > self.max_inline_bytes:
                raise LimitExceededError(f"planned-store ledger root exceeds the {self.max_inline_bytes}-byte limit")
            locator = self._planned_ledger_locator(plan_id)
            try:
                _write_once(self.root, locator, payload)
            except IntegrityError as error:
                raise StateTransitionError("plan already has a different immutable store population") from error
            return LayerRef(
                ledger_id,
                _PLANNED_STORE_LAYER_KIND,
                _PLANNED_STORE_SCHEMA_ID,
                _DOCUMENT_STORE_PROFILE_ID,
                locator,
                sha256_digest(payload),
                store_count,
            )
        finally:
            temporary.unlink(missing_ok=True)

    def planned_store_ledger(self, plan_id: str) -> LayerRef:
        locator = self._planned_ledger_locator(plan_id)
        path = _contained(self.root, locator)
        if path.is_file() and path.stat().st_size > self.max_inline_bytes:
            raise LimitExceededError(f"planned-store ledger root exceeds the {self.max_inline_bytes}-byte limit")
        payload = _read_exact(self.root, locator)
        value = thaw_json(parse_canonical_json(payload, label=f"planned-store ledger for {plan_id}"))
        if not isinstance(value, dict):
            raise IntegrityError("planned-store ledger root must be a JSON object")
        try:
            reference = LayerRef(
                value["ledgerId"],
                value["layerKind"],
                value["schemaId"],
                value["profileId"],
                locator,
                sha256_digest(payload),
                value["recordCount"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(f"planned-store ledger root is invalid: {error}") from error
        self.verify_planned_store_ledger(reference)
        return reference

    def verify_planned_store_ledger(self, reference: LayerRef) -> None:
        root, member_path = self._planned_root(reference)
        count = 0

        def stores() -> Iterator[dict[str, Any]]:
            nonlocal count
            for store_reference in self._iter_planned_member(
                member_path,
                plan_id=root["planId"],
                expected_count=root["recordCount"],
            ):
                count += 1
                yield store_reference.to_dict()

        if ordered_json_sequence_digest(stores()) != root["orderedStoreSetDigest"]:
            raise IntegrityError("planned-store ledger order digest differs from its member")
        if count != root["recordCount"] or count != reference.record_count:
            raise IntegrityError("planned-store ledger count differs")

    def stream_planned_stores(self, reference: LayerRef) -> Iterator[StoreRef]:
        root, member_path = self._planned_root(reference)
        yield from self._iter_planned_member(
            member_path,
            plan_id=root["planId"],
            expected_count=root["recordCount"],
        )


class LocalJsonlRecordStorage:
    """Immutable partitioned JSON Lines layers with reusable partition members."""

    def __init__(
        self,
        root: Path,
        *,
        max_member_bytes: int = 256 * 1024**2,
        max_record_bytes: int = 8 * 1024**2,
        max_root_bytes: int = 32 * 1024**2,
        max_open_members: int = 32,
        max_merge_scratch_bytes: int = 128 * 1024**3,
        merge_scratch_root: Path | None = None,
    ) -> None:
        if min(
            max_member_bytes,
            max_record_bytes,
            max_root_bytes,
            max_open_members,
            max_merge_scratch_bytes,
        ) <= 0:
            raise ValueError("record storage limits must be positive")
        self.root = _storage_root(root)
        self.max_member_bytes = max_member_bytes
        self.max_record_bytes = max_record_bytes
        self.max_root_bytes = max_root_bytes
        self.max_open_members = max_open_members
        self.max_merge_scratch_bytes = max_merge_scratch_bytes
        self.merge_scratch_root = (
            None if merge_scratch_root is None else _storage_root(merge_scratch_root)
        )
        self.last_write_peak_open_members = 0
        self.last_read_peak_open_members = 0
        self._staging = _contained(self.root, ".staging/records", create_parents=True).parent
        self._staging.mkdir(exist_ok=True)

    @staticmethod
    def _bucket(value: str, count: int) -> int:
        return partition_bucket(value, count)

    @staticmethod
    def _member_locator(digest: str) -> str:
        hexadecimal = require_sha256(digest).removeprefix("sha256:")
        return f"record-members/sha256/{hexadecimal[:2]}/{hexadecimal}.jsonl"

    def _load_root(self, reference: LayerRef) -> dict[str, Any]:
        path = _contained(self.root, reference.state_ref)
        if path.is_file() and path.stat().st_size > self.max_root_bytes:
            raise LimitExceededError(f"record layer root exceeds the {self.max_root_bytes}-byte limit")
        payload = _read_exact(self.root, reference.state_ref)
        if sha256_digest(payload) != reference.digest:
            raise IntegrityError("record layer root differs from its reference")
        value = thaw_json(parse_canonical_json(payload, label=reference.layer_id))
        if not isinstance(value, dict):
            raise IntegrityError("record layer root must be a JSON object")
        return value

    def write_layer(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        layer_kind: str,
        schema: RecordSchema,
        partition_policy: PartitionPolicy,
        base: LayerRef | None = None,
        replace_partitions: frozenset[int] | None = None,
    ) -> LayerRef:
        require_text(layer_kind, "layer_kind")
        members: dict[tuple[int, int], dict[str, Any]] = {}
        if base is None:
            if replace_partitions is not None:
                raise ValueError("replace_partitions requires a base layer")
        else:
            if replace_partitions is None:
                raise ValueError("an incremental layer requires replace_partitions")
            if any(partition < 0 or partition >= partition_policy.bucket_count for partition in replace_partitions):
                raise ValueError("replacement partition is outside the layer partition policy")
            self.verify(base)
            base_root = self._load_root(base)
            if (
                base_root["layerKind"] != layer_kind
                or base_root["schema"] != self._schema_dict(schema)
                or base_root["partitionPolicy"] != self._policy_dict(partition_policy)
            ):
                raise IntegrityError("incremental layer is incompatible with its base")
            for member in base_root["members"]:
                partition = member["partition"]
                if partition not in replace_partitions:
                    sequence = member.get("sequence", 0)
                    members[(partition, sequence)] = {**member, "sequence": sequence}

        writers = _BoundedPartitionWriters(self._staging, max_open=self.max_open_members)
        counts: dict[tuple[int, int], int] = {}
        sizes: dict[tuple[int, int], int] = {}
        current_sequences: dict[int, int] = {}
        previous_identity: str | None = None
        try:
            for record in records:
                if set(record) != set(schema.fields):
                    raise IntegrityError("record does not match its closed logical schema")
                identity = require_text(record[schema.identity_field], schema.identity_field)
                partition_value = require_text(record[schema.partition_field], schema.partition_field)
                if previous_identity is not None and identity <= previous_identity:
                    raise IntegrityError("record input must be strictly ordered by logical identity")
                previous_identity = identity
                partition = self._bucket(partition_value, partition_policy.bucket_count)
                if replace_partitions is not None and partition not in replace_partitions:
                    raise IntegrityError("incremental records include a partition not declared for replacement")
                line = canonical_json_bytes(record) + b"\n"
                if len(line) > self.max_record_bytes:
                    raise LimitExceededError(f"record exceeds the {self.max_record_bytes}-byte limit")
                if len(line) > self.max_member_bytes:
                    raise LimitExceededError(
                        f"record exceeds the {self.max_member_bytes}-byte member limit"
                    )
                sequence = current_sequences.get(partition, 0)
                key = (partition, sequence)
                if sizes.get(key, 0) + len(line) > self.max_member_bytes:
                    sequence += 1
                    current_sequences[partition] = sequence
                    key = (partition, sequence)
                size = sizes.get(key, 0) + len(line)
                writers.write(key, line)
                counts[key] = counts.get(key, 0) + 1
                sizes[key] = size
            writers.close()
            for key, temporary in writers.paths.items():
                partition, sequence = key
                _sync_file(temporary)
                digest, byte_size = _sha256_file(temporary)
                if byte_size != sizes[key]:
                    raise IntegrityError("record member size differs from its streamed write count")
                locator = self._member_locator(digest)
                destination = _contained(self.root, locator, create_parents=True)
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    if destination.is_symlink() or not destination.is_file():
                        raise IntegrityError("record member conflicts with an existing immutable object") from None
                    existing_digest, existing_size = _sha256_file(destination)
                    if existing_digest != digest or existing_size != byte_size:
                        raise IntegrityError("record member conflicts with an existing immutable object") from None
                members[key] = {
                    "partition": partition,
                    "sequence": sequence,
                    "path": locator,
                    "mediaType": "application/x-ndjson",
                    "byteSize": byte_size,
                    "digest": digest,
                    "recordCount": counts[key],
                    "schemaId": schema.schema_id,
                }
            ordered_members = [members[key] for key in sorted(members)]
            content = {
                "layerKind": layer_kind,
                "schema": self._schema_dict(schema),
                "profileId": _PROFILE_ID,
                "partitionPolicy": self._policy_dict(partition_policy),
                "members": ordered_members,
                "recordCount": sum(member["recordCount"] for member in ordered_members),
            }
            layer_id = stable_urn("record-layer", content)
            root = {
                "format": "docspec-record-layer",
                "formatVersion": "1.1",
                "layerId": layer_id,
                **content,
            }
            root_payload = canonical_json_file_bytes(root)
            if len(root_payload) > self.max_root_bytes:
                raise LimitExceededError(f"record layer root exceeds the {self.max_root_bytes}-byte limit")
            root_digest = sha256_digest(root_payload)
            hexadecimal = root_digest.removeprefix("sha256:")
            locator = f"record-layers/sha256/{hexadecimal[:2]}/{hexadecimal}.json"
            _write_once(self.root, locator, root_payload)
            reference = LayerRef(
                layer_id,
                layer_kind,
                schema.schema_id,
                _PROFILE_ID,
                locator,
                root_digest,
                content["recordCount"],
            )
            self.verify(reference)
            return reference
        finally:
            writers.close()
            self.last_write_peak_open_members = writers.peak_open
            for temporary in writers.paths.values():
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _schema_dict(schema: RecordSchema) -> dict[str, Any]:
        return {
            "schemaId": schema.schema_id,
            "fields": list(schema.fields),
            "identityField": schema.identity_field,
            "partitionField": schema.partition_field,
        }

    @staticmethod
    def _policy_dict(policy: PartitionPolicy) -> dict[str, Any]:
        return {"policyId": policy.policy_id, "bucketCount": policy.bucket_count}

    @staticmethod
    def _schema_from_root(root: Mapping[str, Any]) -> RecordSchema:
        value = root["schema"]
        if not isinstance(value, dict) or set(value) != {"schemaId", "fields", "identityField", "partitionField"}:
            raise IntegrityError("record layer schema has an invalid closed shape")
        return RecordSchema(value["schemaId"], tuple(value["fields"]), value["identityField"], value["partitionField"])

    @staticmethod
    def _policy_from_root(root: Mapping[str, Any]) -> PartitionPolicy:
        value = root["partitionPolicy"]
        if not isinstance(value, dict) or set(value) != {"policyId", "bucketCount"}:
            raise IntegrityError("record layer partition policy has an invalid closed shape")
        return PartitionPolicy(value["policyId"], value["bucketCount"])

    def _iter_member(self, member: Mapping[str, Any], schema: RecordSchema) -> Iterator[dict[str, Any]]:
        if member["byteSize"] > self.max_member_bytes:
            raise LimitExceededError(f"record member exceeds the {self.max_member_bytes}-byte limit")
        path = _verified_member_path(
            self.root,
            member,
            media_type="application/x-ndjson",
            schema_id=schema.schema_id,
            extra_fields=frozenset(
                {"partition", "sequence"} if "sequence" in member else {"partition"}
            ),
        )
        if member["path"] != self._member_locator(member["digest"]):
            raise IntegrityError("record member bytes or description differ")
        previous: str | None = None
        count = 0
        for value in _iter_canonical_json_lines(
            path,
            label="record member",
            max_line_bytes=self.max_record_bytes,
        ):
            if set(value) != set(schema.fields):
                raise IntegrityError("record member row does not match its closed logical schema")
            identity = require_text(value[schema.identity_field], schema.identity_field)
            if previous is not None and identity <= previous:
                raise IntegrityError("record member identities are not strictly ordered")
            previous = identity
            count += 1
            yield value
        if count != member["recordCount"]:
            raise IntegrityError("record member count differs from its description")

    def _verified_root(self, reference: LayerRef) -> tuple[dict[str, Any], RecordSchema, PartitionPolicy]:
        root = self._load_root(reference)
        expected_root = {
            "format",
            "formatVersion",
            "layerId",
            "layerKind",
            "schema",
            "profileId",
            "partitionPolicy",
            "members",
            "recordCount",
        }
        if (
            set(root) != expected_root
            or root["format"] != "docspec-record-layer"
            or root["formatVersion"] not in {"1.0", "1.1"}
        ):
            raise IntegrityError("record layer root has an unknown format or invalid closed shape")
        schema = self._schema_from_root(root)
        policy = self._policy_from_root(root)
        if root["profileId"] != _PROFILE_ID:
            raise IntegrityError("record layer names an unknown storage profile")
        expected_content = {
            "layerKind": root["layerKind"],
            "schema": root["schema"],
            "profileId": root["profileId"],
            "partitionPolicy": root["partitionPolicy"],
            "members": root["members"],
            "recordCount": root["recordCount"],
        }
        if stable_urn("record-layer", expected_content) != root["layerId"]:
            raise IntegrityError("record layer identity differs from its canonical content")
        if (
            reference.layer_id != root["layerId"]
            or reference.layer_kind != root["layerKind"]
            or reference.schema_id != schema.schema_id
            or reference.profile_id != root["profileId"]
            or reference.record_count != root["recordCount"]
        ):
            raise IntegrityError("record layer root differs from its reference")
        expected_locator = f"record-layers/sha256/{reference.digest[7:9]}/{reference.digest[7:]}.json"
        if reference.state_ref != expected_locator:
            raise IntegrityError("record layer locator differs from its digest")
        if not isinstance(root["members"], list):
            raise IntegrityError("record layer members must be a list")
        member_keys: list[tuple[int, int]] = []
        declared_total = 0
        for member in root["members"]:
            expected_member = {"partition", "path", "mediaType", "byteSize", "digest", "recordCount", "schemaId"}
            if root["formatVersion"] == "1.1":
                expected_member.add("sequence")
            if not isinstance(member, dict) or set(member) != expected_member:
                raise IntegrityError("record layer member has an invalid closed shape")
            partition = member["partition"]
            if not isinstance(partition, int) or isinstance(partition, bool) or not 0 <= partition < policy.bucket_count:
                raise IntegrityError("record member partition is outside its policy")
            sequence = member.get("sequence", 0)
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
                raise IntegrityError("record member sequence is invalid")
            if any(not isinstance(member[name], int) or isinstance(member[name], bool) or member[name] < 0 for name in ("byteSize", "recordCount")):
                raise IntegrityError("record member counts must be non-negative integers")
            require_sha256(member["digest"], "record member digest")
            require_relative_path(member["path"], "record member path")
            if member["path"] != self._member_locator(member["digest"]):
                raise IntegrityError("record member locator differs from its digest")
            declared_total += member["recordCount"]
            member_keys.append((partition, sequence))
        if member_keys != sorted(set(member_keys)):
            raise IntegrityError("record layer partition shards must be distinct and ordered")
        sequences_by_partition: dict[int, list[int]] = {}
        for partition, sequence in member_keys:
            sequences_by_partition.setdefault(partition, []).append(sequence)
        if any(sequences != list(range(len(sequences))) for sequences in sequences_by_partition.values()):
            raise IntegrityError("record layer partition shard sequences must be contiguous")
        if declared_total != root["recordCount"] or declared_total != reference.record_count:
            raise IntegrityError("record layer declared count differs from its members")
        return root, schema, policy

    def _iter_partition_member(
        self,
        member: Mapping[str, Any],
        schema: RecordSchema,
        policy: PartitionPolicy,
    ) -> Iterator[dict[str, Any]]:
        for record in self._iter_member(member, schema):
            if self._bucket(record[schema.partition_field], policy.bucket_count) != member["partition"]:
                raise IntegrityError("logical record appears in the wrong partition")
            yield record

    def _merge_sorted_streams(
        self,
        streams: list[Iterator[dict[str, Any]]],
        schema: RecordSchema,
    ) -> Iterator[dict[str, Any]]:
        """Merge sorted streams with a bounded fan-in and deterministic cleanup."""

        self.last_read_peak_open_members = max(self.last_read_peak_open_members, len(streams))
        heap: list[tuple[str, int, dict[str, Any]]] = []
        previous_identity: str | None = None
        try:
            for index, stream in enumerate(streams):
                record = next(stream, None)
                if record is not None:
                    heapq.heappush(heap, (record[schema.identity_field], index, record))
            while heap:
                identity, index, record = heapq.heappop(heap)
                if previous_identity is not None and identity <= previous_identity:
                    raise IntegrityError("logical record identities are not globally unique and ordered")
                previous_identity = identity
                yield record
                following = next(streams[index], None)
                if following is not None:
                    heapq.heappush(heap, (following[schema.identity_field], index, following))
        finally:
            for stream in streams:
                close = getattr(stream, "close", None)
                if close is not None:
                    close()

    def _iter_merge_run(self, path: Path, schema: RecordSchema) -> Iterator[dict[str, Any]]:
        previous_identity: str | None = None
        for record in _iter_canonical_json_lines(
            path,
            label="record merge run",
            max_line_bytes=self.max_record_bytes,
        ):
            if set(record) != set(schema.fields):
                raise IntegrityError("record merge run row does not match its closed logical schema")
            identity = require_text(record[schema.identity_field], schema.identity_field)
            if previous_identity is not None and identity <= previous_identity:
                raise IntegrityError("record merge run identities are not strictly ordered")
            previous_identity = identity
            yield record

    def _write_merge_run(
        self,
        path: Path,
        streams: list[Iterator[dict[str, Any]]],
        schema: RecordSchema,
    ) -> None:
        with path.open("xb") as handle:
            for record in self._merge_sorted_streams(streams, schema):
                handle.write(canonical_json_bytes(record) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _groups[T](values: list[T], size: int) -> Iterator[list[T]]:
        for offset in range(0, len(values), size):
            yield values[offset : offset + size]

    def _stream_selected_members(
        self,
        selected_members: list[Mapping[str, Any]],
        schema: RecordSchema,
        policy: PartitionPolicy,
    ) -> Iterator[dict[str, Any]]:
        """Externally merge partition shards without opening one file per partition."""

        self.last_read_peak_open_members = 0
        if not selected_members:
            return
        if len(selected_members) <= self.max_open_members:
            streams = [self._iter_partition_member(member, schema, policy) for member in selected_members]
            yield from self._merge_sorted_streams(streams, schema)
            return
        if self.max_open_members < 2:
            raise LimitExceededError("record merge fan-in must be at least two for a multi-member layer")

        selected_bytes = sum(member["byteSize"] for member in selected_members)
        if selected_bytes > self.max_merge_scratch_bytes // 2:
            raise LimitExceededError(
                "record merge requires more than the configured bounded scratch capacity"
            )

        with tempfile.TemporaryDirectory(
            prefix="docspec-record-merge-",
            dir=self.merge_scratch_root,
        ) as directory:
            scratch = Path(directory)
            runs: list[Path] = []
            for index, group in enumerate(self._groups(selected_members, self.max_open_members)):
                run = scratch / f"pass-000-{index:08d}.jsonl"
                streams = [self._iter_partition_member(member, schema, policy) for member in group]
                self._write_merge_run(run, streams, schema)
                runs.append(run)

            pass_number = 1
            while len(runs) > self.max_open_members:
                merged_runs: list[Path] = []
                for index, group in enumerate(self._groups(runs, self.max_open_members)):
                    run = scratch / f"pass-{pass_number:03d}-{index:08d}.jsonl"
                    streams = [self._iter_merge_run(source, schema) for source in group]
                    self._write_merge_run(run, streams, schema)
                    for source in group:
                        source.unlink()
                    merged_runs.append(run)
                runs = merged_runs
                pass_number += 1

            yield from self._merge_sorted_streams(
                [self._iter_merge_run(run, schema) for run in runs],
                schema,
            )

    def verify(self, reference: LayerRef) -> None:
        total = sum(1 for _ in self.stream(reference))
        if total != reference.record_count:
            raise IntegrityError("record layer count differs from its members")

    def stream(
        self,
        reference: LayerRef,
        *,
        partitions: frozenset[int] | None = None,
    ) -> Iterator[dict[str, Any]]:
        root, schema, policy = self._verified_root(reference)
        if partitions is not None and any(partition < 0 or partition >= policy.bucket_count for partition in partitions):
            raise ValueError("selected partition is outside the layer partition policy")
        selected_members = [
            member for member in root["members"] if partitions is None or member["partition"] in partitions
        ]
        yield from self._stream_selected_members(selected_members, schema, policy)

    def lookup(
        self,
        reference: LayerRef,
        record_id: str,
        *,
        partition_value: str | None = None,
    ) -> dict[str, Any] | None:
        require_text(record_id, "record_id")
        root = self._load_root(reference)
        schema = self._schema_from_root(root)
        partitions = None
        if partition_value is not None:
            require_text(partition_value, "partition_value")
            policy = self._policy_from_root(root)
            partitions = frozenset({self._bucket(partition_value, policy.bucket_count)})
        for record in self.stream(reference, partitions=partitions):
            identity = record[schema.identity_field]
            if identity == record_id:
                return record
            if identity > record_id:
                return None
        return None

    def scan_partition_value(
        self,
        reference: LayerRef,
        partition_value: str,
    ) -> Iterator[dict[str, Any]]:
        require_text(partition_value, "partition_value")
        root = self._load_root(reference)
        policy = self._policy_from_root(root)
        partition = self._bucket(partition_value, policy.bucket_count)
        yield from self.stream(reference, partitions=frozenset({partition}))

    def identity_field(self, reference: LayerRef) -> str:
        _, schema, _ = self._verified_root(reference)
        return schema.identity_field

    def schema(self, reference: LayerRef) -> RecordSchema:
        """Return the verified logical schema independently of physical members."""

        _, schema, _ = self._verified_root(reference)
        return schema

    def partition_policy(self, reference: LayerRef) -> PartitionPolicy:
        _, _, policy = self._verified_root(reference)
        return policy


class RootOnlyBlobProfileStateReachability:
    """Traverse the portable profile root, whose membership lives in records."""

    def references(
        self,
        reference: ArtifactRef,
        state: Mapping[str, Any],
    ) -> Iterator[BlobRef]:
        del reference
        if set(state) != {"profileId", "profileVersion", "storageRoot"}:
            raise IntegrityError("blob profile state has an invalid closed shape")
        for field in ("profileId", "profileVersion", "storageRoot"):
            require_text(state[field], f"blob profile state {field}")
        yield from ()


def _release_layer(release: DocumentRelease, layer_kind: str) -> LayerRef:
    matches = [layer for layer in release.active_layers if layer.layer_kind == layer_kind]
    if len(matches) != 1:
        raise IntegrityError(f"release must contain exactly one {layer_kind!r} layer")
    return matches[0]


class _LocalDocumentCatalogReader:
    """One verified local release view with partition-directed logical reads."""

    def __init__(self, release: DocumentRelease, records: RecordStorage) -> None:
        self._release = release
        self._records = records

    @property
    def release(self) -> DocumentRelease:
        return self._release

    def lookup(self, *, layer_kind: str, record_id: str) -> dict[str, Any] | None:
        partition_value = record_id if layer_kind == "source-items" else None
        return self._records.lookup(
            _release_layer(self._release, layer_kind),
            record_id,
            partition_value=partition_value,
        )

    def scan(self, *, layer_kind: str) -> Iterator[dict[str, Any]]:
        yield from self._records.stream(_release_layer(self._release, layer_kind))

    def scan_source(
        self,
        *,
        layer_kind: str,
        source_item_id: str,
    ) -> Iterator[dict[str, Any]]:
        require_text(source_item_id, "source_item_id")
        for record in self._records.scan_partition_value(
            _release_layer(self._release, layer_kind),
            source_item_id,
        ):
            if "sourceItemId" not in record:
                raise IntegrityError(f"release layer {layer_kind!r} does not carry source-item identity")
            if record["sourceItemId"] == source_item_id:
                yield record


class LocalManifestDocumentCatalog:
    """Publish complete releases with an operator-only compare-and-swap head."""

    def __init__(
        self,
        root: Path,
        *,
        records: RecordStorage,
        stores: DocumentStoreRepository,
        controls: ControlRepository,
        blobs: BlobStore | None = None,
        max_release_bytes: int = 1024**2,
    ) -> None:
        if max_release_bytes <= 0:
            raise ValueError("max_release_bytes must be positive")
        self.root = _storage_root(root)
        self.records = records
        self.stores = stores
        self.controls = controls
        self.verifier = DocumentReleaseVerifier(
            controls=controls,
            records=records,
            stores=stores,
            blobs=blobs,
        )
        self.max_release_bytes = max_release_bytes

    @staticmethod
    def _release_key(release_id: str) -> str:
        require_text(release_id, "release_id")
        return hashlib.sha256(release_id.encode("utf-8")).hexdigest()

    def _release_locator(self, release_id: str) -> str:
        key = self._release_key(release_id)
        return f"document-catalog/releases/{key[:2]}/{key}.json"

    @staticmethod
    def _parse_release(payload: bytes, *, label: str) -> DocumentRelease:
        value = thaw_json(parse_canonical_json(payload, label=label))
        if not isinstance(value, dict):
            raise IntegrityError("document release root must be a JSON object")
        try:
            return DocumentRelease.from_dict(value)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"document release is invalid: {error}") from error

    def _verify_release_dependencies(self, release: DocumentRelease) -> None:
        self.verifier.verify(release)

    def open(self, reference: DocumentReleaseRef) -> DocumentRelease:
        expected_locator = self._release_locator(reference.release_id)
        if reference.locator != expected_locator:
            raise IntegrityError("document release locator differs from its identity")
        path = _contained(self.root, reference.locator)
        if path.is_file() and path.stat().st_size > self.max_release_bytes:
            raise LimitExceededError(f"document release exceeds the {self.max_release_bytes}-byte limit")
        payload = _read_exact(self.root, reference.locator)
        if sha256_digest(payload) != reference.digest:
            raise IntegrityError("document release bytes differ from their reference")
        release = self._parse_release(payload, label=reference.release_id)
        if release.release_id != reference.release_id:
            raise IntegrityError("document release identity differs from its reference")
        self._verify_release_dependencies(release)
        return release

    def open_reader(self, reference: DocumentReleaseRef) -> _LocalDocumentCatalogReader:
        """Verify once and return a per-operation immutable release reader."""

        return _LocalDocumentCatalogReader(self.open(reference), self.records)

    _layer = staticmethod(_release_layer)

    def lookup(
        self,
        reference: DocumentReleaseRef,
        *,
        layer_kind: str,
        record_id: str,
    ) -> dict[str, Any] | None:
        return self.open_reader(reference).lookup(layer_kind=layer_kind, record_id=record_id)

    def scan(self, reference: DocumentReleaseRef, *, layer_kind: str) -> Iterator[dict[str, Any]]:
        yield from self.open_reader(reference).scan(layer_kind=layer_kind)

    def compare(
        self,
        older: DocumentReleaseRef,
        newer: DocumentReleaseRef,
        *,
        layer_kind: str,
    ) -> Iterator[tuple[str, str]]:
        old_layer = self._layer(self.open(older), layer_kind)
        new_layer = self._layer(self.open(newer), layer_kind)
        old_identity = self.records.identity_field(old_layer)
        new_identity = self.records.identity_field(new_layer)
        if old_identity != new_identity:
            raise IntegrityError("cannot compare layers with different logical identity fields")
        old_records = iter(self.records.stream(old_layer))
        new_records = iter(self.records.stream(new_layer))
        old_record = next(old_records, None)
        new_record = next(new_records, None)
        while old_record is not None or new_record is not None:
            if old_record is None:
                yield new_record[new_identity], "added"
                new_record = next(new_records, None)
            elif new_record is None:
                yield old_record[old_identity], "deleted"
                old_record = next(old_records, None)
            elif old_record[old_identity] < new_record[new_identity]:
                yield old_record[old_identity], "deleted"
                old_record = next(old_records, None)
            elif old_record[old_identity] > new_record[new_identity]:
                yield new_record[new_identity], "added"
                new_record = next(new_records, None)
            else:
                if canonical_json_bytes(old_record) != canonical_json_bytes(new_record):
                    yield old_record[old_identity], "changed"
                old_record = next(old_records, None)
                new_record = next(new_records, None)

    def stage(self, release: DocumentRelease) -> ArtifactRef:
        self._verify_release_dependencies(release)
        payload = release.file_bytes
        if len(payload) > self.max_release_bytes:
            raise LimitExceededError(f"document release exceeds the {self.max_release_bytes}-byte limit")
        digest = sha256_digest(payload)
        locator = f"document-catalog/staged/{digest[7:9]}/{digest[7:]}.json"
        _write_once(self.root, locator, payload)
        return ArtifactRef(release.release_id, locator, digest, "application/json", len(payload))

    def _load_staged(self, reference: ArtifactRef) -> DocumentRelease:
        path = _contained(self.root, reference.locator)
        if path.is_file() and path.stat().st_size > self.max_release_bytes:
            raise LimitExceededError(f"staged document release exceeds the {self.max_release_bytes}-byte limit")
        payload = _read_exact(self.root, reference.locator)
        _verify_artifact_bytes(reference, payload)
        if reference.locator != f"document-catalog/staged/{reference.digest[7:9]}/{reference.digest[7:]}.json":
            raise IntegrityError("staged release locator differs from its digest")
        release = self._parse_release(payload, label=reference.artifact_id)
        if release.release_id != reference.artifact_id:
            raise IntegrityError("staged release identity differs from its reference")
        self._verify_release_dependencies(release)
        return release

    def current(self) -> DocumentReleaseRef | None:
        locator = "document-catalog/current.json"
        path = _contained(self.root, locator)
        if not path.exists():
            return None
        payload = _read_exact(self.root, locator)
        value = thaw_json(parse_canonical_json(payload, label="document catalog current pointer"))
        if not isinstance(value, dict) or set(value) != {"format", "formatVersion", "release"}:
            raise IntegrityError("document catalog current pointer has an invalid closed shape")
        if value["format"] != "docspec-document-catalog-current" or value["formatVersion"] != "1.0":
            raise IntegrityError("document catalog current pointer has an unknown format")
        try:
            reference = DocumentReleaseRef.from_dict(value["release"])
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"document catalog current reference is invalid: {error}") from error
        self.open(reference)
        return reference

    def _write_current(self, reference: DocumentReleaseRef) -> None:
        locator = "document-catalog/current.json"
        destination = _contained(self.root, locator, create_parents=True)
        if destination.is_symlink():
            raise IntegrityError("document catalog current pointer must not be a symlink")
        payload = canonical_json_file_bytes(
            {
                "format": "docspec-document-catalog-current",
                "formatVersion": "1.0",
                "release": reference.to_dict(),
            }
        )
        descriptor, name = tempfile.mkstemp(prefix="current-", dir=destination.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def commit(
        self,
        staged: ArtifactRef,
        *,
        expected_base: DocumentReleaseRef | None,
        stores: Iterable[StoreRef],
    ) -> DocumentReleaseRef:
        release = self._load_staged(staged)
        previous_store_id: str | None = None

        def verified_store_values() -> Iterator[dict[str, Any]]:
            nonlocal previous_store_id
            for reference in stores:
                if previous_store_id is not None and reference.store_id <= previous_store_id:
                    raise IntegrityError("catalog commit store references must be sorted and distinct")
                previous_store_id = reference.store_id
                store = self.stores.load(reference)
                if store.state != StoreState.SEALED:
                    raise IntegrityError("catalog commit contains an unsealed document store")
                yield reference.to_dict()

        if ordered_json_sequence_digest(verified_store_values()) != release.store_receipt_set_digest:
            raise IntegrityError("catalog commit store receipt set differs from the release")
        lock = _contained(self.root, "document-catalog/.commit.lock", create_parents=True)
        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise StateTransitionError("another document catalog commit is in progress") from error
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(release.release_id.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            current = self.current()
            locator = self._release_locator(release.release_id)
            new_reference = release.reference(locator)
            if current == new_reference:
                return new_reference
            if current != expected_base:
                raise StaleBaseError("document catalog current release differs from the expected base")
            if release.previous_release != expected_base:
                raise IntegrityError("document release lineage differs from the expected catalog base")
            _write_once(self.root, locator, release.file_bytes)
            self.open(new_reference)
            self._write_current(new_reference)
            return new_reference
        finally:
            lock.unlink(missing_ok=True)


__all__ = [
    "LocalContentAddressedBlobStore",
    "LocalDocumentStoreRepository",
    "LocalJsonControlRepository",
    "LocalJsonlRecordStorage",
    "LocalManifestDocumentCatalog",
    "RootOnlyBlobProfileStateReachability",
]
