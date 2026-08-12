"""Contained local, HTTPS, and anonymous-S3 acquisition behind sealed routes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Self
from urllib.parse import quote, unquote, urljoin, urlsplit

from docspec.domain.content import CandidateFile
from docspec.domain.identity import identity_digest, require_relative_path, require_text, stable_urn
from docspec.errors import DocSpecError, IntegrityError, LimitExceededError
from docspec.ports.content_fetcher import ContentFetcher, FetchMetadata, FetchStream

_MISSING_CODES = frozenset({"404", "NoSuchKey", "NoSuchObject", "NotFound"})
_CHANGED_CODES = frozenset({"412", "PreconditionFailed"})
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


class HttpsContentFetcherError(ConnectionError, DocSpecError):
    """An HTTPS operation failed without exposing provider details."""


class S3ContentFetcherError(ConnectionError, DocSpecError):
    """An anonymous S3 operation failed without exposing provider details."""


def _https_host(value: object) -> str:
    host = require_text(value, "HTTPS allowed host").lower()
    try:
        host.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("HTTPS allowed host must use its ASCII spelling") from error
    parsed = urlsplit(f"https://{host}/")
    if (
        parsed.hostname != host
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or host.endswith(".")
    ):
        raise ValueError("HTTPS allowed host must be one exact host name")
    return host


@dataclass(frozen=True, slots=True)
class HttpsContentFetcherConfig:
    """Identity-bearing network and redirect bounds for HTTPS acquisition."""

    allowed_hosts: tuple[str, ...]
    user_agent: str
    chunk_size: int = 1024 * 1024
    connect_timeout_seconds: int = 30
    read_timeout_seconds: int = 120
    max_redirects: int = 5
    max_connections: int = 16

    def __post_init__(self) -> None:
        hosts = tuple(sorted({_https_host(host) for host in self.allowed_hosts}))
        if not hosts:
            raise ValueError("HTTPS acquisition requires at least one allowed host")
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "user_agent", require_text(self.user_agent, "HTTPS user agent"))
        for name in (
            "chunk_size",
            "connect_timeout_seconds",
            "read_timeout_seconds",
            "max_connections",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(self.max_redirects, bool) or not isinstance(self.max_redirects, int) or self.max_redirects < 0:
            raise ValueError("max_redirects must be a non-negative integer")

    def identity_content(self) -> dict[str, Any]:
        return {
            "format": "docspec-https-content-fetcher-config",
            "formatVersion": "1.0",
            "allowedHosts": list(self.allowed_hosts),
            "userAgent": self.user_agent,
            "chunkSize": self.chunk_size,
            "connectTimeoutSeconds": self.connect_timeout_seconds,
            "readTimeoutSeconds": self.read_timeout_seconds,
            "maxRedirects": self.max_redirects,
            "maxConnections": self.max_connections,
            "acceptEncoding": "identity",
        }

    @property
    def digest(self) -> str:
        return identity_digest(self.identity_content())


@dataclass(frozen=True, slots=True)
class AnonymousS3ContentFetcherConfig:
    """Identity-bearing bounds for one public S3 source prefix."""

    bucket: str
    prefix: str
    region_name: str = "us-east-1"
    chunk_size: int = 1024 * 1024
    connect_timeout_seconds: int = 30
    read_timeout_seconds: int = 30
    sdk_max_attempts: int = 3
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
            "sdk_max_attempts",
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
            "formatVersion": "1.0",
            "bucket": self.bucket,
            "prefix": self.prefix,
            "regionName": self.region_name,
            "chunkSize": self.chunk_size,
            "connectTimeoutSeconds": self.connect_timeout_seconds,
            "readTimeoutSeconds": self.read_timeout_seconds,
            "sdkMaxAttempts": self.sdk_max_attempts,
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


def _provider_error_identity(error: Exception) -> tuple[str | None, int | None]:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None, None
    details = response.get("Error")
    metadata = response.get("ResponseMetadata")
    code = details.get("Code") if isinstance(details, Mapping) else None
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
    return str(code) if code is not None else None, status if isinstance(status, int) else None


def _close_body(body: object) -> None:
    close = getattr(body, "close", None)
    if callable(close):
        close()


def _validated_https_url(url: object, allowed_hosts: tuple[str, ...], label: str) -> str:
    try:
        value = require_text(url, label)
    except ValueError as error:
        raise IntegrityError(str(error)) from error
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise IntegrityError(f"{label} contains a control character")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise IntegrityError(f"{label} has an invalid port") from error
    host = parsed.hostname.lower() if parsed.hostname is not None else None
    if (
        parsed.scheme != "https"
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise IntegrityError(f"{label} must be a fragment-free HTTPS URL without credentials or an explicit port")
    if host not in allowed_hosts:
        raise IntegrityError(f"{label} host is outside the configured HTTPS boundary")
    return value


def _content_length(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise IntegrityError("HTTPS response content length is invalid")
    return int(value)


class HttpsContentFetcher:
    """Stream HTTPS candidates through an exact host and redirect allowlist."""

    downloader_id = "docspec.content-fetcher.https.v1"

    def __init__(self, client: Any, config: HttpsContentFetcherConfig) -> None:
        if client is None or not callable(getattr(client, "stream", None)):
            raise ValueError("HTTPS client must provide streaming requests")
        self.client = client
        self.config = config
        self.configuration_digest = config.digest

    @classmethod
    def from_httpx(cls, config: HttpsContentFetcherConfig) -> Self:
        """Create a bounded client without importing httpx in the core package."""

        try:
            import httpx  # type: ignore[import-not-found]
        except ImportError as error:
            raise HttpsContentFetcherError("install the docspec[http] extra for HTTPS acquisition") from error
        try:
            client = httpx.Client(
                timeout=httpx.Timeout(
                    config.read_timeout_seconds,
                    connect=config.connect_timeout_seconds,
                ),
                follow_redirects=False,
                limits=httpx.Limits(
                    max_connections=config.max_connections,
                    max_keepalive_connections=config.max_connections,
                ),
            )
        except Exception as error:
            raise HttpsContentFetcherError("could not create the HTTPS source client") from error
        return cls(client, config)

    def _open(self, url: str) -> tuple[Any, Any]:
        try:
            context = self.client.stream(
                "GET",
                url,
                headers={
                    "User-Agent": self.config.user_agent,
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                },
                follow_redirects=False,
            )
            return context, context.__enter__()
        except Exception as error:
            raise HttpsContentFetcherError("HTTPS acquisition failed") from error

    @staticmethod
    def _close(context: Any) -> None:
        try:
            context.__exit__(None, None, None)
        except Exception as error:
            raise HttpsContentFetcherError("HTTPS response could not be closed") from error

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
        current_url = _validated_https_url(candidate.locator, self.config.allowed_hosts, "candidate locator")
        visited = {current_url}
        redirects = 0
        context: Any | None = None
        response: Any | None = None

        while True:
            context, response = self._open(current_url)
            status = getattr(response, "status_code", None)
            headers = getattr(response, "headers", None)
            if isinstance(status, bool) or not isinstance(status, int) or not isinstance(headers, Mapping):
                self._close(context)
                raise HttpsContentFetcherError("HTTPS acquisition returned an invalid response")
            if status not in _REDIRECT_CODES:
                break
            location = headers.get("location")
            try:
                if redirects >= self.config.max_redirects:
                    raise IntegrityError("HTTPS candidate exceeds the configured redirect limit")
                if not isinstance(location, str) or not location:
                    raise IntegrityError("HTTPS redirect location must be a non-empty string")
                redirected = _validated_https_url(
                    urljoin(current_url, location),
                    self.config.allowed_hosts,
                    "HTTPS redirect location",
                )
                if redirected in visited:
                    raise IntegrityError("HTTPS candidate contains a redirect cycle")
            except Exception:
                self._close(context)
                raise
            self._close(context)
            redirects += 1
            current_url = redirected
            visited.add(redirected)

        assert context is not None and response is not None
        status = response.status_code
        if status == 429 or status >= 500:
            self._close(context)
            raise HttpsContentFetcherError("HTTPS acquisition returned a retryable response")
        if status != 200:
            self._close(context)
            raise IntegrityError(f"HTTPS candidate returned status {status}")

        try:
            content_encoding = response.headers.get("content-encoding")
            if content_encoding is not None and content_encoding.lower().strip() not in {"", "identity"}:
                raise IntegrityError("HTTPS response ignored the identity content-encoding requirement")
            declared_length = _content_length(response.headers.get("content-length"))
            if declared_length is not None and declared_length > max_bytes:
                raise LimitExceededError(f"candidate exceeds the {max_bytes}-byte acquisition limit")
            if candidate.expected_size is not None and (
                declared_length is not None and declared_length != candidate.expected_size
            ):
                raise IntegrityError("HTTPS response content length differs from the sealed candidate")
        except Exception:
            self._close(context)
            raise

        started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        response_closed = False

        def close_response_once() -> None:
            nonlocal response_closed
            if not response_closed:
                response_closed = True
                self._close(context)

        def chunks() -> Any:
            seen = 0
            try:
                try:
                    iterator = response.iter_raw(chunk_size=self.config.chunk_size)
                    for chunk in iterator:
                        if not isinstance(chunk, bytes):
                            raise HttpsContentFetcherError("HTTPS streaming response returned non-byte content")
                        if not chunk:
                            continue
                        seen += len(chunk)
                        if seen > max_bytes:
                            raise LimitExceededError(f"candidate exceeds the {max_bytes}-byte acquisition limit")
                        if candidate.expected_size is not None and seen > candidate.expected_size:
                            raise IntegrityError("HTTPS response exceeds the sealed candidate size")
                        yield chunk
                except (HttpsContentFetcherError, IntegrityError, LimitExceededError):
                    raise
                except Exception as error:
                    raise HttpsContentFetcherError("HTTPS streaming read failed") from error
            finally:
                close_response_once()
            if declared_length is not None and seen != declared_length:
                raise IntegrityError("HTTPS response is truncated")
            if candidate.expected_size is not None and seen != candidate.expected_size:
                raise IntegrityError("HTTPS response differs from the sealed candidate size")

        return FetchStream(
            FetchMetadata(
                self.downloader_id,
                self.configuration_digest,
                candidate.transport_version,
                started_at,
                task_id,
                attempt_id,
            ),
            chunks(),
            close_callback=close_response_once,
        )


class AnonymousS3ContentFetcher:
    """Stream conditionally pinned public S3 objects into DocSpec."""

    downloader_id = "docspec.content-fetcher.anonymous-s3.v1"

    def __init__(self, client: Any, config: AnonymousS3ContentFetcherConfig) -> None:
        if client is None:
            raise ValueError("S3 source client must be provided")
        self.client = client
        self.config = config
        self.configuration_digest = config.digest

    @classmethod
    def from_boto3(cls, config: AnonymousS3ContentFetcherConfig) -> Self:
        """Create an unsigned client without importing boto3 in the core package."""

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
                    retries={"max_attempts": config.sdk_max_attempts, "mode": "standard"},
                    max_pool_connections=config.max_pool_connections,
                ),
            )
        except Exception as error:
            raise S3ContentFetcherError("could not create the anonymous S3 source client") from error
        return cls(client, config)

    def _candidate_record(self, candidate: CandidateFile) -> dict[str, Any]:
        metadata = candidate.metadata
        raw = metadata.get("s3") if isinstance(metadata, Mapping) else None
        if not isinstance(raw, Mapping) or set(raw) != {"bucket", "key", "size", "etag", "lastModified"}:
            raise IntegrityError("S3 candidate metadata has an invalid closed shape")
        try:
            record = _s3_version_content(
                bucket=raw["bucket"],
                key=raw["key"],
                size=raw["size"],
                etag=raw["etag"],
                last_modified=raw["lastModified"],
            )
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"S3 candidate metadata is invalid: {error}") from error
        parsed = urlsplit(candidate.locator)
        try:
            parsed_port = parsed.port
        except ValueError as error:
            raise IntegrityError("S3 candidate locator is not canonical") from error
        key = unquote(parsed.path.removeprefix("/"))
        if (
            parsed.scheme != "s3"
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
            or parsed_port is not None
            or candidate.locator != s3_locator(parsed.netloc, key)
        ):
            raise IntegrityError("S3 candidate locator is not canonical")
        if parsed.netloc != record["bucket"] or key != record["key"]:
            raise IntegrityError("S3 candidate locator differs from its sealed metadata")
        if record["bucket"] != self.config.bucket or not record["key"].startswith(self.config.prefix):
            raise IntegrityError("S3 candidate is outside the configured source boundary")
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
        record = self._candidate_record(candidate)
        if record["size"] > max_bytes:
            raise LimitExceededError(f"candidate exceeds the {max_bytes}-byte acquisition limit")
        started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        try:
            response = self.client.get_object(
                Bucket=record["bucket"],
                Key=record["key"],
                IfMatch=record["etag"],
            )
        except Exception as error:
            code, status = _provider_error_identity(error)
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
                candidate.transport_version,
                started_at,
                task_id,
                attempt_id,
            ),
            chunks(),
            close_callback=close_body_once,
        )


class RoutingContentFetcher:
    """Route configured locator schemes through sealed delegates."""

    downloader_id = "docspec.content-fetcher.routing.v1"

    def __init__(
        self,
        *,
        local: ContentFetcher,
        s3: ContentFetcher,
        https: ContentFetcher | None = None,
        max_object_bytes: int | None = None,
    ) -> None:
        self.local = local
        self.s3 = s3
        self.https = https
        if max_object_bytes is not None and (
            isinstance(max_object_bytes, bool) or not isinstance(max_object_bytes, int) or max_object_bytes <= 0
        ):
            raise ValueError("routing maximum object bytes must be a positive integer")
        self.max_object_bytes = max_object_bytes
        local_id = require_text(getattr(local, "downloader_id", None), "local downloader identity")
        s3_id = require_text(getattr(s3, "downloader_id", None), "S3 downloader identity")
        local_digest = require_text(
            getattr(local, "configuration_digest", None),
            "local downloader configuration digest",
        )
        s3_digest = require_text(
            getattr(s3, "configuration_digest", None),
            "S3 downloader configuration digest",
        )
        routes = [
            {
                "locator": "relative-path",
                "downloaderId": local_id,
                "configurationDigest": local_digest,
            },
            {
                "locator": "s3",
                "downloaderId": s3_id,
                "configurationDigest": s3_digest,
            },
        ]
        if https is not None:
            routes.append(
                {
                    "locator": "https",
                    "downloaderId": require_text(
                        getattr(https, "downloader_id", None),
                        "HTTPS downloader identity",
                    ),
                    "configurationDigest": require_text(
                        getattr(https, "configuration_digest", None),
                        "HTTPS downloader configuration digest",
                    ),
                }
            )
        self.configuration_digest = identity_digest(
            {
                "format": "docspec-routing-content-fetcher-config",
                "formatVersion": "1.0",
                "routes": routes,
                "maximumObjectBytes": max_object_bytes,
            }
        )

    def fetch(
        self,
        candidate: CandidateFile,
        *,
        max_bytes: int,
        task_id: str,
        attempt_id: str,
    ) -> FetchStream:
        parsed = urlsplit(candidate.locator)
        if parsed.scheme == "":
            delegate = self.local
        elif parsed.scheme == "s3":
            delegate = self.s3
        elif parsed.scheme == "https" and self.https is not None:
            delegate = self.https
        else:
            raise IntegrityError("candidate locator scheme is not configured")
        bounded_max_bytes = max_bytes if self.max_object_bytes is None else min(max_bytes, self.max_object_bytes)
        stream = delegate.fetch(
            candidate,
            max_bytes=bounded_max_bytes,
            task_id=task_id,
            attempt_id=attempt_id,
        )
        metadata = replace(
            stream.metadata,
            downloader_id=self.downloader_id,
            downloader_configuration_digest=self.configuration_digest,
        )
        return FetchStream(metadata, stream.chunks, close_callback=stream.close)


__all__ = [
    "AnonymousS3ContentFetcher",
    "AnonymousS3ContentFetcherConfig",
    "HttpsContentFetcher",
    "HttpsContentFetcherConfig",
    "HttpsContentFetcherError",
    "RoutingContentFetcher",
    "S3ContentFetcherError",
    "public_s3_url",
    "s3_locator",
    "s3_transport_version",
]
