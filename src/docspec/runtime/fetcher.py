"""Bind injected fetch evidence to the implementation selected for this worker."""

from __future__ import annotations

from docspec.domain.content import CandidateFile
from docspec.errors import IntegrityError, ProfileError
from docspec.ports.content_fetcher import ContentFetcher, FetchStream, content_fetcher_identity


def _content_fetcher_identity(fetcher: ContentFetcher) -> dict[str, str]:
    try:
        return content_fetcher_identity(fetcher)
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
            stream.metadata.verify_request(candidate, identity=self._identity, task_id=task_id, attempt_id=attempt_id)
        except BaseException:
            stream.close()
            raise
        return stream
