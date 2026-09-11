"""Shared acquisition fixtures, extracted from tests.conformance.test_acquisition."""

from __future__ import annotations

from typing import Any

from docspec.domain.content import CandidateFile
from docspec.ports.content_fetcher import FetchStream
from tests import helpers as _helpers

SharedFixtureContentFetcher = _helpers.SharedFixtureContentFetcher


class _FailFirstFetchFetcher:
    """Delegate to the real fetcher, losing one candidate's first transport."""

    def __init__(self, delegate: SharedFixtureContentFetcher, *, flaky_locator: str) -> None:
        self.delegate = delegate
        self.flaky_locator = flaky_locator
        self.calls: dict[str, int] = {}

    def fetch(self, candidate: CandidateFile, **kwargs: Any) -> FetchStream:
        locator_name = candidate.locator.rsplit("/", 1)[-1]
        count = self.calls.get(locator_name, 0) + 1
        self.calls[locator_name] = count
        stream = self.delegate.fetch(candidate, **kwargs)
        if locator_name != self.flaky_locator or count != 1:
            return stream

        def interrupted() -> Any:
            yield from stream.chunks
            raise ConnectionError("transport ended before verification")

        return FetchStream(stream.metadata, interrupted())
