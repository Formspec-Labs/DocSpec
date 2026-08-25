"""Verify reconciled release state and conditionally publish it."""

from __future__ import annotations

from itertools import zip_longest
from typing import Any, Iterator

from docspec.domain.delivery import core_delivery_schemas, verify_logical_release_layers
from docspec.domain.execution import ExecutionHandoff, ExecutionProfile, StoreTaskResult, StoreTaskStatus
from docspec.domain.identity import identity_digest, ordered_json_sequence_digest
from docspec.domain.jobs import StoreState
from docspec.domain.plans import ProcessingPlan
from docspec.domain.profiles import ProfileRole
from docspec.domain.receipts import CatalogCommitReceipt, RunReceipt
from docspec.domain.references import ArtifactRef, BlobRef, DocumentReleaseRef, LayerRef, StoreRef
from docspec.domain.release import DocumentRelease
from docspec.domain.storage import PartitionPolicy
from docspec.errors import IntegrityError, ProfileError, StateTransitionError
from docspec.ports.blob_store import BlobStore
from docspec.ports.control_repository import ControlRepository
from docspec.ports.document_catalog import DocumentCatalog
from docspec.ports.document_store_repository import DocumentStoreRepository
from docspec.ports.record_storage import RecordStorage


def _verify_execution_evidence(
    controls: ControlRepository,
    records: RecordStorage,
    run: RunReceipt,
) -> None:
    """Verify one execution profile, handoff, and task-result ledger without buffering them."""

    try:
        profile = ExecutionProfile.from_dict(controls.load(run.execution_profile))
        handoff = ExecutionHandoff.from_dict(controls.load(run.execution_handoff))
    except (TypeError, ValueError) as error:
        raise IntegrityError(f"run execution controls are invalid: {error}") from error
    if profile.profile_id != run.execution_profile.artifact_id:
        raise IntegrityError("run execution-profile identity differs from its reference")
    for reference in profile.control_artifacts:
        controls.verify(reference)
    if handoff.handoff_id != run.execution_handoff.artifact_id:
        raise IntegrityError("run execution-handoff identity differs from its reference")
    if (
        handoff.processing_plan != run.plan
        or handoff.execution_profile != run.execution_profile
        or handoff.worker_composition != profile.worker_composition
        or handoff.planned_store_ledger != run.planned_store_ledger
        or handoff.base_release != run.base_release
        or handoff.expected_task_count != run.store_count
    ):
        raise IntegrityError("run execution evidence differs from its receipt inputs")

    task_rows = records.stream(run.task_result_ledger)
    store_rows = records.stream(run.store_ledger)
    count = 0
    for task_row, store_row in zip_longest(task_rows, store_rows):
        if task_row is None or store_row is None:
            raise IntegrityError("run task-result and store ledgers have different populations")
        if set(task_row) != {"recordId", "sourceItemId", "result"}:
            raise IntegrityError("run task-result ledger contains an invalid closed record")
        if set(store_row) != {"recordId", "sourceItemId", "store"}:
            raise IntegrityError("run store ledger contains an invalid closed record")
        try:
            result = StoreTaskResult.from_dict(task_row["result"])
            store = StoreRef.from_dict(store_row["store"])
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"run execution ledger contains an invalid reference: {error}") from error
        if (
            task_row["recordId"] != store.store_id
            or task_row["sourceItemId"] != store.store_id
            or store_row["recordId"] != store.store_id
            or store_row["sourceItemId"] != store.store_id
            or result.handoff_id != handoff.handoff_id
            or result.task.processing_plan_id != handoff.processing_plan.artifact_id
            or result.task.operation_id != handoff.operation_id
            or result.status is not StoreTaskStatus.SUCCEEDED
            or result.output_store != store
        ):
            raise IntegrityError("run task-result and store ledgers disagree")
        count += 1
    if count != run.store_count:
        raise IntegrityError("run execution-result count differs from its receipt")


def catalog_commit_token_digest(
    *,
    base_release: DocumentReleaseRef | None,
    run_receipt: ArtifactRef,
    store_receipt_set_digest: str,
    layers: tuple[LayerRef, ...],
) -> str:
    """Identify the exact run state authorized by a catalog commit receipt."""

    return identity_digest(
        {
            "baseRelease": None if base_release is None else base_release.to_dict(),
            "runReceipt": run_receipt.to_dict(),
            "storeReceiptSetDigest": store_receipt_set_digest,
            "layers": [layer.to_dict() for layer in layers],
        }
    )


def complete_release_counts(
    layers: tuple[LayerRef, ...],
    blob_roots: tuple[ArtifactRef, ...],
) -> dict[str, int]:
    """Compute the complete active-state counts recorded by a release."""

    counts = {f"layer:{layer.layer_kind}": layer.record_count for layer in layers}
    counts["activeLayers"] = len(layers)
    counts["activeBlobRoots"] = len(blob_roots)
    counts["logicalRecords"] = sum(layer.record_count for layer in layers)
    return counts


def complete_release_coverage(
    run_coverage: dict[str, Any],
    counts: dict[str, int],
) -> dict[str, Any]:
    """Carry run coverage forward and bind it to the complete active source layer."""

    coverage = dict(run_coverage)
    coverage["activeSourceItems"] = counts.get("layer:source-items", 0)
    return coverage


def complete_release_failure_summary(
    records: RecordStorage,
    layers: tuple[LayerRef, ...],
) -> dict[str, Any]:
    """Compute the active failure summary from the exact persisted failure layer."""

    failure_layer = next((layer for layer in layers if layer.layer_kind == "failures"), None)
    if failure_layer is None:
        return {"counts": {}, "first": None}
    counts: dict[str, int] = {}
    first: dict[str, Any] | None = None
    for row in records.stream(failure_layer):
        payload = row.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("failureClass"), str):
            raise IntegrityError("failure layer contains an invalid failure summary record")
        name = payload["failureClass"]
        counts[name] = counts.get(name, 0) + 1
        if first is None:
            first = payload
    return {"counts": dict(sorted(counts.items())), "first": first}


class DocumentReleaseVerifier:
    """Verify that one release and all of its immutable dependencies describe one build."""

    def __init__(
        self,
        *,
        controls: ControlRepository,
        records: RecordStorage,
        stores: DocumentStoreRepository,
        blobs: BlobStore | None = None,
    ) -> None:
        self._controls = controls
        self._records = records
        self._stores = stores
        self._blobs = blobs

    def verify(self, release: DocumentRelease) -> None:
        plan = self._load_plan(release.processing_plan)
        run = self._load_run(release.run_receipt)
        commit = self._load_commit(release.catalog_commit_receipt)
        policy = self._release_partition_policy(release.partition_policy)

        self._verify_receipt_links(release, plan, run, commit, policy)
        self._verify_expected_layers(release, plan)
        self._verify_run_ledgers(plan, run, policy)
        self._verify_active_layers(release, plan, policy)
        self._verify_blob_roots(release, plan)
        verified_blobs: set[tuple[str, str, int]] = set()

        def verify_retained_blob(reference: BlobRef) -> None:
            if not release.blob_roots:
                raise IntegrityError("release retains content without a declared blob root")
            if self._blobs is None:
                raise IntegrityError("release retains content without an injected blob store verifier")
            identity = (reference.locator, reference.digest, reference.byte_size)
            if identity in verified_blobs:
                return
            try:
                self._blobs.verify(reference)
            except (OSError, TypeError, ValueError, IntegrityError) as error:
                raise IntegrityError(
                    f"release retained blob {reference.digest} failed verification: {error}"
                ) from error
            verified_blobs.add(identity)

        verify_logical_release_layers(
            {layer.layer_kind: self._records.stream(layer) for layer in release.active_layers},
            verify_artifact=self._controls.verify,
            verify_blob=verify_retained_blob,
        )

        store_digest = self._verified_store_digest(plan, run)
        if store_digest != release.store_receipt_set_digest:
            raise IntegrityError("release store receipt-set digest differs from its verified store ledger")
        counts = complete_release_counts(release.active_layers, release.blob_roots)
        if release.counts != counts:
            raise IntegrityError("release counts differ from its active layers and blob roots")
        if release.coverage != complete_release_coverage(run.coverage, counts):
            raise IntegrityError("release coverage differs from its linked run and active source layer")
        if release.failures != complete_release_failure_summary(self._records, release.active_layers):
            raise IntegrityError("release failure summary differs from its active failure layer")

    def _load_plan(self, reference: ArtifactRef) -> ProcessingPlan:
        try:
            plan = ProcessingPlan.from_dict(self._controls.load(reference))
        except (KeyError, TypeError, ValueError, ProfileError) as error:
            raise IntegrityError(f"release processing plan is invalid: {error}") from error
        if plan.plan_id != reference.artifact_id:
            raise IntegrityError("release processing-plan identity differs from its artifact reference")
        return plan

    def _load_run(self, reference: ArtifactRef) -> RunReceipt:
        try:
            run = RunReceipt.from_dict(self._controls.load(reference))
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(f"release run receipt is invalid: {error}") from error
        if run.run_id != reference.artifact_id:
            raise IntegrityError("release run-receipt identity differs from its artifact reference")
        return run

    def _load_commit(self, reference: ArtifactRef) -> CatalogCommitReceipt:
        try:
            receipt = CatalogCommitReceipt.from_dict(self._controls.load(reference))
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(f"release catalog commit receipt is invalid: {error}") from error
        if receipt.receipt_id != reference.artifact_id:
            raise IntegrityError("release commit-receipt identity differs from its artifact reference")
        return receipt

    @staticmethod
    def _release_partition_policy(value: dict[str, Any]) -> PartitionPolicy:
        if set(value) != {"policyId", "bucketCount"}:
            raise IntegrityError("release partition policy has an invalid closed shape")
        bucket_count = value["bucketCount"]
        if not isinstance(bucket_count, int) or isinstance(bucket_count, bool):
            raise IntegrityError("release partition bucket count must be an integer")
        try:
            return PartitionPolicy(value["policyId"], bucket_count)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"release partition policy is invalid: {error}") from error

    @staticmethod
    def _verify_receipt_links(
        release: DocumentRelease,
        plan: ProcessingPlan,
        run: RunReceipt,
        commit: CatalogCommitReceipt,
        policy: PartitionPolicy,
    ) -> None:
        if release.processing_plan != run.plan:
            raise IntegrityError("release and run receipt name different processing plans")
        if release.source_catalog != plan.source_catalog or release.source_catalog != run.source_catalog:
            raise IntegrityError("release, plan, and run receipt name different source catalogs")
        if (
            release.previous_release != plan.base_release
            or release.previous_release != run.base_release
            or release.previous_release != commit.base_release
            or release.previous_release != commit.expected_head
        ):
            raise IntegrityError("release, plan, run, and commit receipt name different base releases")
        if release.profiles != plan.profiles:
            raise IntegrityError("release profiles differ from the processing plan")
        if release.retention_dispositions != plan.retention_policy:
            raise IntegrityError("release retention dispositions differ from the processing plan")
        if release.active_layers != run.staged_layers:
            raise IntegrityError("release active layers differ from the run receipt")
        if release.blob_roots != run.blob_roots:
            raise IntegrityError("release blob roots differ from the run receipt")
        if release.store_receipt_set_digest != run.store_receipt_set_digest:
            raise IntegrityError("release store receipt-set digest differs from the run receipt")
        if release.partition_policy != run.partition_policy or policy.bucket_count != plan.partition_count:
            raise IntegrityError("release partition policy differs from the run receipt or processing plan")
        if not run.stateful:
            raise IntegrityError("a DocumentRelease cannot name a stateless run receipt")
        if run.counts.get("rejectedStores", 0):
            raise IntegrityError("a DocumentRelease cannot name a run with rejected stores")
        if release.run_receipt != commit.run_receipt:
            raise IntegrityError("catalog commit receipt names a different run receipt")
        catalog_profile = plan.profiles.for_role(ProfileRole.DOCUMENT_CATALOG)
        if commit.profile_id != catalog_profile.profile_id:
            raise IntegrityError("catalog commit receipt uses a profile not pinned by the processing plan")
        if commit.prepared_at != run.completed_at:
            raise IntegrityError("catalog commit receipt and run receipt have different completion times")
        expected_token = catalog_commit_token_digest(
            base_release=release.previous_release,
            run_receipt=release.run_receipt,
            store_receipt_set_digest=run.store_receipt_set_digest,
            layers=run.staged_layers,
        )
        if commit.commit_token_digest != expected_token:
            raise IntegrityError("catalog commit token differs from the linked run state")

    def _verify_run_ledgers(
        self,
        plan: ProcessingPlan,
        run: RunReceipt,
        policy: PartitionPolicy,
    ) -> None:
        document_store_profile = plan.profiles.for_role(ProfileRole.DOCUMENT_STORE)
        if run.planned_store_ledger != self._stores.planned_store_ledger(plan.plan_id):
            raise IntegrityError("run receipt names a planned-store ledger from another plan")
        if run.planned_store_ledger.profile_id != document_store_profile.profile_id:
            raise IntegrityError("planned-store ledger uses a profile not pinned by the processing plan")

        record_profile = plan.profiles.for_role(ProfileRole.RECORD_STORAGE)
        for layer, expected_kind in (
            (run.store_ledger, "run-store-receipts"),
            (run.selection_ledger, "run-selection"),
            (run.task_result_ledger, "execution-task-results"),
        ):
            if layer.layer_kind != expected_kind:
                raise IntegrityError(f"run receipt {expected_kind} ledger has an unexpected logical kind")
            self._records.verify(layer)
            if layer.profile_id != record_profile.profile_id:
                raise IntegrityError(f"run receipt {expected_kind} ledger uses an unpinned record profile")
            if self._records.partition_policy(layer) != policy:
                raise IntegrityError(f"run receipt {expected_kind} ledger uses a different partition policy")
        _verify_execution_evidence(self._controls, self._records, run)

    def _verify_active_layers(
        self,
        release: DocumentRelease,
        plan: ProcessingPlan,
        policy: PartitionPolicy,
    ) -> None:
        record_profile = plan.profiles.for_role(ProfileRole.RECORD_STORAGE)
        for layer in release.active_layers:
            self._records.verify(layer)
            if layer.profile_id != record_profile.profile_id:
                raise IntegrityError(f"release layer {layer.layer_kind} uses an unpinned record profile")
            if self._records.partition_policy(layer) != policy:
                raise IntegrityError(f"release layer {layer.layer_kind} uses a different partition policy")

    @staticmethod
    def _verify_expected_layers(release: DocumentRelease, plan: ProcessingPlan) -> None:
        expected = set(core_delivery_schemas()) | {
            f"derived:{processor_id}" for processor_id in plan.stages.processor_ids
        }
        actual = {layer.layer_kind for layer in release.active_layers}
        missing = expected - actual
        if missing:
            raise IntegrityError(f"release is missing required active layers: {sorted(missing)}")
        unexpected_derived = {
            kind for kind in actual - expected if kind.startswith("derived:")
        }
        if unexpected_derived:
            raise IntegrityError(f"release contains unplanned derived layers: {sorted(unexpected_derived)}")

    def _verify_blob_roots(self, release: DocumentRelease, plan: ProcessingPlan) -> None:
        blob_profile = plan.profiles.for_role(ProfileRole.BLOB_STORAGE)
        for root in release.blob_roots:
            value = self._controls.load(root)
            if set(value) != {"profileId", "profileVersion", "storageRoot"}:
                raise IntegrityError("release blob-root profile state has an invalid closed shape")
            if value["profileId"] != blob_profile.profile_id or value["profileVersion"] != blob_profile.version:
                raise IntegrityError("release blob root uses a profile not pinned by the processing plan")
            if not isinstance(value["storageRoot"], str) or not value["storageRoot"]:
                raise IntegrityError("release blob-root profile state has an invalid storage root")

    def _verified_store_digest(self, plan: ProcessingPlan, run: RunReceipt) -> str:
        count = 0

        def verified_references() -> Iterator[dict[str, Any]]:
            nonlocal count
            for row in self._records.stream(run.store_ledger):
                if set(row) != {"recordId", "sourceItemId", "store"}:
                    raise IntegrityError("run store ledger contains a record with an invalid closed shape")
                try:
                    reference = StoreRef.from_dict(row["store"])
                except (TypeError, ValueError) as error:
                    raise IntegrityError(f"run store ledger contains an invalid store reference: {error}") from error
                if row["recordId"] != reference.store_id or row["sourceItemId"] != reference.store_id:
                    raise IntegrityError("run store ledger record identity differs from its store reference")
                store = self._stores.load(reference)
                if store.state != StoreState.SEALED:
                    raise IntegrityError("run store ledger names an unsealed document store")
                if store.plan_id != plan.plan_id:
                    raise IntegrityError("run store ledger names a document store from another plan")
                count += 1
                yield reference.to_dict()

        digest = ordered_json_sequence_digest(verified_references())
        if count != run.store_count:
            raise IntegrityError("run store ledger count differs from the run receipt")
        if digest != run.store_receipt_set_digest:
            raise IntegrityError("run store ledger differs from its receipt-set digest")
        return digest


class ReleaseCommitService:
    """Keep workers in staging; this service is the sole release visibility point."""

    def __init__(
        self,
        *,
        plan_ref: ArtifactRef,
        controls: ControlRepository,
        records: RecordStorage,
        document_catalog: DocumentCatalog,
    ) -> None:
        self._plan_ref = plan_ref
        self._controls = controls
        self._records = records
        self._document_catalog = document_catalog

    def commit_release(
        self,
        base_document_release_ref: DocumentReleaseRef | None,
        run_receipt_ref: ArtifactRef,
    ) -> DocumentReleaseRef:
        plan = self._load_plan()
        run = self._load_run(run_receipt_ref)
        if run.plan != self._plan_ref or run.base_release != base_document_release_ref:
            raise IntegrityError("run receipt differs from the requested plan or base release")
        if not run.stateful:
            raise StateTransitionError("a stateless returned-result run cannot commit a DocumentRelease")
        if run.counts.get("rejectedStores", 0):
            raise StateTransitionError("a run with rejected stores cannot publish catalog state")
        current = self._document_catalog.current()
        if current is not None:
            opened = self._document_catalog.open(current)
            if opened.previous_release == base_document_release_ref and opened.run_receipt == run_receipt_ref:
                return current
        self._verify_run_layers(plan, run)
        commit_token = catalog_commit_token_digest(
            base_release=base_document_release_ref,
            run_receipt=run_receipt_ref,
            store_receipt_set_digest=run.store_receipt_set_digest,
            layers=run.staged_layers,
        )
        catalog_profile = plan.profiles.for_role(ProfileRole.DOCUMENT_CATALOG)
        commit_receipt = CatalogCommitReceipt.create(
            profile_id=catalog_profile.profile_id,
            base_release=base_document_release_ref,
            expected_head=base_document_release_ref,
            run_receipt=run_receipt_ref,
            commit_token_digest=commit_token,
            prepared_at=run.completed_at,
        )
        commit_receipt_ref = self._controls.put(
            kind="catalog-commit-receipts",
            artifact_id=commit_receipt.receipt_id,
            value=commit_receipt.to_dict(),
        )
        failures = complete_release_failure_summary(self._records, run.staged_layers)
        counts = complete_release_counts(run.staged_layers, run.blob_roots)
        coverage = complete_release_coverage(run.coverage, counts)
        release = DocumentRelease.create(
            release_id=self._document_catalog.release_id(plan, run.partition_policy),
            previous_release=base_document_release_ref,
            source_catalog=run.source_catalog,
            processing_plan=self._plan_ref,
            profiles=plan.profiles,
            active_layers=run.staged_layers,
            blob_roots=run.blob_roots,
            retention_dispositions=plan.retention_policy,
            store_receipt_set_digest=run.store_receipt_set_digest,
            run_receipt=run_receipt_ref,
            catalog_commit_receipt=commit_receipt_ref,
            counts=counts,
            failures=failures,
            coverage=coverage,
            partition_policy=run.partition_policy,
        )
        staged = self._document_catalog.stage(release)
        committed = self._document_catalog.commit(
            staged,
            expected_base=base_document_release_ref,
            stores=self._store_references(run),
        )
        if committed.release_id != release.release_id:
            raise IntegrityError("document catalog committed a different release")
        return committed

    def _load_plan(self) -> ProcessingPlan:
        self._controls.verify(self._plan_ref)
        return ProcessingPlan.from_dict(self._controls.load(self._plan_ref))

    def _load_run(self, reference: ArtifactRef) -> RunReceipt:
        self._controls.verify(reference)
        try:
            return RunReceipt.from_dict(self._controls.load(reference))
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"run receipt is invalid: {error}") from error

    def _verify_run_layers(self, plan: ProcessingPlan, run: RunReceipt) -> None:
        record_profile = plan.profiles.for_role(ProfileRole.RECORD_STORAGE)
        self._records.verify(run.store_ledger)
        self._records.verify(run.selection_ledger)
        self._records.verify(run.task_result_ledger)
        if run.store_ledger.profile_id != record_profile.profile_id:
            raise IntegrityError("run ledger uses a record profile not pinned by the plan")
        if run.selection_ledger.profile_id != record_profile.profile_id:
            raise IntegrityError("selection ledger uses a record profile not pinned by the plan")
        if run.task_result_ledger.profile_id != record_profile.profile_id:
            raise IntegrityError("task-result ledger uses a record profile not pinned by the plan")
        _verify_execution_evidence(self._controls, self._records, run)
        layer_kinds = [layer.layer_kind for layer in run.staged_layers]
        if layer_kinds != sorted(set(layer_kinds)):
            raise IntegrityError("run active layer kinds must be sorted and distinct")
        for layer in run.staged_layers:
            self._records.verify(layer)
            if layer.profile_id != record_profile.profile_id:
                raise IntegrityError(f"layer {layer.layer_kind} uses a record profile not pinned by the plan")
        digest = ordered_json_sequence_digest(row["store"] for row in self._records.stream(run.store_ledger))
        if digest != run.store_receipt_set_digest:
            raise IntegrityError("run store ledger differs from its receipt-set digest")

    def _store_references(self, run: RunReceipt) -> Iterator[StoreRef]:
        count = 0
        for row in self._records.stream(run.store_ledger):
            try:
                reference = StoreRef.from_dict(row["store"])
            except (TypeError, ValueError) as error:
                raise IntegrityError(f"run store reference is invalid: {error}") from error
            count += 1
            yield reference
        if count != run.store_count:
            raise IntegrityError("run store ledger count differs")
