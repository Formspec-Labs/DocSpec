"""Shared maintenance fixtures, extracted from tests.test_maintenance."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.domain.content import SourceItem
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.references import DocumentReleaseRef, StoreRef
from docspec.domain.storage import PartitionPolicy
from docspec.processing.processors import ContentStatisticsProcessor
from tests.helpers import (
    SharedFixtureContentFetcher,
    document_release_producer,
    source_catalog_reader,
    write_shared_source_catalog,
)
from tests.support.pipeline import _plan, _run, _write_source


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
