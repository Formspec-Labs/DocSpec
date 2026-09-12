"""Bind injected fetch evidence to the implementation selected for this worker."""

from __future__ import annotations

from docspec.domain.content import CandidateFile
from docspec.domain.identity import require_sha256, require_text
from docspec.errors import IntegrityError, ProfileError
from docspec.ports.content_fetcher import ContentFetcher, FetchStream


def _content_fetcher_identity(fetcher: ContentFetcher) -> dict[str, str]:
    try:
        return {
            "implementationId": require_text(getattr(fetcher, "downloader_id", None), "downloader identity"),
            "configurationDigest": require_sha256(
                getattr(fetcher, "configuration_digest", None), "downloader configuration digest",
            ),
        }
    except ValueError as error:
        raise ProfileError(f"content fetcher identity is invalid: {error}") from error


class _BoundContentFetcher:
    """Check the configured fetcher and its receipt before accepting source bytes."""

    def __init__(self, fetcher: ContentFetcher) -> None:
        self._fetcher = fetcher
        self._identity = _content_fetcher_identity(fetcher)
        self.downloader_id = self._identity["implementationId"]
        self.configuration_digest = self._identity["configurationDigest"]

    def fetch(
        self, candidate: CandidateFile, *, max_bytes: int, task_id: str, attempt_id: str,
    ) -> FetchStream:
        try:
            current = _content_fetcher_identity(self._fetcher)
        except ProfileError as error:
            raise IntegrityError("content fetcher identity changed after preparation") from error
        if current != self._identity:
            raise IntegrityError("content fetcher identity changed after preparation")
        stream = self._fetcher.fetch(candidate, max_bytes=max_bytes, task_id=task_id, attempt_id=attempt_id)
        try:
            metadata = stream.metadata
            if (
                metadata.downloader_id != self.downloader_id
                or metadata.downloader_configuration_digest != self.configuration_digest
                or metadata.task_id != task_id
                or metadata.attempt_id != attempt_id
                or metadata.transport_version != candidate.transport_version
            ):
                raise IntegrityError("fetch metadata differs from the prepared worker or requested acquisition")
        except BaseException:
            stream.close()
            raise
        return stream
