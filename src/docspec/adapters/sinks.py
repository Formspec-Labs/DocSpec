"""Portable durable, returned-result, and hybrid result sinks."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Callable, Protocol

from docspec.domain.delivery import (
    DeliveryAccumulator,
    DeliveryRecord,
    core_delivery_schemas,
    delivery_entry_population,
    delivery_store_verdict,
    iter_delivery_records,
)
from docspec.domain.jobs import DocumentStore
from docspec.domain.receipts import DeliveryReceipt
from docspec.domain.references import ArtifactRef, LayerRef
from docspec.domain.storage import RecordSchema
from docspec.errors import IntegrityError
from docspec.ports.record_storage import PartitionPolicy, RecordStorage


def _successful_receipt(
    *,
    store: DocumentStore,
    sink_id: str,
    profile_id: str,
    summary: DeliveryAccumulator,
    layers: tuple[LayerRef, ...],
    blob_roots: tuple[ArtifactRef, ...],
    returned_result: ArtifactRef | None,
    completed_at: str,
) -> DeliveryReceipt:
    """Build the one complete receipt shape shared by every built-in sink."""

    entry_count, population_digest = delivery_entry_population(store)
    return DeliveryReceipt.create(
        store_id=store.store_id,
        store_revision=store.revision,
        sink_id=sink_id,
        profile_id=profile_id,
        delivered_entry_count=entry_count,
        delivered_entry_population_digest=population_digest,
        record_count=summary.record_count,
        byte_count=summary.byte_count,
        idempotency_set_digest=summary.digest,
        accepted_record_count=summary.record_count,
        rejected_record_count=0,
        retried_record_count=0,
        undelivered_record_count=0,
        final_verdict=delivery_store_verdict(store),
        layers=layers,
        blob_roots=blob_roots,
        returned_result=returned_result,
        completed_at=completed_at,
    )


class ResultReceiver(Protocol):
    """A synchronous acknowledgement boundary that naturally applies backpressure."""

    def accept(self, idempotency_key: str, record: Mapping[str, Any]) -> None: ...

    def finish(self, *, record_count: int, byte_count: int, digest: str) -> ArtifactRef | None: ...


def _return_records(
    receiver: ResultReceiver,
    records: Iterable[DeliveryRecord],
) -> tuple[DeliveryAccumulator, ArtifactRef | None]:
    """Send one record at a time; a successful return acknowledges each key."""

    summary = DeliveryAccumulator()
    for item in records:
        record = summary.add(item)
        receiver.accept(item.idempotency_key, record)
    returned = receiver.finish(
        record_count=summary.record_count,
        byte_count=summary.byte_count,
        digest=summary.digest,
    )
    return summary, returned


def _collect_layers(
    records: Iterable[DeliveryRecord],
) -> tuple[DeliveryAccumulator, dict[str, RecordSchema], dict[str, list[DeliveryRecord]]]:
    """Collect one bounded store stream into its distinct logical layers."""

    summary = DeliveryAccumulator()
    schemas = core_delivery_schemas()
    grouped: dict[str, list[DeliveryRecord]] = {kind: [] for kind in schemas}
    for item in records:
        summary.add(item)
        existing = schemas.get(item.layer_kind)
        if existing is not None and existing != item.schema:
            raise IntegrityError(f"delivery layer {item.layer_kind!r} uses more than one schema")
        schemas[item.layer_kind] = item.schema
        grouped.setdefault(item.layer_kind, []).append(item)
    return summary, schemas, grouped


class ReturnedResultSink:
    def __init__(
        self,
        *,
        sink_id: str,
        profile_id: str,
        receiver: ResultReceiver,
        clock: Callable[[], str],
    ) -> None:
        self._sink_id = sink_id
        self._profile_id = profile_id
        self._receiver = receiver
        self._clock = clock

    @property
    def sink_id(self) -> str:
        return self._sink_id

    @property
    def profile_id(self) -> str:
        return self._profile_id

    def deliver(self, store: DocumentStore, records: Iterable[DeliveryRecord]) -> DeliveryReceipt:
        summary, returned = _return_records(self._receiver, records)
        return _successful_receipt(
            store=store,
            sink_id=self.sink_id,
            profile_id=self._profile_id,
            summary=summary,
            layers=(),
            blob_roots=(),
            returned_result=returned,
            completed_at=self._clock(),
        )


class DurableDatasetSink:
    def __init__(
        self,
        *,
        sink_id: str,
        profile_id: str,
        storage: RecordStorage,
        partition_policy: PartitionPolicy,
        blob_roots: tuple[ArtifactRef, ...],
        clock: Callable[[], str],
    ) -> None:
        self._sink_id = sink_id
        self._profile_id = profile_id
        self._storage = storage
        self._partition_policy = partition_policy
        self._blob_roots = blob_roots
        self._clock = clock

    @property
    def sink_id(self) -> str:
        return self._sink_id

    @property
    def profile_id(self) -> str:
        return self._profile_id

    def deliver(self, store: DocumentStore, records: Iterable[DeliveryRecord]) -> DeliveryReceipt:
        """Stage one bounded store as independent immutable layer fragments."""

        summary, layer_schemas, grouped = _collect_layers(records)
        layers: list[LayerRef] = []
        for layer_kind in sorted(layer_schemas):
            schema = layer_schemas[layer_kind]
            layer_records = sorted(grouped[layer_kind], key=lambda item: item.record_id)
            layers.append(
                self._storage.write_layer(
                    (item.to_record() for item in layer_records),
                    layer_kind=layer_kind,
                    schema=schema,
                    partition_policy=self._partition_policy,
                )
            )
        return _successful_receipt(
            store=store,
            sink_id=self.sink_id,
            profile_id=self._profile_id,
            summary=summary,
            layers=tuple(layers),
            blob_roots=self._blob_roots,
            returned_result=None,
            completed_at=self._clock(),
        )


class HybridResultSink:
    def __init__(
        self,
        *,
        sink_id: str,
        profile_id: str,
        durable: DurableDatasetSink,
        receiver: ResultReceiver,
        clock: Callable[[], str],
    ) -> None:
        self._sink_id = sink_id
        self._profile_id = profile_id
        self._durable = durable
        self._receiver = receiver
        self._clock = clock

    @property
    def sink_id(self) -> str:
        return self._sink_id

    @property
    def profile_id(self) -> str:
        return self._profile_id

    def deliver(self, store: DocumentStore, records: Iterable[DeliveryRecord]) -> DeliveryReceipt:
        durable = self._durable.deliver(store, iter_delivery_records(store))
        summary, returned = _return_records(self._receiver, records)
        if (
            summary.record_count != durable.record_count
            or summary.byte_count != durable.byte_count
            or summary.digest != durable.idempotency_set_digest
        ):
            raise IntegrityError("hybrid durable and returned streams describe different records")
        return _successful_receipt(
            store=store,
            sink_id=self.sink_id,
            profile_id=self._profile_id,
            summary=summary,
            layers=durable.layers,
            blob_roots=durable.blob_roots,
            returned_result=returned,
            completed_at=self._clock(),
        )


class CallbackReceiver:
    """Adapt two callables to the returned-result acknowledgement boundary."""

    def __init__(
        self,
        accept: Callable[[str, Mapping[str, Any]], None],
        finish: Callable[[int, int, str], ArtifactRef | None] | None = None,
    ) -> None:
        self._accept = accept
        self._finish = finish

    def accept(self, idempotency_key: str, record: Mapping[str, Any]) -> None:
        self._accept(idempotency_key, record)

    def finish(self, *, record_count: int, byte_count: int, digest: str) -> ArtifactRef | None:
        if self._finish is None:
            return None
        return self._finish(record_count, byte_count, digest)
