"""Validated assembly of local services shared by Python and command callers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rulespec_artifacts import Producer
from typing import Any

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.adapters.processor_cache import LocalSqliteProcessorResultCache
from docspec.adapters.sinks import DurableDatasetSink
from docspec.adapters.catalog_artifact.reader import (
    SourceCatalogArtifactReader,
)
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.adapters.storage import (
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.application.delivery import StoreDeliveryService
from docspec.application.execution import StoreExecutionService
from docspec.application.processor_rules import verify_processor_policies
from docspec.errors import IntegrityError, ProfileError
from docspec.application.stage_identity import verify_stage_implementations
from docspec.runtime.storage import _local_profiles, _local_storage
from docspec.runtime.fetcher import _BoundContentFetcher, _content_fetcher_identity
from docspec.workspace import LocalWorkspace
from docspec.domain.execution import ExecutionLimits
from docspec.domain.identity import (
    require_text,
    stable_urn,
)
from docspec.domain.plans import ProcessingPlan
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.processors import ProcessorPayload, ProcessorResult, ProcessorSet
from docspec.domain.profiles import ProfileRole
from docspec.domain.references import ArtifactRef
from docspec.domain.storage import PartitionPolicy
from docspec.ports.content_fetcher import ContentFetcher
from docspec.ports.extractor import Extractor
from docspec.ports.segmenter import Segmenter
from docspec.processing.artifacts import RepresentationPayload, SegmentPayload
from docspec.ports.processor import Processor
from docspec.ports.source_catalog import ImmutableSourceCatalogReader
from docspec.processing.extraction import DefaultExtractorRegistry, ExtractionResult
from docspec.processing.segmentation import DefaultSegmenterRegistry
from docspec.processing.processors import ContentStatisticsProcessor


@dataclass(slots=True)
class _LocalRunComposition:
    workspace: LocalWorkspace
    retry_policy: RetryPolicy
    accepted_failure_policy: AcceptedFailurePolicy
    source_catalog_producer: Producer
    execution_limits: ExecutionLimits
    deadline_epoch_seconds: int
    plan: ProcessingPlan
    controls: LocalJsonControlRepository
    stores: LocalDocumentStoreRepository
    records: LocalJsonlRecordStorage
    catalog: LocalManifestDocumentCatalog
    source_catalog: ImmutableSourceCatalogReader
    partition_policy: PartitionPolicy
    plan_ref: ArtifactRef
    sink_ref: ArtifactRef
    executor: StoreExecutionService
    delivery: StoreDeliveryService
    clock: Callable[[], str]
    content_fetcher: ContentFetcher


def _local_processor_cache_path(roots: dict[str, Path]) -> Path:
    return roots["reconciliation"] / "processor-results.sqlite3"


def _worker_composition_value(composition: _LocalRunComposition) -> dict[str, Any]:
    """Describe the same effective worker when preparing or recovering tasks."""

    return {
        "format": "docspec-local-worker-composition",
        "formatVersion": "2.0",
        "implementationId": "docspec.runtime.local-worker/v1",
        "processingPlan": composition.plan_ref.to_dict(),
        "profileSet": composition.plan.profiles.to_dict(),
        "roots": {name: path.as_posix() for name, path in sorted(composition.workspace.roots.items())},
        "retryPolicy": composition.retry_policy.to_dict(),
        "acceptedFailurePolicy": composition.accepted_failure_policy.to_dict(),
        "contentFetcher": _content_fetcher_identity(composition.content_fetcher),
        "completedAt": composition.clock(),
        "documentReleaseProducer": composition.catalog.producer.as_dict(),
        "sourceCatalogProducer": composition.source_catalog_producer.as_dict(),
        "partitionPolicy": {
            "policyId": composition.partition_policy.policy_id,
            "bucketCount": composition.partition_policy.bucket_count,
        },
        "resultSink": composition.sink_ref.to_dict(),
    }


def _verify_plan_policies(
    plan: ProcessingPlan,
    retry_policy: RetryPolicy,
    accepted_failure_policy: AcceptedFailurePolicy,
) -> None:
    if retry_policy.digest != plan.retry_policy_digest:
        raise ProfileError("local run retry policy differs from the processing plan")
    if accepted_failure_policy.digest != plan.accepted_failure_policy_digest:
        raise ProfileError("local run accepted-failure policy differs from the processing plan")
    if retry_policy.max_attempts != plan.limits.max_attempts:
        raise ProfileError("local run retry policy differs from the plan attempt limit")


def _verified_processors(
    plan: ProcessingPlan,
    retry_policy: RetryPolicy,
    accepted_failure_policy: AcceptedFailurePolicy,
    processors: Mapping[str, Processor[ProcessorPayload, ProcessorResult]] | None = None,
) -> dict[str, Processor[ProcessorPayload, ProcessorResult]]:
    _verify_plan_policies(plan, retry_policy, accepted_failure_policy)
    if processors is None and not plan.stages.processor_ids:
        processors = {}
    elif processors is None:
        statistics = ContentStatisticsProcessor(retry_policy=retry_policy)
        available = {statistics.description.processor_id: statistics}
        try:
            processors = {identifier: available[identifier] for identifier in plan.stages.processor_ids}
        except KeyError as error:
            raise ProfileError(f"local composition has no processor {error.args[0]}") from error
    if set(processors) != set(plan.stages.processor_ids) or any(
        identifier != processor.description.processor_id for identifier, processor in processors.items()
    ):
        raise ProfileError("local processor implementations differ from the processing plan")
    processors = {identifier: processors[identifier] for identifier in plan.stages.processor_ids}
    descriptions = tuple(processor.description for processor in processors.values())
    if ProcessorSet(descriptions) != plan.processors:
        raise ProfileError("local processor implementations differ from the processing plan")
    try:
        verify_processor_policies(plan, descriptions)
    except IntegrityError as error:
        raise ProfileError(str(error)) from error
    return dict(processors)


def _utc_instant(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProfileError(f"{label} must be an RFC 3339 UTC instant ending in Z")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ProfileError(f"{label} must be an RFC 3339 UTC instant") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ProfileError(f"{label} must use UTC")
    return value


def _stage_implementations(
    extractor: Extractor[ExtractionResult] | None,
    segmenter: Segmenter[RepresentationPayload, SegmentPayload] | None,
    *,
    requests_extraction: bool,
    requests_segmentation: bool,
) -> tuple[Extractor[ExtractionResult] | None, Segmenter[RepresentationPayload, SegmentPayload] | None]:
    if not requests_extraction and extractor is not None:
        raise ProfileError("an extractor was supplied but extraction is not requested")
    if not requests_segmentation and segmenter is not None:
        raise ProfileError("a segmenter was supplied but segmentation is not requested")
    return (
        DefaultExtractorRegistry() if requests_extraction and extractor is None else extractor,
        DefaultSegmenterRegistry() if requests_segmentation and segmenter is None else segmenter,
    )


def _compose_local_run(
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
    partition_policy_id: str,
    result_sink_id: str,
    content_fetcher: ContentFetcher | None,
    extractor: Extractor[ExtractionResult] | None,
    segmenter: Segmenter[RepresentationPayload, SegmentPayload] | None,
    processors: Mapping[str, Processor[ProcessorPayload, ProcessorResult]] | None,
    source_catalog: ImmutableSourceCatalogReader | None,
) -> _LocalRunComposition:
    extractor, segmenter = _stage_implementations(
        extractor, segmenter,
        requests_extraction=plan.stages.requests_extraction,
        requests_segmentation=plan.stages.requests_segmentation,
    )
    try:
        verify_stage_implementations(plan.stages, extractor=extractor, segmenter=segmenter)
    except IntegrityError as error:
        raise ProfileError(str(error)) from error
    if not isinstance(execution_limits, ExecutionLimits):
        raise ProfileError("execution_limits must be an ExecutionLimits value")
    profiles = _local_profiles(plan, workspace)
    processors = _verified_processors(plan, retry_policy, accepted_failure_policy, processors)
    if type(deadline_epoch_seconds) is not int or deadline_epoch_seconds < 1:
        raise ProfileError("deadline_epoch_seconds must be a positive integer")
    completed_at = _utc_instant(completed_at, label="completed_at")
    if not isinstance(source_catalog_producer, Producer) or not isinstance(document_release_producer, Producer):
        raise ProfileError("source and document producer acceptance must be supplied independently")
    require_text(result_sink_id, "result_sink_id")
    partition_policy = PartitionPolicy(partition_policy_id, plan.partition_count)
    roots = workspace.roots
    fetcher = content_fetcher if content_fetcher is not None else LocalFileContentFetcher(roots["sourceContent"])
    bound_fetcher = _BoundContentFetcher(fetcher)
    controls, stores, records, blobs, catalog = _local_storage(
        roots,
        profiles,
        document_release_producer,
    )
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    if source_catalog is None:
        source_catalog = SourceCatalogArtifactReader(
            LocalSourceCatalogStore(roots["sourceCatalog"], create=False),
            producer=source_catalog_producer,
        )

    def clock() -> str:
        return completed_at

    blob_profile = plan.profiles.for_role(ProfileRole.BLOB_STORAGE)
    blob_state = {
        "profileId": blob_profile.profile_id,
        "profileVersion": blob_profile.version,
        "storageRoot": blobs.root.as_posix(),
    }
    blob_root = controls.put(
        kind="profile-state",
        artifact_id=stable_urn("profile-state", blob_state),
        value=blob_state,
    )
    result_profile = plan.profiles.for_role(ProfileRole.RESULT_DELIVERY)
    sink = DurableDatasetSink(
        sink_id=result_sink_id,
        profile_id=result_profile.profile_id,
        storage=records,
        partition_policy=partition_policy,
        blob_roots=(blob_root,),
        clock=clock,
    )
    sink_ref = controls.put(
        kind="sinks",
        artifact_id=sink.sink_id,
        value={"sinkId": sink.sink_id, "profileId": sink.profile_id},
    )
    executor = StoreExecutionService(
        plan_ref=plan_ref,
        controls=controls,
        stores=stores,
        document_catalog=catalog,
        blobs=blobs,
        fetcher=bound_fetcher,
        extractor=extractor,
        segmenter=segmenter,
        processors=processors,
        retry_policy=retry_policy,
        accepted_failure_policy=accepted_failure_policy,
        clock=clock,
        processor_cache=(
            LocalSqliteProcessorResultCache(_local_processor_cache_path(roots))
            if plan.stages.processor_ids else None
        ),
    )
    delivery = StoreDeliveryService(stores=stores, controls=controls, sinks={sink.sink_id: sink})
    return _LocalRunComposition(
        workspace, retry_policy, accepted_failure_policy, source_catalog_producer,
        execution_limits, deadline_epoch_seconds,
        plan,
        controls,
        stores,
        records,
        catalog,
        source_catalog,
        partition_policy,
        plan_ref,
        sink_ref,
        executor,
        delivery,
        clock,
        fetcher,
    )
