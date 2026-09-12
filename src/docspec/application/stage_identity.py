"""Bind selected stage implementations and their outputs to the processing plan."""

from __future__ import annotations

from docspec.domain.content import CapturedFile, Representation, Segment
from docspec.domain.plans import StagePolicy
from docspec.errors import IntegrityError
from docspec.ports.extractor import Extractor
from docspec.ports.segmenter import Segmenter
from docspec.processing.artifacts import RepresentationPayload, SegmentPayload
from docspec.processing.extraction import ExtractionResult


def configured_stage_policy(
    extractor: Extractor[ExtractionResult],
    segmenter: Segmenter[RepresentationPayload, SegmentPayload],
    processor_ids: tuple[str, ...] = (),
) -> StagePolicy:
    """Derive the existing plan value from the objects that will do the work."""

    try:
        return StagePolicy(
            extractor_id=extractor.extractor_id,
            extractor_configuration_digest=extractor.configuration_digest,
            segmenter_id=segmenter.segmenter_id,
            segmenter_policy_digest=segmenter.policy_digest,
            processor_ids=processor_ids,
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise IntegrityError(f"stage implementations must declare valid identities and settings: {error}") from error


def verify_stage_implementations(
    stages: StagePolicy,
    *,
    extractor: Extractor[ExtractionResult],
    segmenter: Segmenter[RepresentationPayload, SegmentPayload],
) -> None:
    if configured_stage_policy(extractor, segmenter, stages.processor_ids) != stages:
        raise IntegrityError("injected extraction or segmentation settings differ from the processing plan")


def verify_extraction_identity(
    extractor: Extractor[ExtractionResult],
    captured: CapturedFile,
    representation: Representation,
) -> None:
    if (representation.extractor_id, representation.configuration_digest) != extractor.selected_identity(captured):
        raise IntegrityError("representation differs from the selected extractor identity or settings")


def verify_segment_identity(segment: Segment, selected_identity: tuple[str, str]) -> None:
    if (segment.segmenter_id, segment.policy_digest) != selected_identity:
        raise IntegrityError("segment differs from the selected segmenter identity or policy")
