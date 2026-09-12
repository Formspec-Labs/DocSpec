"""Persist and recover the existing execution handoff and its exact worker settings."""

from __future__ import annotations

from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.application.planner import RunPlanner
from docspec.domain.execution import (
    EXECUTE_AND_DELIVER_OPERATION_ID, ExecutionHandoff, ExecutionProfile,
    iter_store_tasks, summarize_store_tasks,
)
from docspec.domain.identity import stable_urn
from docspec.domain.references import ArtifactRef
from docspec.errors import IntegrityError
from docspec.runtime.composition import _LocalRunComposition, _local_processor_cache_path, _worker_composition_value
from docspec.runtime.execution import PreparedLocalRun


def _prepare_local_run(
    composition: _LocalRunComposition,
    *,
    resume: bool | None,
) -> PreparedLocalRun:
    plan = composition.plan
    roots = composition.workspace.roots
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
    worker_composition_value = _worker_composition_value(composition)
    worker_composition = controls.put(
        kind="worker-compositions",
        artifact_id=stable_urn("worker-composition", worker_composition_value),
        value=worker_composition_value,
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
        "observedAt": composition.clock(),
        "verificationScope": "configuration-only",
    }
    cache_state = controls.put(
        kind="processor-cache-states",
        artifact_id=stable_urn("processor-cache-state", cache_state_value),
        value=cache_state_value,
    )
    execution_profile = ExecutionProfile(
        worker_composition,
        composition.execution_limits.max_task_index_bytes,
        composition.deadline_epoch_seconds,
        cache_profile,
        cache_state,
    )
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
    return PreparedLocalRun(execution_profile, execution_profile_ref, handoff, handoff_ref, composition)


def _load_prepared_local_run(
    composition: _LocalRunComposition,
    handoff_ref: ArtifactRef,
) -> PreparedLocalRun:
    try:
        handoff = ExecutionHandoff.from_dict(composition.controls.load(handoff_ref))
        profile = ExecutionProfile.from_dict(composition.controls.load(handoff.execution_profile))
    except (TypeError, ValueError) as error:
        raise IntegrityError(f"saved local execution handoff is invalid: {error}") from error
    if handoff.handoff_id != handoff_ref.artifact_id:
        raise IntegrityError("saved execution handoff identity differs from its reference")
    if handoff.operation_id != EXECUTE_AND_DELIVER_OPERATION_ID:
        raise IntegrityError("saved execution handoff names an unsupported local operation")
    if profile.profile_id != handoff.execution_profile.artifact_id:
        raise IntegrityError("saved execution profile identity differs from its reference")
    if (
        profile.max_task_index_bytes != composition.execution_limits.max_task_index_bytes
        or profile.deadline_epoch_seconds != composition.deadline_epoch_seconds
    ):
        raise IntegrityError("saved execution settings differ from the reconstructed local worker")
    expected_worker = _worker_composition_value(composition)
    for reference in profile.control_artifacts:
        value = composition.controls.load(reference)
        if reference == profile.worker_composition and (
            value != expected_worker
            or reference.artifact_id != stable_urn("worker-composition", expected_worker)
        ):
            raise IntegrityError("saved worker composition differs from the reconstructed local worker")
    planned_ledger = composition.stores.planned_store_ledger(composition.plan.plan_id)
    if (
        handoff.processing_plan != composition.plan_ref
        or handoff.execution_profile.artifact_id != profile.profile_id
        or handoff.worker_composition != profile.worker_composition
        or handoff.planned_store_ledger != planned_ledger
        or handoff.result_sink != composition.sink_ref
        or handoff.base_release != composition.plan.base_release
    ):
        raise IntegrityError("saved execution handoff differs from the local run composition")
    return PreparedLocalRun(profile, handoff.execution_profile, handoff, handoff_ref, composition)
