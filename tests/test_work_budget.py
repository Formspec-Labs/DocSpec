from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
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
from docspec.domain.content import AcquisitionDisposition, CandidateFile, SourceItem
from docspec.domain.identity import sha256_digest
from docspec.domain.jobs import ChangeKind, DocumentEntry, DocumentStore, FailureClass
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.processors import (
    ProcessorDescription,
    ProcessorInput,
    ProcessorItemLimits,
    ProcessorSet,
)
from docspec.domain.references import SourceCatalogRef
from docspec.errors import LimitExceededError
from docspec.ports.content_fetcher import FetchStream
from docspec.processing.extraction import DefaultExtractorRegistry, ExtractionResult
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.processing.segmentation import DefaultSegmenterRegistry
from tests.helpers import profile_set


NOW = "2026-08-05T12:00:00Z"


class _ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _limits(
    *,
    source_bytes: int = 1024,
    pages_or_frames: int = 100,
    segments: int = 100,
    processor_cost: int = 100,
    memory_bytes: int = 1024,
    duration_seconds: int = 60,
    max_entries: int = 4,
) -> WorkLimits:
    return WorkLimits(
        max_entries,
        source_bytes,
        pages_or_frames,
        segments,
        processor_cost,
        memory_bytes,
        duration_seconds,
        3,
    )


def test_work_budget_counts_verified_units_once_and_tracks_peak_memory() -> None:
    clock = _ManualClock()
    budget = WorkBudget(
        _limits(
            source_bytes=10,
            pages_or_frames=3,
            segments=4,
            processor_cost=2,
            memory_bytes=10,
            duration_seconds=5,
        ),
        monotonic=clock,
    )

    budget.charge_source_bytes("source-a", 4)
    budget.charge_source_bytes("source-a", 4)
    budget.charge_source_bytes("source-b", 6)
    assert budget.observe_extraction(
        "representation-a",
        representation_kind="pdf-text",
        metadata={"pageCount": 2},
    ) == 2
    budget.observe_extraction("representation-image", representation_kind="image", metadata={})
    budget.charge_segments("representation-a", 4)
    budget.charge_segments("representation-a", 4)
    invocation = budget.processor_invocation_id("entry-a", "processor-a", ("segment-a",))
    budget.charge_processor(invocation)
    budget.charge_processor(invocation)
    with budget.materialization_scope() as memory:
        memory.reserve("first", 6)
        memory.reserve("second", 4)
        memory.release("first")
        assert budget.usage.current_memory_bytes == 4

    assert budget.usage.to_dict() == {
        "sourceBytes": 10,
        "pagesOrFrames": 3,
        "segments": 4,
        "processorCost": 1,
        "currentMemoryBytes": 0,
        "peakMemoryBytes": 10,
    }
    clock.now = 5
    budget.check_duration()
    clock.now = 5.001
    with pytest.raises(LimitExceededError, match="elapsed duration"):
        budget.check_duration()


class _ReadThenFailOnceFetcher:
    """Model a transport loss after bytes were read but before verification."""

    def __init__(self, delegate: LocalFileContentFetcher) -> None:
        self.delegate = delegate
        self.calls = 0

    def fetch(self, candidate: CandidateFile, **kwargs: Any) -> FetchStream:
        self.calls += 1
        stream = self.delegate.fetch(candidate, **kwargs)
        if self.calls != 1:
            return stream

        def interrupted() -> Any:
            yield from stream.chunks
            raise ConnectionError("transport ended before verification")

        return FetchStream(stream.metadata, interrupted())


class _PageReportingExtractor:
    extractor_id = DefaultExtractorRegistry.extractor_id

    def __init__(self, *, page_count: int, after: Callable[[], None] | None = None) -> None:
        self._delegate = DefaultExtractorRegistry()
        self._page_count = page_count
        self._after = after

    def extract(self, captured: Any, source_bytes: bytes) -> ExtractionResult:
        result = self._delegate.extract(captured, source_bytes)
        if self._after is not None:
            self._after()
        return ExtractionResult(
            result.payload,
            replace(result.receipt, metadata={"pageCount": self._page_count}),
        )


def _execute(
    tmp_path: Path,
    *,
    documents: tuple[tuple[str, tuple[bytes, ...]], ...],
    limits: WorkLimits,
    processor: Any | None = None,
    fetcher_factory: Callable[[LocalFileContentFetcher], Any] | None = None,
    extractor: Any | None = None,
    monotonic: Callable[[], float] | None = None,
) -> tuple[DocumentStore, Any]:
    sources = tmp_path / "sources"
    sources.mkdir(parents=True)
    source_items: list[SourceItem] = []
    for document_id, payloads in documents:
        candidates: list[CandidateFile] = []
        for index, content in enumerate(payloads):
            path = sources / f"{document_id}-{index}.txt"
            path.write_bytes(content)
            candidates.append(
                CandidateFile(
                    f"candidate-{index}",
                    path.name,
                    "text/plain",
                    expected_digest=sha256_digest(content),
                    expected_size=len(content),
                    transport_version=f"fixture:{document_id}:{index}",
                )
            )
        source_items.append(SourceItem(document_id, "v1", tuple(candidates)))

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
    base_fetcher = LocalFileContentFetcher(sources)
    fetcher = base_fetcher if fetcher_factory is None else fetcher_factory(base_fetcher)
    retry = RetryPolicy(max_attempts=limits.max_attempts, base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    processors = {} if processor is None else {processor.description.processor_id: processor}
    stages = StagePolicy(
        (DefaultExtractorRegistry.extractor_id,),
        DefaultSegmenterRegistry.segmenter_id,
        tuple(processors),
    )
    source_ref = SourceCatalogRef("source-catalog", "source-catalog.json", sha256_digest(b"source-catalog"))
    plan = ProcessingPlan.create(
        source_catalog=source_ref,
        base_release=None,
        profiles=profile_set(),
        limits=limits,
        stages=stages,
        processors=ProcessorSet(tuple(item.description for item in processors.values())),
        partition_count=8,
        selection={},
        retention_policy={"sourceBytes": "retained"},
        data_use_policy={"dataUse": "local-bytes-only"},
        retry_policy_digest=retry.digest,
        accepted_failure_policy_digest=accepted.digest,
    )
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    planned = DocumentStore.planned(
        plan_id=plan.plan_id,
        logical_partition="bucket-00000/store-00000000",
        entries=tuple(DocumentEntry.create(item, ChangeKind.ADDED, stages) for item in source_items),
        limits=limits,
    )
    planned_ref = stores.save(planned)
    keywords: dict[str, Any] = {}
    if monotonic is not None:
        keywords["monotonic"] = monotonic
    processed_ref = StoreExecutionService(
        plan_ref=plan_ref,
        controls=controls,
        stores=stores,
        document_catalog=catalog,
        blobs=blobs,
        fetcher=fetcher,
        extractor=extractor or DefaultExtractorRegistry(),
        segmenter=DefaultSegmenterRegistry(),
        processors=processors,
        retry_policy=retry,
        accepted_failure_policy=accepted,
        clock=lambda: NOW,
        sleep=lambda _: None,
        **keywords,
    ).execute_store(planned_ref)
    return stores.load(processed_ref), fetcher


def _processor_attempt_receipts(tmp_path: Path, entry: DocumentEntry) -> list[dict[str, Any]]:
    controls = LocalJsonControlRepository(tmp_path / "controls")
    return [
        value
        for reference in entry.stage_receipts
        if (value := controls.load(reference)).get("format")
        == "docspec-processor-attempt-receipt"
    ]


def test_verified_source_bytes_are_not_double_charged_after_a_transport_retry(tmp_path: Path) -> None:
    content = b"exact"
    processed, fetcher = _execute(
        tmp_path,
        documents=(("source-a", (content,)),),
        limits=_limits(source_bytes=len(content), memory_bytes=32),
        fetcher_factory=_ReadThenFailOnceFetcher,
    )

    entry = processed.entries[0]
    assert entry.disposition == AcquisitionDisposition.CAPTURED
    assert fetcher.calls == 2
    assert [failure.failure_class for failure in entry.failures] == [FailureClass.TRANSIENT_EXTERNAL]

    resumed_budget = WorkBudget(_limits(source_bytes=len(content) + 1, memory_bytes=32))
    resumed_budget.seed_verified_entries(processed.entries, {})
    assert resumed_budget.usage.source_bytes == len(content)
    assert resumed_budget.usage.segments == 1
    with pytest.raises(LimitExceededError, match="source bytes"):
        resumed_budget.charge_source_bytes("next-source", 2)


def test_resume_accounting_includes_zero_output_processor_invocations() -> None:
    item = SourceItem(
        "source-a",
        "v1",
        (CandidateFile("candidate", "source-a.txt", "text/plain"),),
    )
    entry = replace(
        DocumentEntry.create(
            item,
            ChangeKind.ADDED,
            StagePolicy(("tests.extractor/v1",), "tests.segmenter/v1", ("tests.processor/v1",)),
        ),
        disposition=AcquisitionDisposition.CAPTURED,
    )
    budget = WorkBudget(_limits(processor_cost=1))

    budget.seed_verified_entries(
        (entry,),
        {entry.entry_id: ("urn:docspec:test:abstained-invocation",)},
    )

    assert budget.usage.processor_cost == 1
    with pytest.raises(LimitExceededError, match="processor cost"):
        budget.charge_processor("urn:docspec:test:next-invocation")


def test_source_and_memory_limits_apply_to_aggregate_store_work(tmp_path: Path) -> None:
    source_limited, _ = _execute(
        tmp_path / "source-limit",
        documents=(("source-a", (b"123456",)), ("source-b", (b"abcdef",))),
        limits=_limits(source_bytes=10, memory_bytes=32),
    )
    assert [entry.disposition for entry in source_limited.entries] == [
        AcquisitionDisposition.CAPTURED,
        AcquisitionDisposition.REJECTED_RUN,
    ]
    assert source_limited.entries[1].failures[-1].failure_class == FailureClass.DETERMINISTIC_INPUT

    memory_limited, _ = _execute(
        tmp_path / "memory-limit",
        documents=(("source-a", (b"123456", b"abcdef")),),
        limits=_limits(source_bytes=12, memory_bytes=10),
    )
    entry = memory_limited.entries[0]
    assert entry.disposition == AcquisitionDisposition.REJECTED_RUN
    assert len(entry.captured_files) == 2
    assert len(entry.representations) == 1
    assert entry.failures[-1].failure_class == FailureClass.DETERMINISTIC_INPUT


def test_observed_pages_segments_and_processor_invocations_enforce_actual_limits(tmp_path: Path) -> None:
    pages_limited, _ = _execute(
        tmp_path / "pages-limit",
        documents=(("source-a", (b"page-shaped text",)),),
        limits=_limits(pages_or_frames=1, memory_bytes=64),
        extractor=_PageReportingExtractor(page_count=2),
    )
    assert pages_limited.entries[0].disposition == AcquisitionDisposition.REJECTED_RUN

    segments_limited, _ = _execute(
        tmp_path / "segments-limit",
        documents=(("source-a", (b"first\n\nsecond",)),),
        limits=_limits(segments=1, memory_bytes=64),
    )
    segment_entry = segments_limited.entries[0]
    assert segment_entry.disposition == AcquisitionDisposition.REJECTED_RUN
    assert segment_entry.representations and not segment_entry.segments

    processor = ContentStatisticsProcessor(
        retry_policy=RetryPolicy(base_delay_milliseconds=0)
    )
    processor_limited, _ = _execute(
        tmp_path / "processor-limit",
        documents=(("source-a", (b"first\n\nsecond",)),),
        limits=_limits(segments=2, processor_cost=1, memory_bytes=64),
        processor=processor,
    )
    processor_entry = processor_limited.entries[0]
    assert processor_entry.disposition == AcquisitionDisposition.REJECTED_RUN
    assert len(processor_entry.segments) == 2
    assert len(processor_entry.derived_records) == 1


def test_processor_input_declaration_is_enforced_before_invocation(tmp_path: Path) -> None:
    delegate = ContentStatisticsProcessor(
        retry_policy=RetryPolicy(base_delay_milliseconds=0)
    )
    original = delegate.description

    class IncompatibleProcessor:
        description = ProcessorDescription.create(
            name=original.name,
            version=original.version,
            implementation_id=original.implementation_id,
            accepted_inputs=(ProcessorInput("segment", ("docspec-segment/1",), ("image/png",)),),
            output_schema_id=original.output_schema_id,
            output_media_types=original.output_media_types,
            external_resources=original.external_resources,
            dependencies=original.dependencies,
            deterministic=original.deterministic,
            cache_policy=original.cache_policy,
            configuration_digest=original.configuration_digest,
            data_use_policy_digest=original.data_use_policy_digest,
            item_limits=original.item_limits,
            retry_policy_digest=original.retry_policy_digest,
            capabilities=original.capabilities,
        )

        def __init__(self) -> None:
            self.calls = 0

        def process(self, request, payload, prerequisite_results):
            self.calls += 1
            return delegate.process(request, payload, prerequisite_results)

    processor = IncompatibleProcessor()
    processed, _ = _execute(
        tmp_path,
        documents=(("source-a", (b"plain text",)),),
        limits=_limits(memory_bytes=64),
        processor=processor,
    )

    assert processor.calls == 0
    assert processed.entries[0].disposition == AcquisitionDisposition.REJECTED_RUN
    assert processed.entries[0].failures[-1].failure_class == FailureClass.ARTIFACT_INTEGRITY


def test_processor_retry_is_bounded_receipted_and_charged_once(tmp_path: Path) -> None:
    retry = RetryPolicy(base_delay_milliseconds=0)

    class FailOnceProcessor:
        def __init__(self) -> None:
            self.delegate = ContentStatisticsProcessor(retry_policy=retry)
            self.description = self.delegate.description
            self.calls = 0

        def process(self, request, payload, prerequisite_results):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("transient provider failure")
            return self.delegate.process(request, payload, prerequisite_results)

    processor = FailOnceProcessor()
    processed, _ = _execute(
        tmp_path,
        documents=(("source-a", (b"retry content",)),),
        limits=_limits(processor_cost=1, memory_bytes=64),
        processor=processor,
    )
    entry = processed.entries[0]

    assert processor.calls == 2
    assert entry.disposition == AcquisitionDisposition.CAPTURED
    assert [receipt["outcome"] for receipt in _processor_attempt_receipts(tmp_path, entry)] == [
        "failed",
        "succeeded",
    ]


def test_processor_retry_exhaustion_preserves_every_attempt(tmp_path: Path) -> None:
    retry = RetryPolicy(base_delay_milliseconds=0)

    class AlwaysFailProcessor:
        def __init__(self) -> None:
            self.description = ContentStatisticsProcessor(retry_policy=retry).description
            self.calls = 0

        def process(self, request, payload, prerequisite_results):
            self.calls += 1
            raise ConnectionError("persistent provider failure")

    processor = AlwaysFailProcessor()
    processed, _ = _execute(
        tmp_path,
        documents=(("source-a", (b"retry content",)),),
        limits=_limits(processor_cost=1, memory_bytes=64),
        processor=processor,
    )
    entry = processed.entries[0]

    assert processor.calls == retry.max_attempts
    assert entry.disposition == AcquisitionDisposition.REJECTED_RUN
    receipts = _processor_attempt_receipts(tmp_path, entry)
    assert [receipt["attempt"] for receipt in receipts] == [1, 2, 3]
    assert all(receipt["outcome"] == "failed" for receipt in receipts)


def test_processor_item_duration_uses_worker_observation(tmp_path: Path) -> None:
    retry = RetryPolicy(base_delay_milliseconds=0)
    clock = _ManualClock()
    limits = ProcessorItemLimits(1, 1024, 1, 1024, 1)

    class SlowUnderreportingProcessor:
        def __init__(self) -> None:
            self.delegate = ContentStatisticsProcessor(
                item_limits=limits,
                retry_policy=retry,
            )
            self.description = self.delegate.description
            self.calls = 0

        def process(self, request, payload, prerequisite_results):
            self.calls += 1
            result = self.delegate.process(request, payload, prerequisite_results)
            clock.now += 2
            return replace(
                result,
                resource_use=replace(result.resource_use, duration_milliseconds=0),
            )

    processor = SlowUnderreportingProcessor()
    processed, _ = _execute(
        tmp_path,
        documents=(("source-a", (b"slow content",)),),
        limits=_limits(processor_cost=1, memory_bytes=64, duration_seconds=10),
        processor=processor,
        monotonic=clock,
    )

    assert processor.calls == 1
    assert processed.entries[0].disposition == AcquisitionDisposition.REJECTED_RUN
    assert _processor_attempt_receipts(tmp_path, processed.entries[0])[0]["outcome"] == "failed"


def test_elapsed_duration_is_checked_after_injected_stage_work(tmp_path: Path) -> None:
    clock = _ManualClock()

    def advance() -> None:
        clock.now = 2

    processed, _ = _execute(
        tmp_path,
        documents=(("source-a", (b"content",)),),
        limits=_limits(memory_bytes=32, duration_seconds=1),
        extractor=_PageReportingExtractor(page_count=0, after=advance),
        monotonic=clock,
    )

    entry = processed.entries[0]
    assert entry.disposition == AcquisitionDisposition.REJECTED_RUN
    assert entry.failures[-1].failure_class == FailureClass.DETERMINISTIC_INPUT
