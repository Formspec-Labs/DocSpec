from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters.sinks import DurableDatasetSink, HybridResultSink, ReturnedResultSink
from docspec.adapters.source_catalog import LocalFileContentFetcher
from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.application.commit import ReleaseCommitService
from docspec.application.delivery import StoreDeliveryService
from docspec.application.execution import StoreExecutionService
from docspec.domain.content import AcquisitionDisposition, CandidateFile, SourceItem
from docspec.domain.delivery import (
    DeliveryRecord,
    delivery_entry_population,
    delivery_store_verdict,
    iter_delivery_records,
)
from docspec.domain.identity import ordered_json_sequence_digest, sha256_digest
from docspec.domain.jobs import ChangeKind, DocumentEntry, DocumentStore, StoreState
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.receipts import DeliveryReceipt, RunReceipt
from docspec.domain.references import ArtifactRef, LayerRef, SourceCatalogRef, StoreRef
from docspec.domain.storage import PartitionPolicy, RecordSchema
from docspec.errors import IntegrityError, StateTransitionError
from docspec.processing.extraction import DefaultExtractorRegistry
from docspec.processing.segmentation import DefaultSegmenterRegistry
from tests.helpers import artifact, persist_execution_evidence, profile_set


NOW = "2026-08-05T12:00:00Z"
PARTITIONS = PartitionPolicy("source-item-sha256-v1", 8)
STAGES = StagePolicy(
    (DefaultExtractorRegistry.extractor_id,),
    DefaultSegmenterRegistry.segmenter_id,
)


def _clock() -> str:
    return NOW


def _limits(*, max_entries: int = 2) -> WorkLimits:
    return WorkLimits(max_entries, 1024 * 1024, 100, 100, 1000, 1024 * 1024, 60, 3)


def _terminal_store(item_id: str = "source-1") -> DocumentStore:
    source = SourceItem(
        item_id,
        "v1",
        (CandidateFile("primary", f"{item_id}.txt", "text/plain"),),
    )
    entry = replace(
        DocumentEntry.create(source, ChangeKind.ADDED, STAGES),
        disposition=AcquisitionDisposition.CAPTURED,
    )
    return DocumentStore.planned(
        plan_id="plan-1",
        logical_partition="bucket-00000/store-00000000",
        entries=(entry,),
        limits=_limits(),
    ).start("attempt-1")


class _RecordingReceiver:
    def __init__(self, *, fail_on_attempt: int | None = None) -> None:
        self.fail_on_attempt = fail_on_attempt
        self.attempted: list[str] = []
        self.accepted: dict[str, dict[str, Any]] = {}
        self.finished: list[tuple[int, int, str]] = []
        self.result = artifact("returned-result")

    def accept(self, idempotency_key: str, record: Mapping[str, Any]) -> None:
        assert record["idempotencyKey"] == idempotency_key
        self.attempted.append(idempotency_key)
        if self.fail_on_attempt == len(self.attempted):
            self.fail_on_attempt = None
            raise ConnectionError("receiver interrupted before acknowledgement")
        self.accepted.setdefault(idempotency_key, dict(record))

    def finish(self, *, record_count: int, byte_count: int, digest: str) -> ArtifactRef:
        self.finished.append((record_count, byte_count, digest))
        return self.result


class _DroppingSink:
    sink_id = "urn:docspec:test:sink:dropping"
    profile_id = "urn:docspec:test:profile:returned"

    def deliver(self, store: DocumentStore, records: Iterator[DeliveryRecord]) -> DeliveryReceipt:
        del records
        entry_count, population_digest = delivery_entry_population(store)
        return DeliveryReceipt.create(
            store_id=store.store_id,
            store_revision=store.revision,
            sink_id=self.sink_id,
            profile_id=self.profile_id,
            delivered_entry_count=entry_count,
            delivered_entry_population_digest=population_digest,
            record_count=0,
            byte_count=0,
            idempotency_set_digest=sha256_digest(b""),
            accepted_record_count=0,
            rejected_record_count=0,
            retried_record_count=0,
            undelivered_record_count=0,
            final_verdict=delivery_store_verdict(store),
            layers=(),
            blob_roots=(),
            returned_result=artifact("false-acknowledgement"),
            completed_at=NOW,
        )


def test_returned_sink_streams_with_acknowledgement_backpressure_and_stable_replay() -> None:
    store = _terminal_store()
    expected = tuple(iter_delivery_records(store))
    receiver = _RecordingReceiver()
    sink = ReturnedResultSink(
        sink_id="urn:docspec:test:sink:returned",
        profile_id="urn:docspec:test:profile:returned",
        receiver=receiver,
        clock=_clock,
    )

    def acknowledged_stream() -> Iterator[DeliveryRecord]:
        for index, record in enumerate(expected):
            assert len(receiver.attempted) == index
            yield record

    first = sink.deliver(store, acknowledged_stream())
    replay = sink.deliver(store, iter(expected))
    expected_keys = [record.idempotency_key for record in expected]

    assert first.returned_result == receiver.result
    assert first.record_count == len(expected)
    assert first.delivered_entry_count == len(store.entries)
    assert first.delivered_entry_population_digest == delivery_entry_population(store)[1]
    assert first.accepted_record_count == first.record_count
    assert first.rejected_record_count == 0
    assert first.retried_record_count == 0
    assert first.undelivered_record_count == 0
    assert first.final_verdict == delivery_store_verdict(store)
    assert receiver.attempted == expected_keys + expected_keys
    assert set(receiver.accepted) == set(expected_keys)
    assert first.receipt_id == replay.receipt_id
    assert first.idempotency_set_digest == replay.idempotency_set_digest


def _receipt_copy(receipt: DeliveryReceipt, **changes: Any) -> DeliveryReceipt:
    values: dict[str, Any] = {
        "store_id": receipt.store_id,
        "store_revision": receipt.store_revision,
        "sink_id": receipt.sink_id,
        "profile_id": receipt.profile_id,
        "delivered_entry_count": receipt.delivered_entry_count,
        "delivered_entry_population_digest": receipt.delivered_entry_population_digest,
        "record_count": receipt.record_count,
        "byte_count": receipt.byte_count,
        "idempotency_set_digest": receipt.idempotency_set_digest,
        "accepted_record_count": receipt.accepted_record_count,
        "rejected_record_count": receipt.rejected_record_count,
        "retried_record_count": receipt.retried_record_count,
        "undelivered_record_count": receipt.undelivered_record_count,
        "final_verdict": receipt.final_verdict,
        "layers": receipt.layers,
        "blob_roots": receipt.blob_roots,
        "returned_result": receipt.returned_result,
        "completed_at": receipt.completed_at,
        "warnings": receipt.warnings,
    }
    values.update(changes)
    return DeliveryReceipt.create(**values)


def test_delivery_receipt_outcomes_are_complete_and_bounded() -> None:
    store = _terminal_store()
    receiver = _RecordingReceiver()
    receipt = ReturnedResultSink(
        sink_id="urn:docspec:test:sink:returned",
        profile_id="urn:docspec:test:profile:returned",
        receiver=receiver,
        clock=_clock,
    ).deliver(store, iter_delivery_records(store))

    with pytest.raises(ValueError, match="outcomes do not reconcile"):
        _receipt_copy(receipt, accepted_record_count=receipt.record_count - 1)
    with pytest.raises(ValueError, match="retried-record count"):
        _receipt_copy(receipt, retried_record_count=receipt.record_count + 1)
    with pytest.raises(ValueError, match="must accept every offered record"):
        _receipt_copy(
            receipt,
            accepted_record_count=receipt.record_count - 1,
            undelivered_record_count=1,
        )


@pytest.mark.parametrize(
    ("disposition", "expected"),
    [
        (AcquisitionDisposition.ACCEPTED_FAILURE, "accepted-failure"),
        (AcquisitionDisposition.REJECTED_RUN, "rejected"),
    ],
)
def test_sink_receipt_uses_the_terminal_store_verdict(
    disposition: AcquisitionDisposition,
    expected: str,
) -> None:
    store = _terminal_store()
    store = replace(store, entries=(replace(store.entries[0], disposition=disposition),))
    receiver = _RecordingReceiver()
    receipt = ReturnedResultSink(
        sink_id="urn:docspec:test:sink:returned",
        profile_id="urn:docspec:test:profile:returned",
        receiver=receiver,
        clock=_clock,
    ).deliver(store, iter_delivery_records(store))

    assert receipt.final_verdict.value == expected


def test_durable_and_hybrid_sinks_replay_to_the_same_immutable_layers(tmp_path: Path) -> None:
    store = _terminal_store()
    expected = tuple(iter_delivery_records(store))
    records = LocalJsonlRecordStorage(tmp_path / "records")
    blob_root = artifact("blob-root")
    durable = DurableDatasetSink(
        sink_id="urn:docspec:test:sink:durable",
        profile_id="urn:docspec:test:profile:durable",
        storage=records,
        partition_policy=PARTITIONS,
        blob_roots=(blob_root,),
        clock=_clock,
    )

    first = durable.deliver(store, iter(expected))
    replay = durable.deliver(store, iter(expected))

    assert first.receipt_id == replay.receipt_id
    assert first.layers == replay.layers
    assert first.blob_roots == (blob_root,)
    assert sum(layer.record_count for layer in first.layers) == len(expected)
    assert {layer.layer_kind for layer in first.layers} == {
        "dispositions",
        "failures",
        "files",
        "receipts",
        "representations",
        "segments",
        "source-items",
    }
    for layer in first.layers:
        records.verify(layer)

    receiver = _RecordingReceiver()
    hybrid = HybridResultSink(
        sink_id="urn:docspec:test:sink:hybrid",
        profile_id="urn:docspec:test:profile:hybrid",
        durable=durable,
        receiver=receiver,
        clock=_clock,
    )
    hybrid_first = hybrid.deliver(store, iter(expected))
    hybrid_replay = hybrid.deliver(store, iter(expected))

    assert hybrid_first.profile_id == "urn:docspec:test:profile:hybrid"
    assert hybrid_first.layers == first.layers
    assert hybrid_first.returned_result == receiver.result
    assert hybrid_first.receipt_id == hybrid_replay.receipt_id
    assert len(receiver.accepted) == len(expected)
    assert len(receiver.attempted) == 2 * len(expected)


def test_interrupted_return_delivery_replays_keys_and_completed_delivery_short_circuits(
    tmp_path: Path,
) -> None:
    stores = LocalDocumentStoreRepository(tmp_path / "stores")
    controls = LocalJsonControlRepository(tmp_path / "controls")
    processed = _terminal_store()
    processed_ref = stores.save(processed)
    receiver = _RecordingReceiver(fail_on_attempt=2)
    sink = ReturnedResultSink(
        sink_id="urn:docspec:test:sink:returned",
        profile_id="urn:docspec:test:profile:returned",
        receiver=receiver,
        clock=_clock,
    )
    sink_ref = controls.put(
        kind="sinks",
        artifact_id=sink.sink_id,
        value={"sinkId": sink.sink_id, "profileId": sink.profile_id},
    )
    service = StoreDeliveryService(stores=stores, controls=controls, sinks={sink.sink_id: sink})

    with pytest.raises(ConnectionError, match="before acknowledgement"):
        service.deliver_store(processed_ref, sink_ref)

    assert stores.latest(processed.store_id) == processed_ref
    assert stores.load(processed_ref).state == StoreState.RUNNING

    sealed_ref = service.deliver_store(processed_ref, sink_ref)
    sealed = stores.load(sealed_ref)
    assert sealed.state == StoreState.SEALED
    assert sealed.delivery_receipt is not None
    receipt = DeliveryReceipt.from_dict(controls.load(sealed.delivery_receipt))
    expected_keys = [record.idempotency_key for record in iter_delivery_records(processed)]
    assert receiver.attempted == [expected_keys[0], expected_keys[1], *expected_keys]
    assert set(receiver.accepted) == set(expected_keys)
    assert receipt.returned_result == receiver.result

    delivery_attempts = list(receiver.attempted)
    assert service.deliver_store(processed_ref, sink_ref) == sealed_ref
    assert receiver.attempted == delivery_attempts


def test_delivery_refuses_a_sink_receipt_that_drops_the_expected_stream(tmp_path: Path) -> None:
    stores = LocalDocumentStoreRepository(tmp_path / "stores")
    controls = LocalJsonControlRepository(tmp_path / "controls")
    processed = _terminal_store()
    processed_ref = stores.save(processed)
    sink = _DroppingSink()
    sink_ref = controls.put(
        kind="sinks",
        artifact_id=sink.sink_id,
        value={"sinkId": sink.sink_id, "profileId": sink.profile_id},
    )
    service = StoreDeliveryService(stores=stores, controls=controls, sinks={sink.sink_id: sink})

    with pytest.raises(IntegrityError, match="complete expected record stream"):
        service.deliver_store(processed_ref, sink_ref)

    assert stores.latest(processed.store_id) == processed_ref
    assert stores.load(processed_ref).state == StoreState.RUNNING


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


def _plan(source: SourceCatalogRef, retry: RetryPolicy, accepted: AcceptedFailurePolicy) -> ProcessingPlan:
    return ProcessingPlan.create(
        source_catalog=source,
        base_release=None,
        profiles=profile_set(),
        limits=_limits(),
        stages=STAGES,
        processors=ProcessorSet(()),
        partition_count=PARTITIONS.bucket_count,
        selection={},
        retention_policy={"sourceBytes": "retained"},
        data_use_policy={"dataUse": "local-bytes-only"},
        retry_policy_digest=retry.digest,
        accepted_failure_policy_digest=accepted.digest,
    )


def test_stateless_returned_run_cannot_advance_the_document_catalog(tmp_path: Path) -> None:
    controls = LocalJsonControlRepository(tmp_path / "controls")
    records = LocalJsonlRecordStorage(tmp_path / "records")
    stores = LocalDocumentStoreRepository(tmp_path / "stores")
    catalog = LocalManifestDocumentCatalog(
        tmp_path / "catalog",
        records=records,
        stores=stores,
        controls=controls,
    )
    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    source = SourceCatalogRef("source-catalog", "source-catalog.json", sha256_digest(b"source-catalog"))
    plan = _plan(source, retry, accepted)
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    planned_store_ledger = stores.seal_planned_stores(plan.plan_id, ())
    store_ledger = records.write_layer(
        (),
        layer_kind="run-store-receipts",
        schema=_STORE_LEDGER_SCHEMA,
        partition_policy=PARTITIONS,
    )
    selection_ledger = records.write_layer(
        (),
        layer_kind="run-selection",
        schema=_SELECTION_LEDGER_SCHEMA,
        partition_policy=PARTITIONS,
    )
    execution_profile, execution_handoff, task_result_ledger = persist_execution_evidence(
        controls=controls,
        records=records,
        plan_ref=plan_ref,
        planned_store_ledger=planned_store_ledger,
        planned_stores=(),
        sealed_stores=(),
        partition_policy=PARTITIONS,
    )
    run = RunReceipt.create(
        plan=plan_ref,
        execution_profile=execution_profile,
        execution_handoff=execution_handoff,
        source_catalog=source,
        base_release=None,
        planned_store_ledger=planned_store_ledger,
        store_ledger=store_ledger,
        store_count=0,
        selection_ledger=selection_ledger,
        selected_item_count=0,
        task_result_ledger=task_result_ledger,
        store_receipt_set_digest=ordered_json_sequence_digest(()),
        staged_layers=(),
        blob_roots=(),
        counts={},
        failures={},
        coverage={},
        partition_policy={"policyId": PARTITIONS.policy_id, "bucketCount": PARTITIONS.bucket_count},
        stateful=False,
        completed_at=NOW,
    )
    run_ref = controls.put(kind="run-receipts", artifact_id=run.run_id, value=run.to_dict())

    with pytest.raises(StateTransitionError, match="stateless returned-result run"):
        ReleaseCommitService(
            plan_ref=plan_ref,
            controls=controls,
            records=records,
            document_catalog=catalog,
        ).commit_release(None, run_ref)

    assert catalog.current() is None


class _WorkerInterrupted(RuntimeError):
    pass


class _InterruptingStoreRepository:
    """Persist one useful checkpoint, then model a worker disappearing."""

    def __init__(self, delegate: LocalDocumentStoreRepository) -> None:
        self.delegate = delegate
        self.armed = True

    def save(self, store: DocumentStore) -> StoreRef:
        reference = self.delegate.save(store)
        terminal = sum(entry.terminal for entry in store.entries)
        if self.armed and store.state == StoreState.RUNNING and terminal == 1 < len(store.entries):
            self.armed = False
            raise _WorkerInterrupted("worker disappeared after the durable checkpoint")
        return reference

    def load(self, reference: StoreRef) -> DocumentStore:
        return self.delegate.load(reference)

    def latest(self, store_id: str) -> StoreRef | None:
        return self.delegate.latest(store_id)

    def revisions(self, store_id: str) -> tuple[StoreRef, ...]:
        return self.delegate.revisions(store_id)

    def seal_planned_stores(self, plan_id: str, references: Iterable[StoreRef]) -> LayerRef:
        return self.delegate.seal_planned_stores(plan_id, references)

    def planned_store_ledger(self, plan_id: str) -> LayerRef:
        return self.delegate.planned_store_ledger(plan_id)

    def verify_planned_store_ledger(self, reference: LayerRef) -> None:
        self.delegate.verify_planned_store_ledger(reference)

    def stream_planned_stores(self, reference: LayerRef) -> Iterator[StoreRef]:
        yield from self.delegate.stream_planned_stores(reference)


class _CountingFetcher:
    def __init__(self, delegate: LocalFileContentFetcher) -> None:
        self.delegate = delegate
        self.calls: list[str] = []

    def fetch(
        self,
        candidate: CandidateFile,
        *,
        max_bytes: int,
        task_id: str,
        attempt_id: str,
    ) -> Any:
        self.calls.append(candidate.candidate_id)
        return self.delegate.fetch(
            candidate,
            max_bytes=max_bytes,
            task_id=task_id,
            attempt_id=attempt_id,
        )


def test_worker_restart_reuses_the_verified_entry_checkpoint(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    source_items: list[SourceItem] = []
    for name, content in (("first", b"First verified paragraph."), ("second", b"Second paragraph.")):
        path = sources / f"{name}.txt"
        path.write_bytes(content)
        source_items.append(
            SourceItem(
                f"source-{name}",
                "v1",
                (
                    CandidateFile(
                        name,
                        path.name,
                        "text/plain",
                        expected_digest=sha256_digest(content),
                        expected_size=len(content),
                        transport_version=f"fixture:{name}:v1",
                    ),
                ),
            )
        )

    controls = LocalJsonControlRepository(tmp_path / "controls")
    durable_stores = LocalDocumentStoreRepository(tmp_path / "stores")
    stores = _InterruptingStoreRepository(durable_stores)
    blobs = LocalContentAddressedBlobStore(tmp_path / "blobs")
    records = LocalJsonlRecordStorage(tmp_path / "records")
    catalog = LocalManifestDocumentCatalog(
        tmp_path / "catalog",
        records=records,
        stores=durable_stores,
        controls=controls,
        blobs=blobs,
    )
    fetcher = _CountingFetcher(LocalFileContentFetcher(sources))
    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    source_ref = SourceCatalogRef("source-catalog", "source-catalog.json", sha256_digest(b"source-catalog"))
    plan = _plan(source_ref, retry, accepted)
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    planned = DocumentStore.planned(
        plan_id=plan.plan_id,
        logical_partition="bucket-00000/store-00000000",
        entries=tuple(DocumentEntry.create(item, ChangeKind.ADDED, STAGES) for item in source_items),
        limits=plan.limits,
    )
    planned_ref = durable_stores.save(planned)
    service = StoreExecutionService(
        plan_ref=plan_ref,
        controls=controls,
        stores=stores,
        document_catalog=catalog,
        blobs=blobs,
        fetcher=fetcher,
        extractor=DefaultExtractorRegistry(),
        segmenter=DefaultSegmenterRegistry(),
        processors={},
        retry_policy=retry,
        accepted_failure_policy=accepted,
        clock=_clock,
        sleep=lambda _: None,
    )

    with pytest.raises(_WorkerInterrupted, match="durable checkpoint"):
        service.execute_store(planned_ref)

    checkpoint_ref = durable_stores.latest(planned.store_id)
    assert checkpoint_ref is not None
    checkpoint = durable_stores.load(checkpoint_ref)
    assert [entry.terminal for entry in checkpoint.entries] == [True, False]
    first_entry = checkpoint.entries[0]
    assert first_entry.captured_files and first_entry.representations and first_entry.segments
    for captured in first_entry.captured_files:
        blobs.verify(captured.blob)

    resumed_ref = service.execute_store(planned_ref)
    resumed = durable_stores.load(resumed_ref)

    assert resumed.state == StoreState.RUNNING
    assert all(entry.terminal for entry in resumed.entries)
    assert resumed.entries[0] == first_entry
    assert fetcher.calls == ["first", "second"]
    assert len(resumed.attempts) == 2
    for entry in resumed.entries:
        for captured in entry.captured_files:
            blobs.verify(captured.blob)

    first_blob = resumed.entries[0].captured_files[0].blob
    (blobs.root / first_blob.locator).write_bytes(b"tampered checkpoint bytes")
    with pytest.raises(IntegrityError, match="blob size|blob bytes"):
        service.execute_store(planned_ref)
