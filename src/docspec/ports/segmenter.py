"""Replaceable, source-grounded segmentation."""

from __future__ import annotations

from typing import Protocol, TypeVar

RepresentationPayload_contra = TypeVar("RepresentationPayload_contra", contravariant=True)
SegmentPayload_co = TypeVar("SegmentPayload_co", covariant=True)


class Segmenter(Protocol[RepresentationPayload_contra, SegmentPayload_co]):
    def segment(self, representation: RepresentationPayload_contra) -> tuple[SegmentPayload_co, ...]: ...
