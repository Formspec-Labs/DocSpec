"""One prepared local run, usable directly or by an external scheduler."""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

from docspec.adapters.execution import LocalExecutionBackend
from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.application.commit import ReleaseCommitService
from docspec.application.reconcile import RunReconciler
from docspec.application.store_state import load_latest_store
from docspec.domain.execution import ExecutionHandoff, ExecutionProfile, StoreTask, StoreTaskResult, iter_store_tasks
from docspec.domain.jobs import StoreState
from docspec.domain.identity import stable_urn
from docspec.domain.plans import ProcessingPlan
from docspec.domain.references import ArtifactRef, DocumentReleaseRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.runtime.composition import _LocalRunComposition, _worker_composition_value
from docspec.runtime.task_membership import _TaskMembershipIndex


@dataclass(frozen=True, slots=True)
class PreparedLocalRun:
    """Existing saved work with bound services; no separate run state or ledger.

    Use ``run()`` for local execution, or pass ``handoff``, ``task_source`` and
    ``execute_task`` to a scheduler. Reconcile its result stream through this
    same object. Reconstruct it with ``prepare_local_run(..., handoff_ref=...)``.
    Direct task callers must close this object after their workers stop, or use
    it as a context manager. Closing releases scratch; later use rebuilds it.
    """

    execution_profile: ExecutionProfile
    execution_profile_ref: ArtifactRef
    handoff: ExecutionHandoff
    handoff_ref: ArtifactRef
    _composition: _LocalRunComposition = field(repr=False, compare=False)
    _membership: _TaskMembershipIndex = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_membership", _TaskMembershipIndex(
            self._composition.stores,
            self.handoff,
            self._composition.workspace.roots["reconciliation"] / "task-membership",
            max_scratch_bytes=self.execution_profile.limits.max_scratch_bytes_per_worker,
        ))

    @property
    def plan(self) -> ProcessingPlan:
        """The exact admitted plan used by this prepared run's bound services."""

        return self._composition.plan

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release task-admission scratch after active workers have stopped."""
        self._membership.close()

    def _require_handoff(self, handoff: ExecutionHandoff) -> None:
        if handoff != self.handoff:
            raise IntegrityError("local worker received a different execution handoff")
        if handoff.worker_composition.artifact_id != stable_urn(
            "worker-composition", _worker_composition_value(self._composition),
        ):
            raise IntegrityError("local worker settings changed after preparation")
        self._composition.executor.verify_configuration()

    def task_source(self, handoff: ExecutionHandoff) -> Iterator[StoreTask]:
        """Stream the existing sealed task ledger for this exact handoff."""
        self._require_handoff(handoff)
        with closing(self._composition.stores.stream_planned_stores(handoff.planned_store_ledger)) as references:
            yield from iter_store_tasks(self._composition.plan.plan_id, handoff.operation_id, references)

    def execute_task(self, handoff: ExecutionHandoff, task: StoreTask) -> StoreTaskResult:
        """Resume the latest verified store revision, process it, and deliver it."""
        self._require_handoff(handoff)
        if time.time() >= self.execution_profile.deadline_epoch_seconds:
            raise LimitExceededError("execution profile deadline has expired")
        composition = self._composition
        if (
            task.processing_plan_id != composition.plan.plan_id
            or task.operation_id != handoff.operation_id
            or handoff.processing_plan != composition.plan_ref
        ):
            raise IntegrityError("local worker received a task outside its sealed execution handoff")
        self._membership.require_member(task.input_store)
        if time.time() >= self.execution_profile.deadline_epoch_seconds:
            raise LimitExceededError("execution profile deadline has expired")
        current_ref, current_store = load_latest_store(composition.stores, task.input_store)
        if current_store.plan_id != composition.plan.plan_id:
            raise IntegrityError("local worker recovered a document store from another processing plan")
        if current_store.state is StoreState.SEALED:
            sealed = composition.delivery.deliver_store(current_ref, handoff.result_sink)
        else:
            processed = composition.executor.execute_store(current_ref)
            sealed = composition.delivery.deliver_store(processed, handoff.result_sink)
        return StoreTaskResult.succeeded(handoff_id=handoff.handoff_id, task=task, output_store=sealed)

    def reconcile(self, results: Iterable[StoreTaskResult]) -> ArtifactRef:
        """Verify completed task evidence and write the existing run receipt."""
        composition = self._composition
        plan = composition.plan
        return RunReconciler(
            plan_ref=composition.plan_ref,
            execution_profile_ref=self.execution_profile_ref,
            execution_handoff_ref=self.handoff_ref,
            source_catalog_ref=plan.source_catalog,
            base_release_ref=plan.base_release,
            controls=composition.controls,
            stores=composition.stores,
            records=composition.records,
            document_catalog=composition.catalog,
            source_catalog=composition.source_catalog,
            workspace_factory=LocalSqliteReconciliationWorkspaceFactory(composition.workspace.roots["reconciliation"]),
            partition_policy=composition.partition_policy,
            clock=composition.clock,
        ).reconcile_run(results)

    def retain(self, run_ref: ArtifactRef) -> DocumentReleaseRef:
        """Retain a verified run result without changing the selected release."""
        composition = self._composition
        return ReleaseCommitService(
            plan_ref=composition.plan_ref,
            controls=composition.controls,
            records=composition.records,
            document_catalog=composition.catalog,
        ).retain_release(composition.plan.base_release, run_ref)

    def run(self) -> ArtifactRef:
        """Execute outstanding tasks locally and reconcile their results."""
        try:
            with closing(self.task_source(self.handoff)) as tasks:
                with closing(LocalExecutionBackend(
                    self.execution_profile,
                    self.execute_task,
                    profile_reference=self.execution_profile_ref,
                    controls=self._composition.controls,
                    max_workers=self.execution_profile.limits.worker_count,
                ).execute(self.handoff, tasks)) as results:
                    return self.reconcile(results)
        finally:
            self.close()
