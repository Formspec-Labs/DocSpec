"""Route acquisition through the explicitly configured protocol delegates."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import ClassVar
from urllib.parse import urlsplit

from docspec.domain.content import CandidateFile
from docspec.domain.identity import identity_digest
from docspec.errors import IntegrityError
from docspec.ports.content_fetcher import ContentFetcher, FetchStream, content_fetcher_identity


@dataclass(frozen=True, slots=True)
class RoutingContentFetcher:
    """Compose any nonempty selection of local, S3, and HTTPS fetchers.

    Delegates and the byte allowance are fixed. The configuration digest reads
    each delegate's current settings so a prepared worker can detect mutation.
    The caller owns delegate client lifetimes.
    """

    local: ContentFetcher | None = None
    s3: ContentFetcher | None = None
    https: ContentFetcher | None = None
    max_object_bytes: int | None = None
    downloader_id: ClassVar[str] = "docspec.content-fetcher.routing.v1"

    def __post_init__(self) -> None:
        if self.local is None and self.s3 is None and self.https is None:
            raise ValueError("routing requires at least one configured fetcher")
        if self.max_object_bytes is not None and (
            type(self.max_object_bytes) is not int or self.max_object_bytes <= 0
        ):
            raise ValueError("routing maximum object bytes must be a positive integer")
        # Validate configured identities immediately, including unused routes.
        self.configuration_digest

    @property
    def configuration_digest(self) -> str:
        routes = []
        for locator, delegate in (("relative-path", self.local), ("s3", self.s3), ("https", self.https)):
            if delegate is not None:
                identity = content_fetcher_identity(delegate)
                routes.append({
                    "locator": locator, "downloaderId": identity["implementationId"],
                    "configurationDigest": identity["configurationDigest"],
                })
        return identity_digest({
            "format": "docspec-routing-content-fetcher-config", "formatVersion": "1.0",
            "routes": routes, "maximumObjectBytes": self.max_object_bytes,
        })

    def fetch(
        self, candidate: CandidateFile, *, max_bytes: int, task_id: str, attempt_id: str,
    ) -> FetchStream:
        delegate = {"": self.local, "s3": self.s3, "https": self.https}.get(urlsplit(candidate.locator).scheme)
        if delegate is None:
            raise IntegrityError("candidate locator scheme is not configured")
        configuration = self.configuration_digest
        identity = content_fetcher_identity(delegate)
        allowance = max_bytes if self.max_object_bytes is None else min(max_bytes, self.max_object_bytes)
        stream = delegate.fetch(candidate, max_bytes=allowance, task_id=task_id, attempt_id=attempt_id)
        try:
            stream.metadata.verify_request(candidate, identity=identity, task_id=task_id, attempt_id=attempt_id)
            if self.configuration_digest != configuration:
                raise IntegrityError("routing fetcher configuration changed during acquisition")
            metadata = replace(
                stream.metadata, downloader_id=self.downloader_id, downloader_configuration_digest=configuration,
            )
        except BaseException:
            stream.close()
            raise
        return FetchStream(metadata, stream.chunks, close_callback=stream.close)
