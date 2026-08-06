"""Seal processed stores only after complete, receipted result delivery."""

from __future__ import annotations

from collections.abc import Mapping

from docspec.domain.delivery import (
    DeliveryAccumulator,
    delivery_entry_population,
    delivery_store_verdict,
    iter_delivery_records,
    summarize_delivery_records,
)
from docspec.domain.jobs import DocumentStore, StoreState
from docspec.domain.receipts import DeliveryReceipt
from docspec.domain.references import ArtifactRef, StoreRef
from docspec.errors import IntegrityError, StateTransitionError
from docspec.ports.control_repository import ControlRepository
from docspec.ports.document_store_repository import DocumentStoreRepository
from docspec.ports.result_sink import ResultSink

from .store_state import load_latest_store


class StoreDeliveryService:
    def __init__(
        self,
        *,
        stores: DocumentStoreRepository,
        controls: ControlRepository,
        sinks: Mapping[str, ResultSink],
    ) -> None:
        self._stores = stores
        self._controls = controls
        self._sinks = sinks

    def deliver_store(self, processed_document_store_ref: StoreRef, sink_ref: ArtifactRef) -> StoreRef:
        self._controls.verify(sink_ref)
        sink_configuration = self._controls.load(sink_ref)
        if set(sink_configuration) != {"sinkId", "profileId"}:
            raise IntegrityError("result sink reference has an invalid closed configuration")
        current_ref, store = load_latest_store(self._stores, processed_document_store_ref)
        sink = self._sinks.get(sink_ref.artifact_id)
        if sink is None:
            raise IntegrityError(f"unknown result sink {sink_ref.artifact_id}")
        if (
            sink_configuration["sinkId"] != sink.sink_id
            or sink_configuration["profileId"] != sink.profile_id
            or sink_ref.artifact_id != sink.sink_id
        ):
            raise IntegrityError("result sink reference differs from the injected sink")
        if store.state == StoreState.SEALED:
            if store.delivery_receipt is None:
                raise IntegrityError("sealed document store has no delivery receipt")
            existing = DeliveryReceipt.from_dict(self._controls.load(store.delivery_receipt))
            if existing.sink_id != sink.sink_id or existing.profile_id != sink.profile_id:
                raise IntegrityError("sealed document store was delivered through a different sink")
            self._verify_receipt(store, existing)
            return current_ref
        if store.state != StoreState.RUNNING:
            raise StateTransitionError("delivery requires a processed running document store")
        if any(not entry.terminal for entry in store.entries):
            raise StateTransitionError("delivery requires every document entry to be terminal")
        expected = summarize_delivery_records(iter_delivery_records(store))
        receipt = sink.deliver(store, iter_delivery_records(store))
        self._verify_receipt(store, receipt, expected=expected)
        receipt_ref = self._controls.put(
            kind="delivery-receipts",
            artifact_id=receipt.receipt_id,
            value=receipt.to_dict(),
        )
        verdict = delivery_store_verdict(store)
        sealed = store.seal(verdict, receipt_ref)
        return self._stores.save(sealed)

    @staticmethod
    def _verify_receipt(
        store: DocumentStore,
        receipt: DeliveryReceipt,
        *,
        expected: DeliveryAccumulator | None = None,
    ) -> None:
        if expected is None:
            expected = summarize_delivery_records(iter_delivery_records(store))
        entry_count, population_digest = delivery_entry_population(store)
        expected_revision = store.revision - 1 if store.state == StoreState.SEALED else store.revision
        if receipt.store_id != store.store_id or receipt.store_revision != expected_revision:
            raise IntegrityError("delivery receipt names a different document store or revision")
        if (
            receipt.delivered_entry_count != entry_count
            or receipt.delivered_entry_population_digest != population_digest
        ):
            raise IntegrityError("delivery receipt differs from the ordered document-entry population")
        if (
            receipt.record_count != expected.record_count
            or receipt.byte_count != expected.byte_count
            or receipt.idempotency_set_digest != expected.digest
            or receipt.accepted_record_count != expected.record_count
            or receipt.rejected_record_count != 0
            or receipt.undelivered_record_count != 0
            or receipt.final_verdict != delivery_store_verdict(store)
        ):
            raise IntegrityError("delivery receipt differs from the complete expected record stream")
        if store.state == StoreState.SEALED and store.verdict != receipt.final_verdict:
            raise IntegrityError("sealed document store verdict differs from its delivery receipt")
        if not receipt.layers and receipt.returned_result is None:
            raise IntegrityError("delivery receipt contains neither durable layers nor returned-result acknowledgement")
