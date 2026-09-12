"""Provider-neutral acquisition of one source-catalog candidate."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Protocol, Self

from docspec.domain.content import CandidateFile
from docspec.domain.identity import require_sha256, require_text
from docspec.errors import IntegrityError


@dataclass(frozen=True, slots=True)
class FetchMetadata:
    downloader_id: str
    downloader_configuration_digest: str
    transport_version: str | None
    acquisition_started_at: str
    task_id: str
    attempt_id: str

    def __post_init__(self) -> None:
        for label, value in (
            ("downloader_id", self.downloader_id),
            ("acquisition_started_at", self.acquisition_started_at),
            ("task_id", self.task_id),
            ("attempt_id", self.attempt_id),
        ):
            require_text(value, label)
        require_sha256(self.downloader_configuration_digest, "downloader configuration digest")
        if self.transport_version is not None:
            require_text(self.transport_version, "transport_version")

    def verify_request(
        self, candidate: CandidateFile, *, identity: dict[str, str], task_id: str, attempt_id: str,
    ) -> None:
        """Bind reported acquisition evidence to the selected implementation and request."""
        if (
            self.downloader_id != identity["implementationId"]
            or self.downloader_configuration_digest != identity["configurationDigest"]
            or self.task_id != task_id
            or self.attempt_id != attempt_id
            or (candidate.transport_version is not None and self.transport_version != candidate.transport_version)
        ):
            raise IntegrityError("fetch metadata differs from the prepared worker or requested acquisition")


@dataclass(slots=True)
class FetchStream:
    metadata: FetchMetadata
    chunks: Iterator[bytes]
    close_callback: Callable[[], None] | None = field(default=None, repr=False, compare=False)
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    def close(self) -> None:
        """Release the iterator and its source exactly once, even before iteration."""

        if self._closed:
            return
        self._closed = True
        first_error: BaseException | None = None
        close_iterator = getattr(self.chunks, "close", None)
        if callable(close_iterator):
            try:
                close_iterator()
            except BaseException as error:
                first_error = error
        if self.close_callback is not None:
            try:
                self.close_callback()
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object | None,
    ) -> bool:
        self.close()
        return False


class ContentFetcher(Protocol):
    @property
    def downloader_id(self) -> str: ...

    @property
    def configuration_digest(self) -> str: ...

    def fetch(
        self,
        candidate: CandidateFile,
        *,
        max_bytes: int,
        task_id: str,
        attempt_id: str,
    ) -> FetchStream: ...


def content_fetcher_identity(fetcher: ContentFetcher) -> dict[str, str]:
    """Read a configured fetcher's declared identity without retaining client secrets."""
    return {
        "implementationId": require_text(getattr(fetcher, "downloader_id", None), "downloader identity"),
        "configurationDigest": require_sha256(
            getattr(fetcher, "configuration_digest", None), "downloader configuration digest",
        ),
    }


AcquisitionSource = ContentFetcher
