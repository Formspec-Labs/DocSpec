"""Resumable acquisition and processing of one bounded DocumentStore reference."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from typing import Any

from docspec.domain.content import (
    AcquisitionDisposition,
    CapturedFile,
    DerivedRecord,
    Representation,
    Segment,
    SourceItem,
)
from docspec.domain.identity import stable_urn
from docspec.domain.jobs import (
    DocumentEntry,
    DocumentStore,
    EntryExecutionMode,
    FailureClass,
    FailureRecord,
    StoreState,
)
from docspec.domain.plans import ProcessingPlan
from docspec.domain.policies import (
    AcceptedFailurePolicy,
    DataUsePolicy,
    ProcessorExecutionScope,
    RetryPolicy,
)
from docspec.domain.processors import (
    ProcessorCacheMode,
    ProcessorDescription,
    ProcessorPayload,
    ProcessorRequest,
    ProcessorResult,
    ProcessorSet,
)
from docspec.domain.references import ArtifactRef, StoreRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.blob_store import BlobStore
from docspec.ports.content_fetcher import ContentFetcher
from docspec.ports.control_repository import ControlRepository
from docspec.ports.document_store_repository import DocumentStoreRepository
from docspec.ports.document_catalog import DocumentCatalog, DocumentCatalogReader
from docspec.ports.extractor import Extractor
from docspec.ports.processor import Processor
from docspec.ports.processor_cache import ProcessorResultCache
from docspec.ports.segmenter import Segmenter
from docspec.processing.artifacts import RepresentationPayload, SegmentPayload, verify_segment_representation
from docspec.processing.extraction import ExtractionResult
from docspec.processing.segmentation import SegmentationReceipt

from .execution_checkpoints import EntryCheckpointVerifier, VerifiedEntryCheckpoint
from .processor_rules import (
    flatten_processor_records,
    processor_request,
    projected_segment_byte_size,
    require_segment_input,
    validate_processor_result,
)
from .store_state import load_latest_store
from .work_budget import MemoryScope, WorkBudget


class StoreExecutionService:
    """Execute the same reference-only task locally or behind any scheduler."""

    def __init__(
        self,
        *,
        plan_ref: ArtifactRef,
        controls: ControlRepository,
        stores: DocumentStoreRepository,
        document_catalog: DocumentCatalog,
        blobs: BlobStore,
        fetcher: ContentFetcher,
        extractor: Extractor[ExtractionResult],
        segmenter: Segmenter[RepresentationPayload, SegmentPayload],
        processors: Mapping[str, Processor[ProcessorPayload, ProcessorResult]],
        retry_policy: RetryPolicy,
        accepted_failure_policy: AcceptedFailurePolicy,
        clock: Callable[[], str],
        processor_cache: ProcessorResultCache | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._plan_ref = plan_ref
        self._controls = controls
        self._stores = stores
        self._document_catalog = document_catalog
        self._blobs = blobs
        self._fetcher = fetcher
        self._extractor = extractor
        self._segmenter = segmenter
        self._processors = dict(processors)
        self._retry_policy = retry_policy
        self._accepted_failure_policy = accepted_failure_policy
        self._clock = clock
        self._processor_cache = processor_cache
        self._sleep = sleep
        self._monotonic = monotonic
        self._checkpoints = EntryCheckpointVerifier(
            controls=controls,
            blobs=blobs,
            extractor=extractor,
            retry_policy=retry_policy,
        )

    def execute_store(self, planned_document_store_ref: StoreRef) -> StoreRef:
        plan = self._load_plan()
        current_ref, store = load_latest_store(self._stores, planned_document_store_ref)
        if store.plan_id != plan.plan_id:
            raise IntegrityError("document store belongs to another processing plan")
        if store.state == StoreState.SEALED:
            for entry in store.entries:
                self._checkpoints.verify_terminal_entry(entry, plan)
            if store.delivery_receipt is None:
                raise IntegrityError("sealed document store has no delivery receipt")
            self._controls.verify(store.delivery_receipt)
            return current_ref
        budget = WorkBudget(plan.limits, monotonic=self._monotonic)
        verified_checkpoints: dict[str, VerifiedEntryCheckpoint] = {}
        verified_processor_invocations: dict[str, tuple[str, ...]] = {}
        for entry in store.entries:
            checkpoint = self._checkpoints.verify_entry(entry, plan)
            verified_checkpoints[entry.entry_id] = checkpoint
            verified_processor_invocations[entry.entry_id] = checkpoint.processor_invocations
        budget.check_duration()
        budget.seed_verified_entries(store.entries, verified_processor_invocations)
        base_reader = self._reprocessing_reader(store, plan)
        attempt_number = len(store.attempts) + 1
        attempt_id = stable_urn("store-attempt", {"storeId": store.store_id, "attempt": attempt_number})
        store = store.start(attempt_id)
        current_ref = self._stores.save(store)
        entries = list(store.entries)
        for index, entry in enumerate(entries):
            if entry.terminal:
                continue
            budget.check_duration()

            def checkpoint_entry(partial: DocumentEntry) -> None:
                nonlocal store, current_ref
                self._checkpoints.verify_entry(partial, plan)
                entries[index] = partial
                store = store.checkpoint(tuple(entries))
                current_ref = self._stores.save(store)

            entries[index] = self._execute_entry(
                entry,
                store,
                plan,
                attempt_id,
                budget,
                base_reader,
                verified_checkpoints[entry.entry_id],
                checkpoint_entry,
            )
            self._checkpoints.verify_terminal_entry(entries[index], plan)
            store = store.checkpoint(tuple(entries))
            current_ref = self._stores.save(store)
        return current_ref

    def _reprocessing_reader(
        self,
        store: DocumentStore,
        plan: ProcessingPlan,
    ) -> DocumentCatalogReader | None:
        if not any(entry.execution_mode == EntryExecutionMode.PROCESSORS_ONLY for entry in store.entries):
            return None
        if plan.base_release is None:
            raise IntegrityError("processor-only reprocessing requires a pinned base release")
        return self._document_catalog.open_reader(plan.base_release)

    def _load_plan(self) -> ProcessingPlan:
        self._controls.verify(self._plan_ref)
        plan = ProcessingPlan.from_dict(self._controls.load(self._plan_ref))
        if plan.retry_policy_digest != self._retry_policy.digest:
            raise IntegrityError("injected retry policy differs from the processing plan")
        if plan.accepted_failure_policy_digest != self._accepted_failure_policy.digest:
            raise IntegrityError("injected accepted-failure policy differs from the processing plan")
        if plan.limits.max_attempts != self._retry_policy.max_attempts:
            raise IntegrityError("work limits and retry policy disagree on maximum attempts")
        descriptions = tuple(self._processor(identifier).description for identifier in plan.stages.processor_ids)
        processor_set = ProcessorSet(descriptions)
        if processor_set != plan.processors:
            raise IntegrityError("injected processor descriptions differ from the processing plan")
        expected_data_use = plan.data_use_policy.digest
        for description in descriptions:
            if description.data_use_policy_digest != expected_data_use:
                raise IntegrityError(f"processor {description.processor_id} differs from the plan data-use policy")
            if (
                description.execution_scope is ProcessorExecutionScope.DECLARED_EXTERNAL
                and not plan.data_use_policy.allows_external_processing
            ):
                raise IntegrityError(
                    f"processor {description.processor_id} declares external execution under a local-only data-use policy"
                )
            if description.retry_policy_digest != self._retry_policy.digest:
                raise IntegrityError(f"processor {description.processor_id} differs from the plan retry policy")
        return plan

    def _processor(self, identifier: str) -> Processor[ProcessorPayload, ProcessorResult]:
        try:
            processor = self._processors[identifier]
        except KeyError as error:
            raise IntegrityError(f"processing plan names unknown processor {identifier}") from error
        if processor.description.processor_id != identifier:
            raise IntegrityError("processor registry key differs from its description")
        return processor

    def _execute_entry(
        self,
        entry: DocumentEntry,
        store: DocumentStore,
        plan: ProcessingPlan,
        store_attempt_id: str,
        budget: WorkBudget,
        base_reader: DocumentCatalogReader | None,
        checkpoint: VerifiedEntryCheckpoint,
        checkpoint_entry: Callable[[DocumentEntry], None],
    ) -> DocumentEntry:
        if entry.execution_mode == EntryExecutionMode.PROCESSORS_ONLY:
            if base_reader is None:
                raise IntegrityError("processor-only reprocessing requires a verified base release")
            return self._reprocess_entry(
                entry,
                plan,
                budget,
                base_reader,
                checkpoint,
                checkpoint_entry,
            )

        captured = list(entry.captured_files)
        representations = list(entry.representations)
        representation_payloads: deque[tuple[RepresentationPayload, str]] = deque()
        segments = list(entry.segments)
        segment_payloads: list[SegmentPayload] = []
        processor_results = dict(checkpoint.processor_results)
        derived_by_processor: dict[str, list[DerivedRecord]] = {}
        for description in plan.processors.execution_order:
            records: list[DerivedRecord] = []
            for segment in segments:
                pair = processor_results.get((description.processor_id, segment.segment_id))
                if pair is not None:
                    records.extend(pair[1].derived_records)
            if records or description.processor_id in checkpoint.completed_processors:
                derived_by_processor[description.processor_id] = records
        if flatten_processor_records(plan, derived_by_processor) != entry.derived_records:
            raise IntegrityError("verified processor results differ from checkpoint records")
        failures = list(entry.failures)
        receipt_refs = list(entry.stage_receipts)

        def current_entry() -> DocumentEntry:
            return replace(
                entry,
                captured_files=tuple(captured),
                representations=tuple(representations),
                segments=tuple(segments),
                derived_records=flatten_processor_records(plan, derived_by_processor),
                failures=tuple(failures),
                stage_receipts=tuple(receipt_refs),
            )

        def failed(stage: str, error: Exception) -> DocumentEntry:
            failures.append(self._failure(stage, error, len(failures) + 1))
            return self._failed_entry(current_entry(), failures)

        with budget.materialization_scope() as memory:
            if not checkpoint.extraction_complete:
                try:
                    if representations:
                        representation_payloads = self._load_representation_payloads(
                            entry,
                            plan,
                            memory,
                        )
                except Exception as error:
                    return failed("processing", error)
                for index in range(len(representations), len(entry.source_item.candidates)):
                    candidate = entry.source_item.candidates[index]
                    budget.check_duration()
                    if index < len(captured):
                        captured_file = captured[index]
                    else:
                        captured_file, attempt_failures = self._capture_candidate(
                            entry,
                            store,
                            candidate,
                            plan,
                            store_attempt_id,
                            budget,
                        )
                        failures.extend(attempt_failures)
                        if captured_file is None:
                            return self._failed_entry(current_entry(), failures)
                        captured.append(captured_file)
                        checkpoint_entry(current_entry())
                    try:
                        source_unit = budget.stage_unit_id(entry.entry_id, "source", captured_file.file_id)
                        source_memory = f"source:{source_unit}"
                        source_bytes = self._read_worker_bytes(
                            captured_file,
                            plan,
                            memory,
                            source_memory,
                        )
                        extraction = self._extractor.extract(captured_file, source_bytes)
                        budget.check_duration()
                        extractor_registry_id = getattr(self._extractor, "extractor_id", None)
                        actual_extractor_id = extraction.payload.representation.extractor_id
                        if (
                            actual_extractor_id not in plan.stages.extractor_ids
                            and extractor_registry_id not in plan.stages.extractor_ids
                        ):
                            raise IntegrityError(
                                f"extractor {actual_extractor_id} is not pinned by the processing plan"
                            )
                        representation_id = extraction.payload.representation.representation_id
                        representation_unit = budget.stage_unit_id(
                            entry.entry_id,
                            "representation",
                            f"{captured_file.file_id}:{representation_id}",
                        )
                        representation_memory = f"representation:{representation_unit}"
                        if extraction.payload.content is source_bytes:
                            memory.rename(source_memory, representation_memory)
                        else:
                            memory.reserve(representation_memory, len(extraction.payload.content))
                            memory.release(source_memory)
                        budget.observe_extraction(
                            representation_unit,
                            representation_kind=extraction.payload.representation.kind,
                            metadata=extraction.receipt.metadata,
                        )
                        representation_payload = self._persist_representation(extraction.payload, plan)
                        representation_payloads.append((representation_payload, representation_unit))
                        representations.append(representation_payload.representation)
                        receipt_refs.append(
                            self._put_receipt(
                                "extraction-receipts",
                                "extraction-receipt",
                                extraction.receipt.to_dict(),
                            )
                        )
                        del extraction, source_bytes
                    except Exception as error:
                        return failed("processing", error)
                    checkpoint_entry(current_entry())
            elif not checkpoint.segmentation_complete:
                representation_payloads = self._load_representation_payloads(
                    entry,
                    plan,
                    memory,
                )

            if not checkpoint.segmentation_complete:
                try:
                    segmenter_registry_id = getattr(self._segmenter, "segmenter_id", None)
                    if segmenter_registry_id != plan.stages.segmenter_id:
                        raise IntegrityError("injected segmenter registry differs from the processing plan")
                    while representation_payloads:
                        representation, representation_unit = representation_payloads.popleft()
                        budget.check_duration()
                        results = self._segmenter.segment(representation)
                        representation_id = representation.representation.representation_id
                        budget.charge_segments(representation_unit, len(results))
                        for result in results:
                            verify_segment_representation(result, representation)
                            memory.reserve(
                                f"segment:{representation_unit}:{result.segment.segment_id}",
                                len(result.content),
                            )
                            persisted = self._persist_segment(result, plan)
                            segment_payloads.append(persisted)
                            segments.append(persisted.segment)
                        memory.release(f"representation:{representation_unit}")
                        segmentation_receipt = SegmentationReceipt(
                            representation_id,
                            plan.stages.segmenter_id,
                            tuple(result.segment.segment_id for result in results),
                        )
                        receipt_refs.append(
                            self._put_receipt(
                                "segmentation-receipts",
                                "segmentation-receipt",
                                segmentation_receipt.to_dict(),
                            )
                        )
                except Exception as error:
                    return failed("processing", error)
                checkpoint_entry(current_entry())

            remaining_processors = tuple(
                identifier
                for identifier in plan.stages.processor_ids
                if identifier not in checkpoint.completed_processors
            )
            if remaining_processors and not segment_payloads:
                segment_payloads = self._load_segment_payloads(entry, plan, memory, budget)

            for identifier in remaining_processors:
                try:
                    self._run_processor_graph(
                        entry,
                        plan,
                        segment_payloads,
                        (identifier,),
                        budget,
                        derived_by_processor,
                        processor_results,
                        receipt_refs,
                    )
                    budget.check_duration()
                except Exception as error:
                    return failed("processing", error)
                checkpoint_entry(current_entry())

            budget.check_duration()
            return replace(
                current_entry(),
                disposition=AcquisitionDisposition.CAPTURED,
            )

    def _run_processor_graph(
        self,
        entry: DocumentEntry,
        plan: ProcessingPlan,
        segments: list[SegmentPayload],
        requested: tuple[str, ...],
        budget: WorkBudget,
        derived_by_processor: dict[str, list[DerivedRecord]],
        result_by_processor_segment: dict[tuple[str, str], tuple[ArtifactRef, ProcessorResult]],
        receipt_refs: list[ArtifactRef],
    ) -> None:
        for identifier in requested:
            processor = self._processor(identifier)
            records = derived_by_processor.setdefault(identifier, [])
            for segment in segments:
                require_segment_input(processor.description, segment)
                segment_id = segment.segment.segment_id
                prerequisite_pairs = []
                for dependency in processor.description.dependencies:
                    pair = result_by_processor_segment.get((dependency, segment_id))
                    if pair is None:
                        raise IntegrityError(
                            f"processor {identifier} has no verified {dependency} result for segment {segment_id}"
                        )
                    prerequisite_pairs.append(pair)
                invocation_id = budget.processor_invocation_id(
                    entry.entry_id,
                    identifier,
                    (segment_id,),
                )
                request = processor_request(
                    self._plan_ref,
                    entry,
                    plan,
                    processor.description,
                    segment.segment,
                    tuple(reference for reference, _ in prerequisite_pairs),
                    invocation_id,
                )
                payload = ProcessorPayload.for_segment(
                    segment.segment,
                    segment.content,
                    request.allowed_fields,
                )
                if prerequisite_pairs and "prerequisiteResults" not in request.allowed_fields:
                    raise IntegrityError(
                        f"processor {identifier} requires prerequisite results excluded by the data-use policy"
                    )
                result, result_ref, cache_disposition = self._invoke_processor(
                    processor,
                    request,
                    payload,
                    plan.data_use_policy,
                    segment,
                    tuple(result for _, result in prerequisite_pairs),
                    budget,
                    receipt_refs,
                )
                records.extend(result.derived_records)
                result_by_processor_segment[(identifier, segment_id)] = (result_ref, result)
                receipt_refs.append(
                    self._put_receipt(
                        "processor-invocation-receipts",
                        "processor-invocation-receipt",
                        {
                            "format": "docspec-processor-invocation-receipt",
                            "formatVersion": "1.0",
                            "processorId": identifier,
                            "segmentId": segment_id,
                            "request": request.to_dict(),
                            "result": result_ref.to_dict(),
                            "cacheDisposition": cache_disposition,
                        },
                    )
                )

    def _invoke_processor(
        self,
        processor: Processor[ProcessorPayload, ProcessorResult],
        request: ProcessorRequest,
        payload: ProcessorPayload,
        data_use_policy: DataUsePolicy,
        segment: SegmentPayload,
        prerequisites: tuple[ProcessorResult, ...],
        budget: WorkBudget,
        receipt_refs: list[ArtifactRef],
    ) -> tuple[ProcessorResult, ArtifactRef, str]:
        budget.check_duration()
        budget.charge_processor(request.invocation_id)
        description = processor.description
        cache_enabled = (
            self._processor_cache is not None
            and description.deterministic
            and description.cache_policy.mode is ProcessorCacheMode.EXACT_INPUTS
        )
        cache_disposition = "bypassed"
        if cache_enabled:
            try:
                cached_ref = self._processor_cache.lookup(request.reuse_key)
            except Exception:
                cache_disposition = "unavailable"
            else:
                if cached_ref is None:
                    cache_disposition = "miss"
                else:
                    try:
                        cached = self._verified_cached_result(
                            cached_ref,
                            request,
                            description,
                            segment,
                            prerequisites,
                            data_use_policy,
                        )
                    except Exception:
                        cache_disposition = "invalid"
                        try:
                            self._processor_cache.discard(request.reuse_key, cached_ref)
                        except Exception:
                            cache_disposition = "unavailable"
                    else:
                        budget.check_duration()
                        return cached, cached_ref, "hit"

        result: ProcessorResult | None = None
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            attempt_started = self._monotonic()
            try:
                candidate = processor.process(request, payload, prerequisites)
                elapsed_seconds = self._monotonic() - attempt_started
                if elapsed_seconds < 0:
                    raise IntegrityError("processor monotonic clock moved backwards")
                if elapsed_seconds > description.item_limits.max_duration_seconds:
                    raise LimitExceededError("processor execution exceeds its declared item duration limit")
                validate_processor_result(
                    candidate,
                    request,
                    description,
                    segment.segment,
                    payload.input_byte_size,
                    prerequisites,
                    data_use_policy=data_use_policy,
                    require_current_request=True,
                )
            except Exception as error:
                elapsed_seconds = self._monotonic() - attempt_started
                if elapsed_seconds < 0:
                    raise IntegrityError("processor monotonic clock moved backwards") from error
                failure = self._failure("processor", error, attempt)
                receipt_refs.append(
                    self._put_receipt(
                        "processor-attempt-receipts",
                        "processor-attempt-receipt",
                        {
                            "format": "docspec-processor-attempt-receipt",
                            "formatVersion": "1.0",
                            "processorId": description.processor_id,
                            "segmentId": segment.segment.segment_id,
                            "requestId": request.request_id,
                            "invocationId": request.invocation_id,
                            "attempt": attempt,
                            "outcome": "failed",
                            "elapsedMilliseconds": int(elapsed_seconds * 1000),
                            "failure": failure.to_dict(),
                        },
                    )
                )
                if not failure.retryable or attempt == self._retry_policy.max_attempts:
                    raise
                delay = self._retry_policy.delay_milliseconds(request.invocation_id, attempt) / 1000
                self._sleep(delay)
                budget.check_duration()
                continue
            receipt_refs.append(
                self._put_receipt(
                    "processor-attempt-receipts",
                    "processor-attempt-receipt",
                    {
                        "format": "docspec-processor-attempt-receipt",
                        "formatVersion": "1.0",
                        "processorId": description.processor_id,
                        "segmentId": segment.segment.segment_id,
                        "requestId": request.request_id,
                        "invocationId": request.invocation_id,
                        "attempt": attempt,
                        "outcome": "succeeded",
                        "elapsedMilliseconds": int(elapsed_seconds * 1000),
                        "failure": None,
                    },
                )
            )
            result = candidate
            break
        if result is None:
            raise IntegrityError("processor retry loop ended without a result or failure")
        budget.check_duration()
        result_ref = self._controls.put(
            kind="processor-results",
            artifact_id=result.result_id,
            value=result.to_dict(),
        )
        if cache_enabled:
            try:
                winner_ref = self._processor_cache.put_if_absent(request.reuse_key, result_ref)
            except Exception:
                cache_disposition = "unavailable"
            else:
                if winner_ref != result_ref:
                    try:
                        winner = self._verified_cached_result(
                            winner_ref,
                            request,
                            description,
                            segment,
                            prerequisites,
                            data_use_policy,
                        )
                    except Exception:
                        try:
                            self._processor_cache.discard(request.reuse_key, winner_ref)
                            replacement_ref = self._processor_cache.put_if_absent(
                                request.reuse_key,
                                result_ref,
                            )
                        except Exception:
                            cache_disposition = "unavailable"
                        else:
                            if replacement_ref != result_ref:
                                try:
                                    replacement = self._verified_cached_result(
                                        replacement_ref,
                                        request,
                                        description,
                                        segment,
                                        prerequisites,
                                        data_use_policy,
                                    )
                                except Exception:
                                    cache_disposition = "unavailable"
                                else:
                                    return replacement, replacement_ref, "hit"
                    else:
                        return winner, winner_ref, "hit"
        return result, result_ref, cache_disposition

    def _verified_cached_result(
        self,
        reference: ArtifactRef,
        request: ProcessorRequest,
        description: ProcessorDescription,
        segment: SegmentPayload,
        prerequisites: tuple[ProcessorResult, ...],
        data_use_policy: DataUsePolicy,
    ) -> ProcessorResult:
        cached = ProcessorResult.from_dict(self._controls.load(reference))
        validate_processor_result(
            cached,
            request,
            description,
            segment.segment,
            ProcessorPayload.for_segment(
                segment.segment,
                segment.content,
                request.allowed_fields,
            ).input_byte_size,
            prerequisites,
            data_use_policy=data_use_policy,
            require_current_request=False,
        )
        if cached.result_id != reference.artifact_id:
            raise IntegrityError("cached processor-result identity differs from its reference")
        return cached

    @staticmethod
    def _base_payloads(
        reader: DocumentCatalogReader,
        *,
        layer_kind: str,
        source_item_id: str,
    ) -> Iterator[dict[str, Any]]:
        expected = {"recordId", "sourceItemId", "idempotencyKey", "deleted", "payload"}
        for row in reader.scan_source(layer_kind=layer_kind, source_item_id=source_item_id):
            if set(row) != expected or row["sourceItemId"] != source_item_id:
                raise IntegrityError(f"base {layer_kind!r} record has an invalid closed shape")
            if not isinstance(row["deleted"], bool) or not isinstance(row["payload"], dict):
                raise IntegrityError(f"base {layer_kind!r} record has an invalid payload wrapper")
            yield row["payload"]

    def _reprocess_entry(
        self,
        entry: DocumentEntry,
        plan: ProcessingPlan,
        budget: WorkBudget,
        reader: DocumentCatalogReader,
        checkpoint: VerifiedEntryCheckpoint,
        checkpoint_entry: Callable[[DocumentEntry], None],
    ) -> DocumentEntry:
        """Reuse exact base content and run only the invalid processor subgraph."""

        source_item_id = entry.source_item.item_id
        requested = entry.requested_stages.processor_ids
        requested_set = set(requested)
        current_processor_ids = set(plan.stages.processor_ids)
        expected_order = tuple(identifier for identifier in plan.stages.processor_ids if identifier in requested_set)
        if requested != expected_order:
            raise IntegrityError("processor-only stages are not an ordered subset of the processing plan")
        if (
            entry.requested_stages.extractor_ids != plan.stages.extractor_ids
            or entry.requested_stages.segmenter_id != plan.stages.segmenter_id
        ):
            raise IntegrityError("processor-only stages changed extraction or segmentation policy")

        layer_kinds = {layer.layer_kind for layer in reader.release.active_layers}
        try:
            source_payloads = tuple(
                self._base_payloads(reader, layer_kind="source-items", source_item_id=source_item_id)
            )
            if len(source_payloads) != 1 or SourceItem.from_dict(source_payloads[0]) != entry.source_item:
                raise IntegrityError("processor-only source item differs from the pinned base release")
            captured = tuple(
                CapturedFile.from_dict(value)
                for value in self._base_payloads(reader, layer_kind="files", source_item_id=source_item_id)
            )
            representations = tuple(
                Representation.from_dict(value)
                for value in self._base_payloads(
                    reader,
                    layer_kind="representations",
                    source_item_id=source_item_id,
                )
            )
            segments = tuple(
                Segment.from_dict(value)
                for value in self._base_payloads(reader, layer_kind="segments", source_item_id=source_item_id)
            )
            if len(segments) > plan.limits.max_segments:
                raise LimitExceededError("base source item exceeds the processor-only segment limit")

            derived_by_processor: dict[str, list[DerivedRecord]] = {}
            for description in plan.processors.execution_order:
                identifier = description.processor_id
                if identifier in requested_set:
                    continue
                layer_kind = f"derived:{identifier}"
                if layer_kind not in layer_kinds:
                    continue
                derived_by_processor[identifier] = [
                    DerivedRecord.from_dict(value)
                    for value in self._base_payloads(
                        reader,
                        layer_kind=layer_kind,
                        source_item_id=source_item_id,
                    )
                ]

            disposition_payloads = tuple(
                self._base_payloads(reader, layer_kind="dispositions", source_item_id=source_item_id)
            )
            if len(disposition_payloads) != 1:
                raise IntegrityError("base source item requires exactly one disposition record")
            raw_warnings = disposition_payloads[0].get("warnings")
            if not isinstance(raw_warnings, list) or not all(isinstance(item, str) for item in raw_warnings):
                raise IntegrityError("base disposition warnings are invalid")
            warnings = tuple(raw_warnings)

            receipt_refs: list[ArtifactRef] = []
            base_result_candidates: dict[
                tuple[str, str],
                tuple[ArtifactRef, ProcessorResult],
            ] = {}
            for value in self._base_payloads(reader, layer_kind="receipts", source_item_id=source_item_id):
                if set(value) != {"entryId", "artifact"} or not isinstance(value["artifact"], dict):
                    raise IntegrityError("base stage receipt record has an invalid payload")
                receipt_ref = ArtifactRef.from_dict(value["artifact"])
                receipt = self._controls.load(receipt_ref)
                if receipt.get("format") == "docspec-processor-attempt-receipt":
                    continue
                if receipt.get("format") != "docspec-processor-invocation-receipt":
                    receipt_refs.append(receipt_ref)
                    continue
                processor_id = receipt["processorId"]
                if not isinstance(processor_id, str):
                    raise IntegrityError("base processor invocation receipt has an invalid processor identity")
                if processor_id in requested_set or processor_id not in current_processor_ids:
                    continue
                expected = {
                    "format",
                    "formatVersion",
                    "processorId",
                    "segmentId",
                    "request",
                    "result",
                    "cacheDisposition",
                }
                if set(receipt) != expected or receipt["formatVersion"] != "1.0":
                    raise IntegrityError("base processor invocation receipt has an invalid closed shape")
                segment_id = receipt["segmentId"]
                if not isinstance(segment_id, str):
                    raise IntegrityError("base processor invocation receipt has an invalid segment identity")
                result_ref = ArtifactRef.from_dict(receipt["result"])
                result = ProcessorResult.from_dict(self._controls.load(result_ref))
                key = (processor_id, segment_id)
                if result.result_id != result_ref.artifact_id or key in base_result_candidates:
                    raise IntegrityError("base processor result has an invalid or repeated identity")
                base_result_candidates[key] = (result_ref, result)

            result_by_processor_segment: dict[
                tuple[str, str],
                tuple[ArtifactRef, ProcessorResult],
            ] = {}
            segments_by_id = {segment.segment_id: segment for segment in segments}
            for description in plan.processors.execution_order:
                processor_id = description.processor_id
                if processor_id in requested_set:
                    continue
                records = {
                    record.derived_id: record
                    for record in derived_by_processor.get(processor_id, ())
                }
                covered_records: set[str] = set()
                for segment in segments:
                    key = (processor_id, segment.segment_id)
                    try:
                        result_ref, result = base_result_candidates[key]
                    except KeyError as error:
                        raise IntegrityError("base release is missing an unaffected processor result") from error
                    prerequisite_pairs = []
                    for dependency in description.dependencies:
                        pair = result_by_processor_segment.get((dependency, segment.segment_id))
                        if pair is None:
                            raise IntegrityError("base processor result is missing a prerequisite result")
                        prerequisite_pairs.append(pair)
                    request = processor_request(
                        self._plan_ref,
                        entry,
                        plan,
                        description,
                        segment,
                        tuple(reference for reference, _ in prerequisite_pairs),
                        WorkBudget.processor_invocation_id(
                            entry.entry_id,
                            processor_id,
                            (segment.segment_id,),
                        ),
                    )
                    validate_processor_result(
                        result,
                        request,
                        description,
                        segment,
                        projected_segment_byte_size(segment, request.allowed_fields),
                        tuple(value for _, value in prerequisite_pairs),
                        data_use_policy=plan.data_use_policy,
                        require_current_request=False,
                    )
                    for record in result.derived_records:
                        if records.get(record.derived_id) != record or record.derived_id in covered_records:
                            raise IntegrityError("base processor result differs from its durable derived layer")
                        covered_records.add(record.derived_id)
                    result_by_processor_segment[key] = (result_ref, result)
                    receipt_refs.append(
                        self._put_receipt(
                            "processor-invocation-receipts",
                            "processor-invocation-receipt",
                            {
                                "format": "docspec-processor-invocation-receipt",
                                "formatVersion": "1.0",
                                "processorId": processor_id,
                                "segmentId": segment.segment_id,
                                "request": request.to_dict(),
                                "result": result_ref.to_dict(),
                                "cacheDisposition": "reused-base",
                            },
                        )
                    )
                if covered_records != set(records):
                    raise IntegrityError("base derived layer is not covered by exact processor results")
            if set(base_result_candidates) != set(result_by_processor_segment):
                raise IntegrityError("base processor receipts include an unused result")
            base_entry = replace(
                entry,
                captured_files=captured,
                representations=representations,
                segments=segments,
                derived_records=flatten_processor_records(plan, derived_by_processor),
                stage_receipts=tuple(receipt_refs),
                warnings=warnings,
            )
            result_by_processor_segment, _ = self._checkpoints.verify_processor_receipts(
                base_entry,
                plan,
                segments_by_id,
            )
            if entry.stage_receipts:
                if (
                    entry.captured_files != captured
                    or entry.representations != representations
                    or entry.segments != segments
                    or entry.warnings != warnings
                ):
                    raise IntegrityError("processor-only checkpoint content differs from the pinned base release")
                for key, pair in result_by_processor_segment.items():
                    if checkpoint.processor_results.get(key) != pair:
                        raise IntegrityError("processor-only checkpoint changed an unaffected base result")
                result_by_processor_segment = dict(checkpoint.processor_results)
                receipt_refs = list(entry.stage_receipts)
                derived_by_processor = {}
                for description in plan.processors.execution_order:
                    records: list[DerivedRecord] = []
                    for segment in segments:
                        pair = result_by_processor_segment.get(
                            (description.processor_id, segment.segment_id)
                        )
                        if pair is not None:
                            records.extend(pair[1].derived_records)
                    if records or description.processor_id in checkpoint.completed_processors:
                        derived_by_processor[description.processor_id] = records
                checkpoint_records = flatten_processor_records(plan, derived_by_processor)
                if checkpoint_records != entry.derived_records:
                    raise IntegrityError("processor-only checkpoint records differ from its exact results")
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"processor-only base content is invalid: {error}") from error

        failures = list(entry.failures)

        def current_entry() -> DocumentEntry:
            return replace(
                entry,
                captured_files=captured,
                representations=representations,
                segments=segments,
                derived_records=flatten_processor_records(plan, derived_by_processor),
                failures=tuple(failures),
                stage_receipts=tuple(receipt_refs),
                warnings=warnings,
            )

        remaining = tuple(
            identifier
            for identifier in requested
            if identifier not in checkpoint.completed_processors
        )
        with budget.materialization_scope() as memory:
            try:
                segment_payloads: list[SegmentPayload] = []
                if remaining:
                    content_entry = replace(
                        entry,
                        representations=representations,
                        segments=segments,
                    )
                    segment_payloads = self._load_segment_payloads(
                        content_entry,
                        plan,
                        memory,
                        budget,
                    )
            except Exception as error:
                failures.append(self._failure("processing", error, len(failures) + 1))
                return self._failed_entry(current_entry(), failures)

            for identifier in remaining:
                try:
                    self._run_processor_graph(
                        entry,
                        plan,
                        segment_payloads,
                        (identifier,),
                        budget,
                        derived_by_processor,
                        result_by_processor_segment,
                        receipt_refs,
                    )
                    budget.check_duration()
                except Exception as error:
                    failures.append(self._failure("processing", error, len(failures) + 1))
                    return self._failed_entry(current_entry(), failures)
                checkpoint_entry(current_entry())

            completed = replace(
                current_entry(),
                disposition=AcquisitionDisposition.CAPTURED,
            )
            self._checkpoints.verify_terminal_entry(completed, plan)
            return completed

    def _capture_candidate(
        self,
        entry: DocumentEntry,
        store: DocumentStore,
        candidate: Any,
        plan: ProcessingPlan,
        store_attempt_id: str,
        budget: WorkBudget,
    ) -> tuple[CapturedFile | None, tuple[FailureRecord, ...]]:
        task_id = stable_urn(
            "acquisition-task",
            {"storeId": store.store_id, "entryId": entry.entry_id, "candidateId": candidate.candidate_id},
        )
        failures: list[FailureRecord] = []
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            budget.check_duration()
            attempt_id = stable_urn(
                "acquisition-attempt",
                {"taskId": task_id, "storeAttemptId": store_attempt_id, "attempt": attempt},
            )
            try:
                remaining_source_bytes = budget.remaining_source_bytes
                if remaining_source_bytes <= 0:
                    raise LimitExceededError("document store source-byte budget is exhausted")
                with self._fetcher.fetch(
                    candidate,
                    max_bytes=remaining_source_bytes,
                    task_id=task_id,
                    attempt_id=attempt_id,
                ) as fetched:
                    blob = self._blobs.put_if_absent(
                        fetched.chunks,
                        media_type=candidate.media_type,
                        expected_digest=candidate.expected_digest,
                        expected_size=candidate.expected_size,
                        max_bytes=remaining_source_bytes,
                    )
                    captured = CapturedFile.create(
                        source_item_id=entry.source_item.item_id,
                        source_version=entry.source_item.version,
                        candidate_id=candidate.candidate_id,
                        blob=blob,
                        media_type=candidate.media_type,
                        acquisition_started_at=fetched.metadata.acquisition_started_at,
                        acquired_at=self._clock(),
                        downloader_id=fetched.metadata.downloader_id,
                        downloader_configuration_digest=fetched.metadata.downloader_configuration_digest,
                        transport_version=fetched.metadata.transport_version,
                        task_id=fetched.metadata.task_id,
                        attempt_id=fetched.metadata.attempt_id,
                    )
                budget.charge_source_bytes(
                    budget.stage_unit_id(entry.entry_id, "source", captured.file_id),
                    captured.blob.byte_size,
                )
                budget.check_duration()
                return captured, tuple(failures)
            except Exception as error:
                failure = self._failure("acquisition", error, attempt)
                failures.append(failure)
                if not failure.retryable or attempt == self._retry_policy.max_attempts:
                    break
                delay = self._retry_policy.delay_milliseconds(task_id, attempt) / 1000
                self._sleep(delay)
        return None, tuple(failures)

    def _read_worker_bytes(
        self,
        captured: CapturedFile,
        plan: ProcessingPlan,
        memory: MemoryScope,
        memory_identity: str,
    ) -> bytes:
        memory.reserve(memory_identity, captured.blob.byte_size)
        try:
            return b"".join(
                self._blobs.read(
                    captured.blob,
                    max_bytes=plan.limits.max_memory_bytes,
                )
            )
        except BaseException:
            memory.release(memory_identity)
            raise

    def _load_representation_payloads(
        self,
        entry: DocumentEntry,
        plan: ProcessingPlan,
        memory: MemoryScope,
    ) -> deque[tuple[RepresentationPayload, str]]:
        """Reload a verified extraction checkpoint for the segmenter."""

        payloads: deque[tuple[RepresentationPayload, str]] = deque()
        for representation in entry.representations:
            unit_id = WorkBudget.stage_unit_id(
                entry.entry_id,
                "representation",
                f"{representation.file_id}:{representation.representation_id}",
            )
            memory_identity = f"representation:{unit_id}"
            memory.reserve(memory_identity, representation.blob.byte_size)
            content = b"".join(
                self._blobs.read(
                    representation.blob,
                    max_bytes=plan.limits.max_memory_bytes,
                )
            )
            payloads.append((RepresentationPayload(representation, content), unit_id))
        return payloads

    def _load_segment_payloads(
        self,
        entry: DocumentEntry,
        plan: ProcessingPlan,
        memory: MemoryScope,
        budget: WorkBudget,
    ) -> list[SegmentPayload]:
        """Reload a verified segmentation checkpoint for remaining processors."""

        segments_by_representation: dict[str, list[Segment]] = {}
        for segment in entry.segments:
            segments_by_representation.setdefault(segment.representation_id, []).append(segment)
        payloads_by_id: dict[str, SegmentPayload] = {}
        for representation in entry.representations:
            selected = segments_by_representation.pop(representation.representation_id, ())
            if not selected:
                continue
            budget.check_duration()
            self._blobs.verify(representation.blob)
            representation_memory = (
                f"resume-representation:{entry.entry_id}:{representation.representation_id}"
            )
            memory.reserve(representation_memory, representation.blob.byte_size)
            representation_content = b"".join(
                self._blobs.read(
                    representation.blob,
                    max_bytes=plan.limits.max_memory_bytes,
                )
            )
            representation_payload = RepresentationPayload(representation, representation_content)
            for segment in selected:
                budget.check_duration()
                self._blobs.verify(segment.content)
                memory_identity = f"resume-segment:{entry.entry_id}:{segment.segment_id}"
                memory.reserve(memory_identity, segment.content.byte_size)
                content = b"".join(
                    self._blobs.read(
                        segment.content,
                        max_bytes=plan.limits.max_memory_bytes,
                    )
                )
                payload = SegmentPayload(segment, content)
                verify_segment_representation(payload, representation_payload)
                payloads_by_id[segment.segment_id] = payload
            memory.release(representation_memory)
        if segments_by_representation:
            raise IntegrityError("checkpoint segment names a missing representation")
        return [payloads_by_id[segment.segment_id] for segment in entry.segments]

    def _persist_representation(
        self,
        payload: RepresentationPayload,
        plan: ProcessingPlan,
    ) -> RepresentationPayload:
        blob = self._blobs.put_if_absent(
            (payload.content,),
            media_type=payload.representation.blob.media_type,
            expected_digest=payload.representation.blob.digest,
            expected_size=payload.representation.blob.byte_size,
            max_bytes=plan.limits.max_memory_bytes,
        )
        representation = replace(payload.representation, blob=blob)
        return RepresentationPayload(representation, payload.content)

    def _persist_segment(self, payload: SegmentPayload, plan: ProcessingPlan) -> SegmentPayload:
        blob = self._blobs.put_if_absent(
            (payload.content,),
            media_type=payload.segment.content.media_type,
            expected_digest=payload.segment.content.digest,
            expected_size=payload.segment.content.byte_size,
            max_bytes=plan.limits.max_memory_bytes,
        )
        segment = replace(payload.segment, content=blob)
        return SegmentPayload(segment, payload.content)

    def _put_receipt(self, kind: str, identity_kind: str, value: Mapping[str, Any]) -> ArtifactRef:
        artifact_id = stable_urn(identity_kind, value)
        return self._controls.put(kind=kind, artifact_id=artifact_id, value=value)

    def _failed_entry(self, entry: DocumentEntry, failures: list[FailureRecord]) -> DocumentEntry:
        accepted = bool(failures) and self._accepted_failure_policy.accepts(failures[-1])
        disposition = (
            AcquisitionDisposition.ACCEPTED_FAILURE if accepted else AcquisitionDisposition.REJECTED_RUN
        )
        return replace(entry, disposition=disposition, failures=tuple(failures))

    @staticmethod
    def _failure(stage: str, error: Exception, attempt: int) -> FailureRecord:
        if isinstance(error, LimitExceededError):
            failure_class = FailureClass.DETERMINISTIC_INPUT
        elif isinstance(error, IntegrityError):
            failure_class = FailureClass.ARTIFACT_INTEGRITY
        elif isinstance(error, MemoryError):
            failure_class = FailureClass.TRANSIENT_RESOURCE
        elif isinstance(error, (TimeoutError, ConnectionError, OSError)):
            failure_class = FailureClass.TRANSIENT_EXTERNAL
        elif isinstance(error, (ValueError, TypeError)):
            failure_class = FailureClass.DETERMINISTIC_INPUT
        else:
            failure_class = FailureClass.IMPLEMENTATION_DEFECT
        retryable = failure_class in {FailureClass.TRANSIENT_EXTERNAL, FailureClass.TRANSIENT_RESOURCE}
        diagnostic = f"docspec.{stage}.{type(error).__name__.lower()}"
        detail = f"{stage} failed with {type(error).__name__}"
        return FailureRecord(failure_class, diagnostic, detail, attempt, retryable)
