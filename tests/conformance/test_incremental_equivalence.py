from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.adapters.source_catalog_artifact import SourceCatalogArtifactReader
from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.application.maintenance import ReleaseCompactionService, logical_release_state_digest
from docspec.application.planner import RunPlanner
from docspec.domain.content import AcquisitionDisposition, SourceItem
from docspec.domain.identity import canonical_json_bytes
from docspec.domain.jobs import ChangeKind, EntryExecutionMode, FailureClass
from docspec.domain.maintenance import ReleaseCompactionReceipt
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.references import DocumentReleaseRef
from docspec.domain.storage import PartitionPolicy

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_helpers = importlib.import_module("tests.helpers")
SharedFixtureContentFetcher = _helpers.SharedFixtureContentFetcher
write_shared_source_catalog = _helpers.write_shared_source_catalog
source_catalog_reader = _helpers.source_catalog_reader
document_release_producer = _helpers.document_release_producer
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
# tests.conformance.test_document_store imports this module at collection time
# (for _platform and friends), so importing it back at module level here would
# be circular; import it lazily inside the test that needs _FailingProcessor.

_CORE_STATE_LAYERS = ("source-items", "files", "representations", "segments")


def _targeted_plan(
    source,
    base,
    processors,
    retry: RetryPolicy,
    accepted: AcceptedFailurePolicy,
    *,
    selection: dict[str, Any],
):
    """The shared plan shape with an explicit targeted selection."""

    untargeted = _plan(source, base, processors, retry, accepted)
    return type(untargeted).create(
        source_catalog=untargeted.source_catalog,
        base_release=untargeted.base_release,
        profiles=untargeted.profiles,
        limits=untargeted.limits,
        stages=untargeted.stages,
        processors=untargeted.processors,
        partition_count=untargeted.partition_count,
        selection=selection,
        retention_policy=untargeted.retention_policy,
        data_use_policy=untargeted.data_use_policy,
        retry_policy_digest=untargeted.retry_policy_digest,
        accepted_failure_policy_digest=untargeted.accepted_failure_policy_digest,
    )


@dataclass(frozen=True)
class _Platform:
    sources: Path
    source_catalog: SourceCatalogArtifactReader
    source_catalog_root: Path
    controls: LocalJsonControlRepository
    stores: LocalDocumentStoreRepository
    blobs: LocalContentAddressedBlobStore
    records: LocalJsonlRecordStorage
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
    records = LocalJsonlRecordStorage(root / "records", max_member_bytes=member_bytes)
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


def test_clean_incremental_targeted_and_compacted_paths_converge_on_active_document_state(
    tmp_path: Path,
) -> None:
    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()

    evolving = _platform(tmp_path / "evolving", member_bytes=3700)
    first_a = _write_source(
        evolving.sources / "a.txt",
        "Alpha before the update in its initial paragraph.",
    )
    first_b = _write_source(
        evolving.sources / "b.txt",
        "Bravo remains stable in its only paragraph.",
    )
    first_i = _write_source(
        evolving.sources / "i.txt",
        "India shares a storage partition and remains stable.",
    )
    first_source = evolving.publish_source(
        (
                SourceItem("document-a", "v1", (first_a,), metadata={"expectedSegments": 1}),
                SourceItem("document-b", "v1", (first_b,), metadata={"expectedSegments": 1}),
                SourceItem("document-i", "v1", (first_i,), metadata={"expectedSegments": 1}),
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
        fetcher=SharedFixtureContentFetcher(evolving.sources),
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
        SourceItem("document-i", "v1", (first_i,), metadata={"expectedSegments": 1}),
    )
    final_source = evolving.publish_source(final_items, name="final")
    final_processor = _CountingProcessor(_description("equivalence", "2", retry))
    fetcher = _CountingFetcher(SharedFixtureContentFetcher(evolving.sources))
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

    def _clean_state(
        root: Path,
        *,
        alpha_text: str,
        alpha_version: str,
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        clean = _platform(root, member_bytes=1024 * 1024)
        clean_items = (
            SourceItem(
                "document-a",
                alpha_version,
                (_write_source(clean.sources / "a.txt", alpha_text),),
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
                SourceItem(
                    "document-i",
                    "v1",
                    (
                        _write_source(
                            clean.sources / "i.txt",
                            "India shares a storage partition and remains stable.",
                        ),
                    ),
                    metadata={"expectedSegments": 1},
                ),
        )
        clean_source = clean.publish_source(clean_items)
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
            fetcher=SharedFixtureContentFetcher(clean.sources),
            processors=(clean_processor,),
            partition_policy=clean.partition_policy,
        )
        return _active_document_state(clean.catalog, clean_release)

    assert _clean_state(
        tmp_path / "clean-incremental",
        alpha_text="Alpha after the update in its final paragraph.",
        alpha_version="v2",
    ) == _active_document_state(evolving.catalog, incremental_release)

    # Targeted pass: only document-a changes again, and the plan selects it
    # explicitly; unrelated content must be neither refetched nor reprocessed.
    targeted_text = "Alpha targeted once more in its last paragraph."
    targeted_a = _write_source(evolving.sources / "a.txt", targeted_text)
    targeted_source = evolving.publish_source(
        (
            SourceItem("document-a", "v3", (targeted_a,), metadata={"expectedSegments": 1}),
                final_items[1],
                final_items[2],
                final_items[3],
        ),
        name="targeted",
    )
    targeted_processor = _CountingProcessor(_description("equivalence", "2", retry))
    targeted_fetcher = _CountingFetcher(SharedFixtureContentFetcher(evolving.sources))
    targeted_extractor = _CountingExtractor()
    targeted_segmenter = _CountingSegmenter()
    targeted_plan = _targeted_plan(
        targeted_source,
        incremental_release,
        (targeted_processor,),
        retry,
        accepted,
        selection={"includeItemIds": ["document-a"]},
    )
    targeted_planned, _, _, _, targeted_release = _run(
        plan=targeted_plan,
        source_catalog=evolving.source_catalog,
        controls=evolving.controls,
        stores=evolving.stores,
        blobs=evolving.blobs,
        records=evolving.records,
        catalog=evolving.catalog,
        fetcher=targeted_fetcher,
        processors=(targeted_processor,),
        extractor=targeted_extractor,
        segmenter=targeted_segmenter,
        partition_policy=evolving.partition_policy,
    )
    targeted_entries = tuple(
        entry
        for reference in targeted_planned
        for entry in evolving.stores.load(reference).entries
    )
    assert [entry.source_item.item_id for entry in targeted_entries] == ["document-a"]
    assert (targeted_entries[0].change, targeted_entries[0].execution_mode) == (
        ChangeKind.CHANGED,
        EntryExecutionMode.FULL,
    )
    assert (len(targeted_fetcher.calls), targeted_extractor.calls, targeted_segmenter.calls) == (1, 1, 1)

    compacted_records = LocalJsonlRecordStorage(
        evolving.records.root,
        max_member_bytes=1024 * 1024,
    )
    compacting_catalog = LocalManifestDocumentCatalog(
        evolving.catalog.root,
        records=compacted_records,
        stores=evolving.stores,
        controls=evolving.controls,
        producer=document_release_producer(),
        blobs=evolving.blobs,
    )
    receipt_ref = ReleaseCompactionService(
        controls=evolving.controls,
        records=compacted_records,
        stores=evolving.stores,
        document_catalog=compacting_catalog,
        clock=lambda: "2026-08-05T13:00:00Z",
    ).compact(targeted_release)
    receipt = ReleaseCompactionReceipt.from_dict(evolving.controls.load(receipt_ref))
    compacted_release = receipt.successor_release

    clean_targeted_state = _clean_state(
        tmp_path / "clean-targeted",
        alpha_text=targeted_text,
        alpha_version="v3",
    )
    targeted_state = _active_document_state(compacting_catalog, targeted_release)
    compacted_state = _active_document_state(compacting_catalog, compacted_release)
    assert clean_targeted_state == targeted_state == compacted_state

    targeted = compacting_catalog.open(targeted_release)
    compacted = compacting_catalog.open(compacted_release)
    assert receipt.source_logical_state_digest == receipt.successor_logical_state_digest
    assert logical_release_state_digest(compacted_records, targeted) == logical_release_state_digest(
        compacted_records,
        compacted,
    )
    assert all(
        list(compacting_catalog.compare(targeted_release, compacted_release, layer_kind=layer.layer_kind)) == []
        for layer in targeted.active_layers
    )


def test_a_previously_failed_item_is_replanned_as_repair_and_unfailed_items_stay_dropped(
    tmp_path: Path,
) -> None:
    """A document that ends one run with an accepted-failure disposition must
    be replanned as repairable work on the next run, even when neither the
    source catalog nor the governing plan changed in between — otherwise it
    is stuck forever. An item that succeeded must still classify as
    UNCHANGED and drop out of the plan entirely, so one unrelated failure
    does not turn every incremental run into a full rebuild."""

    _FailingProcessor = importlib.import_module("tests.conformance.test_document_store")._FailingProcessor

    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy((FailureClass.DETERMINISTIC_INPUT,))
    platform = _platform(tmp_path / "repair", member_bytes=1024 * 1024)

    steady_a = _write_source(
        platform.sources / "steady-a.txt",
        "Steady A never changes across either run.",
    )
    steady_b = _write_source(
        platform.sources / "steady-b.txt",
        "Steady B never changes across either run either.",
    )
    broken = _write_source(
        platform.sources / "broken.txt",
        "Broken starts out failing its processor.",
    )
    # source-native records must be strictly ordered by item id.
    items = (
        SourceItem("document-broken", "v1", (broken,), metadata={"expectedSegments": 1}),
        SourceItem("document-steady-a", "v1", (steady_a,), metadata={"expectedSegments": 1}),
        SourceItem("document-steady-b", "v1", (steady_b,), metadata={"expectedSegments": 1}),
    )
    description = _description("repair-failed-item", "1", retry)

    # First run: "document-broken" fails its processor deterministically and
    # is accepted as a failure rather than blocking the release.
    first_source = platform.publish_source(items, name="first")
    failing_processor = _FailingProcessor(
        description,
        fail_item_id="document-broken",
        error=ValueError("declared deterministic fixture failure"),
    )
    first_plan = _plan(first_source, None, (failing_processor,), retry, accepted)
    first_planned, _, first_sealed, _, first_release = _run(
        plan=first_plan,
        source_catalog=platform.source_catalog,
        controls=platform.controls,
        stores=platform.stores,
        blobs=platform.blobs,
        records=platform.records,
        catalog=platform.catalog,
        fetcher=SharedFixtureContentFetcher(platform.sources),
        processors=(failing_processor,),
        partition_policy=platform.partition_policy,
        accepted_failure_policy=accepted,
    )

    first_planned_entries = {
        entry.source_item.item_id: entry
        for reference in first_planned
        for entry in platform.stores.load(reference).entries
    }
    assert set(first_planned_entries) == {"document-steady-a", "document-steady-b", "document-broken"}
    assert {entry.change for entry in first_planned_entries.values()} == {ChangeKind.ADDED}

    first_sealed_entries = {
        entry.source_item.item_id: entry
        for reference in first_sealed
        for entry in platform.stores.load(reference).entries
    }
    assert first_sealed_entries["document-broken"].disposition == AcquisitionDisposition.ACCEPTED_FAILURE
    assert first_sealed_entries["document-steady-a"].disposition == AcquisitionDisposition.CAPTURED
    assert first_sealed_entries["document-steady-b"].disposition == AcquisitionDisposition.CAPTURED

    # Second run: an unchanged source catalog (identical items, identical
    # digests) over an unchanged governing plan (same processor description,
    # limits, stages, retry and accepted-failure policies) — only the base
    # release and the source-catalog reference legitimately differ.
    second_source = platform.publish_source(items, name="second")
    second_plan = _plan(second_source, first_release, (failing_processor,), retry, accepted)
    assert second_plan.governing_content() == first_plan.governing_content(), (
        "the scenario requires an unchanged plan; only the base release and "
        "source-catalog reference may legitimately differ"
    )

    # Plan the second run directly, against the real committed first release,
    # without executing it. The property under test is a planning-time one —
    # "re-plans exactly that item and no others" — so proving it does not
    # need a second full capture/process/deliver/commit pass.
    second_plan_ref = platform.controls.put(
        kind="plans",
        artifact_id=second_plan.plan_id,
        value=second_plan.to_dict(),
    )
    second_planned = tuple(
        RunPlanner(
            source_catalog=platform.source_catalog,
            document_catalog=platform.catalog,
            stores=platform.stores,
            controls=platform.controls,
            workspace_factory=LocalSqliteReconciliationWorkspaceFactory(
                platform.records.root / ".planning-second",
                read_batch_size=1,
            ),
        ).plan_run(second_source, first_release, second_plan_ref)
    )
    second_planned_entries = tuple(
        entry
        for reference in second_planned
        for entry in platform.stores.load(reference).entries
    )
    # The property under test: the plan touches exactly the one previously
    # failed item and nothing else — asserted on the touched-item count, not
    # merely its classification.
    assert len(second_planned_entries) == 1
    (repaired,) = second_planned_entries
    assert repaired.source_item.item_id == "document-broken"
    assert repaired.change == ChangeKind.REPAIR
    assert repaired.execution_mode == EntryExecutionMode.FULL

    # The negative: the two items that succeeded the first time are still
    # UNCHANGED and still dropped from the plan.
    assert {entry.source_item.item_id for entry in second_planned_entries} == {"document-broken"}

    # End-to-end confirmation: the repaired item now actually captures and
    # completes, and the resulting release still carries every item's
    # complete active state, not only the one item this run touched.
    healed_processor = _CountingProcessor(description)
    _, _, second_sealed, _, second_release = _run(
        plan=second_plan,
        source_catalog=platform.source_catalog,
        controls=platform.controls,
        stores=platform.stores,
        blobs=platform.blobs,
        records=platform.records,
        catalog=platform.catalog,
        fetcher=SharedFixtureContentFetcher(platform.sources),
        processors=(healed_processor,),
        partition_policy=platform.partition_policy,
        accepted_failure_policy=accepted,
    )
    second_sealed_entries = {
        entry.source_item.item_id: entry
        for reference in second_sealed
        for entry in platform.stores.load(reference).entries
    }
    assert set(second_sealed_entries) == {"document-broken"}
    assert second_sealed_entries["document-broken"].disposition == AcquisitionDisposition.CAPTURED

    second_state = _active_document_state(platform.catalog, second_release)
    assert {row["sourceItemId"] for row in second_state["source-items"]} == {
        "document-steady-a",
        "document-steady-b",
        "document-broken",
    }
