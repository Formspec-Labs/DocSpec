"""Format-neutral retention and compaction application services."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from docspec.application.commit import ReleaseCommitService
from docspec.application.reconcile import run_ledger_schemas
from docspec.domain.content import CapturedFile, Representation, Segment
from docspec.domain.execution import (
    ExecutionHandoff,
    ExecutionLimits,
    ExecutionProfile,
    summarize_store_tasks,
)
from docspec.domain.identity import (
    identity_digest,
    ordered_json_sequence_digest,
    stable_urn,
)
from docspec.domain.maintenance import BlobRetentionSet, ReleaseCompactionReceipt
from docspec.domain.plans import ProcessingPlan
from docspec.domain.receipts import DeliveryReceipt, RunReceipt
from docspec.domain.references import ArtifactRef, BlobRef, DocumentReleaseRef, LayerRef, StoreRef
from docspec.domain.release import DocumentRelease
from docspec.domain.storage import PartitionPolicy, RecordSchema
from docspec.errors import IntegrityError, StaleBaseError, StateTransitionError
from docspec.ports.blob_store import BlobStore
from docspec.ports.control_repository import ControlRepository
from docspec.ports.document_catalog import DocumentCatalog
from docspec.ports.document_store_repository import DocumentStoreRepository
from docspec.ports.profile_state_reachability import ProfileStateBlobReachability
from docspec.ports.record_storage import RecordStorage
from docspec.ports.record_workspace import RecordWorkspace, RecordWorkspaceFactory

_RETENTION_COLLECTION = "maintenance:blob-retention-references"
_VISITED_STORE_COLLECTION = "maintenance:visited-document-stores"
_RETENTION_SCHEMA = RecordSchema(
    "docspec-blob-retention-reference/1.0",
    (
        "recordId",
        "blobProfileStateId",
        "blobProfileStateDigest",
        "locator",
        "digest",
        "byteSize",
        "mediaType",
    ),
    "recordId",
    "locator",
)
_COMPACTION_OPERATION_ID = "compact-release/v1"
_T = TypeVar("_T")


def _ordered_distinct(
    values: Iterable[_T],
    *,
    identity: Callable[[_T], Any],
    order: Callable[[_T], Any],
    label: str,
) -> tuple[_T, ...]:
    """Normalize explicit small root sets while rejecting identity conflicts."""

    distinct: dict[Any, _T] = {}
    for value in values:
        key = identity(value)
        previous = distinct.get(key)
        if previous is not None and previous != value:
            raise IntegrityError(f"{label} contains conflicting references for {key!r}")
        distinct[key] = value
    return tuple(sorted(distinct.values(), key=order))


def _blob_record(blob_profile_state: ArtifactRef, reference: BlobRef) -> dict[str, Any]:
    reachability_key = {
        "blobProfileState": blob_profile_state.to_dict(),
        "locator": reference.locator,
    }
    return {
        "recordId": stable_urn("blob-retention-reference", reachability_key),
        "blobProfileStateId": blob_profile_state.artifact_id,
        "blobProfileStateDigest": blob_profile_state.digest,
        "locator": reference.locator,
        "digest": reference.digest,
        "byteSize": reference.byte_size,
        "mediaType": reference.media_type,
    }


class BlobRetentionSetService:
    """Derive a scalable blob-retention layer from verified immutable roots."""

    def __init__(
        self,
        *,
        controls: ControlRepository,
        records: RecordStorage,
        stores: DocumentStoreRepository,
        blobs: BlobStore,
        document_catalog: DocumentCatalog,
        profile_state_reachability: ProfileStateBlobReachability,
        workspace_factory: RecordWorkspaceFactory,
        partition_policy: PartitionPolicy,
    ) -> None:
        self._controls = controls
        self._records = records
        self._stores = stores
        self._blobs = blobs
        self._document_catalog = document_catalog
        self._profile_state_reachability = profile_state_reachability
        self._workspace_factory = workspace_factory
        self._partition_policy = partition_policy

    def build(
        self,
        *,
        blob_profile_state: ArtifactRef,
        retained_releases: Iterable[DocumentReleaseRef] = (),
        retained_stores: Iterable[StoreRef] = (),
    ) -> ArtifactRef:
        releases = _ordered_distinct(
            retained_releases,
            identity=lambda item: item.release_id,
            order=lambda item: (item.release_id, item.locator, item.digest),
            label="retained releases",
        )
        stores = _ordered_distinct(
            retained_stores,
            identity=lambda item: (item.store_id, item.revision),
            order=lambda item: (item.store_id, item.revision, item.locator, item.digest),
            label="retained stores",
        )
        if not releases and not stores:
            raise ValueError("blob retention requires at least one immutable root")

        metrics = {
            "profileStateVerificationCount": 0,
            "catalogVerifiedReleaseCount": 0,
            "visitedStoreRevisionCount": 0,
            "activeBlobLayerScanCount": 0,
            "activeBlobRecordReadCount": 0,
            "blobReferenceOccurrenceCount": 0,
            "directBlobVerificationCount": 0,
            "retainedReferenceCount": 0,
            "boundedStreaming": True,
        }
        with self._workspace_factory.create() as workspace:
            state = self._controls.load(blob_profile_state)
            metrics["profileStateVerificationCount"] += 1
            for reference in self._profile_state_reachability.references(blob_profile_state, state):
                self._retain_blob(
                    blob_profile_state,
                    reference,
                    workspace,
                    metrics,
                    verify=True,
                )
            for reference in releases:
                self._retain_release(
                    reference,
                    blob_profile_state,
                    workspace,
                    metrics,
                )
            for reference in stores:
                self._retain_store(
                    reference,
                    blob_profile_state,
                    workspace,
                    metrics,
                )
            layer = self._records.write_layer(
                workspace.stream_records(_RETENTION_COLLECTION),
                layer_kind="blob-retention-references",
                schema=_RETENTION_SCHEMA,
                partition_policy=self._partition_policy,
            )

        self._records.verify(layer)
        metrics["retainedReferenceCount"] = layer.record_count
        retention_set = BlobRetentionSet.create(
            blob_profile_state=blob_profile_state,
            retained_releases=releases,
            retained_stores=stores,
            references=layer,
            verification_evidence=metrics,
        )
        return self._controls.put(
            kind="blob-retention-sets",
            artifact_id=retention_set.retention_set_id,
            value=retention_set.to_dict(),
        )

    def _retain_release(
        self,
        reference: DocumentReleaseRef,
        blob_profile_state: ArtifactRef,
        workspace: RecordWorkspace,
        metrics: dict[str, Any],
    ) -> None:
        release = self._document_catalog.open(reference)
        metrics["catalogVerifiedReleaseCount"] += 1
        for profile_state in release.blob_roots:
            self._require_profile_state(profile_state, blob_profile_state)

        self._retain_active_release_blobs(
            release,
            blob_profile_state,
            workspace,
            metrics,
        )

        try:
            run = RunReceipt.from_dict(self._controls.load(release.run_receipt))
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"retained release run receipt is invalid: {error}") from error
        if run.run_id != release.run_receipt.artifact_id:
            raise IntegrityError("retained release run-receipt identity differs from its reference")
        for row in self._records.stream(run.store_ledger):
            if set(row) != {"recordId", "sourceItemId", "store"}:
                raise IntegrityError("retained release store ledger has an invalid closed record")
            try:
                store_reference = StoreRef.from_dict(row["store"])
            except (TypeError, ValueError) as error:
                raise IntegrityError(f"retained release store reference is invalid: {error}") from error
            if row["recordId"] != store_reference.store_id or row["sourceItemId"] != store_reference.store_id:
                raise IntegrityError("retained release store-ledger identity differs from its reference")
            self._retain_store(
                store_reference,
                blob_profile_state,
                workspace,
                metrics,
            )

    def _retain_active_release_blobs(
        self,
        release: DocumentRelease,
        blob_profile_state: ArtifactRef,
        workspace: RecordWorkspace,
        metrics: dict[str, Any],
    ) -> None:
        parsers: dict[str, tuple[type[Any], str]] = {
            "files": (CapturedFile, "blob"),
            "representations": (Representation, "blob"),
            "segments": (Segment, "content"),
        }
        for layer in release.active_layers:
            configured = parsers.get(layer.layer_kind)
            if configured is None:
                continue
            parser, attribute = configured
            metrics["activeBlobLayerScanCount"] += 1
            for row in self._records.stream(layer):
                metrics["activeBlobRecordReadCount"] += 1
                if set(row) != {"recordId", "sourceItemId", "idempotencyKey", "deleted", "payload"}:
                    raise IntegrityError("retained release blob layer contains an invalid closed record")
                payload = row["payload"]
                if not isinstance(payload, dict):
                    raise IntegrityError("retained release blob layer payload is not an object")
                try:
                    item = parser.from_dict(payload)
                except (TypeError, ValueError) as error:
                    raise IntegrityError(f"retained release blob record is invalid: {error}") from error
                self._retain_blob(
                    blob_profile_state,
                    getattr(item, attribute),
                    workspace,
                    metrics,
                    verify=False,
                )

    def _retain_store(
        self,
        reference: StoreRef,
        blob_profile_state: ArtifactRef,
        workspace: RecordWorkspace,
        metrics: dict[str, Any],
    ) -> None:
        visit_identity = stable_urn(
            "retained-store-visit",
            {"storeId": reference.store_id, "revision": reference.revision},
        )
        visit_record = {"recordId": visit_identity, "store": reference.to_dict()}
        existing_visit = workspace.lookup_record(_VISITED_STORE_COLLECTION, visit_identity)
        if existing_visit is not None:
            if existing_visit != visit_record:
                raise IntegrityError("retained store visit has conflicting immutable references")
            return
        workspace.add_record(
            _VISITED_STORE_COLLECTION,
            identity=visit_identity,
            source_item_id=reference.store_id,
            record=visit_record,
        )
        store = self._stores.load(reference)
        metrics["visitedStoreRevisionCount"] += 1
        for entry in store.entries:
            for captured in entry.captured_files:
                self._retain_blob(blob_profile_state, captured.blob, workspace, metrics, verify=True)
            for representation in entry.representations:
                self._retain_blob(blob_profile_state, representation.blob, workspace, metrics, verify=True)
            for segment in entry.segments:
                self._retain_blob(blob_profile_state, segment.content, workspace, metrics, verify=True)
            for receipt in entry.stage_receipts:
                self._controls.verify(receipt)

        if store.delivery_receipt is None:
            return
        try:
            delivery = DeliveryReceipt.from_dict(self._controls.load(store.delivery_receipt))
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"retained store delivery receipt is invalid: {error}") from error
        if (
            delivery.receipt_id != store.delivery_receipt.artifact_id
            or delivery.store_id != store.store_id
            or delivery.store_revision != store.revision - 1
        ):
            raise IntegrityError("retained store delivery receipt names another store revision")
        for profile_state in delivery.blob_roots:
            self._require_profile_state(profile_state, blob_profile_state)

    @staticmethod
    def _require_profile_state(reference: ArtifactRef, expected: ArtifactRef) -> None:
        if reference != expected:
            raise IntegrityError("blob retention roots mix distinct blob profile states")

    def _retain_blob(
        self,
        blob_profile_state: ArtifactRef,
        reference: BlobRef,
        workspace: RecordWorkspace,
        metrics: dict[str, Any],
        *,
        verify: bool,
    ) -> None:
        metrics["blobReferenceOccurrenceCount"] += 1
        record = _blob_record(blob_profile_state, reference)
        existing = workspace.lookup_record(_RETENTION_COLLECTION, record["recordId"])
        if existing is not None:
            if existing != record:
                raise IntegrityError("blob retention root and locator have conflicting immutable metadata")
            return
        if verify:
            self._blobs.verify(reference)
            metrics["directBlobVerificationCount"] += 1
        workspace.add_record(
            _RETENTION_COLLECTION,
            identity=record["recordId"],
            source_item_id=reference.locator,
            record=record,
        )


def _logical_layers_state_digest(
    records: RecordStorage,
    layers: tuple[LayerRef, ...],
) -> tuple[str, int]:
    summaries = []
    record_read_count = 0
    for layer in layers:
        layer_read_count = 0

        def counted_records() -> Iterable[dict[str, Any]]:
            nonlocal layer_read_count
            for record in records.stream(layer):
                layer_read_count += 1
                yield record

        summaries.append(
            {
                "layerKind": layer.layer_kind,
                "schemaId": layer.schema_id,
                "recordCount": layer.record_count,
                "recordsDigest": ordered_json_sequence_digest(counted_records()),
            }
        )
        if layer_read_count != layer.record_count:
            raise IntegrityError("logical layer stream count differs from its immutable reference")
        record_read_count += layer_read_count
    return identity_digest({"layers": summaries}), record_read_count


def logical_release_state_digest(records: RecordStorage, release: DocumentRelease) -> str:
    """Digest exact logical records without including their physical layer roots."""

    digest, _ = _logical_layers_state_digest(records, release.active_layers)
    return digest


class ReleaseCompactionService:
    """Rewrite physical shards and publish an equivalent successor release."""

    def __init__(
        self,
        *,
        controls: ControlRepository,
        records: RecordStorage,
        stores: DocumentStoreRepository,
        document_catalog: DocumentCatalog,
        clock: Callable[[], str],
    ) -> None:
        self._controls = controls
        self._records = records
        self._stores = stores
        self._document_catalog = document_catalog
        self._clock = clock

    def compact(self, source_reference: DocumentReleaseRef) -> ArtifactRef:
        source = self._document_catalog.open(source_reference)
        source_digest, source_digest_reads = _logical_layers_state_digest(
            self._records,
            source.active_layers,
        )

        compacted_results = tuple(self._compact_layer(layer) for layer in source.active_layers)
        compacted_layers = tuple(layer for layer, _ in compacted_results)
        rewrite_reads = sum(read_count for _, read_count in compacted_results)
        compacted_digest, successor_digest_reads = _logical_layers_state_digest(
            self._records,
            compacted_layers,
        )
        if compacted_digest != source_digest:
            raise IntegrityError("compaction changed active logical records")
        rewritten = tuple(
            layer.layer_kind
            for layer, compacted in zip(source.active_layers, compacted_layers, strict=True)
            if layer != compacted
        )
        if not rewritten:
            raise StateTransitionError("release layers already match the composed compaction profile")
        rewritten_set = set(rewritten)
        reused = tuple(layer.layer_kind for layer in source.active_layers if layer.layer_kind not in rewritten_set)
        completed_at = self._clock()

        plan_ref, run_ref = self._maintenance_run(
            source_reference,
            source,
            compacted_layers,
            rewritten,
            reused,
            completed_at,
        )
        try:
            successor_reference = ReleaseCommitService(
                plan_ref=plan_ref,
                controls=self._controls,
                records=self._records,
                document_catalog=self._document_catalog,
            ).commit_release(source_reference, run_ref)
        except (StaleBaseError, StateTransitionError) as commit_error:
            current = self._document_catalog.current()
            if current is None or current == source_reference:
                raise
            try:
                successor_reference = current
                successor_completed_at = self._verify_successor(
                    source_reference,
                    source,
                    successor_reference,
                    compacted_layers,
                    source_digest,
                    compacted_digest,
                )
            except IntegrityError:
                raise commit_error
        else:
            successor_completed_at = self._verify_successor(
                source_reference,
                source,
                successor_reference,
                compacted_layers,
                source_digest,
                compacted_digest,
            )
        self._after_catalog_commit(successor_reference)
        logical_record_count = sum(layer.record_count for layer in source.active_layers)
        receipt = ReleaseCompactionReceipt.create(
            source_release=source_reference,
            successor_release=successor_reference,
            source_logical_state_digest=source_digest,
            successor_logical_state_digest=compacted_digest,
            rewritten_layer_kinds=tuple(sorted(rewritten)),
            reused_layer_kinds=tuple(sorted(reused)),
            completed_at=successor_completed_at,
            verification_evidence={
                "logicalRecordCount": logical_record_count,
                "logicalRecordReadCount": (
                    source_digest_reads + rewrite_reads + successor_digest_reads
                ),
                "logicalScanPassCount": 3,
                "explicitCatalogOpenCount": 2,
                "boundedStreaming": True,
            },
        )
        return self._controls.put(
            kind="release-compaction-receipts",
            artifact_id=receipt.receipt_id,
            value=receipt.to_dict(),
        )

    def _verify_successor(
        self,
        source_reference: DocumentReleaseRef,
        source: DocumentRelease,
        successor_reference: DocumentReleaseRef,
        compacted_layers: tuple[LayerRef, ...],
        source_digest: str,
        compacted_digest: str,
    ) -> str:
        successor = self._document_catalog.open(successor_reference)
        if (
            successor.previous_release != source_reference
            or successor.active_layers != compacted_layers
            or successor.source_catalog != source.source_catalog
            or successor.profiles != source.profiles
            or successor.blob_roots != source.blob_roots
            or successor.retention_dispositions != source.retention_dispositions
            or successor.counts != source.counts
            or successor.failures != source.failures
            or successor.coverage != source.coverage
            or successor.partition_policy != source.partition_policy
            or compacted_digest != source_digest
        ):
            raise IntegrityError("catalog head is not the intended equivalent compaction successor")
        try:
            plan = ProcessingPlan.from_dict(self._controls.load(successor.processing_plan))
            run = RunReceipt.from_dict(self._controls.load(successor.run_receipt))
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"compaction successor controls are invalid: {error}") from error
        selection = plan.selection
        if (
            set(selection) != {"maintenanceOperation", "sourceReleaseId", "maintenanceCompletedAt"}
            or selection["maintenanceOperation"] != _COMPACTION_OPERATION_ID
            or selection["sourceReleaseId"] != source.release_id
            or selection["maintenanceCompletedAt"] != run.completed_at
            or plan.base_release != source_reference
            or run.base_release != source_reference
            or run.store_count != 0
            or run.selected_item_count != 0
        ):
            raise IntegrityError("catalog head is not a sealed zero-task compaction run")
        return run.completed_at

    def _after_catalog_commit(self, successor_reference: DocumentReleaseRef) -> None:
        """No-op failpoint used to prove receipt recovery after the visibility CAS."""

        del successor_reference

    def _compact_layer(self, layer: LayerRef) -> tuple[LayerRef, int]:
        schema = self._records.schema(layer)
        policy = self._records.partition_policy(layer)
        record_read_count = 0

        def counted_records() -> Iterable[dict[str, Any]]:
            nonlocal record_read_count
            for record in self._records.stream(layer):
                record_read_count += 1
                yield record

        compacted = self._records.write_layer(
            counted_records(),
            layer_kind=layer.layer_kind,
            schema=schema,
            partition_policy=policy,
        )
        if record_read_count != layer.record_count:
            raise IntegrityError("compaction layer stream count differs from its immutable reference")
        return compacted, record_read_count

    def _maintenance_run(
        self,
        source_reference: DocumentReleaseRef,
        source: DocumentRelease,
        compacted_layers: tuple[LayerRef, ...],
        rewritten: tuple[str, ...],
        reused: tuple[str, ...],
        completed_at: str,
    ) -> tuple[ArtifactRef, ArtifactRef]:
        try:
            source_plan = ProcessingPlan.from_dict(self._controls.load(source.processing_plan))
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"compaction source processing plan is invalid: {error}") from error
        plan = ProcessingPlan.create(
            source_catalog=source.source_catalog,
            base_release=source_reference,
            profiles=source.profiles,
            limits=source_plan.limits,
            stages=source_plan.stages,
            processors=source_plan.processors,
            partition_count=source_plan.partition_count,
            selection={
                "maintenanceOperation": _COMPACTION_OPERATION_ID,
                "sourceReleaseId": source.release_id,
                "maintenanceCompletedAt": completed_at,
            },
            retention_policy=source_plan.retention_policy,
            data_use_policy=source_plan.data_use_policy,
            retry_policy_digest=source_plan.retry_policy_digest,
            accepted_failure_policy_digest=source_plan.accepted_failure_policy_digest,
        )
        plan_ref = self._controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
        planned_store_ledger = self._stores.seal_planned_stores(plan.plan_id, ())
        store_schema, selection_schema, task_result_schema = run_ledger_schemas()
        policy = self._common_partition_policy(compacted_layers, plan.partition_count)
        store_ledger = self._records.write_layer(
            (),
            layer_kind="run-store-receipts",
            schema=store_schema,
            partition_policy=policy,
        )
        selection_ledger = self._records.write_layer(
            (),
            layer_kind="run-selection",
            schema=selection_schema,
            partition_policy=policy,
        )
        task_result_ledger = self._records.write_layer(
            (),
            layer_kind="execution-task-results",
            schema=task_result_schema,
            partition_policy=policy,
        )
        execution_profile_ref, execution_handoff_ref = self._execution_evidence(
            plan_ref,
            source_reference,
            planned_store_ledger,
        )
        run = RunReceipt.create(
            plan=plan_ref,
            execution_profile=execution_profile_ref,
            execution_handoff=execution_handoff_ref,
            source_catalog=source.source_catalog,
            base_release=source_reference,
            planned_store_ledger=planned_store_ledger,
            store_ledger=store_ledger,
            store_count=0,
            selection_ledger=selection_ledger,
            selected_item_count=0,
            task_result_ledger=task_result_ledger,
            store_receipt_set_digest=ordered_json_sequence_digest(()),
            staged_layers=compacted_layers,
            blob_roots=source.blob_roots,
            counts={
                "stores": 0,
                "selectedItems": 0,
                "rejectedStores": 0,
                "rewrittenLayers": len(rewritten),
                "reusedLayers": len(reused),
            },
            failures=source.failures,
            coverage=source.coverage,
            partition_policy={"policyId": policy.policy_id, "bucketCount": policy.bucket_count},
            stateful=True,
            completed_at=completed_at,
        )
        run_ref = self._controls.put(kind="run-receipts", artifact_id=run.run_id, value=run.to_dict())
        return plan_ref, run_ref

    def _execution_evidence(
        self,
        plan_ref: ArtifactRef,
        source_reference: DocumentReleaseRef,
        planned_store_ledger: LayerRef,
    ) -> tuple[ArtifactRef, ArtifactRef]:
        worker_content = {"implementationId": "docspec.release-compaction/v1"}
        worker = self._controls.put(
            kind="worker-compositions",
            artifact_id=stable_urn("worker-composition", worker_content),
            value=worker_content,
        )
        scheduler_content = {"adapterId": "docspec.inline-maintenance", "operationId": _COMPACTION_OPERATION_ID}
        scheduler = self._controls.put(
            kind="scheduler-configurations",
            artifact_id=stable_urn("scheduler-configuration", scheduler_content),
            value=scheduler_content,
        )
        profile = ExecutionProfile(
            "docspec.inline-maintenance",
            "1.0.0",
            worker,
            scheduler,
            ExecutionLimits(1, 1, 1, 1, 1, 1, 1, 1, 0, 0),
            2_147_483_647,
        )
        profile_ref = self._controls.put(
            kind="execution-profiles",
            artifact_id=profile.profile_id,
            value=profile.to_dict(),
        )
        task_count, task_set_digest = summarize_store_tasks(())
        sink_content = {"sinkId": "urn:docspec:sink:maintenance:none"}
        sink = self._controls.put(
            kind="sinks",
            artifact_id=sink_content["sinkId"],
            value=sink_content,
        )
        handoff = ExecutionHandoff(
            processing_plan=plan_ref,
            execution_profile=profile_ref,
            worker_composition=worker,
            planned_store_ledger=planned_store_ledger,
            operation_id=_COMPACTION_OPERATION_ID,
            expected_task_count=task_count,
            task_set_digest=task_set_digest,
            result_sink=sink,
            base_release=source_reference,
        )
        handoff_ref = self._controls.put(
            kind="execution-handoffs",
            artifact_id=handoff.handoff_id,
            value=handoff.to_dict(),
        )
        return profile_ref, handoff_ref

    def _common_partition_policy(
        self,
        layers: tuple[LayerRef, ...],
        expected_bucket_count: int,
    ) -> PartitionPolicy:
        if not layers:
            raise IntegrityError("compaction requires at least one active logical layer")
        policy = self._records.partition_policy(layers[0])
        if policy.bucket_count != expected_bucket_count:
            raise IntegrityError("compaction layer policy differs from its processing plan")
        for layer in layers[1:]:
            if self._records.partition_policy(layer) != policy:
                raise IntegrityError("compaction active layers use different partition policies")
        return policy


__all__ = [
    "BlobRetentionSetService",
    "ReleaseCompactionService",
    "logical_release_state_digest",
]
