from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docspec.adapters.source_catalog import LocalFileContentFetcher, LocalJsonlSourceCatalog
from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.application.maintenance import ReleaseCompactionService, logical_release_state_digest
from docspec.domain.content import SourceItem
from docspec.domain.identity import canonical_json_bytes
from docspec.domain.jobs import ChangeKind, EntryExecutionMode
from docspec.domain.maintenance import ReleaseCompactionReceipt
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.references import DocumentReleaseRef
from docspec.domain.storage import PartitionPolicy

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_pipeline_helpers = importlib.import_module("tests.test_application_pipeline")
_processor_helpers = importlib.import_module("tests.test_processor_reprocessing")
_run = _pipeline_helpers._run
_write_source = _pipeline_helpers._write_source
_CountingExtractor = _processor_helpers._CountingExtractor
_CountingFetcher = _processor_helpers._CountingFetcher
_CountingProcessor = _processor_helpers._CountingProcessor
_CountingSegmenter = _processor_helpers._CountingSegmenter
_description = _processor_helpers._description
_plan = _processor_helpers._plan

_CORE_STATE_LAYERS = ("source-items", "files", "representations", "segments")


@dataclass(frozen=True)
class _Platform:
    sources: Path
    source_catalog: LocalJsonlSourceCatalog
    controls: LocalJsonControlRepository
    stores: LocalDocumentStoreRepository
    blobs: LocalContentAddressedBlobStore
    records: LocalJsonlRecordStorage
    catalog: LocalManifestDocumentCatalog
    partition_policy: PartitionPolicy


def _platform(root: Path, *, member_bytes: int) -> _Platform:
    sources = root / "sources"
    sources.mkdir(parents=True)
    source_catalog = LocalJsonlSourceCatalog(root / "source-catalogs")
    controls = LocalJsonControlRepository(root / "controls")
    stores = LocalDocumentStoreRepository(root / "stores")
    blobs = LocalContentAddressedBlobStore(root / "blobs")
    records = LocalJsonlRecordStorage(root / "records", max_member_bytes=member_bytes)
    partition_policy = PartitionPolicy("source-item-sha256-v1", 8)
    catalog = LocalManifestDocumentCatalog(
        root / "document-catalog",
        records=records,
        stores=stores,
        controls=controls,
        blobs=blobs,
    )
    return _Platform(
        sources,
        source_catalog,
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


def test_clean_incremental_targeted_and_compacted_paths_converge_on_active_document_state(
    tmp_path: Path,
) -> None:
    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()

    evolving = _platform(tmp_path / "evolving", member_bytes=2 * 1024)
    first_a = _write_source(
        evolving.sources / "a.txt",
        "Alpha before the update in its initial paragraph.",
    )
    first_b = _write_source(
        evolving.sources / "b.txt",
        "Bravo remains stable in its only paragraph.",
    )
    first_source = evolving.source_catalog.write(
        (
            SourceItem("document-a", "v1", (first_a,), metadata={"expectedSegments": 1}),
            SourceItem("document-b", "v1", (first_b,), metadata={"expectedSegments": 1}),
        )
    )
    initial_processor = _CountingProcessor(_description("equivalence", "1", retry))
    initial_plan = _plan(first_source, None, (initial_processor,), retry, accepted)
    _, _, _, _, initial_release = _run(
        plan=initial_plan,
        source_catalog=evolving.source_catalog,
        controls=evolving.controls,
        stores=evolving.stores,
        blobs=evolving.blobs,
        records=evolving.records,
        catalog=evolving.catalog,
        fetcher=LocalFileContentFetcher(evolving.sources),
        processors=(initial_processor,),
        partition_policy=evolving.partition_policy,
    )

    final_a = _write_source(
        evolving.sources / "a.txt",
        "Alpha after the update in its final paragraph.",
    )
    final_c = _write_source(
        evolving.sources / "c.txt",
        "Charlie is newly added with one final paragraph.",
    )
    final_items = (
        SourceItem("document-a", "v2", (final_a,), metadata={"expectedSegments": 1}),
        SourceItem("document-b", "v1", (first_b,), metadata={"expectedSegments": 1}),
        SourceItem("document-c", "v1", (final_c,), metadata={"expectedSegments": 1}),
    )
    final_source = evolving.source_catalog.write(final_items)
    final_processor = _CountingProcessor(_description("equivalence", "2", retry))
    fetcher = _CountingFetcher(LocalFileContentFetcher(evolving.sources))
    extractor = _CountingExtractor()
    segmenter = _CountingSegmenter()
    incremental_plan = _plan(final_source, initial_release, (final_processor,), retry, accepted)
    planned, _, _, _, incremental_release = _run(
        plan=incremental_plan,
        source_catalog=evolving.source_catalog,
        controls=evolving.controls,
        stores=evolving.stores,
        blobs=evolving.blobs,
        records=evolving.records,
        catalog=evolving.catalog,
        fetcher=fetcher,
        processors=(final_processor,),
        extractor=extractor,
        segmenter=segmenter,
        partition_policy=evolving.partition_policy,
    )

    entries = {
        entry.source_item.item_id: entry
        for reference in planned
        for entry in evolving.stores.load(reference).entries
    }
    assert (entries["document-a"].change, entries["document-a"].execution_mode) == (
        ChangeKind.CHANGED,
        EntryExecutionMode.FULL,
    )
    assert (entries["document-b"].change, entries["document-b"].execution_mode) == (
        ChangeKind.REPAIR,
        EntryExecutionMode.PROCESSORS_ONLY,
    )
    assert (entries["document-c"].change, entries["document-c"].execution_mode) == (
        ChangeKind.ADDED,
        EntryExecutionMode.FULL,
    )
    assert (len(fetcher.calls), extractor.calls, segmenter.calls) == (2, 2, 2)

    clean = _platform(tmp_path / "clean", member_bytes=1024 * 1024)
    clean_items = (
        SourceItem(
            "document-a",
            "v2",
            (
                _write_source(
                    clean.sources / "a.txt",
                    "Alpha after the update in its final paragraph.",
                ),
            ),
            metadata={"expectedSegments": 1},
        ),
        SourceItem(
            "document-b",
            "v1",
            (
                _write_source(
                    clean.sources / "b.txt",
                    "Bravo remains stable in its only paragraph.",
                ),
            ),
            metadata={"expectedSegments": 1},
        ),
        SourceItem(
            "document-c",
            "v1",
            (
                _write_source(
                    clean.sources / "c.txt",
                    "Charlie is newly added with one final paragraph.",
                ),
            ),
            metadata={"expectedSegments": 1},
        ),
    )
    clean_source = clean.source_catalog.write(clean_items)
    clean_processor = _CountingProcessor(_description("equivalence", "2", retry))
    clean_plan = _plan(clean_source, None, (clean_processor,), retry, accepted)
    _, _, _, _, clean_release = _run(
        plan=clean_plan,
        source_catalog=clean.source_catalog,
        controls=clean.controls,
        stores=clean.stores,
        blobs=clean.blobs,
        records=clean.records,
        catalog=clean.catalog,
        fetcher=LocalFileContentFetcher(clean.sources),
        processors=(clean_processor,),
        partition_policy=clean.partition_policy,
    )

    compacted_records = LocalJsonlRecordStorage(
        evolving.records.root,
        max_member_bytes=1024 * 1024,
    )
    compacting_catalog = LocalManifestDocumentCatalog(
        evolving.catalog.root,
        records=compacted_records,
        stores=evolving.stores,
        controls=evolving.controls,
        blobs=evolving.blobs,
    )
    receipt_ref = ReleaseCompactionService(
        controls=evolving.controls,
        records=compacted_records,
        stores=evolving.stores,
        document_catalog=compacting_catalog,
        clock=lambda: "2026-08-05T13:00:00Z",
    ).compact(incremental_release)
    receipt = ReleaseCompactionReceipt.from_dict(evolving.controls.load(receipt_ref))
    compacted_release = receipt.successor_release

    clean_state = _active_document_state(clean.catalog, clean_release)
    incremental_state = _active_document_state(compacting_catalog, incremental_release)
    compacted_state = _active_document_state(compacting_catalog, compacted_release)
    assert clean_state == incremental_state == compacted_state

    incremental = compacting_catalog.open(incremental_release)
    compacted = compacting_catalog.open(compacted_release)
    assert receipt.source_logical_state_digest == receipt.successor_logical_state_digest
    assert logical_release_state_digest(compacted_records, incremental) == logical_release_state_digest(
        compacted_records,
        compacted,
    )
    assert all(
        list(compacting_catalog.compare(incremental_release, compacted_release, layer_kind=layer.layer_kind)) == []
        for layer in incremental.active_layers
    )
