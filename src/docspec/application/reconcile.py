"""Streaming run reconciliation into complete partition-reusing logical layers."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

from docspec.domain.delivery import (
    core_delivery_schemas,
    delivery_entry_population,
    delivery_store_verdict,
    iter_delivery_records,
    record_schema,
    summarize_delivery_records,
)
from docspec.domain.execution import ExecutionHandoff, ExecutionProfile, StoreTask, StoreTaskResult, StoreTaskStatus
from docspec.domain.identity import OrderedJsonSequenceDigester, ordered_json_sequence_digest
from docspec.domain.jobs import DocumentStore, StoreState, StoreVerdict
from docspec.domain.plans import ProcessingPlan
from docspec.domain.profiles import ProfileRole
from docspec.domain.receipts import DeliveryReceipt, RunReceipt
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, LayerRef, SourceCatalogRef, StoreRef
from docspec.domain.storage import PartitionPolicy, RecordSchema, partition_bucket
from docspec.errors import IntegrityError
from docspec.ports.control_repository import ControlRepository
from docspec.ports.document_catalog import DocumentCatalog
from docspec.ports.document_store_repository import DocumentStoreRepository
from docspec.ports.record_storage import RecordStorage
from docspec.ports.reconciliation_workspace import ReconciliationWorkspace, ReconciliationWorkspaceFactory
from docspec.ports.source_catalog import SourceCatalog

_STORE_LEDGER_SCHEMA = RecordSchema(
    "docspec-run-store-reference/1.0",
    ("recordId", "sourceItemId", "store"),
    "recordId",
    "sourceItemId",
)
_SELECTION_LEDGER_SCHEMA = RecordSchema(
    "docspec-run-selection/1.0",
    ("recordId", "sourceItemId", "storeId", "entryId", "change", "disposition"),
    "recordId",
    "sourceItemId",
)
_TASK_RESULT_LEDGER_SCHEMA = RecordSchema(
    "docspec-store-task-result-record/1.0",
    ("recordId", "sourceItemId", "result"),
    "recordId",
    "sourceItemId",
)
_STORE_LEDGER_COLLECTION = "ledger:run-store-receipts"
_SELECTION_LEDGER_COLLECTION = "ledger:run-selection"
_TERMINAL_STORE_RESULTS_COLLECTION = "ledger:terminal-store-results"


def _layer_collection(layer_kind: str) -> str:
    return f"layer:{layer_kind}"


class RunReconciler:
    """Verify sealed jobs and produce one small receipt plus bounded ledgers."""

    def __init__(
        self,
        *,
        plan_ref: ArtifactRef,
        execution_profile_ref: ArtifactRef,
        execution_handoff_ref: ArtifactRef,
        source_catalog_ref: SourceCatalogRef,
        base_release_ref: DocumentReleaseRef | None,
        controls: ControlRepository,
        stores: DocumentStoreRepository,
        records: RecordStorage,
        document_catalog: DocumentCatalog,
        source_catalog: SourceCatalog,
        workspace_factory: ReconciliationWorkspaceFactory,
        partition_policy: PartitionPolicy,
        clock: Callable[[], str],
        stateful: bool = True,
    ) -> None:
        self._plan_ref = plan_ref
        self._execution_profile_ref = execution_profile_ref
        self._execution_handoff_ref = execution_handoff_ref
        self._source_catalog_ref = source_catalog_ref
        self._base_release_ref = base_release_ref
        self._controls = controls
        self._stores = stores
        self._records = records
        self._document_catalog = document_catalog
        self._source_catalog = source_catalog
        self._workspace_factory = workspace_factory
        self._partition_policy = partition_policy
        self._clock = clock
        self._stateful = stateful

    def reconcile_run(self, task_results: Iterable[StoreTaskResult]) -> ArtifactRef:
        with self._workspace_factory.create() as workspace:
            return self._reconcile_run(task_results, workspace)

    def _reconcile_run(
        self,
        task_results: Iterable[StoreTaskResult],
        workspace: ReconciliationWorkspace,
    ) -> ArtifactRef:
        self._controls.verify(self._plan_ref)
        plan = ProcessingPlan.from_dict(self._controls.load(self._plan_ref))
        if plan.source_catalog != self._source_catalog_ref or plan.base_release != self._base_release_ref:
            raise IntegrityError("reconciler inputs differ from the sealed processing plan")
        if self._partition_policy.bucket_count != plan.partition_count:
            raise IntegrityError("record partition policy differs from the processing plan")
        summary = self._source_catalog.verify(self._source_catalog_ref)
        planned_store_ledger = self._stores.planned_store_ledger(plan.plan_id)
        handoff = self._verified_execution_handoff(plan, planned_store_ledger)
        active = self._base_layers()
        fragment_schemas: dict[str, RecordSchema] = {}
        touched_partitions: set[int] = set()
        counts = {
            "stores": 0,
            "selectedItems": 0,
            "capturedFiles": 0,
            "representations": 0,
            "segments": 0,
            "derivedRecords": 0,
            "deliveredRecords": 0,
            "deliveredBytes": 0,
            "acceptedFailureStores": 0,
            "rejectedStores": 0,
        }
        failure_counts: dict[str, int] = {}
        first_failure: dict[str, Any] | None = None
        blob_roots: dict[str, ArtifactRef] = {}

        for reference, store in self._verified_complete_stores(
            task_results,
            planned_store_ledger,
            plan,
            handoff,
            workspace,
        ):
            workspace.add_record(
                _STORE_LEDGER_COLLECTION,
                identity=store.store_id,
                source_item_id=store.store_id,
                record={
                    "recordId": store.store_id,
                    "sourceItemId": store.store_id,
                    "store": reference.to_dict(),
                },
            )
            for entry in store.entries:
                source_item_id = entry.source_item.item_id
                workspace.mark_affected(source_item_id)
                touched_partitions.add(
                    partition_bucket(source_item_id, self._partition_policy.bucket_count)
                )
                workspace.add_record(
                    _SELECTION_LEDGER_COLLECTION,
                    identity=source_item_id,
                    source_item_id=source_item_id,
                    record={
                        "recordId": entry.source_item.item_id,
                        "sourceItemId": entry.source_item.item_id,
                        "storeId": store.store_id,
                        "entryId": entry.entry_id,
                        "change": entry.change.value,
                        "disposition": entry.disposition.value if entry.disposition is not None else None,
                    },
                )
                counts["selectedItems"] += 1
                counts["capturedFiles"] += len(entry.captured_files)
                counts["representations"] += len(entry.representations)
                counts["segments"] += len(entry.segments)
                counts["derivedRecords"] += len(entry.derived_records)
                for failure in entry.failures:
                    name = failure.failure_class.value
                    failure_counts[name] = failure_counts.get(name, 0) + 1
                    if first_failure is None:
                        first_failure = failure.to_dict()
            counts["stores"] += 1
            if store.verdict == StoreVerdict.ACCEPTED_FAILURE:
                counts["acceptedFailureStores"] += 1
            elif store.verdict == StoreVerdict.REJECTED:
                counts["rejectedStores"] += 1
            receipt = self._delivery_receipt(store, plan)
            counts["deliveredRecords"] += receipt.record_count
            counts["deliveredBytes"] += receipt.byte_count
            for root in receipt.blob_roots:
                existing = blob_roots.get(root.artifact_id)
                if existing is not None and existing != root:
                    raise IntegrityError("delivery receipts disagree on a blob root")
                blob_roots[root.artifact_id] = root
            if self._stateful:
                self._spool_store_layers(
                    workspace,
                    active,
                    fragment_schemas,
                    receipt,
                    store,
                    plan,
                )

        if self._stateful:
            active = self._assemble_layers(
                workspace,
                active,
                fragment_schemas,
                frozenset(touched_partitions),
                plan,
            )
        store_ledger = self._records.write_layer(
            workspace.stream_records(_STORE_LEDGER_COLLECTION),
            layer_kind="run-store-receipts",
            schema=_STORE_LEDGER_SCHEMA,
            partition_policy=self._partition_policy,
        )
        selection_ledger = self._records.write_layer(
            workspace.stream_records(_SELECTION_LEDGER_COLLECTION),
            layer_kind="run-selection",
            schema=_SELECTION_LEDGER_SCHEMA,
            partition_policy=self._partition_policy,
        )
        task_result_ledger = self._records.write_layer(
            workspace.stream_records(_TERMINAL_STORE_RESULTS_COLLECTION),
            layer_kind="execution-task-results",
            schema=_TASK_RESULT_LEDGER_SCHEMA,
            partition_policy=self._partition_policy,
        )
        self._records.verify(store_ledger)
        self._records.verify(selection_ledger)
        self._records.verify(task_result_ledger)
        store_digest = ordered_json_sequence_digest(
            row["store"] for row in self._records.stream(store_ledger)
        )
        failures = {
            "counts": dict(sorted(failure_counts.items())),
            "first": first_failure,
        }
        coverage = {
            "catalogItems": summary.item_count,
            "catalogStateCounts": dict(summary.state_counts),
            "selectedItems": counts["selectedItems"],
            "sourceCatalog": dict(summary.coverage),
        }
        receipt = RunReceipt.create(
            plan=self._plan_ref,
            execution_profile=self._execution_profile_ref,
            execution_handoff=self._execution_handoff_ref,
            source_catalog=self._source_catalog_ref,
            base_release=self._base_release_ref,
            planned_store_ledger=planned_store_ledger,
            store_ledger=store_ledger,
            store_count=store_ledger.record_count,
            selection_ledger=selection_ledger,
            selected_item_count=selection_ledger.record_count,
            task_result_ledger=task_result_ledger,
            store_receipt_set_digest=store_digest,
            staged_layers=tuple(active[kind] for kind in sorted(active)) if self._stateful else (),
            blob_roots=tuple(blob_roots[key] for key in sorted(blob_roots)),
            counts=counts,
            failures=failures,
            coverage=coverage,
            partition_policy={
                "policyId": self._partition_policy.policy_id,
                "bucketCount": self._partition_policy.bucket_count,
            },
            stateful=self._stateful,
            completed_at=self._clock(),
        )
        return self._controls.put(kind="run-receipts", artifact_id=receipt.run_id, value=receipt.to_dict())

    def _base_layers(self) -> dict[str, LayerRef]:
        if self._base_release_ref is None:
            return {}
        release = self._document_catalog.open(self._base_release_ref)
        return {layer.layer_kind: layer for layer in release.active_layers}

    def _verified_complete_stores(
        self,
        task_results: Iterable[StoreTaskResult],
        planned_ledger: LayerRef,
        plan: ProcessingPlan,
        handoff: ExecutionHandoff,
        workspace: ReconciliationWorkspace,
    ) -> Iterator[tuple[StoreRef, DocumentStore]]:
        """Boundedly reorder terminal references and collapse identical replay."""

        terminal_count = 0
        for result in task_results:
            if not isinstance(result, StoreTaskResult):
                raise IntegrityError("terminal task stream contains a non-StoreTaskResult value")
            if result.handoff_id != handoff.handoff_id:
                raise IntegrityError("terminal task result names a different execution handoff")
            if result.status is not StoreTaskStatus.SUCCEEDED or result.output_store is None:
                raise IntegrityError("terminal task result did not produce a sealed store reference")
            store_id = result.task.input_store.store_id
            row = {
                "recordId": store_id,
                "sourceItemId": store_id,
                "result": result.to_dict(),
            }
            existing = workspace.lookup_record(_TERMINAL_STORE_RESULTS_COLLECTION, store_id)
            if existing is not None:
                if existing != row:
                    raise IntegrityError(
                        f"terminal task stream conflicts for repeated store {store_id!r}"
                    )
                continue
            workspace.add_record(
                _TERMINAL_STORE_RESULTS_COLLECTION,
                identity=store_id,
                source_item_id=store_id,
                record=row,
            )
            terminal_count += 1

        planned_count = 0
        task_digest = OrderedJsonSequenceDigester()
        for ordinal, planned_reference in enumerate(self._stores.stream_planned_stores(planned_ledger)):
            planned_count += 1
            row = workspace.lookup_record(_TERMINAL_STORE_RESULTS_COLLECTION, planned_reference.store_id)
            if row is None:
                raise IntegrityError(f"terminal store stream is missing planned store at ordinal {ordinal}")
            if set(row) != {"recordId", "sourceItemId", "result"} or not isinstance(row["result"], dict):
                raise IntegrityError("terminal store result has an invalid closed shape")
            try:
                result = StoreTaskResult.from_dict(row["result"])
            except (TypeError, ValueError) as error:
                raise IntegrityError(f"terminal task result is invalid: {error}") from error
            expected_task = StoreTask(plan.plan_id, handoff.operation_id, planned_reference)
            task_digest.accept(expected_task.to_dict())
            if result.task != expected_task or result.handoff_id != handoff.handoff_id:
                raise IntegrityError("terminal task result differs from its sealed planned task")
            assert result.output_store is not None
            sealed_reference = result.output_store
            yield sealed_reference, self._verified_store(sealed_reference, plan)
        if terminal_count != planned_count:
            raise IntegrityError("terminal store stream contains an unknown or extra store")
        if planned_count != handoff.expected_task_count or task_digest.finish() != handoff.task_set_digest:
            raise IntegrityError("sealed execution handoff differs from its reconstructed task population")

    def _verified_execution_handoff(
        self,
        plan: ProcessingPlan,
        planned_store_ledger: LayerRef,
    ) -> ExecutionHandoff:
        try:
            profile = ExecutionProfile.from_dict(self._controls.load(self._execution_profile_ref))
            handoff = ExecutionHandoff.from_dict(self._controls.load(self._execution_handoff_ref))
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"run execution controls are invalid: {error}") from error
        if profile.profile_id != self._execution_profile_ref.artifact_id:
            raise IntegrityError("execution profile identity differs from its artifact reference")
        for reference in profile.control_artifacts:
            self._controls.verify(reference)
        if handoff.handoff_id != self._execution_handoff_ref.artifact_id:
            raise IntegrityError("execution handoff identity differs from its artifact reference")
        if (
            handoff.processing_plan != self._plan_ref
            or handoff.execution_profile != self._execution_profile_ref
            or handoff.planned_store_ledger != planned_store_ledger
            or handoff.base_release != self._base_release_ref
        ):
            raise IntegrityError("execution handoff differs from the reconciler's sealed inputs")
        if handoff.worker_composition != profile.worker_composition:
            raise IntegrityError("execution handoff differs from its resolved worker composition")
        if handoff.expected_task_count != planned_store_ledger.record_count:
            raise IntegrityError("execution handoff task count differs from its planned-store ledger")
        return handoff

    def _verified_store(self, reference: StoreRef, plan: ProcessingPlan) -> DocumentStore:
        store = self._stores.load(reference)
        if store.state != StoreState.SEALED or store.plan_id != plan.plan_id:
            raise IntegrityError("run contains an unsealed store or a store from another plan")
        if store.delivery_receipt is None:
            raise IntegrityError("sealed store has no delivery receipt")
        return store

    def _delivery_receipt(self, store: DocumentStore, plan: ProcessingPlan) -> DeliveryReceipt:
        assert store.delivery_receipt is not None
        self._controls.verify(store.delivery_receipt)
        try:
            receipt = DeliveryReceipt.from_dict(self._controls.load(store.delivery_receipt))
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"store delivery receipt is invalid: {error}") from error
        if receipt.store_id != store.store_id or receipt.store_revision != store.revision - 1:
            raise IntegrityError("delivery receipt names a different store or processed revision")
        entry_count, population_digest = delivery_entry_population(store)
        expected = summarize_delivery_records(iter_delivery_records(store))
        if (
            receipt.delivered_entry_count != entry_count
            or receipt.delivered_entry_population_digest != population_digest
            or receipt.record_count != expected.record_count
            or receipt.byte_count != expected.byte_count
            or receipt.idempotency_set_digest != expected.digest
            or receipt.accepted_record_count != expected.record_count
            or receipt.rejected_record_count != 0
            or receipt.undelivered_record_count != 0
            or receipt.final_verdict != delivery_store_verdict(store)
            or receipt.final_verdict != store.verdict
        ):
            raise IntegrityError("delivery receipt differs from its sealed store population or result")
        expected_profile = plan.profiles.for_role(ProfileRole.RESULT_DELIVERY).profile_id
        if receipt.profile_id != expected_profile:
            raise IntegrityError("delivery receipt differs from the plan result-delivery profile")
        layer_kinds = [layer.layer_kind for layer in receipt.layers]
        if len(set(layer_kinds)) != len(layer_kinds):
            raise IntegrityError("delivery receipt repeats a logical layer kind")
        for layer in receipt.layers:
            self._records.verify(layer)
        if self._stateful and not receipt.layers:
            raise IntegrityError("a stateful run requires durable delivery layers")
        return receipt

    def _spool_store_layers(
        self,
        workspace: ReconciliationWorkspace,
        active: dict[str, LayerRef],
        fragment_schemas: dict[str, RecordSchema],
        receipt: DeliveryReceipt,
        store: DocumentStore,
        plan: ProcessingPlan,
    ) -> None:
        fragments = {layer.layer_kind: layer for layer in receipt.layers}
        affected = {entry.source_item.item_id for entry in store.entries}
        core_schemas = core_delivery_schemas()
        required = set(core_schemas)
        scheduled_derived = {f"derived:{identifier}" for identifier in plan.stages.processor_ids}
        allowed = required | scheduled_derived
        unexpected = set(fragments) - allowed
        if unexpected:
            raise IntegrityError(f"delivery contains unplanned layer kinds: {sorted(unexpected)}")
        for kind, fragment in fragments.items():
            schema = record_schema(kind, fragment.schema_id)
            current = active.get(kind)
            if current is not None and current.schema_id != schema.schema_id:
                raise IntegrityError(f"layer {kind!r} changed schema without a new logical layer identity")
            prior = fragment_schemas.get(kind)
            if prior is not None and prior != schema:
                raise IntegrityError(f"delivery fragments disagree on the schema for layer {kind!r}")
            fragment_schemas[kind] = schema
            collection = _layer_collection(kind)
            for row in self._records.stream(fragment):
                source_item_id = row["sourceItemId"]
                if source_item_id not in affected:
                    raise IntegrityError("delivery layer contains records outside its document store")
                workspace.add_record(
                    collection,
                    identity=row["recordId"],
                    source_item_id=source_item_id,
                    record=row,
                )

    def _assemble_layers(
        self,
        workspace: ReconciliationWorkspace,
        active: dict[str, LayerRef],
        fragment_schemas: Mapping[str, RecordSchema],
        touched: frozenset[int],
        plan: ProcessingPlan,
    ) -> dict[str, LayerRef]:
        """Stage every replacement before publishing each logical layer once."""

        core_schemas = core_delivery_schemas()
        required = set(core_schemas)
        scheduled_derived = {f"derived:{identifier}" for identifier in plan.stages.processor_ids}
        retired_derived = self._retired_derived_layers(plan, active)
        kinds = required | retired_derived | (scheduled_derived & (set(fragment_schemas) | set(active)))
        staged: list[tuple[str, RecordSchema, LayerRef | None]] = []
        for kind in sorted(kinds):
            current = active.get(kind)
            schema = fragment_schemas.get(kind)
            if schema is None:
                if current is not None:
                    schema = record_schema(kind, current.schema_id)
                else:
                    schema = core_schemas[kind]
            if current is not None and current.schema_id != schema.schema_id:
                raise IntegrityError(f"layer {kind!r} changed schema without a new logical layer identity")
            if current is not None and not touched:
                continue
            if current is not None:
                workspace.retain_records(
                    _layer_collection(kind),
                    self._records.stream(current, partitions=touched),
                    identity_field=schema.identity_field,
                    source_item_field=schema.partition_field,
                )
            staged.append((kind, schema, current))

        result = dict(active)
        for kind, schema, current in staged:
            records = workspace.stream_records(_layer_collection(kind))
            if current is None:
                result[kind] = self._records.write_layer(
                    records,
                    layer_kind=kind,
                    schema=schema,
                    partition_policy=self._partition_policy,
                )
            else:
                replacement = self._records.write_layer(
                    records,
                    layer_kind=kind,
                    schema=schema,
                    partition_policy=self._partition_policy,
                    base=current,
                    replace_partitions=touched,
                )
                if kind in retired_derived and replacement.record_count == 0:
                    result.pop(kind, None)
                else:
                    result[kind] = replacement
        return result

    def _retired_derived_layers(
        self,
        plan: ProcessingPlan,
        active: Mapping[str, LayerRef],
    ) -> set[str]:
        """Name prior processor layers whose pinned descriptions are no longer current."""

        if self._base_release_ref is None:
            return set()
        base = self._document_catalog.open(self._base_release_ref)
        self._controls.verify(base.processing_plan)
        try:
            previous = ProcessingPlan.from_dict(self._controls.load(base.processing_plan))
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"base release processing plan is invalid: {error}") from error
        current = {item.processor_id for item in plan.processors.processors}
        return {
            layer_kind
            for item in previous.processors.processors
            if item.processor_id not in current
            and (layer_kind := f"derived:{item.processor_id}") in active
        }
