from __future__ import annotations

import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.application.planner import RunPlanner, logical_partition
from docspec.domain.content import CandidateFile, SourceItem, SourceItemState
from docspec.domain.identity import canonical_json_file_bytes, sha256_digest
from docspec.domain.jobs import ChangeKind, DocumentStore
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import DataUsePolicy, RetentionPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.release import DocumentRelease
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, LayerRef, SourceCatalogRef, StoreRef
from docspec.errors import IntegrityError
from docspec.ports.source_catalog import (
    LocatedSourceCatalogItem,
    SourceCatalogSnapshot,
    SourceCatalogSnapshotSummary,
)
from tests.helpers import EMPTY_DIGEST, artifact, profile_set


@dataclass
class MemoryControls:
    values: dict[str, dict] = field(default_factory=dict)

    def put(self, *, kind: str, artifact_id: str, value: dict) -> ArtifactRef:
        payload = canonical_json_file_bytes(value)
        reference = ArtifactRef(
            artifact_id, f"memory://{kind}/{artifact_id}", sha256_digest(payload), "application/json", len(payload)
        )
        self.values[reference.locator] = value
        return reference

    def load(self, reference: ArtifactRef) -> dict:
        return self.values[reference.locator]

    def verify(self, reference: ArtifactRef) -> None:
        assert sha256_digest(canonical_json_file_bytes(self.load(reference))) == reference.digest


@dataclass
class MemoryCatalogItem:
    item: SourceItem

    def to_processing_item(self) -> SourceItem:
        return self.item


@dataclass
class MemorySourceCatalog:
    reference: SourceCatalogRef
    items: tuple[SourceItem, ...]
    partitions: tuple[str, ...] = ()
    open_calls: int = 0

    def open_snapshot(self, reference: SourceCatalogRef) -> SourceCatalogSnapshot:
        self.open_calls += 1
        assert reference == self.reference
        disposition_counts = {
            "selected": sum(item.state is SourceItemState.ACTIVE for item in self.items),
            "excluded": sum(item.state is SourceItemState.EXCLUDED for item in self.items),
            "deleted": sum(item.state is SourceItemState.DELETED for item in self.items),
            "unavailable": 0,
            "failed": 0,
        }
        summary = SourceCatalogSnapshotSummary(
            logical_id=reference.catalog_id,
            artifact_digest=reference.digest,
            catalog_id=reference.catalog_id,
            catalog_state_digest=reference.digest,
            requested_universe_set_digest=reference.digest,
            selected_source_set_digest=reference.digest,
            item_count=len(self.items),
            disposition_counts=disposition_counts,
            partitions=self.partitions,
            selection_policy={
                "policyId": "urn:test:selection-policy",
                "policyVersion": "1.0.0",
                "policyDigest": reference.digest,
            },
            partition_policy={
                "policyId": "urn:test:partition-policy",
                "policyVersion": "1.0.0",
                "policyDigest": reference.digest,
                "bucketCount": 1,
            },
            join_coverage=(),
            diagnostic_digests={
                "normalizedFieldsDigest": reference.digest,
                "joinedFieldsDigest": reference.digest,
                "dispositionsDigest": reference.digest,
                "reasonsDigest": reference.digest,
                "interpretationsDigest": reference.digest,
                "renditionChoicesDigest": reference.digest,
            },
            source_native_inputs=(
                {
                    "logicalId": "urn:test:source-native",
                    "artifactDigest": reference.digest,
                },
            ),
            byte_measurements={
                "payloadBytesRead": 0,
                "payloadBytesReused": 0,
                "payloadBytesWritten": 0,
                "publicationBytesWritten": 0,
            },
        )
        return SourceCatalogSnapshot(
            summary,
            iter(
                LocatedSourceCatalogItem(
                    MemoryCatalogItem(item),  # type: ignore[arg-type]
                    reference.digest,
                )
                for item in self.items
            ),
        )


@dataclass
class MemoryStores:
    values: dict[tuple[str, int], DocumentStore] = field(default_factory=dict)
    ledgers: dict[str, tuple[LayerRef, tuple[StoreRef, ...]]] = field(default_factory=dict)

    def save(self, store: DocumentStore) -> StoreRef:
        payload = canonical_json_file_bytes(store.to_dict())
        reference = StoreRef(
            store.store_id, store.revision, f"memory://stores/{store.store_id}/{store.revision}", sha256_digest(payload)
        )
        self.values[(store.store_id, store.revision)] = store
        return reference

    def load(self, reference: StoreRef) -> DocumentStore:
        return self.values[(reference.store_id, reference.revision)]

    def latest(self, store_id: str) -> StoreRef | None:
        matches = [store for (identifier, _), store in self.values.items() if identifier == store_id]
        return None if not matches else self.save(max(matches, key=lambda item: item.revision))

    def revisions(self, store_id: str) -> tuple[StoreRef, ...]:
        return tuple(self.save(store) for (identifier, _), store in self.values.items() if identifier == store_id)

    def seal_planned_stores(self, plan_id: str, references: Iterable[StoreRef]) -> LayerRef:
        planned = tuple(references)
        payload = canonical_json_file_bytes([reference.to_dict() for reference in planned])
        ledger = LayerRef(
            f"memory-planned:{plan_id}",
            "planned-document-stores",
            "docspec-planned-store-reference/1.0",
            "memory-document-store",
            f"memory://planned/{plan_id}",
            sha256_digest(payload),
            len(planned),
        )
        self.ledgers[plan_id] = (ledger, planned)
        return ledger

    def planned_store_ledger(self, plan_id: str) -> LayerRef:
        return self.ledgers[plan_id][0]

    def verify_planned_store_ledger(self, reference: LayerRef) -> None:
        assert any(reference == ledger for ledger, _ in self.ledgers.values())

    def stream_planned_stores(self, reference: LayerRef) -> Iterator[StoreRef]:
        yield from next(planned for ledger, planned in self.ledgers.values() if ledger == reference)


class EmptyDocumentCatalog:
    def open_reader(self, reference: DocumentReleaseRef):
        raise AssertionError("initial planning must not open a base release reader")


@dataclass
class MemoryCatalogReader:
    owner: MemoryDocumentCatalog

    @property
    def release(self) -> DocumentRelease:
        return self.owner.release

    def lookup(self, *, layer_kind: str, record_id: str):
        self.owner.lookup_calls += 1
        self.owner.lookup_ids.append(record_id)
        assert layer_kind == "source-items"
        return next((row for row in self.owner.rows() if row["recordId"] == record_id), None)

    def scan(self, *, layer_kind: str) -> Iterator[dict]:
        self.owner.scan_calls += 1
        if layer_kind == "source-items":
            yield from self.owner.rows()
            return
        if layer_kind == "failures":
            yield from self.owner.failure_rows()
            return
        raise AssertionError(f"unexpected layer_kind {layer_kind!r}")


@dataclass
class MemoryDocumentCatalog:
    reference: DocumentReleaseRef
    release: DocumentRelease
    items: tuple[SourceItem, ...]
    failed_item_ids: tuple[str, ...] = ()
    reader_calls: int = 0
    scan_calls: int = 0
    lookup_calls: int = 0
    lookup_ids: list[str] = field(default_factory=list)

    def open_reader(self, reference: DocumentReleaseRef) -> MemoryCatalogReader:
        assert reference == self.reference
        self.reader_calls += 1
        return MemoryCatalogReader(self)

    def rows(self) -> Iterator[dict]:
        for item in self.items:
            yield {
                "recordId": item.item_id,
                "sourceItemId": item.item_id,
                "idempotencyKey": f"memory:{item.item_id}",
                "deleted": item.state == SourceItemState.DELETED,
                "payload": item.to_dict(),
            }

    def failure_rows(self) -> Iterator[dict]:
        """The bounded failures layer: one row per failed source item, id only."""

        for item_id in self.failed_item_ids:
            yield {
                "recordId": f"urn:test:failure:{item_id}",
                "sourceItemId": item_id,
                "idempotencyKey": f"memory:failure:{item_id}",
                "deleted": False,
                "payload": {
                    "entryId": f"urn:test:entry:{item_id}",
                    "failureClass": "deterministic-input",
                    "diagnosticCode": "test.synthetic-failure",
                    "detail": "synthetic fixture failure",
                    "attempt": 1,
                    "retryable": False,
                },
            }


def _source_reference(name: str) -> SourceCatalogRef:
    return SourceCatalogRef(f"urn:docspec:test:catalog:{name}", f"memory://catalogs/{name}", sha256_digest(name.encode()))


def _candidate(
    *,
    candidate_id: str = "primary",
    locator: str = "memory://source.txt",
    digest: str = EMPTY_DIGEST,
    size: int = 12,
    transport: str = "fixture:v1",
    metadata: dict | None = None,
) -> CandidateFile:
    return CandidateFile(
        candidate_id,
        locator,
        "text/plain",
        expected_digest=digest,
        expected_size=size,
        transport_version=transport,
        metadata=metadata,
    )


def _source_item(
    item_id: str,
    *,
    candidates: tuple[CandidateFile, ...] | None = None,
    state: SourceItemState = SourceItemState.ACTIVE,
    metadata: dict | None = None,
) -> SourceItem:
    return SourceItem(
        item_id,
        "v1",
        (_candidate(),) if candidates is None else candidates,
        state=state,
        metadata={"expectedSegments": 1} if metadata is None else metadata,
    )


def _plan(
    source: SourceCatalogRef,
    base: DocumentReleaseRef | None,
    *,
    selection: dict | None = None,
    partition_count: int = 4,
) -> ProcessingPlan:
    return ProcessingPlan.create(
        source_catalog=source,
        base_release=base,
        profiles=profile_set(),
        limits=WorkLimits(10, 1000, 100, 100, 1000, 1000, 60),
        stages=StagePolicy(("text-v1",), "paragraph-v1"),
        processors=ProcessorSet(()),
        partition_count=partition_count,
        selection={} if selection is None else selection,
        retention_policy=RetentionPolicy.retain_all(),
        data_use_policy=DataUsePolicy.local_content(),
        retry_policy_digest=EMPTY_DIGEST,
        accepted_failure_policy_digest=EMPTY_DIGEST,
    )


def _planned_update(
    previous_items: tuple[SourceItem, ...],
    current_items: tuple[SourceItem, ...],
    *,
    selection: dict | None = None,
    previous_selection: dict | None = None,
    partitions: tuple[str, ...] = (),
    failed_item_ids: tuple[str, ...] = (),
) -> tuple[tuple, MemoryDocumentCatalog]:
    controls = MemoryControls()
    stores = MemoryStores()
    previous_source = _source_reference("previous")
    previous_plan = _plan(
        previous_source,
        None,
        selection=selection if previous_selection is None else previous_selection,
    )
    previous_plan_ref = controls.put(kind="plans", artifact_id=previous_plan.plan_id, value=previous_plan.to_dict())
    release = DocumentRelease.create(
        release_id="urn:spicy:artifact:derivation:" + "d" * 64,
        previous_release=None,
        source_catalog=previous_source,
        processing_plan=previous_plan_ref,
        profiles=previous_plan.profiles,
        active_layers=(),
        blob_roots=(),
        retention_dispositions=RetentionPolicy.retain_all(),
        store_receipt_set_digest=EMPTY_DIGEST,
        run_receipt=artifact("run"),
        catalog_commit_receipt=artifact("commit"),
        counts={"sourceItems": len(previous_items)},
        failures={},
        coverage={},
        partition_policy={"policyId": "test", "bucketCount": 4},
    )
    release_ref = release.reference("memory://releases/base", sha256_digest(release.file_bytes))
    current_source = _source_reference("current")
    plan = _plan(current_source, release_ref, selection=selection)
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    catalog = MemoryDocumentCatalog(
        release_ref,
        release,
        previous_items,
        failed_item_ids,
    )
    source_catalog = MemorySourceCatalog(
        current_source,
        current_items,
        partitions,
    )
    with tempfile.TemporaryDirectory(prefix="docspec-planner-test-") as directory:
        planner = RunPlanner(
            source_catalog=source_catalog,
            document_catalog=catalog,
            stores=stores,
            controls=controls,
            workspace_factory=LocalSqliteReconciliationWorkspaceFactory(
                Path(directory),
                read_batch_size=1,
            ),
        )
        references = tuple(planner.plan_run(current_source, release_ref, plan_ref))
    entries = tuple(entry for reference in references for entry in stores.load(reference).entries)
    return entries, catalog


def _planned_initial(
    tmp_path: Path,
    items: tuple[SourceItem, ...],
    *,
    selection: dict,
    partitions: tuple[str, ...] = (),
    partition_count: int = 4,
) -> tuple[tuple, MemorySourceCatalog]:
    controls = MemoryControls()
    stores = MemoryStores()
    source_ref = _source_reference("initial")
    plan = _plan(source_ref, None, selection=selection, partition_count=partition_count)
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    source_catalog = MemorySourceCatalog(source_ref, items, partitions=partitions)
    planner = RunPlanner(
        source_catalog=source_catalog,
        document_catalog=EmptyDocumentCatalog(),
        stores=stores,
        controls=controls,
        workspace_factory=LocalSqliteReconciliationWorkspaceFactory(
            tmp_path / plan.plan_id.rsplit(":", maxsplit=1)[-1],
            read_batch_size=1,
        ),
    )

    references = tuple(planner.plan_run(source_ref, None, plan_ref))
    entries = tuple(entry for reference in references for entry in stores.load(reference).entries)
    return entries, source_catalog


def test_planner_streams_bounded_stores_and_schedules_only_selected_work(tmp_path: Path) -> None:
    source_ref = SourceCatalogRef("urn:docspec:test:catalog", "memory://catalog", sha256_digest(b"catalog"))
    items = tuple(
        SourceItem(
            f"item-{index}",
            "v1",
            (CandidateFile("primary", f"memory://item-{index}", "text/plain", expected_size=10),),
            metadata={"expectedSegments": 2},
        )
        for index in range(5)
    )
    controls = MemoryControls()
    stores = MemoryStores()
    plan = ProcessingPlan.create(
        source_catalog=source_ref,
        base_release=None,
        profiles=profile_set(),
        limits=WorkLimits(2, 100, 10, 10, 20, 100, 20),
        stages=StagePolicy(("text-v1",), "paragraph-v1"),
        processors=ProcessorSet(()),
        partition_count=1,
        selection={"excludeItemIds": ["item-4"]},
        retention_policy=RetentionPolicy.retain_all(),
        data_use_policy=DataUsePolicy.local_content(),
        retry_policy_digest=EMPTY_DIGEST,
        accepted_failure_policy_digest=EMPTY_DIGEST,
    )
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    source_catalog = MemorySourceCatalog(source_ref, items)
    planner = RunPlanner(
        source_catalog=source_catalog,
        document_catalog=EmptyDocumentCatalog(),
        stores=stores,
        controls=controls,
        workspace_factory=LocalSqliteReconciliationWorkspaceFactory(
            tmp_path / "planning",
            read_batch_size=1,
        ),
    )

    references = tuple(planner.plan_run(source_ref, None, plan_ref))
    planned = tuple(stores.load(reference) for reference in references)
    ledger = stores.planned_store_ledger(plan.plan_id)

    assert [len(store.entries) for store in planned] == [2, 2]
    assert ledger.record_count == len(planned)
    assert tuple(stores.stream_planned_stores(ledger)) == references
    assert all(store.limits.max_entries == 2 for store in planned)
    assert {entry.change for store in planned for entry in store.entries} == {ChangeKind.ADDED}
    assert {entry.source_item.item_id for store in planned for entry in store.entries} == {
        "item-0",
        "item-1",
        "item-2",
        "item-3",
    }
    assert source_catalog.open_calls == 1


def test_targeted_selection_uses_source_partitions_and_stable_logical_buckets(tmp_path: Path) -> None:
    partition_count = 8
    items = tuple(
        _source_item(
            f"item-{index:03d}",
            metadata={
                "expectedSegments": 1,
                "sourcePartition": "alpha" if index % 2 == 0 else "beta",
            },
        )
        for index in range(32)
    )
    selected_bucket = logical_partition("item-001", partition_count)

    entries, _ = _planned_initial(
        tmp_path,
        items,
        selection={"sourcePartitions": ["beta"], "logicalBuckets": [selected_bucket]},
        partitions=("alpha", "beta"),
        partition_count=partition_count,
    )

    expected = {
        item.item_id
        for item in items
        if item.metadata["sourcePartition"] == "beta"
        and logical_partition(item.item_id, partition_count) == selected_bucket
    }
    assert expected
    assert {entry.source_item.item_id for entry in entries} == expected
    assert all(entry.change == ChangeKind.ADDED for entry in entries)


def test_changed_selection_targets_rebuild_to_source_partition_and_logical_bucket() -> None:
    partition_count = 4
    items = tuple(
        _source_item(
            f"item-{index:03d}",
            metadata={
                "expectedSegments": 1,
                "sourcePartition": "alpha" if index % 2 == 0 else "beta",
            },
        )
        for index in range(16)
    )
    selected_bucket = logical_partition("item-001", partition_count)
    selection = {"sourcePartitions": ["beta"], "logicalBuckets": [selected_bucket]}

    entries, _ = _planned_update(
        items,
        items,
        selection=selection,
        previous_selection={},
        partitions=("alpha", "beta"),
    )

    expected = {
        item.item_id
        for item in items
        if item.metadata["sourcePartition"] == "beta"
        and logical_partition(item.item_id, partition_count) == selected_bucket
    }
    assert expected
    assert {entry.source_item.item_id for entry in entries} == expected
    assert all(entry.change == ChangeKind.REPAIR for entry in entries)


def test_source_partition_selection_rejects_undeclared_partition_before_item_stream(tmp_path: Path) -> None:
    with pytest.raises(IntegrityError, match="not declared by the catalog"):
        _planned_initial(
            tmp_path,
            (),
            selection={"sourcePartitions": ["missing"]},
            partitions=("alpha",),
        )


def test_source_partition_selection_rejects_malformed_item_partition(tmp_path: Path) -> None:
    malformed = _source_item(
        "item",
        metadata={"expectedSegments": 1, "sourcePartition": 7},
    )

    with pytest.raises(IntegrityError, match="invalid sourcePartition"):
        _planned_initial(
            tmp_path,
            (malformed,),
            selection={"sourcePartitions": ["alpha"]},
            partitions=("alpha",),
        )


@pytest.mark.parametrize(
    ("selection", "message"),
    (
        ({"unknown": []}, "unknown selection fields"),
        ({"includeItemIds": "item"}, "includeItemIds must be an array"),
        ({"excludeItemIds": [1]}, "excludeItemIds\\[0\\] must be a non-empty string"),
        ({"itemIdPrefixes": [""]}, "itemIdPrefixes\\[0\\] must be a non-empty string"),
        ({"mediaTypes": None}, "mediaTypes must be an array"),
        ({"sourcePartitions": [False]}, "sourcePartitions\\[0\\] must be a non-empty string"),
        ({"states": ["retired"]}, "unknown source-item state"),
        ({"logicalBuckets": "0"}, "logicalBuckets must be an array"),
        ({"logicalBuckets": [True]}, "logicalBuckets\\[0\\] must be an integer"),
        ({"logicalBuckets": [-1]}, "logicalBuckets\\[0\\] must be an integer"),
        ({"logicalBuckets": [4]}, "logicalBuckets\\[0\\] must be an integer"),
    ),
)
def test_selection_is_precompiled_and_rejects_malformed_selectors_without_items(
    tmp_path: Path,
    selection: dict,
    message: str,
) -> None:
    with pytest.raises(IntegrityError, match=message):
        _planned_initial(tmp_path, (), selection=selection)


@pytest.mark.parametrize(
    ("current", "expected_change"),
    (
        (
            _source_item("item", candidates=(_candidate(), _candidate(candidate_id="alternate"))),
            ChangeKind.CHANGED,
        ),
        (_source_item("item", candidates=(_candidate(locator="memory://moved.txt"),)), ChangeKind.CHANGED),
        (
            _source_item("item", candidates=(_candidate(digest=sha256_digest(b"changed")),)),
            ChangeKind.CHANGED,
        ),
        (_source_item("item", candidates=(_candidate(size=13),)), ChangeKind.CHANGED),
        (_source_item("item", candidates=(_candidate(transport="fixture:v2"),)), ChangeKind.CHANGED),
        (_source_item("item", candidates=(_candidate(metadata={"rendition": "changed"}),)), ChangeKind.CHANGED),
        (_source_item("item", metadata={"expectedSegments": 2}), ChangeKind.CHANGED),
        (_source_item("item", state=SourceItemState.EXCLUDED), ChangeKind.EXCLUDED),
        (_source_item("item", state=SourceItemState.DELETED), ChangeKind.DELETED),
    ),
    ids=(
        "candidate-population",
        "locator",
        "digest",
        "size",
        "transport",
        "candidate-metadata",
        "source-metadata",
        "excluded-state",
        "deleted-state",
    ),
)
def test_same_version_complete_source_item_changes_are_scheduled(
    current: SourceItem,
    expected_change: ChangeKind,
) -> None:
    entries, catalog = _planned_update((_source_item("item"),), (current,))

    assert [(entry.source_item, entry.change) for entry in entries] == [(current, expected_change)]
    assert catalog.reader_calls == 1
    # One scan of "source-items" plus one bounded scan of "failures" to find repairable work.
    assert catalog.scan_calls == 2
    assert catalog.lookup_calls == 0


def test_complete_snapshot_omissions_create_selected_tombstones_only_once() -> None:
    kept = _source_item("kept")
    missing = _source_item("missing")
    out_of_scope = _source_item("out-of-scope")
    prior_tombstone = _source_item("prior-tombstone", state=SourceItemState.DELETED)

    entries, catalog = _planned_update(
        tuple(sorted((kept, missing, out_of_scope, prior_tombstone), key=lambda item: item.item_id)),
        (kept,),
        selection={"excludeItemIds": [out_of_scope.item_id]},
    )

    assert len(entries) == 1
    deletion = entries[0]
    assert deletion.source_item.item_id == missing.item_id
    assert deletion.source_item.version == missing.version
    assert deletion.source_item.candidates == missing.candidates
    assert deletion.source_item.state == SourceItemState.DELETED
    assert deletion.change == ChangeKind.DELETED
    assert deletion.terminal
    assert catalog.reader_calls == 1
    # One scan of "source-items" plus one bounded scan of "failures" to find repairable work.
    assert catalog.scan_calls == 2
