"""Route acquisition through configured protocol delegates."""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlsplit

from docspec.domain.content import CandidateFile
from docspec.domain.identity import identity_digest, require_text
from docspec.errors import IntegrityError
from docspec.ports.content_fetcher import ContentFetcher, FetchStream




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
