"""Read pinned run evidence without executing work or creating retained state."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, closing

from docspec.domain.delivery import verify_logical_release_layers
from docspec.domain.execution import ExecutionProfile
from docspec.domain.identity import ordered_json_sequence_digest
from docspec.domain.jobs import DocumentStore, StoreState, StoreVerdict
from docspec.domain.plans import ProcessingPlan
from docspec.domain.profiles import ProfileRole
from docspec.domain.receipts import RunReceipt
from docspec.domain.references import ArtifactRef, BlobRef, LayerRef, StoreRef
from docspec.domain.storage import PartitionPolicy
from docspec.errors import IntegrityError
from docspec.ports.control_repository import ControlRepository
from docspec.ports.blob_store import BlobStore
from docspec.ports.document_store_repository import DocumentStoreRepository
from docspec.ports.record_storage import RecordStorage

from .commit import _verify_execution_evidence, verify_expected_layers
from .store_state import load_latest_store


def load_run(
    reference: ArtifactRef,
    plan: ProcessingPlan,
    controls: ControlRepository,
    stores: DocumentStoreRepository,
    records: RecordStorage,
    blobs: BlobStore,
    *,
    admitted_layers: tuple[LayerRef, ...] | None = None,
) -> tuple[RunReceipt, ExecutionProfile]:
    """Admit the immutable ledgers, retaining their exact output-store pins."""

    try:
        run = RunReceipt.from_dict(controls.load(reference))
        saved_plan = ProcessingPlan.from_dict(controls.load(run.plan))
        profile = ExecutionProfile.from_dict(controls.load(run.execution_profile))
    except (TypeError, ValueError) as error:
        raise IntegrityError(f"inspection run controls are invalid: {error}") from error
    if reference.artifact_id != run.run_id or saved_plan != plan or run.plan.artifact_id != plan.plan_id:
        raise IntegrityError("inspection run differs from its processing plan or reference")
    if run.source_catalog != plan.source_catalog or run.base_release != plan.base_release:
        raise IntegrityError("inspection run input pins differ from its processing plan")
    if run.planned_store_ledger != stores.planned_store_ledger(plan.plan_id):
        raise IntegrityError("inspection run names a different planned-store population")
    stores.verify_planned_store_ledger(run.planned_store_ledger)
    record_profile = plan.profiles.for_role(ProfileRole.RECORD_STORAGE).profile_id
    for layer, kind in (
        (run.store_ledger, "run-store-receipts"),
        (run.selection_ledger, "run-selection"),
        (run.task_result_ledger, "execution-task-results"),
    ):
        if layer.layer_kind != kind or layer.profile_id != record_profile:
            raise IntegrityError("inspection run ledger kind or profile differs")
        records.verify(layer)
    if ordered_json_sequence_digest(row["store"] for row in records.stream(run.store_ledger)) != run.store_receipt_set_digest:
        raise IntegrityError("inspection run store-set digest differs")
    _verify_execution_evidence(controls, records, run)
    kinds = tuple(layer.layer_kind for layer in run.staged_layers)
    if kinds != tuple(sorted(set(kinds))):
        raise IntegrityError("inspection result layer kinds must be sorted and distinct")
    for layer in run.staged_layers:
        if layer.profile_id != record_profile:
            raise IntegrityError("inspection result layer uses an unpinned record profile")
        records.verify(layer)
    rejected = selected = 0
    for _reference, store in work_stores(plan, run, stores, records):
        rejected += store.verdict is StoreVerdict.REJECTED
        selected += len(store.entries)
    if rejected != run.counts.get("rejectedStores", 0) or selected != run.selected_item_count:
        raise IntegrityError("inspection run outcome counts differ from its exact stores")
    if admitted_layers is not None and run.staged_layers != admitted_layers:
        raise IntegrityError("inspection run layers differ from the admitted retained result")
    if run.stateful and not rejected and admitted_layers is None:
        _verify_active_result(run, plan, controls, records, blobs)
    return run, profile


def _verify_active_result(
    run: RunReceipt, plan: ProcessingPlan, controls: ControlRepository,
    records: RecordStorage, blobs: BlobStore,
) -> None:
    verify_expected_layers(run.staged_layers, plan)
    if set(run.partition_policy) != {"policyId", "bucketCount"} or type(run.partition_policy["bucketCount"]) is not int:
        raise IntegrityError("inspection result partition policy has an invalid closed shape")
    try:
        policy = PartitionPolicy(run.partition_policy["policyId"], run.partition_policy["bucketCount"])
    except (TypeError, ValueError) as error:
        raise IntegrityError(f"inspection result partition policy is invalid: {error}") from error
    if policy.bucket_count != plan.partition_count:
        raise IntegrityError("inspection result partition count differs from its processing plan")
    for layer in (*run.staged_layers, run.store_ledger, run.selection_ledger, run.task_result_ledger):
        if records.partition_policy(layer) != policy:
            raise IntegrityError("inspection result layer partition policy differs")
    blob_profile = plan.profiles.for_role(ProfileRole.BLOB_STORAGE)
    for root in run.blob_roots:
        value = controls.load(root)
        if set(value) != {"profileId", "profileVersion", "storageRoot"} or (
            value["profileId"] != blob_profile.profile_id or value["profileVersion"] != blob_profile.version
            or not isinstance(value["storageRoot"], str) or not value["storageRoot"]
        ):
            raise IntegrityError("inspection result blob root differs from its pinned profile")

    def verify_blob(reference: BlobRef) -> None:
        if not run.blob_roots:
            raise IntegrityError("inspection result retains content without a declared blob root")
        blobs.verify(reference)

    with ExitStack() as stack:
        layers = {layer.layer_kind: stack.enter_context(closing(records.stream(layer))) for layer in run.staged_layers}
        verify_logical_release_layers(layers, verify_artifact=controls.verify, verify_blob=verify_blob)


def work_stores(
    plan: ProcessingPlan,
    run: RunReceipt | None,
    stores: DocumentStoreRepository,
    records: RecordStorage,
) -> Iterator[tuple[StoreRef, DocumentStore]]:
    """A run reads exact final refs; a plan observes each latest revision once."""

    if run is None:
        if not stores.has_planned_store_ledger(plan.plan_id):
            return
        ledger = stores.planned_store_ledger(plan.plan_id)
        stores.verify_planned_store_ledger(ledger)
        for planned in stores.stream_planned_stores(ledger):
            reference, store = load_latest_store(stores, planned)
            if store.plan_id != plan.plan_id or reference.store_id != planned.store_id:
                raise IntegrityError("observed store differs from the planned population")
            yield reference, store
        return
    count = 0
    for row in records.stream(run.store_ledger):
        reference = StoreRef.from_dict(row["store"])
        store = stores.load(reference)
        if store.plan_id != run.plan.artifact_id or store.state is not StoreState.SEALED:
            raise IntegrityError("inspection run contains an unsealed or differently planned store")
        count += 1
        yield reference, store
    if count != run.store_count:
        raise IntegrityError("inspection run store count differs from its exact ledger")
