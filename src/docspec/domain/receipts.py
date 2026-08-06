"""Small immutable receipts for delivery, run reconciliation, and commit intent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from docspec.domain.identity import freeze_json, require_sha256, require_text, stable_urn, thaw_json
from docspec.domain.jobs import StoreVerdict
from docspec.domain.references import (
    ArtifactRef,
    DocumentReleaseRef,
    LayerRef,
    SourceCatalogRef,
)


def _normalize_nonnegative_counts(value: dict[str, Any], label: str) -> dict[str, int]:
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in value.values()):
        raise ValueError(f"{label} values must be non-negative integers")
    return thaw_json(freeze_json(value, label=label))


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    receipt_id: str
    store_id: str
    store_revision: int
    sink_id: str
    profile_id: str
    delivered_entry_count: int
    delivered_entry_population_digest: str
    record_count: int
    byte_count: int
    idempotency_set_digest: str
    accepted_record_count: int
    rejected_record_count: int
    retried_record_count: int
    undelivered_record_count: int
    final_verdict: StoreVerdict
    layers: tuple[LayerRef, ...]
    blob_roots: tuple[ArtifactRef, ...]
    returned_result: ArtifactRef | None
    completed_at: str
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (
            ("receipt_id", self.receipt_id),
            ("store_id", self.store_id),
            ("sink_id", self.sink_id),
            ("profile_id", self.profile_id),
            ("completed_at", self.completed_at),
        ):
            require_text(value, label)
        counts = (
            self.store_revision,
            self.delivered_entry_count,
            self.record_count,
            self.byte_count,
            self.accepted_record_count,
            self.rejected_record_count,
            self.retried_record_count,
            self.undelivered_record_count,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("delivery receipt counts and revision must be non-negative integers")
        if self.delivered_entry_count == 0:
            raise ValueError("delivery receipt requires a non-empty delivered entry population")
        require_sha256(self.delivered_entry_population_digest, "delivered entry-population digest")
        require_sha256(self.idempotency_set_digest, "idempotency-set digest")
        try:
            verdict = StoreVerdict(self.final_verdict)
        except (TypeError, ValueError) as error:
            raise ValueError("delivery receipt final verdict is not registered") from error
        object.__setattr__(self, "final_verdict", verdict)
        if (
            self.accepted_record_count
            + self.rejected_record_count
            + self.undelivered_record_count
            != self.record_count
        ):
            raise ValueError("delivery receipt record outcomes do not reconcile")
        if self.retried_record_count > self.record_count:
            raise ValueError("delivery receipt retried-record count exceeds its record population")
        if verdict is not StoreVerdict.REJECTED and (
            self.accepted_record_count != self.record_count
            or self.rejected_record_count
            or self.undelivered_record_count
        ):
            raise ValueError("a non-rejected delivery receipt must accept every offered record")
        layer_ids = [item.layer_id for item in self.layers]
        if len(set(layer_ids)) != len(layer_ids):
            raise ValueError("delivery receipt layers must be distinct")
        root_ids = [item.artifact_id for item in self.blob_roots]
        if len(set(root_ids)) != len(root_ids):
            raise ValueError("delivery receipt blob roots must be distinct")
        if self.layers and sum(item.record_count for item in self.layers) != self.accepted_record_count:
            raise ValueError("delivery receipt durable layers differ from accepted-record count")
        if not self.layers and self.returned_result is None:
            raise ValueError("delivery receipt contains no durable layers or returned-result acknowledgement")
        if self.receipt_id != stable_urn("delivery-receipt", self.identity_content()):
            raise ValueError("delivery receipt identity differs")

    @classmethod
    def create(
        cls,
        *,
        store_id: str,
        store_revision: int,
        sink_id: str,
        profile_id: str,
        delivered_entry_count: int,
        delivered_entry_population_digest: str,
        record_count: int,
        byte_count: int,
        idempotency_set_digest: str,
        accepted_record_count: int,
        rejected_record_count: int,
        retried_record_count: int,
        undelivered_record_count: int,
        final_verdict: StoreVerdict,
        layers: tuple[LayerRef, ...],
        blob_roots: tuple[ArtifactRef, ...] = (),
        returned_result: ArtifactRef | None,
        completed_at: str,
        warnings: tuple[str, ...] = (),
    ) -> DeliveryReceipt:
        content = cls._content(
            store_id,
            store_revision,
            sink_id,
            profile_id,
            delivered_entry_count,
            delivered_entry_population_digest,
            record_count,
            byte_count,
            idempotency_set_digest,
            accepted_record_count,
            rejected_record_count,
            retried_record_count,
            undelivered_record_count,
            final_verdict,
            layers,
            blob_roots,
            returned_result,
        )
        return cls(
            stable_urn("delivery-receipt", content),
            store_id,
            store_revision,
            sink_id,
            profile_id,
            delivered_entry_count,
            delivered_entry_population_digest,
            record_count,
            byte_count,
            idempotency_set_digest,
            accepted_record_count,
            rejected_record_count,
            retried_record_count,
            undelivered_record_count,
            StoreVerdict(final_verdict),
            layers,
            blob_roots,
            returned_result,
            completed_at,
            warnings,
        )

    @staticmethod
    def _content(
        store_id: str,
        store_revision: int,
        sink_id: str,
        profile_id: str,
        delivered_entry_count: int,
        delivered_entry_population_digest: str,
        record_count: int,
        byte_count: int,
        idempotency_set_digest: str,
        accepted_record_count: int,
        rejected_record_count: int,
        retried_record_count: int,
        undelivered_record_count: int,
        final_verdict: StoreVerdict,
        layers: tuple[LayerRef, ...],
        blob_roots: tuple[ArtifactRef, ...],
        returned_result: ArtifactRef | None,
    ) -> dict[str, Any]:
        return {
            "storeId": store_id,
            "storeRevision": store_revision,
            "sinkId": sink_id,
            "profileId": profile_id,
            "deliveredEntryCount": delivered_entry_count,
            "deliveredEntryPopulationDigest": delivered_entry_population_digest,
            "recordCount": record_count,
            "byteCount": byte_count,
            "idempotencySetDigest": idempotency_set_digest,
            "acceptedRecordCount": accepted_record_count,
            "rejectedRecordCount": rejected_record_count,
            "retriedRecordCount": retried_record_count,
            "undeliveredRecordCount": undelivered_record_count,
            "finalVerdict": final_verdict.value,
            "layers": [item.to_dict() for item in layers],
            "blobRoots": [item.to_dict() for item in blob_roots],
            "returnedResult": None if returned_result is None else returned_result.to_dict(),
        }

    def identity_content(self) -> dict[str, Any]:
        return self._content(
            self.store_id,
            self.store_revision,
            self.sink_id,
            self.profile_id,
            self.delivered_entry_count,
            self.delivered_entry_population_digest,
            self.record_count,
            self.byte_count,
            self.idempotency_set_digest,
            self.accepted_record_count,
            self.rejected_record_count,
            self.retried_record_count,
            self.undelivered_record_count,
            self.final_verdict,
            self.layers,
            self.blob_roots,
            self.returned_result,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "docspec-delivery-receipt",
            "formatVersion": "1.1",
            "receiptId": self.receipt_id,
            **self.identity_content(),
            "completedAt": self.completed_at,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DeliveryReceipt:
        expected = {
            "format",
            "formatVersion",
            "receiptId",
            "storeId",
            "storeRevision",
            "sinkId",
            "profileId",
            "deliveredEntryCount",
            "deliveredEntryPopulationDigest",
            "recordCount",
            "byteCount",
            "idempotencySetDigest",
            "acceptedRecordCount",
            "rejectedRecordCount",
            "retriedRecordCount",
            "undeliveredRecordCount",
            "finalVerdict",
            "layers",
            "blobRoots",
            "returnedResult",
            "completedAt",
            "warnings",
        }
        if set(value) != expected or value["format"] != "docspec-delivery-receipt" or value["formatVersion"] != "1.1":
            raise ValueError("delivery receipt has an unknown format or invalid closed shape")
        return cls(
            receipt_id=value["receiptId"],
            store_id=value["storeId"],
            store_revision=value["storeRevision"],
            sink_id=value["sinkId"],
            profile_id=value["profileId"],
            delivered_entry_count=value["deliveredEntryCount"],
            delivered_entry_population_digest=value["deliveredEntryPopulationDigest"],
            record_count=value["recordCount"],
            byte_count=value["byteCount"],
            idempotency_set_digest=value["idempotencySetDigest"],
            accepted_record_count=value["acceptedRecordCount"],
            rejected_record_count=value["rejectedRecordCount"],
            retried_record_count=value["retriedRecordCount"],
            undelivered_record_count=value["undeliveredRecordCount"],
            final_verdict=StoreVerdict(value["finalVerdict"]),
            layers=tuple(LayerRef.from_dict(item) for item in value["layers"]),
            blob_roots=tuple(ArtifactRef.from_dict(item) for item in value["blobRoots"]),
            returned_result=None
            if value["returnedResult"] is None
            else ArtifactRef.from_dict(value["returnedResult"]),
            completed_at=value["completedAt"],
            warnings=tuple(value["warnings"]),
        )


@dataclass(frozen=True, slots=True)
class RunReceipt:
    run_id: str
    plan: ArtifactRef
    execution_profile: ArtifactRef
    execution_handoff: ArtifactRef
    source_catalog: SourceCatalogRef
    base_release: DocumentReleaseRef | None
    planned_store_ledger: LayerRef
    store_ledger: LayerRef
    store_count: int
    selection_ledger: LayerRef
    selected_item_count: int
    task_result_ledger: LayerRef
    store_receipt_set_digest: str
    staged_layers: tuple[LayerRef, ...]
    blob_roots: tuple[ArtifactRef, ...]
    counts: dict[str, int]
    failures: dict[str, Any]
    coverage: dict[str, Any]
    partition_policy: dict[str, Any]
    stateful: bool
    completed_at: str

    def __post_init__(self) -> None:
        require_text(self.run_id, "run_id")
        require_text(self.completed_at, "completed_at")
        require_sha256(self.store_receipt_set_digest, "store receipt-set digest")
        if self.planned_store_ledger.layer_kind != "planned-document-stores":
            raise ValueError("run planned-store ledger has an unexpected logical kind")
        if self.task_result_ledger.layer_kind != "execution-task-results":
            raise ValueError("run task-result ledger has an unexpected logical kind")
        if self.planned_store_ledger.record_count != self.store_count:
            raise ValueError("run terminal store count differs from its planned-store ledger")
        if self.store_count < 0 or self.store_ledger.record_count != self.store_count:
            raise ValueError("run store count differs from its ledger")
        if self.task_result_ledger.record_count != self.store_count:
            raise ValueError("run task-result count differs from its terminal store count")
        if self.selected_item_count < 0 or self.selection_ledger.record_count != self.selected_item_count:
            raise ValueError("run selected-item count differs from its ledger")
        layer_ids = [item.layer_id for item in self.staged_layers]
        if len(set(layer_ids)) != len(layer_ids):
            raise ValueError("staged layer identities must be distinct")
        object.__setattr__(self, "counts", _normalize_nonnegative_counts(self.counts, "run counts"))
        object.__setattr__(self, "failures", thaw_json(freeze_json(self.failures, label="run failures")))
        object.__setattr__(self, "coverage", thaw_json(freeze_json(self.coverage, label="run coverage")))
        object.__setattr__(
            self,
            "partition_policy",
            thaw_json(freeze_json(self.partition_policy, label="partition policy")),
        )
        if self.run_id != stable_urn("run-receipt", self.identity_content()):
            raise ValueError("run receipt identity differs")

    @classmethod
    def create(
        cls,
        *,
        plan: ArtifactRef,
        execution_profile: ArtifactRef,
        execution_handoff: ArtifactRef,
        source_catalog: SourceCatalogRef,
        base_release: DocumentReleaseRef | None,
        planned_store_ledger: LayerRef,
        store_ledger: LayerRef,
        store_count: int,
        selection_ledger: LayerRef,
        selected_item_count: int,
        task_result_ledger: LayerRef,
        store_receipt_set_digest: str,
        staged_layers: tuple[LayerRef, ...],
        blob_roots: tuple[ArtifactRef, ...],
        counts: dict[str, int],
        failures: dict[str, Any],
        coverage: dict[str, Any],
        partition_policy: dict[str, Any],
        stateful: bool,
        completed_at: str,
    ) -> RunReceipt:
        content = cls._content(
            plan,
            execution_profile,
            execution_handoff,
            source_catalog,
            base_release,
            planned_store_ledger,
            store_ledger,
            store_count,
            selection_ledger,
            selected_item_count,
            task_result_ledger,
            store_receipt_set_digest,
            staged_layers,
            blob_roots,
            counts,
            failures,
            coverage,
            partition_policy,
            stateful,
        )
        return cls(
            stable_urn("run-receipt", content),
            plan,
            execution_profile,
            execution_handoff,
            source_catalog,
            base_release,
            planned_store_ledger,
            store_ledger,
            store_count,
            selection_ledger,
            selected_item_count,
            task_result_ledger,
            store_receipt_set_digest,
            staged_layers,
            blob_roots,
            counts,
            failures,
            coverage,
            partition_policy,
            stateful,
            completed_at,
        )

    @staticmethod
    def _content(
        plan: ArtifactRef,
        execution_profile: ArtifactRef,
        execution_handoff: ArtifactRef,
        source_catalog: SourceCatalogRef,
        base_release: DocumentReleaseRef | None,
        planned_store_ledger: LayerRef,
        store_ledger: LayerRef,
        store_count: int,
        selection_ledger: LayerRef,
        selected_item_count: int,
        task_result_ledger: LayerRef,
        store_receipt_set_digest: str,
        staged_layers: tuple[LayerRef, ...],
        blob_roots: tuple[ArtifactRef, ...],
        counts: dict[str, int],
        failures: dict[str, Any],
        coverage: dict[str, Any],
        partition_policy: dict[str, Any],
        stateful: bool,
    ) -> dict[str, Any]:
        return {
            "plan": plan.to_dict(),
            "executionProfile": execution_profile.to_dict(),
            "executionHandoff": execution_handoff.to_dict(),
            "sourceCatalog": source_catalog.to_dict(),
            "baseRelease": None if base_release is None else base_release.to_dict(),
            "plannedStoreLedger": planned_store_ledger.to_dict(),
            "storeLedger": store_ledger.to_dict(),
            "storeCount": store_count,
            "selectionLedger": selection_ledger.to_dict(),
            "selectedItemCount": selected_item_count,
            "taskResultLedger": task_result_ledger.to_dict(),
            "storeReceiptSetDigest": store_receipt_set_digest,
            "stagedLayers": [item.to_dict() for item in staged_layers],
            "blobRoots": [item.to_dict() for item in blob_roots],
            "counts": counts,
            "failures": failures,
            "coverage": coverage,
            "partitionPolicy": partition_policy,
            "stateful": stateful,
        }

    def identity_content(self) -> dict[str, Any]:
        return self._content(
            self.plan,
            self.execution_profile,
            self.execution_handoff,
            self.source_catalog,
            self.base_release,
            self.planned_store_ledger,
            self.store_ledger,
            self.store_count,
            self.selection_ledger,
            self.selected_item_count,
            self.task_result_ledger,
            self.store_receipt_set_digest,
            self.staged_layers,
            self.blob_roots,
            self.counts,
            self.failures,
            self.coverage,
            self.partition_policy,
            self.stateful,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "docspec-run-receipt",
            "formatVersion": "1.2",
            "runId": self.run_id,
            **self.identity_content(),
            "completedAt": self.completed_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RunReceipt:
        expected = {
            "format",
            "formatVersion",
            "runId",
            "plan",
            "executionProfile",
            "executionHandoff",
            "sourceCatalog",
            "baseRelease",
            "plannedStoreLedger",
            "storeLedger",
            "storeCount",
            "selectionLedger",
            "selectedItemCount",
            "taskResultLedger",
            "storeReceiptSetDigest",
            "stagedLayers",
            "blobRoots",
            "counts",
            "failures",
            "coverage",
            "partitionPolicy",
            "stateful",
            "completedAt",
        }
        if set(value) != expected or value["format"] != "docspec-run-receipt" or value["formatVersion"] != "1.2":
            raise ValueError("run receipt has an unknown format or invalid closed shape")
        return cls(
            run_id=value["runId"],
            plan=ArtifactRef.from_dict(value["plan"]),
            execution_profile=ArtifactRef.from_dict(value["executionProfile"]),
            execution_handoff=ArtifactRef.from_dict(value["executionHandoff"]),
            source_catalog=SourceCatalogRef.from_dict(value["sourceCatalog"]),
            base_release=None if value["baseRelease"] is None else DocumentReleaseRef.from_dict(value["baseRelease"]),
            planned_store_ledger=LayerRef.from_dict(value["plannedStoreLedger"]),
            store_ledger=LayerRef.from_dict(value["storeLedger"]),
            store_count=value["storeCount"],
            selection_ledger=LayerRef.from_dict(value["selectionLedger"]),
            selected_item_count=value["selectedItemCount"],
            task_result_ledger=LayerRef.from_dict(value["taskResultLedger"]),
            store_receipt_set_digest=value["storeReceiptSetDigest"],
            staged_layers=tuple(LayerRef.from_dict(item) for item in value["stagedLayers"]),
            blob_roots=tuple(ArtifactRef.from_dict(item) for item in value["blobRoots"]),
            counts=value["counts"],
            failures=value["failures"],
            coverage=value["coverage"],
            partition_policy=value["partitionPolicy"],
            stateful=value["stateful"],
            completed_at=value["completedAt"],
        )


@dataclass(frozen=True, slots=True)
class CatalogCommitReceipt:
    receipt_id: str
    profile_id: str
    base_release: DocumentReleaseRef | None
    expected_head: DocumentReleaseRef | None
    run_receipt: ArtifactRef
    commit_token_digest: str
    prepared_at: str

    def __post_init__(self) -> None:
        require_text(self.receipt_id, "catalog commit receipt_id")
        require_text(self.profile_id, "catalog profile_id")
        require_text(self.prepared_at, "catalog commit prepared_at")
        require_sha256(self.commit_token_digest, "catalog commit token digest")
        if self.receipt_id != stable_urn("catalog-commit-receipt", self.identity_content()):
            raise ValueError("catalog commit receipt identity differs")

    @classmethod
    def create(
        cls,
        *,
        profile_id: str,
        base_release: DocumentReleaseRef | None,
        expected_head: DocumentReleaseRef | None,
        run_receipt: ArtifactRef,
        commit_token_digest: str,
        prepared_at: str,
    ) -> CatalogCommitReceipt:
        content = cls._content(profile_id, base_release, expected_head, run_receipt, commit_token_digest)
        return cls(
            stable_urn("catalog-commit-receipt", content),
            profile_id,
            base_release,
            expected_head,
            run_receipt,
            commit_token_digest,
            prepared_at,
        )

    @staticmethod
    def _content(
        profile_id: str,
        base_release: DocumentReleaseRef | None,
        expected_head: DocumentReleaseRef | None,
        run_receipt: ArtifactRef,
        commit_token_digest: str,
    ) -> dict[str, Any]:
        return {
            "profileId": profile_id,
            "baseRelease": None if base_release is None else base_release.to_dict(),
            "expectedHead": None if expected_head is None else expected_head.to_dict(),
            "runReceipt": run_receipt.to_dict(),
            "commitTokenDigest": commit_token_digest,
        }

    def identity_content(self) -> dict[str, Any]:
        return self._content(
            self.profile_id,
            self.base_release,
            self.expected_head,
            self.run_receipt,
            self.commit_token_digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "docspec-catalog-commit-receipt",
            "formatVersion": "1.0",
            "receiptId": self.receipt_id,
            **self.identity_content(),
            "preparedAt": self.prepared_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CatalogCommitReceipt:
        expected = {
            "format",
            "formatVersion",
            "receiptId",
            "profileId",
            "baseRelease",
            "expectedHead",
            "runReceipt",
            "commitTokenDigest",
            "preparedAt",
        }
        if (
            set(value) != expected
            or value["format"] != "docspec-catalog-commit-receipt"
            or value["formatVersion"] != "1.0"
        ):
            raise ValueError("catalog commit receipt has an unknown format or invalid closed shape")
        return cls(
            receipt_id=value["receiptId"],
            profile_id=value["profileId"],
            base_release=None if value["baseRelease"] is None else DocumentReleaseRef.from_dict(value["baseRelease"]),
            expected_head=None if value["expectedHead"] is None else DocumentReleaseRef.from_dict(value["expectedHead"]),
            run_receipt=ArtifactRef.from_dict(value["runReceipt"]),
            commit_token_digest=value["commitTokenDigest"],
            prepared_at=value["preparedAt"],
        )
