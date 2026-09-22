"""HTTPS acquisition with exact host and redirect bounds."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Self
from urllib.parse import urljoin, urlsplit

from docspec.domain.content import CandidateFile
from docspec.domain.identity import identity_digest, require_text
from docspec.errors import DocSpecError, IntegrityError, LimitExceededError
from docspec.ports.content_fetcher import FetchMetadata, FetchStream


_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


class HttpsContentFetcherError(ConnectionError, DocSpecError):
    """An HTTPS operation failed without exposing provider details."""


def _https_host(value: object) -> str:
    """Validate one exact lowercase ASCII host name, refusing ports, credentials, and a trailing dot."""

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
        """Normalize allowed hosts and refuse an empty host list, bad host spellings, or non-positive bounds."""

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


def _validated_https_url(url: object, allowed_hosts: tuple[str, ...], label: str) -> str:
    """Require a fragment-free HTTPS URL without credentials or explicit port on an allowed host."""

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
    """Parse a declared content length, refusing anything but ASCII digits."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise IntegrityError("HTTPS response content length is invalid")
    return int(value)


class HttpsContentFetcher:
    """Stream HTTPS candidates through an exact host and redirect allowlist."""

    downloader_id = "docspec.content-fetcher.https.v1"

    def __init__(self, client: Any, config: HttpsContentFetcherConfig) -> None:
        """Require a client exposing ``stream``; the config carries the identity-bearing bounds."""

        if client is None or not callable(getattr(client, "stream", None)):
            raise ValueError("HTTPS client must provide streaming requests")
        self.client = client
        self.config = config

    @property
    def configuration_digest(self) -> str:
        return self.config.digest

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
        """Open one streaming GET, reporting client failures as HttpsContentFetcherError."""

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
        """Stream one candidate within the allowed hosts, redirect, size, and identity-encoding bounds.

        A 429 or 5xx response is retryable and raises HttpsContentFetcherError;
        any other non-200 status, redirect bound, or size mismatch refuses with
        IntegrityError.
        """

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
