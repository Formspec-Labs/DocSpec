"""Shared sinks fixtures, extracted from tests.test_result_sinks_and_recovery."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from docspec.domain.content import AcquisitionDisposition, CandidateFile, SourceItem
from docspec.domain.jobs import ChangeKind, DocumentEntry, DocumentStore
from docspec.domain.plans import StagePolicy, WorkLimits
from docspec.domain.references import ArtifactRef
from docspec.processing.extraction import DefaultExtractorRegistry
from docspec.processing.segmentation import DefaultSegmenterRegistry
from tests.helpers import artifact

NOW = "2026-08-05T12:00:00Z"

STAGES = StagePolicy(
    (DefaultExtractorRegistry.extractor_id,),
    DefaultSegmenterRegistry.segmenter_id,
)


def _clock() -> str:
    return NOW


def _limits(*, max_entries: int = 2) -> WorkLimits:
    return WorkLimits(max_entries, 1024 * 1024, 100, 100, 1000, 1024 * 1024, 60, 3)


def _terminal_store(item_id: str = "source-1") -> DocumentStore:
    source = SourceItem(
        item_id,
        "v1",
        (CandidateFile("primary", f"{item_id}.txt", "text/plain"),),
    )
    entry = replace(
        DocumentEntry.create(source, ChangeKind.ADDED, STAGES),
        disposition=AcquisitionDisposition.CAPTURED,
    )
    return DocumentStore.planned(
        plan_id="plan-1",
        logical_partition="bucket-00000/store-00000000",
        entries=(entry,),
        limits=_limits(),
    ).start("attempt-1")


class _RecordingReceiver:
    def __init__(self, *, fail_on_attempt: int | None = None) -> None:
        self.fail_on_attempt = fail_on_attempt
        self.attempted: list[str] = []
        self.accepted: dict[str, dict[str, Any]] = {}
        self.finished: list[tuple[int, int, str]] = []
        self.result = artifact("returned-result")

    def accept(self, idempotency_key: str, record: Mapping[str, Any]) -> None:
        assert record["idempotencyKey"] == idempotency_key
        self.attempted.append(idempotency_key)
        if self.fail_on_attempt == len(self.attempted):
            self.fail_on_attempt = None
            raise ConnectionError("receiver interrupted before acknowledgement")
        self.accepted.setdefault(idempotency_key, dict(record))

    def finish(self, *, record_count: int, byte_count: int, digest: str) -> ArtifactRef:
        self.finished.append((record_count, byte_count, digest))
        return self.result
