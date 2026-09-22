"""Replaceable, source-grounded segmentation of one representation."""

from __future__ import annotations

from typing import Protocol, TypeVar

from docspec.domain.content import Representation

RepresentationPayload_contra = TypeVar("RepresentationPayload_contra", contravariant=True)
SegmentPayload_co = TypeVar("SegmentPayload_co", covariant=True)


class Segmenter(Protocol[RepresentationPayload_contra, SegmentPayload_co]):
    """Pin a segmenter's identity and split one representation into source-grounded segments."""

    @property
    def segmenter_id(self) -> str: ...

    @property
    def policy_digest(self) -> str: ...

    def selected_identity(self, representation: Representation) -> tuple[str, str]:
        """Return the selected output segmenter ID and policy digest without reading bytes."""
        ...

    def segment(self, representation: RepresentationPayload_contra) -> tuple[SegmentPayload_co, ...]: ...
