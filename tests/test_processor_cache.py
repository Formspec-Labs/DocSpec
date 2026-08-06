from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from docspec.adapters.processor_cache import LocalSqliteProcessorResultCache
from docspec.adapters.source_catalog import LocalFileContentFetcher, LocalJsonlSourceCatalog
from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.domain.content import SourceItem
from docspec.domain.plans import ProcessingPlan
from docspec.domain.policies import AcceptedFailurePolicy, RetentionPolicy, RetryPolicy
from docspec.domain.processors import ProcessorRecordRef
from docspec.domain.references import ArtifactRef
from docspec.domain.storage import PartitionPolicy
from docspec.processing.extraction import TextExtractor
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.processing.segmentation import ParagraphSegmenter
from tests.helpers import artifact
from tests.test_application_pipeline import _plan, _run, _write_source
from tests.test_processing_pipeline import _captured


class _CountingProcessor:
    def __init__(self) -> None:
        self.delegate = ContentStatisticsProcessor(
            retry_policy=RetryPolicy(base_delay_milliseconds=0)
        )
        self.description = self.delegate.description
        self.calls = 0

    def process(self, request, payload, prerequisite_results):
        self.calls += 1
        return self.delegate.process(request, payload, prerequisite_results)


class _UnavailableCache:
    def lookup(self, reuse_key: str) -> ArtifactRef | None:
        raise OSError("cache unavailable")

    def put_if_absent(self, reuse_key: str, result: ArtifactRef) -> ArtifactRef:
        raise OSError("cache unavailable")

    def discard(self, reuse_key: str, expected: ArtifactRef) -> bool:
        raise OSError("cache unavailable")


class _RaceWinnerCache:
    def __init__(self, winner: ArtifactRef) -> None:
        self.winner = winner

    def lookup(self, reuse_key: str) -> ArtifactRef | None:
        return None

    def put_if_absent(self, reuse_key: str, result: ArtifactRef) -> ArtifactRef:
        return self.winner

    def discard(self, reuse_key: str, expected: ArtifactRef) -> bool:
        return False


def _changed_retention_plan(base: ProcessingPlan, release, *, revision: str = "2") -> ProcessingPlan:
    return ProcessingPlan.create(
        source_catalog=base.source_catalog,
        base_release=release,
        profiles=base.profiles,
        limits=base.limits,
        stages=base.stages,
        processors=base.processors,
        partition_count=base.partition_count,
        selection=base.selection,
        retention_policy=RetentionPolicy.create(minimum_age_seconds=int(revision)),
        data_use_policy=base.data_use_policy,
        retry_policy_digest=base.retry_policy_digest,
        accepted_failure_policy_digest=base.accepted_failure_policy_digest,
    )


def _pipeline(tmp_path: Path):
    sources = tmp_path / "sources"
    sources.mkdir()
    candidate = _write_source(sources / "document.txt", "A single paragraph.")
    item = SourceItem("document-a", "v1", (candidate,), metadata={"expectedSegments": 1})
    source_catalog = LocalJsonlSourceCatalog(tmp_path / "source-catalogs")
    source_ref = source_catalog.write((item,))
    controls = LocalJsonControlRepository(tmp_path / "controls")
    stores = LocalDocumentStoreRepository(tmp_path / "stores")
    blobs = LocalContentAddressedBlobStore(tmp_path / "blobs")
    records = LocalJsonlRecordStorage(tmp_path / "records")
    partition_policy = PartitionPolicy("source-item-sha256-v1", 8)
    catalog = LocalManifestDocumentCatalog(
        tmp_path / "document-catalog",
        records=records,
        stores=stores,
        controls=controls,
        blobs=blobs,
    )
    return {
        "source_catalog": source_catalog,
        "source_ref": source_ref,
        "controls": controls,
        "stores": stores,
        "blobs": blobs,
        "records": records,
        "catalog": catalog,
        "fetcher": LocalFileContentFetcher(sources),
        "partition_policy": partition_policy,
    }


def _run_pipeline(parts, *, plan, processor, processor_cache):
    return _run(
        plan=plan,
        source_catalog=parts["source_catalog"],
        controls=parts["controls"],
        stores=parts["stores"],
        blobs=parts["blobs"],
        records=parts["records"],
        catalog=parts["catalog"],
        fetcher=parts["fetcher"],
        processor=processor,
        processor_cache=processor_cache,
        partition_policy=parts["partition_policy"],
    )


def _processor_receipts(parts, store_reference) -> list[dict]:
    receipts = [
        parts["controls"].load(reference)
        for reference in parts["stores"].load(store_reference).entries[0].stage_receipts
    ]
    return [
        receipt
        for receipt in receipts
        if receipt.get("format") == "docspec-processor-invocation-receipt"
    ]


def test_sqlite_cache_reopens_and_returns_the_existing_immutable_winner(tmp_path: Path) -> None:
    path = tmp_path / "processor-results.sqlite3"
    first = artifact("urn:docspec:test:processor-result:first")
    second = artifact("urn:docspec:test:processor-result:second")

    LocalSqliteProcessorResultCache(path).put_if_absent("reuse-key", first)

    assert LocalSqliteProcessorResultCache(path).lookup("reuse-key") == first
    assert LocalSqliteProcessorResultCache(path).put_if_absent("reuse-key", second) == first


def test_sqlite_cache_concurrent_writers_observe_one_immutable_winner(tmp_path: Path) -> None:
    path = tmp_path / "processor-results.sqlite3"
    references = (
        artifact("urn:docspec:test:processor-result:first"),
        artifact("urn:docspec:test:processor-result:second"),
    )

    def publish(reference: ArtifactRef) -> ArtifactRef:
        return LocalSqliteProcessorResultCache(path).put_if_absent("reuse-key", reference)

    with ThreadPoolExecutor(max_workers=2) as executor:
        winners = tuple(executor.map(publish, references))

    assert winners[0] == winners[1]
    assert winners[0] in references


def test_sqlite_cache_discard_is_conditional_on_the_observed_reference(tmp_path: Path) -> None:
    path = tmp_path / "processor-results.sqlite3"
    first = artifact("urn:docspec:test:processor-result:first")
    replacement = artifact("urn:docspec:test:processor-result:replacement")
    cache = LocalSqliteProcessorResultCache(path)
    cache.put_if_absent("reuse-key", replacement)

    assert cache.discard("reuse-key", first) is False
    assert cache.lookup("reuse-key") == replacement
    assert cache.discard("reuse-key", replacement) is True
    assert cache.lookup("reuse-key") is None


def test_segment_input_digest_survives_physical_blob_relocation() -> None:
    source = b"same logical segment"
    extraction = TextExtractor().extract(_captured(source, "text/plain"), source)
    segment = ParagraphSegmenter().segment(extraction.payload)[0].segment
    relocated = replace(segment, content=replace(segment.content, locator="s3://bucket/relocated"))

    assert relocated.segment_id == segment.segment_id
    assert ProcessorRecordRef.for_segment(relocated) == ProcessorRecordRef.for_segment(segment)


def test_exact_result_cache_skips_reexecution_across_plan_identity(tmp_path: Path) -> None:
    parts = _pipeline(tmp_path)
    processor = _CountingProcessor()
    cache = LocalSqliteProcessorResultCache(tmp_path / "processor-results.sqlite3")
    first_plan = _plan(
        parts["source_ref"],
        None,
        processor,
        RetryPolicy(base_delay_milliseconds=0),
        AcceptedFailurePolicy(),
        buckets=8,
    )
    _, _, _, _, first_release = _run_pipeline(
        parts,
        plan=first_plan,
        processor=processor,
        processor_cache=cache,
    )
    assert processor.calls == 1

    second_plan = _changed_retention_plan(first_plan, first_release)
    _, processed, _, _, _ = _run_pipeline(
        parts,
        plan=second_plan,
        processor=processor,
        processor_cache=LocalSqliteProcessorResultCache(cache.path),
    )

    assert processor.calls == 1
    processor_receipts = _processor_receipts(parts, processed[0])
    assert [receipt["cacheDisposition"] for receipt in processor_receipts] == ["hit"]


def test_invalid_cache_reference_is_recomputed_and_repaired(tmp_path: Path) -> None:
    parts = _pipeline(tmp_path)
    processor = _CountingProcessor()
    first_plan = _plan(
        parts["source_ref"],
        None,
        processor,
        RetryPolicy(base_delay_milliseconds=0),
        AcceptedFailurePolicy(),
        buckets=8,
    )
    _, first_processed, _, _, first_release = _run_pipeline(
        parts,
        plan=first_plan,
        processor=processor,
        processor_cache=None,
    )
    request = _processor_receipts(parts, first_processed[0])[0]["request"]
    cache = LocalSqliteProcessorResultCache(tmp_path / "processor-results.sqlite3")
    cache.put_if_absent(request["reuseKey"], artifact("urn:docspec:test:missing-result"))

    second_plan = _changed_retention_plan(first_plan, first_release)
    _, second_processed, _, _, second_release = _run_pipeline(
        parts,
        plan=second_plan,
        processor=processor,
        processor_cache=cache,
    )
    assert processor.calls == 2
    assert _processor_receipts(parts, second_processed[0])[0]["cacheDisposition"] == "invalid"

    third_plan = _changed_retention_plan(second_plan, second_release, revision="3")
    _, third_processed, _, _, _ = _run_pipeline(
        parts,
        plan=third_plan,
        processor=processor,
        processor_cache=cache,
    )
    assert processor.calls == 2
    assert _processor_receipts(parts, third_processed[0])[0]["cacheDisposition"] == "hit"


def test_overlapping_processor_attempts_converge_on_the_cache_winner(tmp_path: Path) -> None:
    parts = _pipeline(tmp_path)
    processor = _CountingProcessor()
    first_plan = _plan(
        parts["source_ref"],
        None,
        processor,
        RetryPolicy(base_delay_milliseconds=0),
        AcceptedFailurePolicy(),
        buckets=8,
    )
    _, first_processed, _, _, first_release = _run_pipeline(
        parts,
        plan=first_plan,
        processor=processor,
        processor_cache=None,
    )
    first_receipt = _processor_receipts(parts, first_processed[0])[0]
    winner = ArtifactRef.from_dict(first_receipt["result"])

    second_plan = _changed_retention_plan(first_plan, first_release)
    _, second_processed, _, _, _ = _run_pipeline(
        parts,
        plan=second_plan,
        processor=processor,
        processor_cache=_RaceWinnerCache(winner),
    )
    second_receipt = _processor_receipts(parts, second_processed[0])[0]

    assert processor.calls == 2
    assert second_receipt["cacheDisposition"] == "hit"
    assert second_receipt["result"] == first_receipt["result"]


def test_cache_outage_falls_back_to_processor_execution(tmp_path: Path) -> None:
    parts = _pipeline(tmp_path)
    processor = _CountingProcessor()
    plan = _plan(
        parts["source_ref"],
        None,
        processor,
        RetryPolicy(base_delay_milliseconds=0),
        AcceptedFailurePolicy(),
        buckets=8,
    )

    _, processed, _, _, _ = _run_pipeline(
        parts,
        plan=plan,
        processor=processor,
        processor_cache=_UnavailableCache(),
    )

    assert processor.calls == 1
    assert _processor_receipts(parts, processed[0])[0]["cacheDisposition"] == "unavailable"
