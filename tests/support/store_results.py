"""Shared store results fixtures, extracted from tests.conformance.test_document_store."""

from __future__ import annotations

from docspec.domain.jobs import StoreState, StoreVerdict
from docspec.domain.receipts import DeliveryReceipt, RunReceipt
from docspec.domain.references import StoreRef
from tests.support import processors as _processor_helpers

_CountingProcessor = _processor_helpers._CountingProcessor


class _FailingProcessor(_CountingProcessor):
    """The shared counting processor, raising one declared failure for one item."""

    def __init__(self, description, *, fail_item_id: str, error: Exception) -> None:
        super().__init__(description)
        self._fail_item_id = fail_item_id
        self._error = error

    def process(self, request, payload, prerequisite_results):
        if request.source_item_id == self._fail_item_id:
            raise self._error
        return super().process(request, payload, prerequisite_results)


def _reconciled_counts(platform, run: RunReceipt) -> dict[str, int]:
    """Recount every sealed store receipt independently of the reconciler."""

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
    for row in platform.records.stream(run.store_ledger):
        store = platform.stores.load(StoreRef.from_dict(row["store"]))
        assert store.state is StoreState.SEALED
        assert store.verdict is not None
        counts["stores"] += 1
        counts["acceptedFailureStores"] += store.verdict is StoreVerdict.ACCEPTED_FAILURE
        counts["rejectedStores"] += store.verdict is StoreVerdict.REJECTED
        assert store.delivery_receipt is not None
        delivery = DeliveryReceipt.from_dict(platform.controls.load(store.delivery_receipt))
        counts["deliveredRecords"] += delivery.record_count
        counts["deliveredBytes"] += delivery.byte_count
        for entry in store.entries:
            assert entry.disposition is not None, "a sealed store may hold no undecided entry"
            counts["selectedItems"] += 1
            counts["capturedFiles"] += len(entry.captured_files)
            counts["representations"] += len(entry.representations)
            counts["segments"] += len(entry.segments)
            counts["derivedRecords"] += len(entry.derived_records)
    return counts
