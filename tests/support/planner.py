"""Shared planner fixtures, extracted from tests.test_planner."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from docspec.domain.content import SourceItem, SourceItemState
from docspec.domain.identity import canonical_json_file_bytes, sha256_digest
from docspec.domain.jobs import DocumentStore
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, LayerRef, SourceCatalogRef, StoreRef
from docspec.ports.source_catalog import (
    LocatedSourceCatalogItem,
    SourceCatalogSnapshot,
    SourceCatalogSnapshotSummary,
)


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
            reason_counts=tuple(
                {"disposition": name, "reasonCode": "test.fixture", "count": value}
                for name, value in disposition_counts.items()
                if name != "selected" and value
            ),
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
                    "sourceSystemId": "urn:test:source-native", "sourceSystemVersion": "1",
                    "sourceStateScope": "complete-snapshot", "sourceStateDigest": reference.digest,
                    "sourceNativeSchemaSetDigest": reference.digest, "collectionOutcome": None,
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
