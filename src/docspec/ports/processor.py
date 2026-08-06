"""Injected content processing behind a DocSpec-owned interface."""

from __future__ import annotations

from typing import Protocol, TypeVar

from docspec.domain.processors import ProcessorDescription, ProcessorRequest, ProcessorResult

SegmentPayload_contra = TypeVar("SegmentPayload_contra", contravariant=True)


ProcessorResult_co = TypeVar("ProcessorResult_co", bound=ProcessorResult, covariant=True)


class Processor(Protocol[SegmentPayload_contra, ProcessorResult_co]):
    description: ProcessorDescription

    def process(
        self,
        request: ProcessorRequest,
        payload: SegmentPayload_contra,
        prerequisite_results: tuple[ProcessorResult, ...],
    ) -> ProcessorResult_co: ...
