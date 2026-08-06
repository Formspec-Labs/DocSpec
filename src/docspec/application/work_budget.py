"""Aggregate actual-work limits for one bounded DocumentStore execution."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from docspec.domain.identity import stable_urn
from docspec.domain.jobs import DocumentEntry, EntryExecutionMode
from docspec.domain.plans import WorkLimits
from docspec.errors import IntegrityError, LimitExceededError


@dataclass(frozen=True, slots=True)
class WorkUsage:
    """Observable aggregate counters for one store execution."""

    source_bytes: int
    pages_or_frames: int
    segments: int
    processor_cost: int
    current_memory_bytes: int
    peak_memory_bytes: int

    def to_dict(self) -> dict[str, int]:
        return {
            "sourceBytes": self.source_bytes,
            "pagesOrFrames": self.pages_or_frames,
            "segments": self.segments,
            "processorCost": self.processor_cost,
            "currentMemoryBytes": self.current_memory_bytes,
            "peakMemoryBytes": self.peak_memory_bytes,
        }


class WorkBudget:
    """Apply one set of actual-work counters across an entire DocumentStore.

    Verified logical work is charged once by stable identity. A failed transport
    attempt is governed by the retry policy and is not charged as a second source
    object. Memory is a current/peak reservation, while source bytes, observed
    pages or frames, segments, and processor cost are cumulative store totals.
    Elapsed time covers the active worker attempt; idle time between scheduler
    retries is not execution time. Verified cumulative counters are reconstructed
    from the immutable entry checkpoints when a later attempt resumes the store.
    """

    def __init__(
        self,
        limits: WorkLimits,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self._monotonic = monotonic
        self._started = monotonic()
        self._source_bytes = 0
        self._pages_or_frames = 0
        self._segments = 0
        self._processor_cost = 0
        self._current_memory_bytes = 0
        self._peak_memory_bytes = 0
        self._source_charges: dict[str, int] = {}
        self._page_or_frame_charges: dict[str, int] = {}
        self._segment_charges: dict[str, int] = {}
        self._processor_charges: dict[str, int] = {}
        self._memory: dict[str, int] = {}

    @property
    def usage(self) -> WorkUsage:
        return WorkUsage(
            self._source_bytes,
            self._pages_or_frames,
            self._segments,
            self._processor_cost,
            self._current_memory_bytes,
            self._peak_memory_bytes,
        )

    @property
    def remaining_source_bytes(self) -> int:
        return self.limits.max_estimated_bytes - self._source_bytes

    @property
    def elapsed_seconds(self) -> float:
        elapsed = self._monotonic() - self._started
        if elapsed < 0:
            raise IntegrityError("the work-budget monotonic clock moved backwards")
        return elapsed

    def check_duration(self) -> None:
        elapsed = self.elapsed_seconds
        if elapsed > self.limits.max_duration_seconds:
            raise LimitExceededError(
                "document store elapsed duration "
                f"{elapsed:.6f}s exceeds the {self.limits.max_duration_seconds}s limit"
            )

    def charge_source_bytes(self, identity: str, byte_count: int) -> None:
        self._source_bytes = self._charge_once(
            self._source_charges,
            identity,
            byte_count,
            current=self._source_bytes,
            limit=self.limits.max_estimated_bytes,
            label="source bytes",
        )

    def charge_pages_or_frames(self, identity: str, count: int) -> None:
        self._pages_or_frames = self._charge_once(
            self._page_or_frame_charges,
            identity,
            count,
            current=self._pages_or_frames,
            limit=self.limits.max_pages_or_frames,
            label="pages or frames",
        )

    def charge_segments(self, identity: str, count: int) -> None:
        self._segments = self._charge_once(
            self._segment_charges,
            identity,
            count,
            current=self._segments,
            limit=self.limits.max_segments,
            label="segments",
        )

    def charge_processor(self, identity: str, *, cost: int = 1) -> None:
        self._processor_cost = self._charge_once(
            self._processor_charges,
            identity,
            cost,
            current=self._processor_cost,
            limit=self.limits.max_processor_cost,
            label="processor cost",
        )

    def observe_extraction(
        self,
        identity: str,
        *,
        representation_kind: str,
        metadata: Mapping[str, Any],
    ) -> int:
        """Charge an extractor's actual page/frame count when it exposes one."""

        observed: int | None = None
        for field in ("pageCount", "frameCount"):
            value = metadata.get(field)
            if value is None:
                continue
            if type(value) is not int or value < 0:
                raise IntegrityError(f"extraction metadata {field} must be a non-negative integer")
            if observed is not None:
                raise IntegrityError("extraction metadata cannot declare both pageCount and frameCount")
            observed = value
        if observed is None:
            observed = 1 if representation_kind == "image" else 0
        self.charge_pages_or_frames(identity, observed)
        return observed

    def seed_verified_entries(
        self,
        entries: Iterable[DocumentEntry],
        processor_invocations: Mapping[str, Iterable[str]],
    ) -> None:
        """Restore cumulative counters from verified immutable checkpoints.

        The caller verifies both terminal and partial entries before seeding the
        budget. Stable charge identities make a resumed stage idempotent while
        preserving aggregate limits across worker attempts.
        """

        for entry in entries:
            if entry.execution_mode is EntryExecutionMode.FULL:
                for captured in entry.captured_files:
                    self.charge_source_bytes(
                        self.stage_unit_id(entry.entry_id, "source", captured.file_id),
                        captured.blob.byte_size,
                    )
                segments_by_representation: dict[str, int] = {}
                for segment in entry.segments:
                    segments_by_representation[segment.representation_id] = (
                        segments_by_representation.get(segment.representation_id, 0) + 1
                    )
                for representation in entry.representations:
                    unit_id = self.stage_unit_id(
                        entry.entry_id,
                        "representation",
                        f"{representation.file_id}:{representation.representation_id}",
                    )
                    if representation.kind == "pdf-text":
                        pages = {item.page for item in representation.boundaries if item.page is not None}
                        self.charge_pages_or_frames(unit_id, len(pages))
                    elif representation.kind == "image":
                        self.charge_pages_or_frames(unit_id, 1)
                    else:
                        self.charge_pages_or_frames(unit_id, 0)
                    segment_count = segments_by_representation.get(representation.representation_id, 0)
                    if segment_count:
                        self.charge_segments(unit_id, segment_count)
            for invocation_id in processor_invocations.get(entry.entry_id, ()):
                self.charge_processor(invocation_id)
        self.check_duration()

    @staticmethod
    def stage_unit_id(entry_id: str, stage: str, output_id: str) -> str:
        return stable_urn(
            "work-budget-unit",
            {"entryId": entry_id, "stage": stage, "outputId": output_id},
        )

    @staticmethod
    def processor_invocation_id(
        entry_id: str,
        processor_id: str,
        input_ids: Iterable[str],
    ) -> str:
        return stable_urn(
            "processor-invocation",
            {
                "entryId": entry_id,
                "processorId": processor_id,
                "inputIds": list(input_ids),
            },
        )

    def materialization_scope(self) -> MemoryScope:
        return MemoryScope(self)

    @staticmethod
    def _charge_once(
        charges: dict[str, int],
        identity: str,
        amount: int,
        *,
        current: int,
        limit: int,
        label: str,
    ) -> int:
        if not identity:
            raise ValueError(f"{label} charge identity must be non-empty")
        if type(amount) is not int or amount < 0:
            raise ValueError(f"{label} charge must be a non-negative integer")
        prior = charges.get(identity)
        if prior is not None:
            if prior != amount:
                raise IntegrityError(f"{label} charge identity changed amount")
            return current
        proposed = current + amount
        if proposed > limit:
            raise LimitExceededError(
                f"document store {label} {proposed} exceeds the {limit} limit"
            )
        charges[identity] = amount
        return proposed

    def _reserve_memory(self, identity: str, byte_count: int) -> None:
        if not identity:
            raise ValueError("memory reservation identity must be non-empty")
        if type(byte_count) is not int or byte_count < 0:
            raise ValueError("memory reservation must be a non-negative integer")
        prior = self._memory.get(identity)
        if prior is not None:
            if prior != byte_count:
                raise IntegrityError("memory reservation identity changed size")
            return
        proposed = self._current_memory_bytes + byte_count
        if proposed > self.limits.max_memory_bytes:
            raise LimitExceededError(
                "document store materialized memory "
                f"{proposed} exceeds the {self.limits.max_memory_bytes} limit"
            )
        self._memory[identity] = byte_count
        self._current_memory_bytes = proposed
        self._peak_memory_bytes = max(self._peak_memory_bytes, proposed)

    def _release_memory(self, identity: str) -> None:
        try:
            byte_count = self._memory.pop(identity)
        except KeyError as error:
            raise IntegrityError(f"unknown memory reservation {identity}") from error
        self._current_memory_bytes -= byte_count

    def _rename_memory(self, source: str, destination: str) -> None:
        if destination in self._memory:
            raise IntegrityError(f"memory reservation already exists: {destination}")
        try:
            self._memory[destination] = self._memory.pop(source)
        except KeyError as error:
            raise IntegrityError(f"unknown memory reservation {source}") from error


class MemoryScope(AbstractContextManager["MemoryScope"]):
    """Release all materialized buffers when one entry finishes or fails."""

    def __init__(self, budget: WorkBudget) -> None:
        self._budget = budget
        self._reservations: set[str] = set()
        self._closed = False

    def __enter__(self) -> MemoryScope:
        return self

    def reserve(self, identity: str, byte_count: int) -> None:
        self._require_open()
        self._budget._reserve_memory(identity, byte_count)
        self._reservations.add(identity)

    def release(self, identity: str) -> None:
        self._require_open()
        if identity not in self._reservations:
            raise IntegrityError(f"memory scope does not own reservation {identity}")
        self._budget._release_memory(identity)
        self._reservations.remove(identity)

    def rename(self, source: str, destination: str) -> None:
        self._require_open()
        if source not in self._reservations:
            raise IntegrityError(f"memory scope does not own reservation {source}")
        self._budget._rename_memory(source, destination)
        self._reservations.remove(source)
        self._reservations.add(destination)

    def close(self) -> None:
        if self._closed:
            return
        for identity in tuple(self._reservations):
            self._budget._release_memory(identity)
        self._reservations.clear()
        self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise IntegrityError("memory scope is already closed")

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback
        self.close()
