from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.adapters.storage import (
    LocalDocumentStoreRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
    RootOnlyBlobProfileStateReachability,
)
from docspec.application.maintenance import BlobRetentionSetService, ReleaseCompactionService
from docspec.application.reconcile import RunReconciler
from docspec.domain.maintenance import BlobRetentionSet, ReleaseCompactionReceipt
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.receipts import RunReceipt
from docspec.domain.references import BlobRef, DocumentReleaseRef
from docspec.errors import IntegrityError, StaleBaseError
from docspec.processing.processors import ContentStatisticsProcessor
from tests.helpers import (
    SharedFixtureContentFetcher,
    document_release_producer,
    source_catalog_reader,
)
from tests.support.maintenance import (
    _Platform,
    _platform,
)
from tests.support.pipeline import _clock, _plan, _run


def _blob_references(platform: _Platform) -> set[tuple[str, str, int, str]]:
    release = platform.catalog.open(platform.release)
    references: set[tuple[str, str, int, str]] = set()
    for layer_kind, field in (
        ("files", "blob"),
        ("representations", "blob"),
        ("segments", "content"),
    ):
        for row in platform.catalog.scan(platform.release, layer_kind=layer_kind):
            reference = BlobRef.from_dict(row["payload"][field])
            references.add(
                (
                    reference.locator,
                    reference.digest,
                    reference.byte_size,
                    reference.media_type,
                )
            )
    assert release.blob_roots
    return references


def test_retention_set_is_derived_from_verified_release_store_and_profile_roots(tmp_path: Path) -> None:
    platform = _platform(tmp_path, document_count=2, member_bytes=64 * 1024)
    release = platform.catalog.open(platform.release)
    orphan = platform.blobs.put_if_absent((b"unreachable",), media_type="application/octet-stream")
    service = BlobRetentionSetService(
        controls=platform.controls,
        records=platform.records,
        stores=platform.stores,
        blobs=platform.blobs,
        document_catalog=platform.catalog,
        profile_state_reachability=RootOnlyBlobProfileStateReachability(),
        workspace_factory=LocalSqliteReconciliationWorkspaceFactory(tmp_path / "retention-workspace"),
        partition_policy=platform.partition_policy,
    )

    retained_ref = service.build(
        blob_profile_state=release.blob_roots[0],
        retained_releases=(platform.release,),
        retained_stores=(platform.sealed_stores[0],),
    )
    retained = BlobRetentionSet.from_dict(platform.controls.load(retained_ref))
    rows = tuple(platform.records.stream(retained.references))
    actual = {
        (row["locator"], row["digest"], row["byteSize"], row["mediaType"])
        for row in rows
    }

    assert retained.retained_releases == (platform.release,)
    assert retained.retained_stores == (platform.sealed_stores[0],)
    assert retained.blob_profile_state == release.blob_roots[0]
    assert actual == _blob_references(platform)
    assert orphan.locator not in {row["locator"] for row in rows}
    assert retained.references.record_count == len(rows)
    assert {row["blobProfileStateId"] for row in rows} == {release.blob_roots[0].artifact_id}
    assert {row["blobProfileStateDigest"] for row in rows} == {release.blob_roots[0].digest}
    assert retained.verification_evidence == {
        "profileStateVerificationCount": 1,
        "catalogVerifiedReleaseCount": 1,
        "visitedStoreRevisionCount": len(platform.sealed_stores),
        "activeBlobLayerScanCount": 3,
        "activeBlobRecordReadCount": 6,
        "blobReferenceOccurrenceCount": 12,
        "directBlobVerificationCount": 0,
        "retainedReferenceCount": len(rows),
        "boundedStreaming": True,
    }

    retained_blob = BlobRef.from_dict(next(platform.catalog.scan(platform.release, layer_kind="files"))["payload"]["blob"])
    retained_path = platform.blobs.root / retained_blob.locator
    payload = retained_path.read_bytes()
    retained_path.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
    with pytest.raises(IntegrityError, match="retained blob.*failed verification"):
        service.build(
            blob_profile_state=release.blob_roots[0],
            retained_releases=(platform.release,),
        )


def test_retention_set_rejects_mixed_blob_profile_roots(tmp_path: Path) -> None:
    platform = _platform(tmp_path, document_count=1, member_bytes=64 * 1024)
    release = platform.catalog.open(platform.release)
    state = platform.controls.load(release.blob_roots[0])
    unrelated_root = platform.controls.put(
        kind="profile-state",
        artifact_id="urn:docspec:test:unrelated-blob-profile-state",
        value={**state, "storageRoot": (tmp_path / "unrelated-blobs").as_posix()},
    )
    service = BlobRetentionSetService(
        controls=platform.controls,
        records=platform.records,
        stores=platform.stores,
        blobs=platform.blobs,
        document_catalog=platform.catalog,
        profile_state_reachability=RootOnlyBlobProfileStateReachability(),
        workspace_factory=LocalSqliteReconciliationWorkspaceFactory(tmp_path / "retention-workspace"),
        partition_policy=platform.partition_policy,
    )

    with pytest.raises(IntegrityError, match="mix distinct blob profile states"):
        service.build(
            blob_profile_state=unrelated_root,
            retained_releases=(platform.release,),
        )


def test_retention_set_rejects_conflicting_metadata_for_one_root_and_locator(tmp_path: Path) -> None:
    platform = _platform(tmp_path, document_count=1, member_bytes=64 * 1024)
    release = platform.catalog.open(platform.release)
    profile_state = release.blob_roots[0]
    reference = platform.blobs.put_if_absent((b"shared bytes",), media_type="text/plain")
    conflict = BlobRef(
        reference.locator,
        reference.digest,
        reference.byte_size,
        "application/octet-stream",
    )

    class ConflictingReachability:
        def references(self, _reference, _state):
            yield reference
            yield conflict

    service = BlobRetentionSetService(
        controls=platform.controls,
        records=platform.records,
        stores=platform.stores,
        blobs=platform.blobs,
        document_catalog=platform.catalog,
        profile_state_reachability=ConflictingReachability(),
        workspace_factory=LocalSqliteReconciliationWorkspaceFactory(tmp_path / "retention-workspace"),
        partition_policy=platform.partition_policy,
    )

    with pytest.raises(IntegrityError, match="root and locator have conflicting immutable metadata"):
        service.build(
            blob_profile_state=profile_state,
            retained_releases=(platform.release,),
        )


def _member_counts(records: LocalJsonlRecordStorage, reference: DocumentReleaseRef, catalog) -> dict[str, int]:
    release = catalog.open(reference)
    return {
        layer.layer_kind: len(json.loads((records.root / layer.state_ref).read_text())["members"])
        for layer in release.active_layers
    }


def _revision_files(stores: LocalDocumentStoreRepository) -> dict[str, bytes]:
    root = stores.root / "document-stores"
    if not root.exists():
        return {}
    return {
        path.relative_to(stores.root).as_posix(): path.read_bytes()
        for path in root.rglob("*.json")
    }


def _published_release_count(catalog: LocalManifestDocumentCatalog) -> int:
    return len(tuple((catalog.root / "document-catalog/releases").rglob("artifact.json")))


def _compaction_service(
    platform: _Platform,
    *,
    clock,
) -> tuple[ReleaseCompactionService, LocalJsonlRecordStorage, LocalManifestDocumentCatalog]:
    records = LocalJsonlRecordStorage(
        platform.records.root,
        max_member_bytes=1024 * 1024,
    )
    catalog = LocalManifestDocumentCatalog(
        platform.catalog.root,
        records=records,
        stores=platform.stores,
        controls=platform.controls,
        producer=document_release_producer(),
        blobs=platform.blobs,
    )
    return (
        ReleaseCompactionService(
            controls=platform.controls,
            records=records,
            stores=platform.stores,
            document_catalog=catalog,
            clock=clock,
        ),
        records,
        catalog,
    )


def test_compaction_commits_a_zero_task_successor_with_exact_logical_state(tmp_path: Path) -> None:
    platform = _platform(tmp_path, document_count=12, member_bytes=4 * 1024)
    source_counts = _member_counts(platform.records, platform.release, platform.catalog)
    assert any(count > 1 for count in source_counts.values())
    source_blobs = {
        path.relative_to(platform.blobs.root).as_posix(): path.read_bytes()
        for path in platform.blobs.root.rglob("*")
        if path.is_file()
    }
    source_revisions = _revision_files(platform.stores)

    service, compacted_records, compacting_catalog = _compaction_service(
        platform,
        clock=_clock,
    )
    receipt_ref = service.compact(platform.release)
    receipt = ReleaseCompactionReceipt.from_dict(platform.controls.load(receipt_ref))
    successor = compacting_catalog.open(receipt.successor_release)
    successor_counts = _member_counts(compacted_records, receipt.successor_release, compacting_catalog)

    assert compacting_catalog.current() == receipt.successor_release
    assert successor.previous_release == platform.release
    assert receipt.source_logical_state_digest == receipt.successor_logical_state_digest
    assert receipt.rewritten_layer_kinds
    assert any(
        successor_counts[kind] < source_counts[kind]
        for kind in receipt.rewritten_layer_kinds
    )
    for kind in (layer.layer_kind for layer in successor.active_layers):
        assert list(compacting_catalog.compare(platform.release, receipt.successor_release, layer_kind=kind)) == []

    run = RunReceipt.from_dict(platform.controls.load(successor.run_receipt))
    assert run.store_count == 0
    assert run.selected_item_count == 0
    assert run.counts["rewrittenLayers"] == len(receipt.rewritten_layer_kinds)
    logical_record_count = sum(layer.record_count for layer in successor.active_layers)
    assert receipt.verification_evidence == {
        "logicalRecordCount": logical_record_count,
        "logicalRecordReadCount": logical_record_count * 3,
    }
    assert _revision_files(platform.stores) == source_revisions
    assert {
        path.relative_to(platform.blobs.root).as_posix(): path.read_bytes()
        for path in platform.blobs.root.rglob("*")
        if path.is_file()
    } == source_blobs


def test_compaction_retry_recovers_the_successor_after_post_commit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = _platform(tmp_path, document_count=8, member_bytes=4 * 1024)
    completed_times = iter(("2026-08-05T13:00:00Z", "2026-08-05T14:00:00Z"))
    service, _, catalog = _compaction_service(platform, clock=completed_times.__next__)

    def fail_after_commit(successor: DocumentReleaseRef) -> None:
        assert catalog.current() == successor
        raise RuntimeError("controlled post-commit failure")

    monkeypatch.setattr(service, "_after_catalog_commit", fail_after_commit)
    with pytest.raises(RuntimeError, match="controlled post-commit failure"):
        service.compact(platform.release)

    committed_successor = catalog.current()
    assert committed_successor is not None
    assert committed_successor != platform.release
    assert _published_release_count(catalog) == 2

    monkeypatch.setattr(service, "_after_catalog_commit", lambda _successor: None)
    receipt_ref = service.compact(platform.release)
    receipt = ReleaseCompactionReceipt.from_dict(platform.controls.load(receipt_ref))

    assert receipt.successor_release == committed_successor
    assert receipt.completed_at == "2026-08-05T13:00:00Z"
    assert _published_release_count(catalog) == 2


def test_compaction_refuses_an_unrelated_verified_current_before_creating_run_state(tmp_path: Path) -> None:
    platform = _platform(tmp_path, document_count=8, member_bytes=4 * 1024)
    source = platform.catalog.open(platform.release)
    retry = RetryPolicy(base_delay_milliseconds=0)
    processor = ContentStatisticsProcessor(retry_policy=retry)
    plan = _plan(
        source.source_catalog, platform.release, processor, retry, AcceptedFailurePolicy(),
        buckets=platform.partition_policy.bucket_count, max_entries=4,
    )
    planned, processed, sealed, run_ref, ordinary_successor = _run(
        plan=plan, source_catalog=source_catalog_reader(tmp_path / "source-catalogs"),
        controls=platform.controls, stores=platform.stores, blobs=platform.blobs,
        records=platform.records, catalog=platform.catalog,
        fetcher=SharedFixtureContentFetcher(tmp_path / "sources"), processor=processor,
        partition_policy=platform.partition_policy,
    )
    successor = platform.catalog.open(ordinary_successor)
    run = RunReceipt.from_dict(platform.controls.load(run_ref))
    assert planned == processed == sealed == ()
    assert run.store_count == run.selected_item_count == 0
    assert run.blob_roots == successor.blob_roots == source.blob_roots
    assert run.counts["capturedFiles"] == run.counts["deliveredRecords"] == run.counts["deliveredBytes"] == 0
    assert successor.previous_release == platform.release
    assert successor.active_layers == source.active_layers
    assert successor.counts == source.counts
    for layer in source.active_layers:
        assert list(platform.catalog.compare(platform.release, ordinary_successor, layer_kind=layer.layer_kind)) == []
    assert any(count > 1 for count in _member_counts(platform.records, ordinary_successor, platform.catalog).values())
    stateless_ref = RunReconciler(
        plan_ref=run.plan,
        execution_profile_ref=run.execution_profile,
        execution_handoff_ref=run.execution_handoff,
        source_catalog_ref=source.source_catalog,
        base_release_ref=platform.release,
        controls=platform.controls,
        stores=platform.stores,
        records=platform.records,
        document_catalog=platform.catalog,
        source_catalog=source_catalog_reader(tmp_path / "source-catalogs"),
        workspace_factory=LocalSqliteReconciliationWorkspaceFactory(tmp_path / "stateless-reconciliation"),
        partition_policy=platform.partition_policy,
        clock=_clock,
        stateful=False,
    ).reconcile_run(())
    stateless = RunReceipt.from_dict(platform.controls.load(stateless_ref))
    assert not stateless.stateful
    assert stateless.staged_layers == stateless.blob_roots == ()
    assert stateless.counts == run.counts
    controls_before = {
        path.relative_to(platform.controls.root): path.read_bytes()
        for path in platform.controls.root.rglob("*") if path.is_file()
    }
    stores_before = _revision_files(platform.stores)
    release_count_before = _published_release_count(platform.catalog)
    service, _, catalog = _compaction_service(platform, clock=_clock)

    with pytest.raises(StaleBaseError, match="not the intended equivalent compaction successor"):
        service.compact(platform.release)

    assert catalog.current() == ordinary_successor
    assert _published_release_count(catalog) == release_count_before
    assert _revision_files(platform.stores) == stores_before
    assert {
        path.relative_to(platform.controls.root): path.read_bytes()
        for path in platform.controls.root.rglob("*") if path.is_file()
    } == controls_before


def test_concurrent_compactions_converge_on_one_successor_and_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform = _platform(tmp_path, document_count=8, member_bytes=4 * 1024)
    first, _, catalog = _compaction_service(
        platform,
        clock=lambda: "2026-08-05T13:00:00Z",
    )
    second, _, _ = _compaction_service(
        platform,
        clock=lambda: "2026-08-05T14:00:00Z",
    )
    first_committed = Event()
    release_first = Event()

    def pause_after_commit(_successor: DocumentReleaseRef) -> None:
        first_committed.set()
        assert release_first.wait(timeout=20)

    monkeypatch.setattr(first, "_after_catalog_commit", pause_after_commit)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first.compact, platform.release)
        assert first_committed.wait(timeout=20)
        second_future = executor.submit(second.compact, platform.release)
        try:
            second_receipt_ref = second_future.result(timeout=30)
        finally:
            release_first.set()
        first_receipt_ref = first_future.result(timeout=30)

    assert first_receipt_ref == second_receipt_ref
    receipt = ReleaseCompactionReceipt.from_dict(platform.controls.load(first_receipt_ref))
    assert catalog.current() == receipt.successor_release
    assert receipt.completed_at == "2026-08-05T13:00:00Z"
    assert _published_release_count(catalog) == 2
