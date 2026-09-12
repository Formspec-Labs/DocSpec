"""Bounded, read-only questions over the evidence a run already retained."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import closing
from typing import Any

from docspec.domain.delivery import iter_delivery_records
from docspec.domain.jobs import DocumentEntry, DocumentStore, StoreState
from docspec.domain.plans import ProcessingPlan
from docspec.domain.references import ArtifactRef, BlobRef, DocumentReleaseRef, StoreRef
from docspec.errors import IntegrityError
from docspec.ports.blob_store import BlobStore
from docspec.ports.control_repository import ControlRepository
from docspec.ports.document_catalog import DocumentCatalog
from docspec.ports.document_store_repository import DocumentStoreRepository
from docspec.ports.record_storage import RecordStorage
from docspec.ports.record_workspace import RecordWorkspaceFactory
from docspec.ports.source_catalog import SourceCatalogSnapshotSummary, SourceNativeDescription

from .inspection_evidence import ENTRY_COUNT_KEYS, entry_evidence
from .inspection_runs import load_run, work_stores


def _sample_limit(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("inspection sample limit must be a non-negative integer")
    return value


class InspectionView:
    """One plan observation, immutable run, or retained mixed result.

    Reports separate work scheduled by the attempt from complete active result
    layers. A live plan reads each latest store separately, not an atomic
    snapshot. Complete totals require streaming the admitted population; only
    detail samples are capped. No implementation of a processing stage is used.
    """

    def __init__(
        self,
        plan: ProcessingPlan,
        *,
        controls: ControlRepository,
        stores: DocumentStoreRepository,
        records: RecordStorage,
        blobs: BlobStore,
        catalog: DocumentCatalog,
        workspace_factory: RecordWorkspaceFactory,
        run_ref: ArtifactRef | None = None,
        release_ref: DocumentReleaseRef | None = None,
        source_summary: SourceCatalogSnapshotSummary | None = None,
    ) -> None:
        if run_ref is not None and release_ref is not None:
            raise ValueError("choose one run or retained result reference")
        self.plan = plan
        self._controls, self._stores, self._records = controls, stores, records
        self._blobs, self._catalog, self._workspace_factory = blobs, catalog, workspace_factory
        self.release_ref = release_ref
        if release_ref is not None:
            catalog.audit(release_ref)
        self._reader = None if release_ref is None else catalog.open_reader(release_ref)
        if self._reader is not None:
            release = self._reader.release
            if release.processing_plan.artifact_id != plan.plan_id:
                raise IntegrityError("inspection retained result differs from its processing plan")
            run_ref = release.run_receipt
        self.run_ref = run_ref
        self.run, self.execution_profile = (
            (None, None) if run_ref is None else load_run(
                run_ref, plan, controls, stores, records, blobs,
                admitted_layers=None if self._reader is None else self._reader.release.active_layers,
            )
        )
        self._layers = (
            self._reader.release.active_layers if self._reader is not None
            else () if self.run is None else self.run.staged_layers
        )
        if source_summary is not None and (
            source_summary.logical_id != plan.source_catalog.catalog_id
            or source_summary.artifact_digest != plan.source_catalog.digest
        ):
            raise IntegrityError("inspection source summary differs from the pinned source catalog")
        self._source_summary = source_summary

    @property
    def phase(self) -> str:
        if self.release_ref is not None:
            return "retained-result"
        if self.run_ref is not None:
            return "reconciled-run"
        return "plan-observation" if self._stores.has_planned_store_ledger(self.plan.plan_id) else "not-planned"

    @property
    def result_scope(self) -> str:
        if self.release_ref is not None:
            return "retained-active-state"
        if self.run is not None and self.run.stateful:
            return "rejected-staged-output" if self.run.counts.get("rejectedStores", 0) else "reconciled-active-state"
        return "checkpointed-work"

    @property
    def complete_active_state(self) -> bool:
        return self.result_scope in {"retained-active-state", "reconciled-active-state"}

    def _work_stores(self) -> Iterator[tuple[StoreRef, DocumentStore]]:
        yield from work_stores(self.plan, self.run, self._stores, self._records)

    def _entry(self, source_item_id: str) -> tuple[StoreRef, DocumentStore, DocumentEntry] | None:
        if self.run is not None:
            selection = self._records.lookup(self.run.selection_ledger, source_item_id, partition_value=source_item_id)
            if selection is None:
                return None
            row = self._records.lookup(
                self.run.store_ledger, selection["storeId"], partition_value=selection["storeId"],
            )
            if row is None:
                raise IntegrityError("selection evidence names a missing run store")
            reference = StoreRef.from_dict(row["store"])
            store = self._stores.load(reference)
            if store.plan_id != self.run.plan.artifact_id or store.state is not StoreState.SEALED:
                raise IntegrityError("selection evidence names a differently planned store")
            matches = tuple(entry for entry in store.entries if entry.source_item.item_id == source_item_id)
            if len(matches) != 1 or matches[0].entry_id != selection["entryId"]:
                raise IntegrityError("selection evidence does not match its exact store entry")
            return reference, store, matches[0]
        with closing(self._work_stores()) as population:
            for reference, store in population:
                for entry in store.entries:
                    if entry.source_item.item_id == source_item_id:
                        return reference, store, entry
        return None

    def summary(self, *, sample_limit: int = 20) -> dict[str, Any]:
        """Count scheduled work separately from the result's complete population."""

        limit = _sample_limit(sample_limit)
        stores: Counter[str] = Counter()
        dispositions: Counter[str] = Counter()
        modes: Counter[str] = Counter()
        counts: Counter[str] = Counter(dict.fromkeys((*ENTRY_COUNT_KEYS, "scheduledItems"), 0))
        examples: list[dict[str, Any]] = []
        with closing(self._work_stores()) as population:
            for reference, store in population:
                stores[store.state.value] += 1
                for entry in store.entries:
                    evidence = entry_evidence(entry, self.plan, self._controls, sample_limit=limit)
                    counts.update(evidence["counts"])
                    counts["scheduledItems"] += 1
                    dispositions["pending" if entry.disposition is None else entry.disposition.value] += 1
                    modes[entry.execution_mode.value] += 1
                    if len(examples) < limit:
                        examples.append({"sourceItemId": entry.source_item.item_id, "store": reference.to_dict(), **evidence})
        source = None
        if self._source_summary is not None:
            source = {
                "scope": "admitted-source-catalog",
                "itemCount": self._source_summary.item_count,
                "dispositionCounts": dict(self._source_summary.disposition_counts),
                "reasonCounts": [dict(row) for row in self._source_summary.reason_counts],
                "sourceNativeInputs": [
                    SourceNativeDescription.from_dict(value).to_dict() for value in self._source_summary.source_native_inputs
                ],
                "acceptedRecordOutcomes": sorted(self._source_summary.accepted_record_outcomes),
                "collectionEvidenceScope": "provider-reported; bound to the source input pin, without upstream re-admission",
            }
        return {
            "format": "docspec-inspection", "formatVersion": "1.0", "phase": self.phase,
            "planId": self.plan.plan_id,
            "run": None if self.run_ref is None else self.run_ref.to_dict(),
            "release": None if self.release_ref is None else self.release_ref.to_dict(),
            "configuration": self.plan.identity_content(),
            "executionProfile": None if self.execution_profile is None else self.execution_profile.to_dict(),
            "source": source,
            "work": {
                "scope": "scheduled-items", "counts": dict(sorted(counts.items())),
                "stores": dict(sorted(stores.items())), "dispositions": dict(sorted(dispositions.items())),
                "plannedExecutionModes": dict(sorted(modes.items())),
                "sample": examples, "sampleTruncated": counts["scheduledItems"] > len(examples),
            },
            "result": {
                "scope": self.result_scope,
                "completeActiveState": self.complete_active_state,
                "layers": {layer.layer_kind: layer.record_count for layer in self._layers},
            },
            "unavailable": {
                "matchingPlanSelectionCount": "saved work ledgers exclude unchanged and unselected items",
                "sourceOutcomes": None if source is not None else "no separately admitted source catalog was supplied",
                "monetaryCost": "receipts do not define monetary cost",
                "tokenUsage": "receipts do not define token counts",
                "wallClockRunDuration": "run timestamps are fixed input values, not execution timing",
            },
            "verificationScope": (
                "retained-release-and-pinned-run-evidence" if self.release_ref is not None
                else "pinned-run-evidence-and-active-state" if self.complete_active_state
                else "pinned-run-artifacts-and-ledgers" if self.run is not None
                else "planned-ledger-and-individually-observed-store-revisions"
            ),
        }

    def source(self, source_item_id: str, *, sample_limit: int = 20) -> dict[str, Any]:
        """Explain one input, its latest attempt work, and its active output rows."""

        limit = _sample_limit(sample_limit)
        found = self._entry(source_item_id)
        work = None
        if found is not None:
            reference, _store, entry = found
            work = {"store": reference.to_dict(), **entry_evidence(entry, self.plan, self._controls, sample_limit=limit)}
        layers: dict[str, Any] = {}
        for kind in self.layer_kinds:
            sample: list[dict[str, Any]] = []
            count = 0
            with closing(self.records(kind, source_item_id=source_item_id)) as rows:
                for row in rows:
                    count += 1
                    if len(sample) < limit:
                        sample.append(row)
            layers[kind] = {"count": count, "sample": sample, "sampleTruncated": count > len(sample)}
        return {
            "sourceItemId": source_item_id, "phase": self.phase, "work": work,
            "workAvailability": "scheduled-in-this-attempt" if work is not None else "not-in-this-attempt-work-population",
            "result": {"scope": self.result_scope, "layers": layers},
            "note": "result disposition requestedStages applies to this source, including inherited items",
        }

    @property
    def layer_kinds(self) -> tuple[str, ...]:
        if self.result_scope != "checkpointed-work":
            return tuple(layer.layer_kind for layer in self._layers)
        return tuple(sorted((
            "source-items", "files", "representations", "segments", "dispositions", "failures", "receipts",
            *(f"derived:{processor.processor_id}" for processor in self.plan.processors.execution_order),
        )))

    def records(self, layer_kind: str, *, source_item_id: str | None = None) -> Iterator[dict[str, Any]]:
        """Stream existing rows; callers stopping early should close the iterator."""

        if layer_kind not in self.layer_kinds:
            raise ValueError(f"inspection has no layer {layer_kind!r}")
        if self._reader is not None:
            if source_item_id is None:
                yield from self._reader.scan(layer_kind=layer_kind)
            else:
                yield from self._reader.scan_source(layer_kind=layer_kind, source_item_id=source_item_id)
        elif self.result_scope != "checkpointed-work":
            layer = next(layer for layer in self._layers if layer.layer_kind == layer_kind)
            if source_item_id is None:
                yield from self._records.stream(layer)
            else:
                with closing(self._records.scan_partition_value(layer, source_item_id)) as rows:
                    for row in rows:
                        if row["sourceItemId"] == source_item_id:
                            yield row
        else:
            with closing(self._work_stores()) as population:
                for _reference, store in population:
                    with closing(iter_delivery_records(store)) as records:
                        for record in records:
                            if record.layer_kind == layer_kind and (source_item_id is None or record.source_item_id == source_item_id):
                                yield record.to_record()

    def read_blob(self, reference: BlobRef, *, max_bytes: int) -> Iterator[bytes]:
        """Read exact persisted bytes within the caller's explicit byte allowance."""

        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("inspection blob allowance must be a positive integer")
        yield from self._blobs.read(reference, max_bytes=max_bytes)

    def compare(self, other: InspectionView, *, sample_limit: int = 20) -> dict[str, Any]:
        """Compare stable source identities, configuration, work, and active state."""

        from .inspection_comparison import compare_views

        return compare_views(self, other, sample_limit=sample_limit)
