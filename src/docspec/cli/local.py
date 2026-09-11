"""DocSpec command local: explicit local dependencies and task preparation."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.adapters.processor_cache import LocalSqliteProcessorResultCache
from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
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
from docspec.application.planner import RunPlanner
from docspec.cli.requests import _local_storage, _verified_local_plan
from docspec.cli_io import (
    CliError,
)
from docspec.domain.execution import (
    EXECUTE_AND_DELIVER_OPERATION_ID,
    ExecutionHandoff,
    ExecutionLimits,
    ExecutionProfile,
    StoreTask,
    iter_store_tasks,
    summarize_store_tasks,
)
from docspec.domain.identity import (
    stable_urn,
)
from docspec.domain.plans import ProcessingPlan
from docspec.domain.profiles import ProfileRole
from docspec.domain.references import ArtifactRef
from docspec.domain.storage import PartitionPolicy
from docspec.ports.content_fetcher import ContentFetcher
from docspec.ports.source_catalog import ImmutableSourceCatalogReader
from docspec.processing.extraction import DefaultExtractorRegistry
from docspec.processing.segmentation import DefaultSegmenterRegistry


@dataclass(slots=True)
class _LocalRunComposition:
    request: dict[str, Any]
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
    clock: Any
    content_fetcher_composition: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _PreparedLocalRun:
    execution_profile: ExecutionProfile
    execution_profile_ref: ArtifactRef
    handoff: ExecutionHandoff
    handoff_ref: ArtifactRef


def _local_processor_cache_path(roots: dict[str, Path]) -> Path:
    return roots["reconciliation"] / "processor-results.sqlite3"


def _compose_local_run(
    request: dict[str, Any],
    *,
    source_catalog: ImmutableSourceCatalogReader | None = None,
    content_fetcher: ContentFetcher | None = None,
    content_fetcher_composition: dict[str, Any] | None = None,
) -> _LocalRunComposition:
    plan, processors, profiles = _verified_local_plan(request)
    roots = request["roots"]
    controls, stores, records, blobs, catalog = _local_storage(
        roots,
        profiles,
        request["documentReleaseProducer"],
    )
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    if source_catalog is None:
        source_catalog = SourceCatalogArtifactReader(
            LocalSourceCatalogStore(roots["sourceCatalog"], create=False),
            producer=request["sourceCatalogProducer"],
        )
    fetcher = content_fetcher or LocalFileContentFetcher(roots["sourceContent"])
    actual_fetcher_composition = {
        "implementationId": getattr(fetcher, "downloader_id", None),
        "configurationDigest": getattr(fetcher, "configuration_digest", None),
    }
    if content_fetcher_composition is None:
        content_fetcher_composition = actual_fetcher_composition
    elif (
        not isinstance(content_fetcher_composition, dict)
        or content_fetcher_composition.get("implementationId") != actual_fetcher_composition["implementationId"]
        or content_fetcher_composition.get("configurationDigest")
        != actual_fetcher_composition["configurationDigest"]
    ):
        raise CliError("content fetcher differs from its sealed worker composition")
    partition_policy = PartitionPolicy(request["partitionPolicyId"], plan.partition_count)
    completed_at = request["completedAt"]

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
        sink_id=request["resultSinkId"],
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
        fetcher=fetcher,
        extractor=DefaultExtractorRegistry(),
        segmenter=DefaultSegmenterRegistry(),
        processors=processors,
        retry_policy=request["retryPolicy"],
        accepted_failure_policy=request["acceptedFailurePolicy"],
        clock=clock,
        processor_cache=LocalSqliteProcessorResultCache(_local_processor_cache_path(roots)),
    )
    delivery = StoreDeliveryService(stores=stores, controls=controls, sinks={sink.sink_id: sink})
    return _LocalRunComposition(
        request,
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
        content_fetcher_composition,
    )


def _prepared_tasks(
    composition: _LocalRunComposition,
    prepared: _PreparedLocalRun,
) -> Iterator[StoreTask]:
    return iter_store_tasks(
        composition.plan.plan_id,
        prepared.handoff.operation_id,
        composition.stores.stream_planned_stores(prepared.handoff.planned_store_ledger),
    )


def _prepare_local_run(
    composition: _LocalRunComposition,
    *,
    resume: bool | None,
) -> _PreparedLocalRun:
    request = composition.request
    plan = composition.plan
    roots = request["roots"]
    controls = composition.controls
    stores = composition.stores
    if resume is None:
        resume = stores.has_planned_store_ledger(plan.plan_id)
    if not resume:
        planned = RunPlanner(
            source_catalog=composition.source_catalog,
            document_catalog=composition.catalog,
            stores=stores,
            controls=controls,
            workspace_factory=LocalSqliteReconciliationWorkspaceFactory(
                roots["reconciliation"] / "planning",
                read_batch_size=1,
            ),
        ).plan_run(plan.source_catalog, plan.base_release, composition.plan_ref)
        for _ in planned:
            pass
    planned_ledger = stores.planned_store_ledger(plan.plan_id)

    def tasks():
        return iter_store_tasks(
            plan.plan_id,
            EXECUTE_AND_DELIVER_OPERATION_ID,
            stores.stream_planned_stores(planned_ledger),
        )

    task_count, task_set_digest = summarize_store_tasks(tasks())
    worker_composition_value = {
        "format": "docspec-local-worker-composition",
        "formatVersion": "1.1",
        "implementationId": "docspec.cli.local-worker/v2",
        "processingPlan": composition.plan_ref.to_dict(),
        "profileSet": plan.profiles.to_dict(),
        "roots": {name: path.as_posix() for name, path in sorted(roots.items())},
        "retryPolicy": request["retryPolicy"].to_dict(),
        "acceptedFailurePolicy": request["acceptedFailurePolicy"].to_dict(),
        "contentFetcher": composition.content_fetcher_composition,
    }
    worker_composition = controls.put(
        kind="worker-compositions",
        artifact_id=stable_urn("worker-composition", worker_composition_value),
        value=worker_composition_value,
    )
    scheduler_configuration_value = {
        "format": "docspec-local-scheduler-configuration",
        "formatVersion": "1.0",
        "adapterId": "docspec.local-threaded",
        "settings": dict(sorted(request["execution"].items())),
    }
    scheduler_configuration = controls.put(
        kind="scheduler-configurations",
        artifact_id=stable_urn("scheduler-configuration", scheduler_configuration_value),
        value=scheduler_configuration_value,
    )
    cache_profile_value = {
        "format": "docspec-processor-result-cache-profile",
        "formatVersion": "1.0",
        "adapterId": "docspec.local-sqlite-processor-result-cache",
        "adapterVersion": "1.0.0",
        "lookupSemantics": "exact-reuse-key-to-immutable-result-reference",
        "resultAuthority": "control-repository",
        "failureBehavior": "execute-processor",
    }
    cache_profile = controls.put(
        kind="processor-cache-profiles",
        artifact_id=stable_urn("processor-cache-profile", cache_profile_value),
        value=cache_profile_value,
    )
    cache_state_value = {
        "format": "docspec-processor-result-cache-state",
        "formatVersion": "1.0",
        "cacheProfile": cache_profile.to_dict(),
        "databasePath": _local_processor_cache_path(roots).as_posix(),
        "observedAt": request["completedAt"],
        "verificationScope": "configuration-only",
    }
    cache_state = controls.put(
        kind="processor-cache-states",
        artifact_id=stable_urn("processor-cache-state", cache_state_value),
        value=cache_state_value,
    )
    execution_profile = ExecutionProfile(
        "docspec.local-threaded",
        "1.0.0",
        worker_composition,
        scheduler_configuration,
        ExecutionLimits(
            request["execution"]["maxWorkers"],
            1,
            request["execution"]["maxInFlight"],
            request["execution"]["maxScratchBytesPerWorker"],
            request["execution"]["maxNetworkBytesPerTask"],
            request["execution"]["requestRateLimitPerSecond"],
            request["execution"]["maxProviderConcurrency"],
            request["execution"]["maxTaskAttempts"],
            request["execution"]["retryInitialDelayMilliseconds"],
            request["execution"]["retryMaxDelayMilliseconds"],
        ),
        request["execution"]["deadlineEpochSeconds"],
        cache_profile,
        cache_state,
    )
    if execution_profile.limits.max_network_bytes_per_task < plan.limits.max_estimated_bytes:
        raise CliError("execution network bound is lower than one planned store's logical byte bound")
    execution_profile_ref = controls.put(
        kind="execution-profiles",
        artifact_id=execution_profile.profile_id,
        value=execution_profile.to_dict(),
    )
    handoff = ExecutionHandoff(
        processing_plan=composition.plan_ref,
        execution_profile=execution_profile_ref,
        worker_composition=worker_composition,
        planned_store_ledger=planned_ledger,
        operation_id=EXECUTE_AND_DELIVER_OPERATION_ID,
        expected_task_count=task_count,
        task_set_digest=task_set_digest,
        result_sink=composition.sink_ref,
        base_release=plan.base_release,
    )
    handoff_ref = controls.put(
        kind="execution-handoffs",
        artifact_id=handoff.handoff_id,
        value=handoff.to_dict(),
    )
    return _PreparedLocalRun(execution_profile, execution_profile_ref, handoff, handoff_ref)


def _load_prepared_local_run(
    composition: _LocalRunComposition,
    handoff_ref: ArtifactRef,
) -> _PreparedLocalRun:
    try:
        handoff = ExecutionHandoff.from_dict(composition.controls.load(handoff_ref))
        profile = ExecutionProfile.from_dict(composition.controls.load(handoff.execution_profile))
    except (TypeError, ValueError) as error:
        raise CliError(f"saved local execution handoff is invalid: {error}") from error
    if handoff.handoff_id != handoff_ref.artifact_id:
        raise CliError("saved execution handoff identity differs from its reference")
    if profile.profile_id != handoff.execution_profile.artifact_id:
        raise CliError("saved execution profile identity differs from its reference")
    for reference in profile.control_artifacts:
        composition.controls.verify(reference)
    planned_ledger = composition.stores.planned_store_ledger(composition.plan.plan_id)
    if (
        handoff.processing_plan != composition.plan_ref
        or handoff.execution_profile.artifact_id != profile.profile_id
        or handoff.worker_composition != profile.worker_composition
        or handoff.planned_store_ledger != planned_ledger
        or handoff.result_sink != composition.sink_ref
        or handoff.base_release != composition.plan.base_release
    ):
        raise CliError("saved execution handoff differs from the local run composition")
    return _PreparedLocalRun(profile, handoff.execution_profile, handoff, handoff_ref)
