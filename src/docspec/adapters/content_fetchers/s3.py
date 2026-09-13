"""Anonymous S3 acquisition with sealed transport observations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Self
from urllib.parse import quote, unquote, urlsplit

from docspec.domain.content import CandidateFile
from docspec.domain.identity import identity_digest, require_relative_path, require_text, stable_urn
from docspec.errors import DocSpecError, IntegrityError, LimitExceededError
from docspec.ports.content_fetcher import FetchMetadata, FetchStream
from docspec.adapters.s3_errors import provider_error_identity


_MISSING_CODES = frozenset({"404", "NoSuchKey", "NoSuchObject", "NotFound"})
_CHANGED_CODES = frozenset({"412", "PreconditionFailed"})


class S3ContentFetcherError(ConnectionError, DocSpecError):
    """An anonymous S3 operation failed without exposing provider details."""


@dataclass(frozen=True, slots=True)
class AnonymousS3ContentFetcherConfig:
    """Identity-bearing bounds for one public S3 source prefix."""

    bucket: str
    prefix: str
    region_name: str = "us-east-1"
    chunk_size: int = 1024 * 1024
    connect_timeout_seconds: int = 30
    read_timeout_seconds: int = 30
    sdk_total_attempts: int = 1
    max_pool_connections: int = 16
    anonymous: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "bucket", require_text(self.bucket, "S3 source bucket"))
        normalized_prefix = require_relative_path(self.prefix.strip("/"), "S3 source prefix")
        object.__setattr__(self, "prefix", normalized_prefix)
        object.__setattr__(self, "region_name", require_text(self.region_name, "S3 source region"))
        for name in (
            "chunk_size",
            "connect_timeout_seconds",
            "read_timeout_seconds",
            "sdk_total_attempts",
            "max_pool_connections",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.anonymous is not True:
            raise ValueError("anonymous S3 source configuration must disable credentialed requests")

    def identity_content(self) -> dict[str, Any]:
        return {
            "format": "docspec-anonymous-s3-content-fetcher-config",
            "formatVersion": "2.0",
            "bucket": self.bucket,
            "prefix": self.prefix,
            "regionName": self.region_name,
            "chunkSize": self.chunk_size,
            "connectTimeoutSeconds": self.connect_timeout_seconds,
            "readTimeoutSeconds": self.read_timeout_seconds,
            "sdkTotalAttempts": self.sdk_total_attempts,
            "maxPoolConnections": self.max_pool_connections,
            "anonymous": self.anonymous,
        }

    @property
    def digest(self) -> str:
        return identity_digest(self.identity_content())


def _timestamp(value: object, label: str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise IntegrityError(f"{label} must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return require_text(value, label)


def _s3_version_content(
    *,
    bucket: object,
    key: object,
    size: object,
    etag: object,
    last_modified: object,
) -> dict[str, Any]:
    bucket_text = require_text(bucket, "S3 object bucket")
    key_text = require_relative_path(key, "S3 object key")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("S3 object size must be a non-negative integer")
    return {
        "bucket": bucket_text,
        "key": key_text,
        "size": size,
        "etag": require_text(etag, "S3 object ETag"),
        "lastModified": _timestamp(last_modified, "S3 object last-modified time"),
    }


def s3_transport_version(
    *,
    bucket: object,
    key: object,
    size: object,
    etag: object,
    last_modified: object,
) -> str:
    """Identify the complete S3 transport observation used by one candidate."""

    content = _s3_version_content(
        bucket=bucket,
        key=key,
        size=size,
        etag=etag,
        last_modified=last_modified,
    )
    return stable_urn("s3-transport-version", content)


def s3_locator(bucket: str, key: str) -> str:
    """Encode one bucket and key into the only accepted S3 locator spelling."""

    bucket = require_text(bucket, "S3 object bucket")
    key = require_relative_path(key, "S3 object key")
    return f"s3://{bucket}/{quote(key, safe='/')}"


def public_s3_url(*, bucket: str, key: str, region_name: str) -> str:
    bucket = require_text(bucket, "S3 object bucket")
    key = require_relative_path(key, "S3 object key")
    region_name = require_text(region_name, "S3 source region")
    return f"https://{bucket}.s3.{region_name}.amazonaws.com/{quote(key, safe='/')}"


def _close_body(body: object) -> None:
    close = getattr(body, "close", None)
    if callable(close):
        close()


class AnonymousS3ContentFetcher:
    """Stream conditionally pinned public S3 objects into DocSpec."""

    downloader_id = "docspec.content-fetcher.anonymous-s3.v2"

    def __init__(self, client: Any, config: AnonymousS3ContentFetcherConfig) -> None:
        if client is None:
            raise ValueError("S3 source client must be provided")
        self.client = client
        self.config = config

    @property
    def configuration_digest(self) -> str:
        return self.config.digest

    @classmethod
    def from_boto3(cls, config: AnonymousS3ContentFetcherConfig) -> Self:
        """Create an unsigned client with native SDK total-attempt limits.

        The count includes the first request for each HEAD or GET operation.
        Manually supplied clients remain responsible for their own retries.
        """

        try:
            import boto3  # type: ignore[import-not-found]
            from botocore import UNSIGNED  # type: ignore[import-not-found]
            from botocore.config import Config as BotoConfig  # type: ignore[import-not-found]
        except ImportError as error:
            raise S3ContentFetcherError("install the docspec[s3] extra for anonymous S3 acquisition") from error
        try:
            client = boto3.client(
                "s3",
                region_name=config.region_name,
                config=BotoConfig(
                    signature_version=UNSIGNED,
                    connect_timeout=config.connect_timeout_seconds,
                    read_timeout=config.read_timeout_seconds,
                    retries={"total_max_attempts": config.sdk_total_attempts, "mode": "standard"},
                    max_pool_connections=config.max_pool_connections,
                ),
            )
        except Exception as error:
            raise S3ContentFetcherError("could not create the anonymous S3 source client") from error
        return cls(client, config)

    def _candidate_location(self, candidate: CandidateFile) -> tuple[str, str]:
        try:
            parsed = urlsplit(candidate.locator)
            parsed_port = parsed.port
            key = unquote(parsed.path.removeprefix("/"))
            canonical = s3_locator(parsed.netloc, key)
        except ValueError as error:
            raise IntegrityError("S3 candidate locator is not canonical") from error
        if (
            parsed.scheme != "s3"
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
            or parsed_port is not None
            or candidate.locator != canonical
        ):
            raise IntegrityError("S3 candidate locator is not canonical")
        if parsed.netloc != self.config.bucket or not key.startswith(self.config.prefix):
            raise IntegrityError("S3 candidate is outside the configured source boundary")
        return parsed.netloc, key

    def _observed_record(self, bucket: str, key: str) -> dict[str, Any]:
        """Observe an unpinned candidate before the ordinary conditional download."""

        try:
            response = self.client.head_object(Bucket=bucket, Key=key)
        except Exception as error:
            code, status = provider_error_identity(error)
            if code in _MISSING_CODES or status == 404:
                raise IntegrityError("S3 candidate does not exist") from error
            raise S3ContentFetcherError("anonymous S3 observation failed") from error
        if not isinstance(response, Mapping):
            raise S3ContentFetcherError("anonymous S3 observation returned an invalid response")
        # HEAD has no body; release any unexpected body from an injected client.
        _close_body(response.get("Body"))
        try:
            return _s3_version_content(
                bucket=bucket, key=key, size=response.get("ContentLength"),
                etag=response.get("ETag"), last_modified=response.get("LastModified"),
            )
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"S3 observation metadata is invalid: {error}") from error

    def _candidate_record(self, candidate: CandidateFile) -> dict[str, Any]:
        bucket, key = self._candidate_location(candidate)
        metadata = candidate.metadata
        if candidate.transport_version is None and "s3" not in metadata:
            record = self._observed_record(bucket, key)
            if candidate.expected_size is not None and candidate.expected_size != record["size"]:
                raise IntegrityError("S3 observation size differs from the candidate's expected size")
            return record

        # A supplied pin or observation must be complete and exact. Never replace
        # a malformed or partial caller observation with a fresh remote one.
        raw = metadata.get("s3")
        if not isinstance(raw, Mapping) or set(raw) != {"bucket", "key", "size", "etag", "lastModified"}:
            raise IntegrityError("S3 candidate metadata has an invalid closed shape")
        try:
            record = _s3_version_content(
                bucket=raw["bucket"], key=raw["key"], size=raw["size"],
                etag=raw["etag"], last_modified=raw["lastModified"],
            )
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"S3 candidate metadata is invalid: {error}") from error
        if bucket != record["bucket"] or key != record["key"]:
            raise IntegrityError("S3 candidate locator differs from its sealed metadata")
        if candidate.expected_size != record["size"]:
            raise IntegrityError("S3 candidate size differs from its sealed metadata")
        expected_version = s3_transport_version(
            bucket=record["bucket"],
            key=record["key"],
            size=record["size"],
            etag=record["etag"],
            last_modified=record["lastModified"],
        )
        if candidate.transport_version != expected_version:
            raise IntegrityError("S3 candidate transport version differs from its sealed metadata")
        return record

    def fetch(
        self,
        candidate: CandidateFile,
        *,
        max_bytes: int,
        task_id: str,
        attempt_id: str,
    ) -> FetchStream:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        require_text(task_id, "task_id")
        require_text(attempt_id, "attempt_id")
        if candidate.expected_size is not None and candidate.expected_size > max_bytes:
            raise LimitExceededError(f"candidate exceeds the {max_bytes}-byte acquisition limit")
        started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        record = self._candidate_record(candidate)
        if record["size"] > max_bytes:
            raise LimitExceededError(f"candidate exceeds the {max_bytes}-byte acquisition limit")
        transport_version = s3_transport_version(
            bucket=record["bucket"], key=record["key"], size=record["size"],
            etag=record["etag"], last_modified=record["lastModified"],
        )
        try:
            response = self.client.get_object(
                Bucket=record["bucket"],
                Key=record["key"],
                IfMatch=record["etag"],
            )
        except Exception as error:
            code, status = provider_error_identity(error)
            if code in _MISSING_CODES or status == 404:
                raise IntegrityError("sealed S3 candidate does not exist") from error
            if code in _CHANGED_CODES or status == 412:
                raise IntegrityError("sealed S3 candidate ETag changed") from error
            raise S3ContentFetcherError("anonymous S3 acquisition failed") from error
        if not isinstance(response, Mapping):
            raise S3ContentFetcherError("anonymous S3 acquisition returned an invalid response")
        body = response.get("Body")
        if body is None or not callable(getattr(body, "read", None)):
            _close_body(body)
            raise S3ContentFetcherError("anonymous S3 acquisition returned no streaming body")
        try:
            if response.get("ETag") != record["etag"]:
                raise IntegrityError("S3 response ETag differs from the sealed candidate")
            try:
                response_last_modified = _timestamp(
                    response.get("LastModified"),
                    "S3 response last-modified time",
                )
            except (TypeError, ValueError) as error:
                raise IntegrityError(f"S3 response last-modified time is invalid: {error}") from error
            if response_last_modified != record["lastModified"]:
                raise IntegrityError("S3 response last-modified time differs from the sealed candidate")
            content_length = response.get("ContentLength")
            if isinstance(content_length, bool) or not isinstance(content_length, int):
                raise IntegrityError("S3 response content length is invalid")
            if content_length != record["size"]:
                raise IntegrityError("S3 response content length differs from the sealed candidate")
            if content_length > max_bytes:
                raise LimitExceededError(f"candidate exceeds the {max_bytes}-byte acquisition limit")
        except Exception:
            _close_body(body)
            raise

        body_closed = False

        def close_body_once() -> None:
            nonlocal body_closed
            if not body_closed:
                body_closed = True
                _close_body(body)

        def chunks() -> Any:
            seen = 0
            try:
                while True:
                    try:
                        chunk = body.read(self.config.chunk_size)
                    except Exception as error:
                        raise S3ContentFetcherError("anonymous S3 streaming read failed") from error
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise S3ContentFetcherError("anonymous S3 streaming body returned non-byte content")
                    seen += len(chunk)
                    if seen > max_bytes:
                        raise LimitExceededError(f"candidate exceeds the {max_bytes}-byte acquisition limit")
                    if seen > record["size"]:
                        raise IntegrityError("S3 response exceeds the sealed candidate size")
                    yield chunk
            finally:
                close_body_once()
            if seen != record["size"]:
                raise IntegrityError("S3 response is truncated")

        return FetchStream(
            FetchMetadata(
                self.downloader_id,
                self.configuration_digest,
                transport_version,
                started_at,
                task_id,
                attempt_id,
            ),
            chunks(),
            close_callback=close_body_once,
        )
