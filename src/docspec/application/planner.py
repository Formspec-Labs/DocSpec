"""Streaming change planning into bounded, persistent DocumentStore jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, cast

from docspec.domain.content import SourceItem, SourceItemState
from docspec.domain.identity import identity_digest, require_text
from docspec.domain.jobs import ChangeKind, DocumentEntry, DocumentStore, EntryExecutionMode
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, SourceCatalogRef, StoreRef
from docspec.domain.release import DocumentRelease
from docspec.domain.storage import partition_bucket
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.control_repository import ControlRepository
from docspec.ports.document_catalog import DocumentCatalog, DocumentCatalogReader
from docspec.ports.document_store_repository import DocumentStoreRepository
from docspec.ports.record_workspace import RecordWorkspace, RecordWorkspaceFactory
from docspec.ports.source_catalog import ImmutableSourceCatalogReader

_SELECTION_FIELDS = frozenset(
    {
        "includeItemIds",
        "excludeItemIds",
        "itemIdPrefixes",
        "logicalBuckets",
        "mediaTypes",
        "sourcePartitions",
        "states",
    }
)
_ESTIMATE_FIELDS = (
    "estimatedBytes",
    "estimatedPagesOrFrames",
    "expectedSegments",
    "processorCost",
    "estimatedMemoryBytes",
    "estimatedDurationSeconds",
)
_PLANNING_STORE_ORDER_COLLECTION = "planner:store-order"
_SOURCE_PARTITION_METADATA_FIELD = "sourcePartition"


def logical_partition(item_id: str, bucket_count: int) -> int:
    """Assign one source identity to a stable logical bucket."""

    return partition_bucket(item_id, bucket_count)


def _metadata_integer(item: SourceItem, key: str, default: int) -> int:
    value = item.metadata.get(key, default) if item.metadata is not None else default
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise IntegrityError(f"source item {item.item_id} has invalid {key}")
    return value


@dataclass(frozen=True, slots=True)
class WorkEstimate:
    estimated_bytes: int
    pages_or_frames: int
    segments: int
    processor_cost: int
    memory_bytes: int
    duration_seconds: int

    def exceeds(self, limits: WorkLimits) -> str | None:
        checks = (
            ("estimated bytes", self.estimated_bytes, limits.max_estimated_bytes),
            ("pages or frames", self.pages_or_frames, limits.max_pages_or_frames),
            ("segments", self.segments, limits.max_segments),
            ("processor cost", self.processor_cost, limits.max_processor_cost),
            ("memory bytes", self.memory_bytes, limits.max_memory_bytes),
            ("duration seconds", self.duration_seconds, limits.max_duration_seconds),
        )
        return next((name for name, value, maximum in checks if value > maximum), None)

    def plus(self, other: WorkEstimate) -> WorkEstimate:
        return WorkEstimate(
            self.estimated_bytes + other.estimated_bytes,
            self.pages_or_frames + other.pages_or_frames,
            self.segments + other.segments,
            self.processor_cost + other.processor_cost,
            self.memory_bytes + other.memory_bytes,
            self.duration_seconds + other.duration_seconds,
        )


_ZERO_ESTIMATE = WorkEstimate(0, 0, 0, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class _PriorSourceItem:
    item: SourceItem
    deleted: bool


@dataclass(frozen=True, slots=True)
class _PlanImpact:
    """How an otherwise unchanged active source item should be rebuilt."""

    change_kind: ChangeKind | None
    processor_ids: tuple[str, ...] = ()
    processor_only: bool = False

    def requested_stages(self, plan: ProcessingPlan) -> StagePolicy:
        return StagePolicy(
            plan.stages.extractor_ids,
            plan.stages.segmenter_id,
            self.processor_ids,
        )


def _source_item_digest(item: SourceItem) -> str:
    """Identify the complete canonical source-item description."""

    return identity_digest(item.to_dict())


def _string_selector(selection: dict[str, Any], field: str) -> tuple[str, ...] | None:
    if field not in selection:
        return None
    value = selection[field]
    if not isinstance(value, list):
        raise IntegrityError(f"processing plan selection {field} must be an array")
    compiled: list[str] = []
    for index, item in enumerate(value):
        try:
            compiled.append(require_text(item, f"processing plan selection {field}[{index}]"))
        except ValueError as error:
            raise IntegrityError(str(error)) from error
    return tuple(dict.fromkeys(compiled))


@dataclass(frozen=True, slots=True)
class _CompiledSelection:
    """Validated, reusable selection indexes for a streaming planning run."""

    include_item_ids: frozenset[str] | None
    exclude_item_ids: frozenset[str]
    item_id_prefixes: tuple[str, ...]
    logical_buckets: frozenset[int]
    media_types: frozenset[str]
    source_partitions: frozenset[str]
    states: frozenset[SourceItemState]

    @classmethod
    def compile(cls, value: object, *, partition_count: int) -> _CompiledSelection:
        if not isinstance(value, dict):
            raise IntegrityError("processing plan selection must be an object")
        unknown = set(value) - _SELECTION_FIELDS
        if unknown:
            raise IntegrityError(f"processing plan contains unknown selection fields: {sorted(unknown)}")

        include = _string_selector(value, "includeItemIds")
        exclude = _string_selector(value, "excludeItemIds")
        prefixes = _string_selector(value, "itemIdPrefixes")
        media_types = _string_selector(value, "mediaTypes")
        source_partitions = _string_selector(value, "sourcePartitions")
        state_values = _string_selector(value, "states")

        states: set[SourceItemState] = set()
        for state in state_values or ():
            try:
                states.add(SourceItemState(state))
            except ValueError as error:
                raise IntegrityError(f"processing plan selection contains unknown source-item state {state!r}") from error

        bucket_values = value.get("logicalBuckets", [])
        if "logicalBuckets" in value and not isinstance(bucket_values, list):
            raise IntegrityError("processing plan selection logicalBuckets must be an array")
        logical_buckets: set[int] = set()
        for index, bucket in enumerate(bucket_values):
            if not isinstance(bucket, int) or isinstance(bucket, bool) or not 0 <= bucket < partition_count:
                raise IntegrityError(
                    "processing plan selection "
                    f"logicalBuckets[{index}] must be an integer between 0 and {partition_count - 1}"
                )
            logical_buckets.add(bucket)

        return cls(
            None if include is None else frozenset(include),
            frozenset(exclude or ()),
            prefixes or (),
            frozenset(logical_buckets),
            frozenset(media_types or ()),
            frozenset(source_partitions or ()),
            frozenset(states),
        )

    def validate_source_partitions(self, declared: tuple[str, ...]) -> None:
        if not self.source_partitions:
            return
        unknown = self.source_partitions - frozenset(declared)
        if unknown:
            raise IntegrityError(
                "processing plan selection names source partitions not declared by the catalog: "
                f"{sorted(unknown)}"
            )

    def matches(self, item: SourceItem, *, logical_bucket: int) -> bool:
        if self.include_item_ids is not None and item.item_id not in self.include_item_ids:
            return False
        if item.item_id in self.exclude_item_ids:
            return False
        if self.item_id_prefixes and not item.item_id.startswith(self.item_id_prefixes):
            return False
        if self.logical_buckets and logical_bucket not in self.logical_buckets:
            return False
        if self.states and item.state not in self.states:
            return False
        if self.source_partitions:
            source_partition = (item.metadata or {}).get(_SOURCE_PARTITION_METADATA_FIELD)
            if source_partition is None:
                return False
            try:
                source_partition = require_text(source_partition, "source item sourcePartition")
            except ValueError as error:
                raise IntegrityError(f"source item {item.item_id} has invalid sourcePartition: {error}") from error
            if source_partition not in self.source_partitions:
                return False
        return not self.media_types or any(
            candidate.media_type in self.media_types for candidate in item.candidates
        )


def estimate_item(item: SourceItem, limits: WorkLimits, processor_count: int) -> WorkEstimate:
    """Turn declared estimates into all seven planner admission dimensions."""

    if item.state != SourceItemState.ACTIVE:
        return WorkEstimate(0, 0, 0, 0, 1, 1)
    declared_sizes = [candidate.expected_size for candidate in item.candidates]
    if "estimatedBytes" in (item.metadata or {}):
        estimated_bytes = _metadata_integer(item, "estimatedBytes", 0)
    elif all(value is not None for value in declared_sizes):
        estimated_bytes = sum(cast(int, value) for value in declared_sizes)
    else:
        estimated_bytes = limits.max_estimated_bytes
    pages = _metadata_integer(item, "estimatedPagesOrFrames", 1)
    segments = _metadata_integer(item, "expectedSegments", max(1, pages))
    processor_cost = _metadata_integer(item, "processorCost", max(1, segments * max(1, processor_count)))
    memory = _metadata_integer(item, "estimatedMemoryBytes", min(max(1, estimated_bytes), limits.max_memory_bytes))
    duration = _metadata_integer(item, "estimatedDurationSeconds", 1)
    return WorkEstimate(estimated_bytes, pages, segments, processor_cost, memory, duration)


def estimate_processor_reprocessing(
    item: SourceItem,
    limits: WorkLimits,
    processor_count: int,
) -> WorkEstimate:
    """Estimate processor work while source, representation, and segment bytes are reused."""

    segments = _metadata_integer(item, "expectedSegments", 1)
    processor_cost = _metadata_integer(item, "processorCost", segments * processor_count)
    memory = _metadata_integer(item, "estimatedMemoryBytes", limits.max_memory_bytes)
    duration = _metadata_integer(item, "estimatedDurationSeconds", 1)
    return WorkEstimate(0, 0, segments, processor_cost, memory, duration)


@dataclass(slots=True)
class _StoreBuffer:
    partition: int
    sequence: int
    entries: list[DocumentEntry] = field(default_factory=list)
    estimate: WorkEstimate = _ZERO_ESTIMATE

    def can_add(self, estimate: WorkEstimate, limits: WorkLimits) -> bool:
        if len(self.entries) >= limits.max_entries:
            return False
        return self.estimate.plus(estimate).exceeds(limits) is None

    def add(self, entry: DocumentEntry, estimate: WorkEstimate) -> None:
        self.entries.append(entry)
        self.estimate = self.estimate.plus(estimate)


class RunPlanner:
    """Compare one fixed source catalog to an explicit release and save bounded jobs."""

    def __init__(
        self,
        *,
        source_catalog: ImmutableSourceCatalogReader,
        document_catalog: DocumentCatalog,
        stores: DocumentStoreRepository,
        controls: ControlRepository,
        workspace_factory: RecordWorkspaceFactory,
    ) -> None:
        self._source_catalog = source_catalog
        self._document_catalog = document_catalog
        self._stores = stores
        self._controls = controls
        self._workspace_factory = workspace_factory

    def plan_run(
        self,
        source_catalog_ref: SourceCatalogRef,
        base_document_release_ref: DocumentReleaseRef | None,
        plan_ref: ArtifactRef,
    ) -> Iterator[StoreRef]:
        """Stream saved planned-store references; source items never enter scheduler messages."""

        self._controls.verify(plan_ref)
        plan = ProcessingPlan.from_dict(self._controls.load(plan_ref))
        if plan.source_catalog != source_catalog_ref or plan.base_release != base_document_release_ref:
            raise IntegrityError("plan inputs differ from the requested source catalog or base release")
        selection = _CompiledSelection.compile(plan.selection, partition_count=plan.partition_count)
        source_snapshot = self._source_catalog.open_snapshot(source_catalog_ref)
        source_summary = source_snapshot.summary
        selection.validate_source_partitions(source_summary.partitions)
        base_reader = self._open_base_reader(base_document_release_ref)
        base = None if base_reader is None else base_reader.release
        plan_impact = self._plan_impact(base, plan)
        with self._workspace_factory.create() as workspace:
            ledger = self._stores.seal_planned_stores(
                plan.plan_id,
                self._plan_store_references(
                    plan,
                    (item.to_processing_item() for item in source_snapshot.items),
                    base_reader,
                    plan_impact,
                    selection,
                    workspace,
                ),
            )
        yield from self._stores.stream_planned_stores(ledger)

    def _plan_store_references(
        self,
        plan: ProcessingPlan,
        source_items: Iterator[SourceItem],
        base_reader: DocumentCatalogReader | None,
        plan_impact: _PlanImpact,
        selection: _CompiledSelection,
        workspace: RecordWorkspace,
    ) -> Iterator[StoreRef]:
        """Spool selected entries, then build one bounded partition buffer at a time."""

        touched_partitions: set[int] = set()
        ordinal = 0
        for item, change in self._planned_changes(
            source_items,
            base_reader,
            plan_impact.change_kind,
        ):
            bucket = logical_partition(item.item_id, plan.partition_count)
            if not selection.matches(item, logical_bucket=bucket):
                continue
            if change == ChangeKind.UNCHANGED:
                continue
            if change == ChangeKind.REPAIR and plan_impact.processor_only:
                estimate = estimate_processor_reprocessing(
                    item,
                    plan.limits,
                    len(plan_impact.processor_ids),
                )
            else:
                estimate = estimate_item(item, plan.limits, len(plan.stages.processor_ids))
            exceeded = estimate.exceeds(plan.limits)
            if exceeded is not None:
                raise LimitExceededError(f"source item {item.item_id} exceeds the per-store {exceeded} limit")
            entry = DocumentEntry.create(
                item,
                change,
                plan_impact.requested_stages(plan)
                if change == ChangeKind.REPAIR and plan_impact.processor_only
                else plan.stages,
                execution_mode=(
                    EntryExecutionMode.PROCESSORS_ONLY
                    if change == ChangeKind.REPAIR and plan_impact.processor_only
                    else EntryExecutionMode.FULL
                ),
            )
            workspace.add_record(
                self._partition_collection(bucket),
                identity=f"{ordinal:020d}",
                source_item_id=item.item_id,
                record={"ordinal": ordinal, "entry": entry.to_dict()},
            )
            touched_partitions.add(bucket)
            ordinal += 1

        for bucket in sorted(touched_partitions):
            self._save_partition_stores(plan, bucket, workspace)
        for row in workspace.stream_records(_PLANNING_STORE_ORDER_COLLECTION):
            if set(row) != {"store"} or not isinstance(row["store"], dict):
                raise IntegrityError("planning store-order record has an invalid closed shape")
            try:
                yield StoreRef.from_dict(row["store"])
            except (TypeError, ValueError) as error:
                raise IntegrityError(f"planning store-order reference is invalid: {error}") from error

    @staticmethod
    def _partition_collection(partition: int) -> str:
        return f"planner:partition:{partition:05d}"

    @staticmethod
    def _spooled_entry(row: dict[str, Any]) -> tuple[int, DocumentEntry]:
        if set(row) != {"ordinal", "entry"}:
            raise IntegrityError("spooled planning entry has an invalid closed shape")
        ordinal = row["ordinal"]
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
            raise IntegrityError("spooled planning entry has an invalid ordinal")
        if not isinstance(row["entry"], dict):
            raise IntegrityError("spooled planning entry payload must be an object")
        try:
            entry = DocumentEntry.from_dict(row["entry"])
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"spooled planning entry is invalid: {error}") from error
        return ordinal, entry

    @staticmethod
    def _entry_estimate(entry: DocumentEntry, limits: WorkLimits) -> WorkEstimate:
        if entry.execution_mode == EntryExecutionMode.PROCESSORS_ONLY:
            return estimate_processor_reprocessing(
                entry.source_item,
                limits,
                len(entry.requested_stages.processor_ids),
            )
        return estimate_item(entry.source_item, limits, len(entry.requested_stages.processor_ids))

    def _remember_store(
        self,
        workspace: RecordWorkspace,
        reference: StoreRef,
        *,
        order_identity: str,
    ) -> None:
        workspace.add_record(
            _PLANNING_STORE_ORDER_COLLECTION,
            identity=order_identity,
            source_item_id=reference.store_id,
            record={"store": reference.to_dict()},
        )

    def _save_partition_stores(
        self,
        plan: ProcessingPlan,
        partition: int,
        workspace: RecordWorkspace,
    ) -> None:
        buffer = _StoreBuffer(partition, 0)
        for row in workspace.stream_records(self._partition_collection(partition)):
            ordinal, entry = self._spooled_entry(row)
            estimate = self._entry_estimate(entry, plan.limits)
            if buffer.entries and not buffer.can_add(estimate, plan.limits):
                reference = self._save_buffer(plan, buffer)
                self._remember_store(
                    workspace,
                    reference,
                    order_identity=f"closed/{ordinal:020d}",
                )
                buffer = _StoreBuffer(partition, buffer.sequence + 1)
            buffer.add(entry, estimate)
        if buffer.entries:
            reference = self._save_buffer(plan, buffer)
            self._remember_store(
                workspace,
                reference,
                order_identity=f"remaining/{partition:05d}",
            )

    def _open_base_reader(self, reference: DocumentReleaseRef | None) -> DocumentCatalogReader | None:
        if reference is None:
            return None
        return self._document_catalog.open_reader(reference)

    def _plan_impact(self, base: DocumentRelease | None, plan: ProcessingPlan) -> _PlanImpact:
        if base is None:
            return _PlanImpact(None)
        self._controls.verify(base.processing_plan)
        try:
            previous = ProcessingPlan.from_dict(self._controls.load(base.processing_plan))
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"base release processing plan is invalid: {error}") from error
        if previous.governing_content() == plan.governing_content():
            return _PlanImpact(None)
        if self._non_processor_governing_content(previous) != self._non_processor_governing_content(plan):
            return _PlanImpact(ChangeKind.REPAIR, plan.stages.processor_ids)

        previous_by_name = {item.name: item for item in previous.processors.processors}
        current_by_name = {item.name: item for item in plan.processors.processors}
        if len(previous_by_name) != len(previous.processors.processors) or len(current_by_name) != len(
            plan.processors.processors
        ):
            raise IntegrityError("processor-only invalidation requires distinct stable processor names")
        changed = tuple(
            current_by_name[name].processor_id
            for name in sorted(current_by_name)
            if name not in previous_by_name or current_by_name[name] != previous_by_name[name]
        )
        removed = set(previous_by_name) - set(current_by_name)
        if not changed and not removed:
            return _PlanImpact(ChangeKind.REPAIR, plan.stages.processor_ids)
        return _PlanImpact(
            ChangeKind.REPAIR,
            () if not changed else plan.processors.invalidated_by(changed),
            processor_only=True,
        )

    @staticmethod
    def _non_processor_governing_content(plan: ProcessingPlan) -> dict[str, Any]:
        content = plan.governing_content()
        content.pop("processors")
        content["stages"] = {
            "extractorIds": list(plan.stages.extractor_ids),
            "segmenterId": plan.stages.segmenter_id,
        }
        return content

    def _planned_changes(
        self,
        current_items: Iterator[SourceItem],
        base_reader: DocumentCatalogReader | None,
        unchanged_change: ChangeKind | None,
    ) -> Iterator[tuple[SourceItem, ChangeKind]]:
        if base_reader is None:
            for item in current_items:
                yield item, self._classify(item, None, unchanged_change)
            return

        yield from self._merge_snapshot(
            current_items,
            self._previous_source_items(base_reader),
            unchanged_change,
        )

    @staticmethod
    def _prior_source_item(record: dict[str, Any]) -> _PriorSourceItem:
        """Validate one source-item record returned by a verified release reader."""

        expected = {"recordId", "sourceItemId", "idempotencyKey", "deleted", "payload"}
        if set(record) != expected or not isinstance(record["payload"], dict):
            raise IntegrityError("base source-item record has an invalid closed shape")
        try:
            item = SourceItem.from_dict(record["payload"])
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"base source-item payload is invalid: {error}") from error
        if record["recordId"] != item.item_id or record["sourceItemId"] != item.item_id:
            raise IntegrityError("base source-item record identity differs from its payload")
        deleted = record["deleted"]
        if not isinstance(deleted, bool) or deleted != (item.state == SourceItemState.DELETED):
            raise IntegrityError("base source-item deletion marker differs from its payload")
        return _PriorSourceItem(item, deleted)

    def _previous_source_items(self, base_reader: DocumentCatalogReader) -> Iterator[_PriorSourceItem]:
        """Read the verified base source layer once for the whole planning run."""

        for record in base_reader.scan(layer_kind="source-items"):
            yield self._prior_source_item(record)

    def _merge_snapshot(
        self,
        current_items: Iterator[SourceItem],
        previous_items: Iterator[_PriorSourceItem],
        unchanged_change: ChangeKind | None,
    ) -> Iterator[tuple[SourceItem, ChangeKind]]:
        """Merge one complete snapshot with prior state using bounded memory."""

        current = next(current_items, None)
        previous = next(previous_items, None)
        while current is not None or previous is not None:
            if previous is None or (current is not None and current.item_id < previous.item.item_id):
                assert current is not None
                yield current, self._classify(current, None, unchanged_change)
                current = next(current_items, None)
                continue
            if current is None or previous.item.item_id < current.item_id:
                if not previous.deleted:
                    tombstone = SourceItem(
                        item_id=previous.item.item_id,
                        version=previous.item.version,
                        candidates=previous.item.candidates,
                        state=SourceItemState.DELETED,
                        metadata=previous.item.metadata,
                    )
                    yield tombstone, ChangeKind.DELETED
                previous = next(previous_items, None)
                continue
            yield current, self._classify(current, previous, unchanged_change)
            current = next(current_items, None)
            previous = next(previous_items, None)

    @staticmethod
    def _classify(
        item: SourceItem,
        previous: _PriorSourceItem | None,
        unchanged_change: ChangeKind | None,
    ) -> ChangeKind:
        if previous is None:
            if item.state == SourceItemState.DELETED:
                return ChangeKind.DELETED
            if item.state == SourceItemState.EXCLUDED:
                return ChangeKind.EXCLUDED
            return ChangeKind.ADDED
        if _source_item_digest(previous.item) == _source_item_digest(item):
            if unchanged_change is not None and item.state == SourceItemState.ACTIVE:
                return unchanged_change
            return ChangeKind.UNCHANGED
        if item.state == SourceItemState.DELETED:
            return ChangeKind.DELETED
        if item.state == SourceItemState.EXCLUDED:
            return ChangeKind.EXCLUDED
        if previous.deleted:
            return ChangeKind.ADDED
        return ChangeKind.CHANGED

    def _save_buffer(self, plan: ProcessingPlan, buffer: _StoreBuffer) -> StoreRef:
        logical = f"bucket-{buffer.partition:05d}/store-{buffer.sequence:08d}"
        store = DocumentStore.planned(
            plan_id=plan.plan_id,
            logical_partition=logical,
            entries=tuple(buffer.entries),
            limits=plan.limits,
        )
        return self._stores.save(store)
