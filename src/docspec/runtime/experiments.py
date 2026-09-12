"""Prepare ordinary local experiments from selected inputs and implementations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from rulespec_artifacts import Producer

from docspec.application.planner import _CompiledSelection
from docspec.application.stage_identity import configured_stage_policy
from docspec.domain.execution import ExecutionLimits
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, DataUsePolicy, RetentionPolicy, RetryPolicy
from docspec.domain.processors import ProcessorPayload, ProcessorResult, ProcessorSet
from docspec.domain.profiles import ProfileSet
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, SourceCatalogRef
from docspec.ports.content_fetcher import ContentFetcher
from docspec.ports.extractor import Extractor
from docspec.ports.processor import Processor
from docspec.ports.segmenter import Segmenter
from docspec.ports.source_catalog import ImmutableSourceCatalogReader
from docspec.processing.artifacts import RepresentationPayload, SegmentPayload
from docspec.processing.extraction import ExtractionResult
from docspec.profile_registry import ProfileRegistry
from docspec.workspace import LocalWorkspace

from .composition import _stage_implementations
from .defaults import local_execution_limits
from .execution import PreparedLocalRun

StopAfter = Literal["capture", "extraction", "segmentation", "processing"]


def _configured_stages(
    stop_after: StopAfter,
    extractor: Extractor[ExtractionResult] | None,
    segmenter: Segmenter[RepresentationPayload, SegmentPayload] | None,
    processor_ids: tuple[str, ...],
) -> tuple[StagePolicy, Extractor[ExtractionResult] | None, Segmenter[RepresentationPayload, SegmentPayload] | None]:
    if stop_after not in {"capture", "extraction", "segmentation", "processing"}:
        raise ValueError("stop_after must be capture, extraction, segmentation, or processing")
    if stop_after != "processing" and processor_ids:
        raise ValueError("processor identities require stop_after='processing'")
    extractor, segmenter = _stage_implementations(
        extractor, segmenter,
        requests_extraction=stop_after != "capture",
        requests_segmentation=stop_after in {"segmentation", "processing"},
    )
    return configured_stage_policy(extractor, segmenter, processor_ids), extractor, segmenter


def stage_policy(
    *,
    stop_after: StopAfter = "processing",
    extractor: Extractor[ExtractionResult] | None = None,
    segmenter: Segmenter[RepresentationPayload, SegmentPayload] | None = None,
    processor_ids: tuple[str, ...] = (),
) -> StagePolicy:
    """Pin only requested stages, using supplied objects or the supported defaults."""

    return _configured_stages(stop_after, extractor, segmenter, processor_ids)[0]


def prepare_local_experiment(
    source_catalog_ref: SourceCatalogRef,
    workspace: LocalWorkspace,
    *,
    limits: WorkLimits,
    source_catalog_producer: Producer,
    document_release_producer: Producer,
    completed_at: str,
    deadline_epoch_seconds: int,
    stop_after: StopAfter = "processing",
    extractor: Extractor[ExtractionResult] | None = None,
    segmenter: Segmenter[RepresentationPayload, SegmentPayload] | None = None,
    processors: tuple[Processor[ProcessorPayload, ProcessorResult], ...] = (),
    content_fetcher: ContentFetcher | None = None,
    base_release: DocumentReleaseRef | None = None,
    selection: Mapping[str, Any] | None = None,
    retry_policy: RetryPolicy | None = None,
    accepted_failure_policy: AcceptedFailurePolicy | None = None,
    execution_limits: ExecutionLimits | None = None,
    partition_count: int = 1,
    retention_policy: RetentionPolicy | None = None,
    data_use_policy: DataUsePolicy | None = None,
    profiles: ProfileSet | None = None,
    source_catalog: ImmutableSourceCatalogReader | None = None,
    partition_policy_id: str = "source-item-sha256-v1",
    result_sink_id: str = "urn:docspec:local:sink",
    resume: bool | None = None,
    handoff_ref: ArtifactRef | None = None,
) -> PreparedLocalRun:
    """Derive the existing plan from the objects that will do the work.

    The returned prepared run exposes its exact ``plan``. No independent
    experiment identity is saved. A retained base, both producer acceptance
    policies, work limits, evidence timestamp, and deadline are caller choices.
    Identical inputs recover the existing work; a different trial requires an
    explicit change to the plan or workspace, as with ``prepare_local_run``.
    """

    # Keep the same public execution path without a package-initializer cycle.
    from . import prepare_local_run

    if not isinstance(processors, tuple):
        raise TypeError("experiment processors must be a tuple of configured implementations")
    retry = RetryPolicy(max_attempts=limits.max_attempts) if retry_policy is None else retry_policy
    accepted = AcceptedFailurePolicy() if accepted_failure_policy is None else accepted_failure_policy
    selected = tuple((processor.description, processor) for processor in processors)
    graph = ProcessorSet(tuple(description for description, _processor in selected))
    descriptions = ProcessorSet(graph.execution_order)
    processor_ids = tuple(description.processor_id for description in descriptions.execution_order)
    stages, extractor, segmenter = _configured_stages(stop_after, extractor, segmenter, processor_ids)
    chosen_selection = {} if selection is None else dict(selection)
    _CompiledSelection.compile(chosen_selection, partition_count=partition_count)
    plan = ProcessingPlan.create(
        source_catalog=source_catalog_ref, base_release=base_release,
        profiles=ProfileRegistry.from_directory(workspace.profile_directory).local_profiles() if profiles is None else profiles,
        limits=limits, stages=stages, processors=descriptions, partition_count=partition_count,
        selection=chosen_selection,
        retention_policy=RetentionPolicy.retain_all() if retention_policy is None else retention_policy,
        data_use_policy=DataUsePolicy.local_content() if data_use_policy is None else data_use_policy,
        retry_policy_digest=retry.digest, accepted_failure_policy_digest=accepted.digest,
    )
    return prepare_local_run(
        plan, workspace, retry_policy=retry, accepted_failure_policy=accepted,
        source_catalog_producer=source_catalog_producer, document_release_producer=document_release_producer,
        execution_limits=local_execution_limits() if execution_limits is None else execution_limits,
        deadline_epoch_seconds=deadline_epoch_seconds, completed_at=completed_at,
        content_fetcher=content_fetcher, extractor=extractor, segmenter=segmenter,
        processors={description.processor_id: processor for description, processor in selected},
        source_catalog=source_catalog, partition_policy_id=partition_policy_id, result_sink_id=result_sink_id,
        resume=resume, handoff_ref=handoff_ref,
    )
