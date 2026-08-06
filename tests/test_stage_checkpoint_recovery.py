from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters.source_catalog import LocalFileContentFetcher
from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.application.execution import StoreExecutionService
from docspec.application.work_budget import WorkBudget
from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.identity import sha256_digest
from docspec.domain.jobs import ChangeKind, DocumentEntry, DocumentStore, StoreState
from docspec.domain.plans import ProcessingPlan, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.references import SourceCatalogRef, StoreRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.content_fetcher import FetchStream
from tests.test_processor_reprocessing import (
    _CountingExtractor,
    _CountingFetcher,
    _CountingProcessor,
    _CountingSegmenter,
    _description,
    _plan,
)


NOW = "2026-08-05T12:00:00Z"


class _WorkerInterrupted(RuntimeError):
    pass


class _HardWorkerCrash(BaseException):
    pass


class _InterruptAfterStageRepository:
    """Persist a selected partial revision, then model abrupt worker loss."""

    def __init__(
        self,
        delegate: LocalDocumentStoreRepository,
        predicate: Callable[[DocumentStore], bool],
    ) -> None:
        self._delegate = delegate
        self._predicate = predicate
        self._armed = True

    def save(self, store: DocumentStore) -> StoreRef:
        reference = self._delegate.save(store)
        if self._armed and self._predicate(store):
            self._armed = False
            raise _WorkerInterrupted("worker disappeared after the durable stage checkpoint")
        return reference

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _CrashAfterExtractionOnce:
    """Finish extractor work once, then disappear before it can be checkpointed."""

    def __init__(self, delegate: _CountingExtractor) -> None:
        self._delegate = delegate
        self.extractor_id = delegate.extractor_id
        self._armed = True

    @property
    def calls(self) -> int:
        return self._delegate.calls

    def extract(self, captured: Any, source_bytes: bytes) -> Any:
        result = self._delegate.extract(captured, source_bytes)
        if self._armed:
            self._armed = False
            raise _HardWorkerCrash("worker disappeared before the extraction checkpoint")
        return result


class _CrashDuringCandidateOnce:
    """Lose one worker after bytes begin streaming for a selected candidate."""

    def __init__(self, delegate: _CountingFetcher, candidate_id: str) -> None:
        self.delegate = delegate
        self.candidate_id = candidate_id
        self._armed = True

    @property
    def calls(self) -> list[str]:
        return self.delegate.calls

    def fetch(self, candidate: Any, **kwargs: Any) -> FetchStream:
        stream = self.delegate.fetch(candidate, **kwargs)
        if not self._armed or candidate.candidate_id != self.candidate_id:
            return stream
        self._armed = False

        def chunks() -> Any:
            iterator = iter(stream.chunks)
            yield next(iterator)
            raise _HardWorkerCrash("worker disappeared during candidate streaming")

        return FetchStream(stream.metadata, chunks(), close_callback=stream.close)


class _WrongBytesClosingFetcher:
    """Return the wrong digest and record whether the consumer still closes."""

    def __init__(self, delegate: _CountingFetcher) -> None:
        self.delegate = delegate
        self.close_count = 0

    def fetch(self, candidate: Any, **kwargs: Any) -> FetchStream:
        stream = self.delegate.fetch(candidate, **kwargs)
        assert candidate.expected_size is not None

        def close() -> None:
            self.close_count += 1
            stream.close()

        return FetchStream(
            stream.metadata,
            iter((b"x" * candidate.expected_size,)),
            close_callback=close,
        )


@dataclass(slots=True)
class _Harness:
    content: bytes
    plan: ProcessingPlan
    plan_ref: Any
    planned: DocumentStore
    planned_ref: StoreRef
    controls: LocalJsonControlRepository
    stores: LocalDocumentStoreRepository
    blobs: LocalContentAddressedBlobStore
    catalog: LocalManifestDocumentCatalog
    fetcher: _CountingFetcher
    extractor: Any
    segmenter: _CountingSegmenter
    processors: tuple[_CountingProcessor, ...]
    retry: RetryPolicy
    accepted: AcceptedFailurePolicy

    def service(self, stores: Any | None = None) -> StoreExecutionService:
        return StoreExecutionService(
            plan_ref=self.plan_ref,
            controls=self.controls,
            stores=self.stores if stores is None else stores,
            document_catalog=self.catalog,
            blobs=self.blobs,
            fetcher=self.fetcher,
            extractor=self.extractor,
            segmenter=self.segmenter,
            processors={item.description.processor_id: item for item in self.processors},
            retry_policy=self.retry,
            accepted_failure_policy=self.accepted,
            processor_cache=None,
            clock=lambda: NOW,
            sleep=lambda _: None,
        )

    def calls(self) -> tuple[int, int, int, tuple[int, ...]]:
        return (
            len(self.fetcher.calls),
            self.extractor.calls,
            self.segmenter.calls,
            tuple(len(processor.calls) for processor in self.processors),
        )


def _harness(
    tmp_path: Path,
    *,
    content: bytes = b"One exact paragraph.",
    processor_count: int = 1,
    candidate_specs: tuple[tuple[str, str, str, bytes], ...] | None = None,
) -> _Harness:
    sources = tmp_path / "sources"
    sources.mkdir(parents=True)
    specs = candidate_specs or (("primary", "document.txt", "text/plain", content),)
    candidates: list[CandidateFile] = []
    for candidate_id, name, media_type, payload in specs:
        source_path = sources / name
        source_path.write_bytes(payload)
        candidates.append(
            CandidateFile(
                candidate_id,
                source_path.name,
                media_type,
                expected_digest=sha256_digest(payload),
                expected_size=len(payload),
                transport_version=f"fixture:{candidate_id}:v1",
            )
        )
    source_item = SourceItem(
        "document-a",
        "v1",
        tuple(candidates),
    )

    controls = LocalJsonControlRepository(tmp_path / "controls")
    stores = LocalDocumentStoreRepository(tmp_path / "stores")
    blobs = LocalContentAddressedBlobStore(tmp_path / "blobs")
    records = LocalJsonlRecordStorage(tmp_path / "records")
    catalog = LocalManifestDocumentCatalog(
        tmp_path / "catalog",
        records=records,
        stores=stores,
        controls=controls,
        blobs=blobs,
    )
    retry = RetryPolicy(max_attempts=3, base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    processors: list[_CountingProcessor] = []
    for index in range(processor_count):
        dependencies = () if index == 0 else (processors[-1].description.processor_id,)
        processors.append(
            _CountingProcessor(
                _description(
                    f"stage-{index + 1}",
                    "1",
                    retry,
                    dependencies=dependencies,
                )
            )
        )
    source_ref = SourceCatalogRef(
        "source-catalog",
        "source-catalog.json",
        sha256_digest(b"source-catalog"),
    )
    plan = _plan(source_ref, None, tuple(processors), retry, accepted)
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    planned = DocumentStore.planned(
        plan_id=plan.plan_id,
        logical_partition="bucket-00000/store-00000000",
        entries=(DocumentEntry.create(source_item, ChangeKind.ADDED, plan.stages),),
        limits=plan.limits,
    )
    planned_ref = stores.save(planned)
    return _Harness(
        content,
        plan,
        plan_ref,
        planned,
        planned_ref,
        controls,
        stores,
        blobs,
        catalog,
        _CountingFetcher(LocalFileContentFetcher(sources)),
        _CountingExtractor(),
        _CountingSegmenter(),
        tuple(processors),
        retry,
        accepted,
    )


def _entry(store: DocumentStore) -> DocumentEntry:
    assert len(store.entries) == 1
    return store.entries[0]


def _extraction_frontier(store: DocumentStore) -> bool:
    entry = _entry(store)
    return (
        store.state is StoreState.RUNNING
        and not entry.terminal
        and bool(entry.captured_files)
        and bool(entry.representations)
        and not entry.segments
        and not entry.derived_records
    )


def _segmentation_frontier(store: DocumentStore) -> bool:
    entry = _entry(store)
    return (
        store.state is StoreState.RUNNING
        and not entry.terminal
        and bool(entry.captured_files)
        and bool(entry.representations)
        and bool(entry.segments)
        and not entry.derived_records
    )


def _processor_frontier(processor_id: str) -> Callable[[DocumentStore], bool]:
    def selected(store: DocumentStore) -> bool:
        entry = _entry(store)
        return (
            store.state is StoreState.RUNNING
            and not entry.terminal
            and {record.processor_id for record in entry.derived_records} == {processor_id}
        )

    return selected


def _candidate_frontier(captured: int, represented: int) -> Callable[[DocumentStore], bool]:
    def selected(store: DocumentStore) -> bool:
        entry = _entry(store)
        return (
            store.state is StoreState.RUNNING
            and not entry.terminal
            and len(entry.captured_files) == captured
            and len(entry.representations) == represented
            and not entry.segments
            and not entry.derived_records
        )

    return selected


def _two_candidate_harness(tmp_path: Path) -> _Harness:
    return _harness(
        tmp_path,
        candidate_specs=(
            (
                "metadata-json",
                "metadata.json",
                "application/json",
                b'{"data":{"id":"SEC-2020-0001-0001","type":"documents"}}',
            ),
            (
                "rendition-html",
                "content.html",
                "text/html",
                b"<html><body><p>Exact mirrored content.</p></body></html>",
            ),
        ),
    )


def _interrupt(harness: _Harness, predicate: Callable[[DocumentStore], bool]) -> DocumentStore:
    interrupting = _InterruptAfterStageRepository(harness.stores, predicate)
    with pytest.raises(_WorkerInterrupted, match="durable stage checkpoint"):
        harness.service(interrupting).execute_store(harness.planned_ref)
    checkpoint_ref = harness.stores.latest(harness.planned.store_id)
    assert checkpoint_ref is not None
    checkpoint = harness.stores.load(checkpoint_ref)
    assert predicate(checkpoint)
    return checkpoint


def _resume(harness: _Harness) -> DocumentStore:
    resumed_ref = harness.service().execute_store(harness.planned_ref)
    resumed = harness.stores.load(resumed_ref)
    assert _entry(resumed).terminal
    return resumed


def _entry_content(entry: DocumentEntry) -> dict[str, Any]:
    """Compare durable document content without attempt-local acquisition observations."""

    captured = [
        {
            "fileId": item.file_id,
            "sourceItemId": item.source_item_id,
            "sourceVersion": item.source_version,
            "candidateId": item.candidate_id,
            "blob": item.blob.to_dict(),
            "mediaType": item.media_type,
            "transportVersion": item.transport_version,
            "disposition": item.disposition.value,
        }
        for item in entry.captured_files
    ]
    return {
        "entryId": entry.entry_id,
        "sourceItem": entry.source_item.to_dict(),
        "change": entry.change.value,
        "requestedStages": entry.requested_stages.to_dict(),
        "executionMode": entry.execution_mode.value,
        "capturedFiles": captured,
        "representations": [item.to_dict() for item in entry.representations],
        "segments": [item.to_dict() for item in entry.segments],
        "derivedRecords": [item.to_dict() for item in entry.derived_records],
        "disposition": None if entry.disposition is None else entry.disposition.value,
        "failures": [item.to_dict() for item in entry.failures],
        "warnings": list(entry.warnings),
    }


def test_restart_after_extraction_reuses_exact_files_and_representations(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    checkpoint = _interrupt(harness, _extraction_frontier)
    checkpoint_entry = _entry(checkpoint)
    assert harness.calls() == (1, 1, 0, (0,))

    resumed = _resume(harness)

    assert harness.calls() == (1, 1, 1, (1,))
    resumed_entry = _entry(resumed)
    assert resumed_entry.captured_files == checkpoint_entry.captured_files
    assert resumed_entry.representations == checkpoint_entry.representations
    assert len(resumed.attempts) == 2


def test_restart_after_segmentation_reuses_all_content_stages(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    checkpoint = _interrupt(harness, _segmentation_frontier)
    checkpoint_entry = _entry(checkpoint)
    assert harness.calls() == (1, 1, 1, (0,))

    resumed = _resume(harness)

    assert harness.calls() == (1, 1, 1, (1,))
    resumed_entry = _entry(resumed)
    assert resumed_entry.captured_files == checkpoint_entry.captured_files
    assert resumed_entry.representations == checkpoint_entry.representations
    assert resumed_entry.segments == checkpoint_entry.segments


def test_restart_after_processor_layer_runs_only_the_remaining_dependency_layer(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, processor_count=2)
    first, second = harness.processors

    checkpoint = _interrupt(harness, _processor_frontier(first.description.processor_id))
    checkpoint_entry = _entry(checkpoint)
    assert harness.calls() == (1, 1, 1, (1, 0))

    resumed = _resume(harness)

    assert harness.calls() == (1, 1, 1, (1, 1))
    resumed_entry = _entry(resumed)
    first_records = tuple(
        record
        for record in resumed_entry.derived_records
        if record.processor_id == first.description.processor_id
    )
    assert first_records == checkpoint_entry.derived_records
    assert any(record.processor_id == second.description.processor_id for record in resumed_entry.derived_records)


def test_partial_processor_checkpoint_restores_every_cumulative_budget_counter(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    processor = harness.processors[0]
    checkpoint = _interrupt(harness, _processor_frontier(processor.description.processor_id))
    entry = _entry(checkpoint)
    assert len(entry.segments) == 1
    invocation_id = WorkBudget.processor_invocation_id(
        entry.entry_id,
        processor.description.processor_id,
        (entry.segments[0].segment_id,),
    )
    limits = WorkLimits(
        1,
        len(harness.content),
        1,
        1,
        1,
        1024,
        60,
        3,
    )
    budget = WorkBudget(limits)

    budget.seed_verified_entries((entry,), {entry.entry_id: (invocation_id,)})

    assert budget.usage.source_bytes == len(harness.content)
    assert budget.usage.pages_or_frames == 0
    assert budget.usage.segments == 1
    assert budget.usage.processor_cost == 1
    with pytest.raises(LimitExceededError, match="source bytes"):
        budget.charge_source_bytes("next-source", 1)
    with pytest.raises(LimitExceededError, match="segments"):
        budget.charge_segments("next-representation", 1)
    with pytest.raises(LimitExceededError, match="processor cost"):
        budget.charge_processor("next-invocation")


def test_tampered_stage_receipt_fails_closed_before_any_work_restarts(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    checkpoint = _interrupt(harness, _segmentation_frontier)
    checkpoint_entry = _entry(checkpoint)
    segmentation_receipts = [
        reference
        for reference in checkpoint_entry.stage_receipts
        if harness.controls.load(reference).get("format") == "docspec-segmentation-receipt"
    ]
    assert len(segmentation_receipts) == 1
    receipt_ref = segmentation_receipts[0]
    receipt_path = harness.controls.root / receipt_ref.locator
    receipt_path.write_bytes(receipt_path.read_bytes() + b"tampered")
    calls_before_resume = harness.calls()

    with pytest.raises(IntegrityError, match="artifact|bytes|digest|canonical"):
        harness.service().execute_store(harness.planned_ref)

    assert harness.calls() == calls_before_resume


def test_resumed_content_matches_uninterrupted_content_with_candidate_stage_revisions(
    tmp_path: Path,
) -> None:
    content = b"First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    uninterrupted_harness = _harness(
        tmp_path / "uninterrupted",
        content=content,
        processor_count=2,
    )
    uninterrupted = _resume(uninterrupted_harness)
    uninterrupted_entry = _entry(uninterrupted)
    assert len(uninterrupted_entry.segments) == 3

    resumed_harness = _harness(
        tmp_path / "resumed",
        content=content,
        processor_count=2,
    )
    _interrupt(resumed_harness, _segmentation_frontier)
    resumed = _resume(resumed_harness)
    resumed_entry = _entry(resumed)

    assert _entry_content(resumed_entry) == _entry_content(uninterrupted_entry)
    expected_uninterrupted_revisions = 1 + 1 + 2 + 1 + len(uninterrupted_harness.processors) + 1
    assert len(
        uninterrupted_harness.stores.revisions(uninterrupted_harness.planned.store_id)
    ) == expected_uninterrupted_revisions
    assert len(
        resumed_harness.stores.revisions(resumed_harness.planned.store_id)
    ) == expected_uninterrupted_revisions + 1


def test_duplicate_stage_receipt_fails_closed_before_any_work_restarts(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    checkpoint = _interrupt(harness, _extraction_frontier)
    checkpoint_entry = _entry(checkpoint)
    assert len(checkpoint_entry.stage_receipts) == 1
    duplicate_entry = replace(
        checkpoint_entry,
        stage_receipts=(*checkpoint_entry.stage_receipts, checkpoint_entry.stage_receipts[0]),
    )
    harness.stores.save(checkpoint.checkpoint((duplicate_entry,)))
    calls_before_resume = harness.calls()

    with pytest.raises(IntegrityError, match="repeats a stage receipt"):
        harness.service().execute_store(harness.planned_ref)

    assert harness.calls() == calls_before_resume


def test_hard_crash_after_capture_reuses_bytes_and_reruns_only_incomplete_extraction(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    crashing_extractor = _CrashAfterExtractionOnce(harness.extractor)
    harness.extractor = crashing_extractor

    with pytest.raises(_HardWorkerCrash, match="before the extraction checkpoint"):
        harness.service().execute_store(harness.planned_ref)

    checkpoint_ref = harness.stores.latest(harness.planned.store_id)
    assert checkpoint_ref is not None
    checkpoint = harness.stores.load(checkpoint_ref)
    checkpoint_entry = _entry(checkpoint)
    assert not checkpoint_entry.terminal
    assert len(checkpoint_entry.captured_files) == 1
    assert not checkpoint_entry.representations
    assert not checkpoint_entry.segments
    assert not checkpoint_entry.derived_records
    assert harness.calls() == (1, 1, 0, (0,))

    resumed = _resume(harness)

    assert _entry(resumed).terminal
    assert harness.calls() == (1, 2, 1, (1,))
    assert len(resumed.attempts) == 2


@pytest.mark.parametrize(
    ("captured", "represented"),
    [
        (1, 0),
        (1, 1),
        (2, 1),
        (2, 2),
    ],
)
def test_two_candidate_resume_never_refetches_verified_candidates(
    tmp_path: Path,
    captured: int,
    represented: int,
) -> None:
    harness = _two_candidate_harness(tmp_path)
    checkpoint = _interrupt(harness, _candidate_frontier(captured, represented))
    checkpoint_entry = _entry(checkpoint)
    assert len(checkpoint_entry.captured_files) == captured
    assert len(checkpoint_entry.representations) == represented

    resumed = _resume(harness)

    assert _entry(resumed).terminal
    assert harness.fetcher.calls == ["metadata-json", "rendition-html"]
    assert harness.extractor.calls == 2


def test_crash_during_second_candidate_reuses_completed_first_candidate(tmp_path: Path) -> None:
    harness = _two_candidate_harness(tmp_path)
    crashing = _CrashDuringCandidateOnce(harness.fetcher, "rendition-html")
    harness.fetcher = crashing

    with pytest.raises(_HardWorkerCrash, match="candidate streaming"):
        harness.service().execute_store(harness.planned_ref)

    checkpoint_ref = harness.stores.latest(harness.planned.store_id)
    assert checkpoint_ref is not None
    checkpoint_entry = _entry(harness.stores.load(checkpoint_ref))
    assert [item.candidate_id for item in checkpoint_entry.captured_files] == ["metadata-json"]
    assert len(checkpoint_entry.representations) == 1

    resumed = _resume(harness)

    assert _entry(resumed).terminal
    assert crashing.calls.count("metadata-json") == 1
    assert crashing.calls.count("rendition-html") == 2


def test_blob_digest_rejection_closes_fetch_stream(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    wrong = _WrongBytesClosingFetcher(harness.fetcher)
    harness.fetcher = wrong

    result_ref = harness.service().execute_store(harness.planned_ref)
    result = harness.stores.load(result_ref)

    assert _entry(result).terminal
    assert wrong.close_count == 1
    assert wrong.delegate.calls == ["primary"]
