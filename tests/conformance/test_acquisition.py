from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters.source_catalog import LocalFileContentFetcher
from docspec.domain.content import AcquisitionDisposition, CandidateFile, SourceItem
from docspec.domain.identity import sha256_digest
from docspec.domain.jobs import FailureClass, StoreVerdict
from docspec.domain.plans import WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.receipts import RunReceipt
from docspec.domain.references import StoreRef
from docspec.errors import LimitExceededError, StateTransitionError
from docspec.ports.content_fetcher import FetchStream

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_equivalence = importlib.import_module("tests.conformance.test_incremental_equivalence")
_document_store = importlib.import_module("tests.conformance.test_document_store")
_pipeline_helpers = importlib.import_module("tests.test_application_pipeline")
_processor_helpers = importlib.import_module("tests.test_processor_reprocessing")
_platform = _equivalence._platform
_reconciled_counts = _document_store._reconciled_counts
_run = _pipeline_helpers._run
_write_source = _pipeline_helpers._write_source
_CountingProcessor = _processor_helpers._CountingProcessor
_description = _processor_helpers._description
_plan = _processor_helpers._plan


class _FailFirstFetchFetcher:
    """Delegate to the real fetcher, losing one candidate's first transport."""

    def __init__(self, delegate: LocalFileContentFetcher, *, flaky_locator: str) -> None:
        self.delegate = delegate
        self.flaky_locator = flaky_locator
        self.calls: dict[str, int] = {}

    def fetch(self, candidate: CandidateFile, **kwargs: Any) -> FetchStream:
        count = self.calls.get(candidate.locator, 0) + 1
        self.calls[candidate.locator] = count
        stream = self.delegate.fetch(candidate, **kwargs)
        if candidate.locator != self.flaky_locator or count != 1:
            return stream

        def interrupted() -> Any:
            yield from stream.chunks
            raise ConnectionError("transport ended before verification")

        return FetchStream(stream.metadata, interrupted())


def _bounded_plan(source, base, processors, retry, accepted, *, limits: WorkLimits):
    """The shared plan shape with explicit per-store work limits."""

    shaped = _plan(source, base, processors, retry, accepted)
    return type(shaped).create(
        source_catalog=shaped.source_catalog,
        base_release=shaped.base_release,
        profiles=shaped.profiles,
        limits=limits,
        stages=shaped.stages,
        processors=shaped.processors,
        partition_count=shaped.partition_count,
        selection=shaped.selection,
        retention_policy=shaped.retention_policy,
        data_use_policy=shaped.data_use_policy,
        retry_policy_digest=shaped.retry_policy_digest,
        accepted_failure_policy_digest=shaped.accepted_failure_policy_digest,
    )


def _seeded_items(platform, texts: dict[str, str]) -> tuple[SourceItem, ...]:
    items = []
    for item_id in sorted(texts):
        candidate = _write_source(platform.sources / f"{item_id}.txt", texts[item_id])
        items.append(SourceItem(item_id, "v1", (candidate,), metadata={"expectedSegments": 1}))
    return tuple(items)


def _sealed_entries(platform, sealed) -> dict[str, Any]:
    entries = {}
    for reference in sealed:
        for entry in platform.stores.load(reference).entries:
            entries[entry.source_item.item_id] = entry
    return entries


def _physical_objects(platform) -> list[Path]:
    objects = platform.blobs.root / "objects"
    if not objects.exists():
        return []
    return sorted(path for path in objects.rglob("*") if path.is_file())


def test_capture_deduplication_and_deletion_reconcile_across_releases(tmp_path: Path) -> None:
    """A first run captures one unique and two byte-identical documents with
    complete receipts and one shared physical object; an incremental run then
    deletes the unique item without fetching anything, tombstones it in the
    new release, and leaves the base release's history intact."""

    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    processor = _CountingProcessor(_description("acquisition", "1", retry))
    platform = _platform(tmp_path, member_bytes=1024 * 1024)
    shared_text = "Twin documents share these exact bytes."
    texts = {
        "doc-solo": "Only this document holds these bytes.",
        "doc-twin-a": shared_text,
        "doc-twin-b": shared_text,
    }
    items = _seeded_items(platform, texts)
    source_v1 = platform.source_catalog.write(items)
    fetcher = LocalFileContentFetcher(platform.sources)
    _, _, sealed, run_ref, release_v1 = _run(
        plan=_plan(source_v1, None, (processor,), retry, accepted),
        source_catalog=platform.source_catalog,
        controls=platform.controls,
        stores=platform.stores,
        blobs=platform.blobs,
        records=platform.records,
        catalog=platform.catalog,
        fetcher=fetcher,
        processors=(processor,),
        partition_policy=platform.partition_policy,
    )

    entries = _sealed_entries(platform, sealed)
    assert set(entries) == set(texts)
    for item_id, entry in entries.items():
        assert entry.disposition is AcquisitionDisposition.CAPTURED
        (captured,) = entry.captured_files
        payload = texts[item_id].encode("utf-8")
        assert captured.blob.digest == sha256_digest(payload)
        assert captured.blob.byte_size == len(payload), "declared and actual byte size must agree"
        assert captured.media_type == "text/plain"
        assert captured.downloader_id == LocalFileContentFetcher.downloader_id
        assert captured.downloader_configuration_digest, "the receipt pins the downloader configuration"
        assert captured.transport_version, "the receipt preserves the transport version"
        assert captured.acquisition_started_at and captured.acquired_at
        assert captured.task_id and captured.attempt_id, "the receipt pins task and attempt identities"

    twin_blob = entries["doc-twin-a"].captured_files[0].blob
    assert entries["doc-twin-b"].captured_files[0].blob.locator == twin_blob.locator
    assert entries["doc-solo"].captured_files[0].blob.locator != twin_blob.locator
    assert len(_physical_objects(platform)) == 2, "identical bytes share one physical object"

    file_rows = list(platform.catalog.scan(release_v1, layer_kind="files"))
    assert len(file_rows) == 3, "every logical source item appears in the durable file layer"
    twin_locators = {
        row["payload"]["blob"]["locator"]
        for row in file_rows
        if row["payload"]["sourceItemId"].startswith("doc-twin")
    }
    assert twin_locators == {twin_blob.locator}

    dispositions = {
        row["payload"]["entryId"]: row["payload"]["disposition"]
        for row in platform.catalog.scan(release_v1, layer_kind="dispositions")
    }
    assert dispositions == {entry.entry_id: "captured" for entry in entries.values()}

    run = RunReceipt.from_dict(platform.controls.load(run_ref))
    assert dict(run.counts) == _reconciled_counts(platform, run)
    assert run.counts["capturedFiles"] == 3

    survivors = tuple(item for item in items if item.item_id != "doc-solo")
    source_v2 = platform.source_catalog.write(survivors)
    planned_v2, _, sealed_v2, run_v2_ref, release_v2 = _run(
        plan=_plan(source_v2, release_v1, (processor,), retry, accepted),
        source_catalog=platform.source_catalog,
        controls=platform.controls,
        stores=platform.stores,
        blobs=platform.blobs,
        records=platform.records,
        catalog=platform.catalog,
        fetcher=fetcher,
        processors=(processor,),
        partition_policy=platform.partition_policy,
    )

    deletion_entries = [entry for reference in planned_v2 for entry in platform.stores.load(reference).entries]
    assert [(entry.source_item.item_id, entry.change.value) for entry in deletion_entries] == [("doc-solo", "deleted")]
    (deleted_entry,) = _sealed_entries(platform, sealed_v2).values()
    assert deleted_entry.disposition is AcquisitionDisposition.DELETED
    assert deleted_entry.captured_files == (), "a deletion fetches nothing"

    run_v2 = RunReceipt.from_dict(platform.controls.load(run_v2_ref))
    assert dict(run_v2.counts) == _reconciled_counts(platform, run_v2)
    assert run_v2.counts["capturedFiles"] == 0

    assert len(list(platform.catalog.scan(release_v2, layer_kind="files"))) == 2
    tombstone = platform.catalog.lookup(release_v2, layer_kind="source-items", record_id="doc-solo")
    assert tombstone["deleted"] is True
    assert len(list(platform.catalog.scan(release_v1, layer_kind="files"))) == 3, (
        "a deletion must not erase prior immutable history"
    )


def test_a_transport_retry_reconciles_into_a_published_capture(tmp_path: Path) -> None:
    """A capture that loses its first transport retries into exact verified
    bytes, retains the complete retry history, and publishes a release that
    carries the reconciled failure evidence."""

    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    processor = _CountingProcessor(_description("acquisition-retry", "1", retry))
    platform = _platform(tmp_path, member_bytes=1024 * 1024)
    text = "This capture survives one transport loss."
    items = _seeded_items(platform, {"doc-retry": text})
    source = platform.source_catalog.write(items)
    fetcher = _FailFirstFetchFetcher(
        LocalFileContentFetcher(platform.sources),
        flaky_locator="doc-retry.txt",
    )
    _, _, sealed, run_ref, release_ref = _run(
        plan=_plan(source, None, (processor,), retry, accepted),
        source_catalog=platform.source_catalog,
        controls=platform.controls,
        stores=platform.stores,
        blobs=platform.blobs,
        records=platform.records,
        catalog=platform.catalog,
        fetcher=fetcher,
        processors=(processor,),
        partition_policy=platform.partition_policy,
    )

    (retried,) = _sealed_entries(platform, sealed).values()
    assert retried.disposition is AcquisitionDisposition.CAPTURED
    assert fetcher.calls["doc-retry.txt"] == 2
    assert [failure.failure_class for failure in retried.failures] == [FailureClass.TRANSIENT_EXTERNAL]
    assert retried.failures[0].retryable is True
    assert retried.failures[0].attempt == 1
    (captured,) = retried.captured_files
    assert captured.blob.digest == sha256_digest(text.encode("utf-8"))
    (store,) = (platform.stores.load(reference) for reference in sealed)
    assert store.verdict is StoreVerdict.COMPLETED

    run = RunReceipt.from_dict(platform.controls.load(run_ref))
    assert dict(run.counts) == _reconciled_counts(platform, run)
    assert run.failures["counts"] == {FailureClass.TRANSIENT_EXTERNAL.value: 1}
    release = platform.catalog.open(release_ref)
    assert release.failures["counts"] == run.failures["counts"], (
        "the release must carry the reconciled retry evidence"
    )


def test_drifted_source_bytes_fail_closed_without_a_stored_object(tmp_path: Path) -> None:
    """A source whose bytes drifted from their declared identity is an
    integrity failure no policy may accept: the store seals rejected with the
    failure retained, nothing reaches the blob store, the run receipt still
    reconciles, and publication refuses."""

    with pytest.raises(ValueError, match="integrity failures.*cannot be accepted"):
        AcceptedFailurePolicy((FailureClass.ARTIFACT_INTEGRITY,))

    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    processor = _CountingProcessor(_description("acquisition-drift", "1", retry))
    platform = _platform(tmp_path, member_bytes=1024 * 1024)
    items = _seeded_items(platform, {"doc-drift": "The catalog declared these bytes."})
    source = platform.source_catalog.write(items)
    (platform.sources / "doc-drift.txt").write_text(
        "The source grew past its declared identity after cataloging.", encoding="utf-8"
    )
    with pytest.raises(StateTransitionError, match="rejected stores cannot publish"):
        _run(
            plan=_plan(source, None, (processor,), retry, accepted),
            source_catalog=platform.source_catalog,
            controls=platform.controls,
            stores=platform.stores,
            blobs=platform.blobs,
            records=platform.records,
            catalog=platform.catalog,
            fetcher=LocalFileContentFetcher(platform.sources),
            processors=(processor,),
            partition_policy=platform.partition_policy,
        )
    assert platform.catalog.current() is None, "a rejected run must not advance the catalog"
    assert _physical_objects(platform) == [], "a failed capture stores no object"

    receipts = sorted((platform.controls.root / "control" / "run-receipts").rglob("*.json"))
    assert len(receipts) == 1, "reconciliation must still seal one complete run receipt"
    run = RunReceipt.from_dict(json.loads(receipts[0].read_text(encoding="utf-8"))["value"])
    assert dict(run.counts) == _reconciled_counts(platform, run)
    assert run.counts["rejectedStores"] == 1
    assert run.counts["capturedFiles"] == 0
    assert run.failures["counts"] == {FailureClass.ARTIFACT_INTEGRITY.value: 1}

    (row,) = platform.records.stream(run.store_ledger)
    store = platform.stores.load(StoreRef.from_dict(row["store"]))
    assert store.verdict is StoreVerdict.REJECTED
    (drifted,) = store.entries
    assert drifted.disposition is AcquisitionDisposition.REJECTED_RUN
    assert drifted.captured_files == ()
    assert drifted.failures[-1].failure_class is FailureClass.ARTIFACT_INTEGRITY
    assert drifted.failures[-1].retryable is False
    assert drifted.failures[-1].attempt == 1, "an integrity mismatch is not retried"


def test_an_oversized_declared_candidate_refuses_planning_before_any_io(tmp_path: Path) -> None:
    """A source item honestly declaring more bytes than the per-store budget
    fails the plan before acquisition streams anything."""

    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    processor = _CountingProcessor(_description("acquisition-bounds", "1", retry))
    platform = _platform(tmp_path, member_bytes=1024 * 1024)
    items = _seeded_items(platform, {"doc-oversize": "These declared bytes exceed the configured store budget."})
    source = platform.source_catalog.write(items)
    limits = WorkLimits(2, 32, 100, 100, 1000, 1024 * 1024, 60, retry.max_attempts)

    with pytest.raises(LimitExceededError, match="per-store"):
        _run(
            plan=_bounded_plan(source, None, (processor,), retry, accepted, limits=limits),
            source_catalog=platform.source_catalog,
            controls=platform.controls,
            stores=platform.stores,
            blobs=platform.blobs,
            records=platform.records,
            catalog=platform.catalog,
            fetcher=LocalFileContentFetcher(platform.sources),
            processors=(processor,),
            partition_policy=platform.partition_policy,
        )

    assert _physical_objects(platform) == [], "refused planning must not stream source bytes"
    assert platform.catalog.current() is None, "refused planning must not advance the catalog"
