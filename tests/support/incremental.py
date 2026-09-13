"""Shared incremental fixtures, extracted from tests.conformance.test_incremental_equivalence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docspec.adapters.catalog_artifact.reader import (
    SourceCatalogArtifactReader,
)
from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalParquetRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.domain.content import SourceItem
from docspec.domain.identity import canonical_json_bytes
from docspec.domain.references import DocumentReleaseRef
from docspec.domain.storage import PartitionPolicy
from tests import helpers as _helpers

write_shared_source_catalog = _helpers.write_shared_source_catalog

source_catalog_reader = _helpers.source_catalog_reader

document_release_producer = _helpers.document_release_producer

_CORE_STATE_LAYERS = ("source-items", "files", "representations", "segments")


@dataclass(frozen=True)
class _Platform:
    sources: Path
    source_catalog: SourceCatalogArtifactReader
    source_catalog_root: Path
    controls: LocalJsonControlRepository
    stores: LocalDocumentStoreRepository
    blobs: LocalContentAddressedBlobStore
    records: LocalParquetRecordStorage
    catalog: LocalManifestDocumentCatalog
    partition_policy: PartitionPolicy

    def publish_source(self, items: tuple[SourceItem, ...], *, name: str = "catalog"):
        return write_shared_source_catalog(self.source_catalog_root, items, name=name)


def _platform(root: Path, *, member_bytes: int) -> _Platform:
    sources = root / "sources"
    sources.mkdir(parents=True)
    source_catalog_root = root / "source-catalogs"
    source_catalog_root.mkdir()
    source_catalog = source_catalog_reader(source_catalog_root)
    controls = LocalJsonControlRepository(root / "controls")
    stores = LocalDocumentStoreRepository(root / "stores")
    blobs = LocalContentAddressedBlobStore(root / "blobs")
    records = LocalParquetRecordStorage(root / "records", max_member_bytes=member_bytes)
    partition_policy = PartitionPolicy("source-item-sha256-v1", 8)
    catalog = LocalManifestDocumentCatalog(
        root / "document-catalog",
        records=records,
        stores=stores,
        controls=controls,
        producer=document_release_producer(),
        blobs=blobs,
    )
    return _Platform(
        sources,
        source_catalog,
        source_catalog_root,
        controls,
        stores,
        blobs,
        records,
        catalog,
        partition_policy,
    )


def _active_document_state(
    catalog: LocalManifestDocumentCatalog,
    release_ref: DocumentReleaseRef,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Project exact active document facts without run-specific execution evidence."""

    release = catalog.open(release_ref)
    layer_kinds = {
        layer.layer_kind
        for layer in release.active_layers
        if layer.layer_kind in _CORE_STATE_LAYERS or layer.layer_kind.startswith("derived:")
    }
    state: dict[str, tuple[dict[str, Any], ...]] = {}
    for layer_kind in sorted(layer_kinds):
        projected = []
        for row in catalog.scan(release_ref, layer_kind=layer_kind):
            payload = dict(row["payload"])
            record_id = row["recordId"]
            if layer_kind == "files":
                for field in (
                    "acquiredAt",
                    "acquisitionStartedAt",
                    "attemptId",
                    "downloaderConfigurationDigest",
                    "taskId",
                ):
                    payload.pop(field)
            elif layer_kind.startswith("derived:"):
                payload.pop("derivedId")
                payload.pop("providerReceiptDigest")
                record_id = payload["outputDigest"]
            projected.append(
                {
                    "recordId": record_id,
                    "sourceItemId": row["sourceItemId"],
                    "deleted": row["deleted"],
                    "payload": payload,
                }
            )
        state[layer_kind] = tuple(sorted(projected, key=canonical_json_bytes))
    return state
