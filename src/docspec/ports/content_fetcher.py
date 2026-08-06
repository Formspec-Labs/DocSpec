"""Provider-neutral acquisition of one source-catalog candidate."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from docspec.domain.content import CandidateFile
from docspec.domain.identity import require_sha256, require_text


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


@dataclass(frozen=True, slots=True)
class FetchStream:
    metadata: FetchMetadata
    chunks: Iterator[bytes]


class ContentFetcher(Protocol):
    def fetch(
        self,
        candidate: CandidateFile,
        *,
        max_bytes: int,
        task_id: str,
        attempt_id: str,
    ) -> FetchStream: ...


AcquisitionSource = ContentFetcher
