from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import pytest

from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
    RootOnlyBlobProfileStateReachability,
)
from docspec.application.maintenance import BlobRetentionSetService, ReleaseCompactionService
from docspec.domain.content import SourceItem
from docspec.domain.maintenance import BlobRetentionSet, ReleaseCompactionReceipt
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.receipts import RunReceipt
from docspec.domain.references import BlobRef, DocumentReleaseRef, StoreRef
from docspec.domain.storage import PartitionPolicy
from docspec.errors import IntegrityError
from docspec.processing.processors import ContentStatisticsProcessor
from tests.test_application_pipeline import _clock, _plan, _run, _write_source
from tests.helpers import (
    SharedFixtureContentFetcher,
    document_release_producer,
    source_catalog_reader,
    write_shared_source_catalog,
)


@dataclass(frozen=True)
class _Platform:
    controls: LocalJsonControlRepository
    stores: LocalDocumentStoreRepository
    blobs: LocalContentAddressedBlobStore
    records: LocalJsonlRecordStorage
    catalog: LocalManifestDocumentCatalog
    partition_policy: PartitionPolicy
    sealed_stores: tuple[StoreRef, ...]
    release: DocumentReleaseRef


def _platform(tmp_path: Path, *, document_count: int, member_bytes: int) -> _Platform:
    sources = tmp_path / "sources"
    sources.mkdir()
    source_catalog_root = tmp_path / "source-catalogs"
    source_catalog_root.mkdir()
    source_catalog = source_catalog_reader(source_catalog_root)
    controls = LocalJsonControlRepository(tmp_path / "controls")
    stores = LocalDocumentStoreRepository(tmp_path / "stores")
    blobs = LocalContentAddressedBlobStore(tmp_path / "blobs")
    records = LocalJsonlRecordStorage(tmp_path / "records", max_member_bytes=member_bytes)
    partition_policy = PartitionPolicy("source-item-sha256-v1", 1)
    catalog = LocalManifestDocumentCatalog(
        tmp_path / "document-catalog",
        records=records,
        stores=stores,
        controls=controls,
        producer=document_release_producer(),
        blobs=blobs,
    )
    items = tuple(
        SourceItem(
            f"document-{index:04d}",
            "v1",
            (
                _write_source(
                    sources / f"document-{index:04d}.txt",
                    f"Document {index} has independently retained source content.",
                ),
            ),
        )
        for index in range(document_count)
    )
    source = write_shared_source_catalog(source_catalog_root, items)
    retry = RetryPolicy(base_delay_milliseconds=0)
    processor = ContentStatisticsProcessor(retry_policy=retry)
    plan = _plan(
        source,
        None,
        processor,
        retry,
        AcceptedFailurePolicy(),
        buckets=partition_policy.bucket_count,
        max_entries=4,
    )
    _, _, sealed, _, release = _run(
        plan=plan,
        source_catalog=source_catalog,
        controls=controls,
        stores=stores,
        blobs=blobs,
        records=records,
        catalog=catalog,
        fetcher=SharedFixtureContentFetcher(sources),
        processor=processor,
        partition_policy=partition_policy,
    )
    return _Platform(controls, stores, blobs, records, catalog, partition_policy, sealed, release)


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
        "logicalScanPassCount": 3,
        "explicitCatalogOpenCount": 2,
        "boundedStreaming": True,
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
