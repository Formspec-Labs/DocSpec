"""The five small-reference functions shared by local and external schedulers."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from docspec.application.commit import ReleaseCommitService
from docspec.application.delivery import StoreDeliveryService
from docspec.application.execution import StoreExecutionService
from docspec.application.planner import RunPlanner
from docspec.application.reconcile import RunReconciler
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, SourceCatalogRef, StoreRef


class DocSpecApplication:
    """A composition-only façade; all dependencies are injected behind DocSpec ports."""

    def __init__(
        self,
        *,
        planner: RunPlanner,
        executor: StoreExecutionService,
        delivery: StoreDeliveryService,
        reconciler: RunReconciler,
        committer: ReleaseCommitService,
    ) -> None:
        self._planner = planner
        self._executor = executor
        self._delivery = delivery
        self._reconciler = reconciler
        self._committer = committer

    def plan_run(
        self,
        source_catalog_ref: SourceCatalogRef,
        base_document_release_ref: DocumentReleaseRef | None,
        plan_ref: ArtifactRef,
    ) -> Iterator[StoreRef]:
        return self._planner.plan_run(source_catalog_ref, base_document_release_ref, plan_ref)

    def execute_store(self, planned_document_store_ref: StoreRef) -> StoreRef:
        return self._executor.execute_store(planned_document_store_ref)

    def deliver_store(self, processed_document_store_ref: StoreRef, sink_ref: ArtifactRef) -> StoreRef:
        return self._delivery.deliver_store(processed_document_store_ref, sink_ref)

    def reconcile_run(self, sealed_document_store_refs: Iterable[StoreRef]) -> ArtifactRef:
        return self._reconciler.reconcile_run(sealed_document_store_refs)

    def commit_release(
        self,
        base_document_release_ref: DocumentReleaseRef | None,
        run_receipt_ref: ArtifactRef,
    ) -> DocumentReleaseRef:
        return self._committer.commit_release(base_document_release_ref, run_receipt_ref)
