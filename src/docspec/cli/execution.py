"""DocSpec command execution: portable task execution and result reconciliation."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from docspec.adapters.execution import LocalExecutionBackend
from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.application.reconcile import RunReconciler
from docspec.application.store_state import load_latest_store
from docspec.cli.local import (
    _compose_local_run,
    _LocalRunComposition,
    _prepare_local_run,
    _prepared_tasks,
    _PreparedLocalRun,
)
from docspec.cli.requests import _local_run_request
from docspec.cli_io import (
    CliError,
)
from docspec.domain.execution import (
    ExecutionHandoff,
    StoreTask,
    StoreTaskResult,
)
from docspec.domain.jobs import StoreState
from docspec.domain.references import ArtifactRef
from docspec.ports.content_fetcher import ContentFetcher
from docspec.ports.source_catalog import ImmutableSourceCatalogReader


def run_local(
    request_path: Path,
    *,
    resume: bool | None = None,
    content_fetcher: ContentFetcher | None = None,
) -> ArtifactRef:
    """Run a closed local request, optionally injecting a content source.

    This is the same execution path as the run commands. An injected fetcher
    is recorded in the worker composition for this execution.
    """
    return _execute_local_run(
        _local_run_request(request_path), resume=resume, content_fetcher=content_fetcher
    )


def _execute_local_task(
    composition: _LocalRunComposition,
    prepared: _PreparedLocalRun,
    task: StoreTask,
) -> StoreTaskResult:
    handoff = prepared.handoff
    if (
        task.processing_plan_id != composition.plan.plan_id
        or task.operation_id != handoff.operation_id
        or handoff.processing_plan != composition.plan_ref
    ):
        raise CliError("local worker received a task outside its sealed execution handoff")
    current_ref, current_store = load_latest_store(composition.stores, task.input_store)
    if current_store.plan_id != composition.plan.plan_id:
        raise CliError("local worker recovered a document store from another processing plan")
    if current_store.state is StoreState.SEALED:
        sealed = composition.delivery.deliver_store(current_ref, handoff.result_sink)
    else:
        processed = composition.executor.execute_store(current_ref)
        sealed = composition.delivery.deliver_store(processed, handoff.result_sink)
    return StoreTaskResult.succeeded(
        handoff_id=handoff.handoff_id,
        task=task,
        output_store=sealed,
    )


def _reconcile_local_run(
    composition: _LocalRunComposition,
    prepared: _PreparedLocalRun,
    results: Iterable[StoreTaskResult],
) -> ArtifactRef:
    plan = composition.plan
    roots = composition.request["roots"]
    return RunReconciler(
        plan_ref=composition.plan_ref,
        execution_profile_ref=prepared.execution_profile_ref,
        execution_handoff_ref=prepared.handoff_ref,
        source_catalog_ref=plan.source_catalog,
        base_release_ref=plan.base_release,
        controls=composition.controls,
        stores=composition.stores,
        records=composition.records,
        document_catalog=composition.catalog,
        source_catalog=composition.source_catalog,
        workspace_factory=LocalSqliteReconciliationWorkspaceFactory(roots["reconciliation"]),
        partition_policy=composition.partition_policy,
        clock=composition.clock,
    ).reconcile_run(results)


def _execute_local_run(
    request: dict[str, Any],
    *,
    resume: bool | None,
    source_catalog: ImmutableSourceCatalogReader | None = None,
    content_fetcher: ContentFetcher | None = None,
    content_fetcher_composition: dict[str, Any] | None = None,
) -> ArtifactRef:
    composition = _compose_local_run(
        request,
        source_catalog=source_catalog,
        content_fetcher=content_fetcher,
        content_fetcher_composition=content_fetcher_composition,
    )
    prepared = _prepare_local_run(composition, resume=resume)

    def execute_and_deliver(_handoff: ExecutionHandoff, task: StoreTask) -> StoreTaskResult:
        return _execute_local_task(composition, prepared, task)

    results = LocalExecutionBackend(
        prepared.execution_profile,
        execute_and_deliver,
        profile_reference=prepared.execution_profile_ref,
        controls=composition.controls,
        max_workers=request["execution"]["maxWorkers"],
    ).execute(prepared.handoff, _prepared_tasks(composition, prepared))
    return _reconcile_local_run(composition, prepared, results)
