"""Supported local lifecycle assembly for typed Python callers and commands."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from rulespec_artifacts import Producer

from docspec.domain.execution import ExecutionLimits
from docspec.domain.plans import ProcessingPlan, StagePolicy
from docspec.application.stage_identity import configured_stage_policy
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.processors import ProcessorPayload, ProcessorResult
from docspec.domain.references import ArtifactRef
from docspec.ports.content_fetcher import ContentFetcher
from docspec.ports.extractor import Extractor
from docspec.ports.segmenter import Segmenter
from docspec.processing.artifacts import RepresentationPayload, SegmentPayload
from docspec.processing.extraction import ExtractionResult
from docspec.ports.processor import Processor
from docspec.ports.source_catalog import ImmutableSourceCatalogReader
from docspec.runtime.composition import _compose_local_run, _stage_implementations
from docspec.runtime.execution import PreparedLocalRun
from docspec.runtime.preparation import _load_prepared_local_run, _prepare_local_run
from docspec.workspace import LocalWorkspace

__all__ = ["PreparedLocalRun", "prepare_local_run", "stage_policy"]


def stage_policy(
    *,
    stop_after: Literal["capture", "extraction", "segmentation", "processing"] = "processing",
    extractor: Extractor[ExtractionResult] | None = None,
    segmenter: Segmenter[RepresentationPayload, SegmentPayload] | None = None,
    processor_ids: tuple[str, ...] = (),
) -> StagePolicy:
    """Pin requested stages, constructing defaults only for stages that will run.

    ``capture`` retains source files; ``extraction`` also retains representations;
    ``segmentation`` also retains segments; ``processing`` runs the listed
    processors. Objects and processor IDs beyond the stopping point are refused.
    """
    if stop_after not in {"capture", "extraction", "segmentation", "processing"}:
        raise ValueError("stop_after must be capture, extraction, segmentation, or processing")
    if stop_after != "processing" and processor_ids:
        raise ValueError("processor identities require stop_after='processing'")
    extractor, segmenter = _stage_implementations(
        extractor, segmenter,
        requests_extraction=stop_after != "capture",
        requests_segmentation=stop_after in {"segmentation", "processing"},
    )
    return configured_stage_policy(extractor, segmenter, processor_ids)



def prepare_local_run(
    plan: ProcessingPlan,
    workspace: LocalWorkspace,
    *,
    retry_policy: RetryPolicy,
    accepted_failure_policy: AcceptedFailurePolicy,
    source_catalog_producer: Producer,
    document_release_producer: Producer,
    execution_limits: ExecutionLimits,
    deadline_epoch_seconds: int,
    completed_at: str,
    content_fetcher: ContentFetcher | None = None,
    extractor: Extractor[ExtractionResult] | None = None,
    segmenter: Segmenter[RepresentationPayload, SegmentPayload] | None = None,
    processors: Mapping[str, Processor[ProcessorPayload, ProcessorResult]] | None = None,
    source_catalog: ImmutableSourceCatalogReader | None = None,
    partition_policy_id: str = "source-item-sha256-v1",
    result_sink_id: str = "urn:docspec:local:sink",
    resume: bool | None = None,
    handoff_ref: ArtifactRef | None = None,
) -> PreparedLocalRun:
    """Prepare or recover capture and the processing stages requested by the plan.

    Pass existing domain values directly; no request or plan files are needed.
    Source and document producer acceptance are independent explicit choices.
    Injected fetchers declare ``downloader_id`` and ``configuration_digest``;
    injected processor descriptions must exactly match the plan's ProcessorSet.
    Requested extraction and segmentation objects must match the plan's pins;
    use ``stage_policy`` to build them from the same objects, or from the defaults.
    Unrequested stages reject supplied objects and do not construct defaults.

    With ``handoff_ref``, verify the saved handoff against reconstructed services,
    including the same execution limits and deadline; supplied settings cannot
    silently override or be ignored by saved work.
    Otherwise ``resume=None`` reuses an existing planned-store ledger when present;
    ``False`` plans the work, and ``True`` requires the existing ledger. Identical
    work in one workspace recovers progress rather than creating another trial.
    """
    if handoff_ref is not None and resume is not None:
        raise ValueError("choose saved-handoff recovery or a planning resume setting")
    if resume is not None and type(resume) is not bool:
        raise ValueError("resume must be a boolean or None")
    composition = _compose_local_run(
        plan, workspace,
        retry_policy=retry_policy,
        accepted_failure_policy=accepted_failure_policy,
        source_catalog_producer=source_catalog_producer,
        document_release_producer=document_release_producer,
        execution_limits=execution_limits,
        deadline_epoch_seconds=deadline_epoch_seconds,
        completed_at=completed_at,
        content_fetcher=content_fetcher,
        extractor=extractor,
        segmenter=segmenter,
        processors=processors,
        source_catalog=source_catalog,
        partition_policy_id=partition_policy_id,
        result_sink_id=result_sink_id,
    )
    if handoff_ref is not None:
        return _load_prepared_local_run(composition, handoff_ref)
    return _prepare_local_run(composition, resume=resume)
