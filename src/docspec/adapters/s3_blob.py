"""Immutable content-addressed blobs on Amazon S3 or an S3-compatible service."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Self

from docspec.domain.identity import require_relative_path, require_sha256, require_text
from docspec.domain.references import BlobRef
from docspec.errors import DocSpecError, IntegrityError, LimitExceededError

_DIGEST_METADATA = "docspec-sha256"
_SIZE_METADATA = "docspec-byte-size"
_MAX_SINGLE_PUT_BYTES = 5 * 1024**3
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound", "NoSuchObject"})
_CONFLICT_CODES = frozenset({"409", "412", "ConditionalRequestConflict", "PreconditionFailed"})


class S3BlobStoreError(DocSpecError):
    """An S3 operation failed without exposing a provider-specific exception."""


@dataclass(frozen=True, slots=True)
class S3BlobStoreConfig:
    """Portable operating limits shared by Amazon S3 and compatible services."""

    max_blob_bytes: int = _MAX_SINGLE_PUT_BYTES
    transfer_chunk_bytes: int = 1024 * 1024
    staging_directory: Path | None = None

    def __post_init__(self) -> None:
        if isinstance(self.max_blob_bytes, bool) or self.max_blob_bytes <= 0:
            raise ValueError("max_blob_bytes must be a positive integer")
        if self.max_blob_bytes > _MAX_SINGLE_PUT_BYTES:
            raise ValueError("max_blob_bytes must not exceed the 5 GiB single-request limit")
        if isinstance(self.transfer_chunk_bytes, bool) or self.transfer_chunk_bytes <= 0:
            raise ValueError("transfer_chunk_bytes must be a positive integer")
        if self.staging_directory is not None:
            object.__setattr__(self, "staging_directory", Path(self.staging_directory))


def _provider_error_identity(error: Exception) -> tuple[str | None, int | None]:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None, None
    details = response.get("Error")
    metadata = response.get("ResponseMetadata")
    code = details.get("Code") if isinstance(details, Mapping) else None
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
    return str(code) if code is not None else None, status if isinstance(status, int) else None


def _is_provider_error(error: Exception, codes: frozenset[str]) -> bool:
    code, status = _provider_error_identity(error)
    return code in codes or (status is not None and str(status) in codes)


def _response_mapping(value: object, operation: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise S3BlobStoreError(f"S3 {operation} returned an invalid response")
    return value


def _close_body(body: object) -> None:
    close = getattr(body, "close", None)
    if callable(close):
        close()


class S3ContentAddressedBlobStore:
    """Implement ``BlobStore`` once for Amazon S3, R2, and compatible APIs.

    The injected client uses the boto3 S3 client method shapes. Provider response
    objects remain inside this adapter; callers receive only DocSpec records and
    errors.
    """

    def __init__(
        self,
        client: Any,
        *,
        bucket: str,
        prefix: str = "",
        config: S3BlobStoreConfig | None = None,
    ) -> None:
        if client is None:
            raise ValueError("client must be provided")
        self.client = client
        self.bucket = require_text(bucket, "S3 bucket")
        self.prefix = self._normalize_prefix(prefix)
        self.config = config or S3BlobStoreConfig()
        self._staging_directory = self._prepare_staging_directory(self.config.staging_directory)

    @classmethod
    def from_boto3(
        cls,
        *,
        bucket: str,
        prefix: str = "",
        config: S3BlobStoreConfig | None = None,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        profile_name: str | None = None,
        client_options: Mapping[str, Any] | None = None,
    ) -> Self:
        """Create the adapter lazily from the optional ``docspec[s3]`` extra."""

        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as error:
            raise S3BlobStoreError("install the docspec[s3] extra to create a boto3 client") from error

        options = dict(client_options or {})
        if endpoint_url is not None:
            options.setdefault("endpoint_url", endpoint_url)
        if region_name is not None:
            options.setdefault("region_name", region_name)
        try:
            session = boto3.Session(profile_name=profile_name) if profile_name is not None else boto3.Session()
            client = session.client("s3", **options)
        except Exception as error:
            raise S3BlobStoreError("could not create the S3 client") from error
        return cls(client, bucket=bucket, prefix=prefix, config=config)

    @staticmethod
    def _normalize_prefix(prefix: str) -> str:
        if not isinstance(prefix, str):
            raise ValueError("S3 prefix must be a string")
        normalized = prefix.strip("/")
        if not normalized:
            return ""
        if "\\" in normalized:
            raise ValueError("S3 prefix must use forward slashes")
        return require_relative_path(normalized, "S3 prefix")

    @staticmethod
    def _prepare_staging_directory(path: Path | None) -> Path | None:
        if path is None:
            return None
        directory = Path(path)
        if directory.is_symlink():
            raise IntegrityError("S3 staging directory must not be a symlink")
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise IntegrityError("S3 staging directory must be a regular directory")
        return directory.resolve(strict=True)

    @staticmethod
    def _locator(digest: str) -> str:
        hexadecimal = require_sha256(digest, "blob digest").removeprefix("sha256:")
        return f"objects/sha256/{hexadecimal[:2]}/{hexadecimal}"

    def _key(self, locator: str) -> str:
        locator = require_relative_path(locator, "blob locator")
        return f"{self.prefix}/{locator}" if self.prefix else locator

    def _validate_reference(self, reference: BlobRef) -> None:
        if reference.locator != self._locator(reference.digest):
            raise IntegrityError("blob locator does not match its digest")

    def _head_optional(self, reference: BlobRef) -> Mapping[str, Any] | None:
        self._validate_reference(reference)
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=self._key(reference.locator))
        except Exception as error:
            if _is_provider_error(error, _MISSING_CODES):
                return None
            raise S3BlobStoreError("S3 stat failed") from error
        return _response_mapping(response, "stat")

    def _head_required(self, reference: BlobRef) -> Mapping[str, Any]:
        response = self._head_optional(reference)
        if response is None:
            raise S3BlobStoreError("immutable blob does not exist")
        return response

    @staticmethod
    def _validate_head(reference: BlobRef, response: Mapping[str, Any]) -> None:
        byte_size = response.get("ContentLength")
        if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size != reference.byte_size:
            raise IntegrityError("S3 blob size differs from its immutable reference")
        if response.get("ContentType") != reference.media_type:
            raise IntegrityError("S3 blob media type differs from its immutable reference")
        raw_metadata = response.get("Metadata")
        if not isinstance(raw_metadata, Mapping):
            raise IntegrityError("S3 blob lacks DocSpec integrity metadata")
        metadata = {str(key).casefold(): str(value) for key, value in raw_metadata.items()}
        if metadata.get(_DIGEST_METADATA) != reference.digest:
            raise IntegrityError("S3 blob digest metadata differs from its immutable reference")
        if metadata.get(_SIZE_METADATA) != str(reference.byte_size):
            raise IntegrityError("S3 blob size metadata differs from its immutable reference")

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
        if expected_size is not None and (
            isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0
        ):
            raise ValueError("expected_size must be a non-negative integer")
        if max_bytes is not None and (isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0):
            raise ValueError("max_bytes must be a non-negative integer")
        limit = self.config.max_blob_bytes if max_bytes is None else min(self.config.max_blob_bytes, max_bytes)

        descriptor, temporary_name = tempfile.mkstemp(
            prefix="docspec-s3-blob-",
            dir=self._staging_directory,
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        byte_size = 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("blob chunks must be bytes")
                    byte_size += len(chunk)
                    if byte_size > limit:
                        raise LimitExceededError(f"blob exceeds the {limit}-byte write limit")
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())

            actual_digest = f"sha256:{digest.hexdigest()}"
            if expected_digest is not None and actual_digest != expected_digest:
                raise IntegrityError("downloaded bytes differ from the expected digest")
            if expected_size is not None and byte_size != expected_size:
                raise IntegrityError("downloaded bytes differ from the expected size")
            reference = BlobRef(self._locator(actual_digest), actual_digest, byte_size, media_type)

            existing = self._head_optional(reference)
            if existing is not None:
                self._validate_head(reference, existing)
                self.verify(reference)
                return reference

            with temporary.open("rb") as body:
                try:
                    self.client.put_object(
                        Bucket=self.bucket,
                        Key=self._key(reference.locator),
                        Body=body,
                        ContentLength=reference.byte_size,
                        ContentType=reference.media_type,
                        Metadata={
                            _DIGEST_METADATA: reference.digest,
                            _SIZE_METADATA: str(reference.byte_size),
                        },
                        IfNoneMatch="*",
                    )
                except Exception as error:
                    if _is_provider_error(error, _CONFLICT_CODES):
                        raced = self._head_required(reference)
                        self._validate_head(reference, raced)
                        self.verify(reference)
                        return reference
                    raise S3BlobStoreError("S3 conditional blob creation failed") from error

            self._validate_head(reference, self._head_required(reference))
            return reference
        finally:
            temporary.unlink(missing_ok=True)

    def stat(self, reference: BlobRef) -> BlobRef:
        self._validate_head(reference, self._head_required(reference))
        return reference

    def _open_body(self, reference: BlobRef, *, range_header: str | None = None) -> tuple[Mapping[str, Any], Any]:
        arguments = {"Bucket": self.bucket, "Key": self._key(reference.locator)}
        if range_header is not None:
            arguments["Range"] = range_header
        try:
            response = self.client.get_object(**arguments)
        except Exception as error:
            raise S3BlobStoreError("S3 blob read failed") from error
        normalized = _response_mapping(response, "read")
        body = normalized.get("Body")
        if body is None or not callable(getattr(body, "read", None)):
            raise S3BlobStoreError("S3 blob read returned no streaming body")
        return normalized, body

    def read(
        self,
        reference: BlobRef,
        *,
        chunk_size: int | None = None,
        max_bytes: int | None = None,
    ) -> Iterator[bytes]:
        effective_chunk_size = self.config.transfer_chunk_bytes if chunk_size is None else chunk_size
        if (
            isinstance(effective_chunk_size, bool)
            or not isinstance(effective_chunk_size, int)
            or effective_chunk_size <= 0
        ):
            raise ValueError("chunk_size must be a positive integer")
        if max_bytes is not None and (isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0):
            raise ValueError("max_bytes must be a non-negative integer")
        if max_bytes is not None and reference.byte_size > max_bytes:
            raise LimitExceededError(f"blob exceeds the {max_bytes}-byte read limit")
        self._validate_head(reference, self._head_required(reference))

        response, body = self._open_body(reference)
        content_length = response.get("ContentLength")
        if content_length != reference.byte_size:
            _close_body(body)
            raise IntegrityError("S3 read size differs from its immutable reference")
        digest = hashlib.sha256()
        seen = 0
        try:
            while True:
                try:
                    chunk = body.read(effective_chunk_size)
                except Exception as error:
                    raise S3BlobStoreError("S3 streaming read failed") from error
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise S3BlobStoreError("S3 streaming body returned non-byte content")
                seen += len(chunk)
                if seen > reference.byte_size:
                    raise IntegrityError("S3 blob exceeds its immutable byte size")
                digest.update(chunk)
                yield chunk
        finally:
            _close_body(body)
        if seen != reference.byte_size or f"sha256:{digest.hexdigest()}" != reference.digest:
            raise IntegrityError("S3 blob bytes differ from their immutable reference")

    def read_range(self, reference: BlobRef, *, start: int, end: int) -> bytes:
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end < start
            or end > reference.byte_size
        ):
            raise ValueError("blob range must be a contained half-open interval")
        self._validate_head(reference, self._head_required(reference))
        if start == end:
            return b""

        response, body = self._open_body(reference, range_header=f"bytes={start}-{end - 1}")
        expected_size = end - start
        if response.get("ContentLength") != expected_size:
            _close_body(body)
            raise IntegrityError("S3 range response has an unexpected byte size")
        expected_range = f"bytes {start}-{end - 1}/{reference.byte_size}"
        if response.get("ContentRange") not in {None, expected_range}:
            _close_body(body)
            raise IntegrityError("S3 range response does not match the requested interval")
        result = bytearray()
        try:
            while len(result) < expected_size:
                try:
                    chunk = body.read(min(self.config.transfer_chunk_bytes, expected_size - len(result)))
                except Exception as error:
                    raise S3BlobStoreError("S3 range streaming read failed") from error
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise S3BlobStoreError("S3 range body returned non-byte content")
                result.extend(chunk)
            if body.read(1):
                raise IntegrityError("S3 range response exceeded the requested interval")
        finally:
            _close_body(body)
        if len(result) != expected_size:
            raise IntegrityError("S3 range response ended before the requested interval")
        return bytes(result)

    @staticmethod
    def _materialization_destination(root: Path, relative_path: str) -> Path:
        relative = PurePosixPath(require_relative_path(relative_path, "materialized path"))
        root = Path(root)
        if root.is_symlink():
            raise IntegrityError("materialization root must not be a symlink")
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise IntegrityError("materialization root must be a regular directory")
        resolved_root = root.resolve(strict=True)
        parent = resolved_root
        for part in relative.parts[:-1]:
            parent /= part
            if parent.is_symlink():
                raise IntegrityError("materialized path traverses a symlink")
            parent.mkdir(exist_ok=True)
            if parent.is_symlink() or not parent.is_dir():
                raise IntegrityError("materialized path parent must be a regular directory")
        destination = resolved_root.joinpath(*relative.parts)
        if destination.is_symlink():
            raise IntegrityError("materialized path must not be a symlink")
        return destination

    def materialize(self, reference: BlobRef, root: Path, relative_path: str) -> Path:
        destination = self._materialization_destination(root, relative_path)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise IntegrityError(f"refusing to replace materialized file: {relative_path}") from error
        try:
            with os.fdopen(descriptor, "wb") as output:
                for chunk in self.read(reference, chunk_size=self.config.transfer_chunk_bytes):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return destination

    def verify(self, reference: BlobRef) -> None:
        for _ in self.read(reference, chunk_size=self.config.transfer_chunk_bytes):
            pass


__all__ = ["S3BlobStoreConfig", "S3BlobStoreError", "S3ContentAddressedBlobStore"]
