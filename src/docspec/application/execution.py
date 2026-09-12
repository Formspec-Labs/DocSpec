"""Resumable acquisition and processing of one bounded DocumentStore reference."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import replace
from threading import Lock
from typing import Any

from docspec.domain.content import (
    AcquisitionDisposition,
    CapturedFile,
    DerivedRecord,
    Segment,
)
from docspec.domain.identity import stable_urn
from docspec.domain.jobs import (
    DocumentEntry,
    DocumentStore,
    EntryExecutionMode,
    FailureRecord,
    StoreState,
)
from docspec.domain.plans import ProcessingPlan
from docspec.domain.policies import (
    AcceptedFailurePolicy,
    RetryPolicy,
)
from docspec.domain.processors import (
    ProcessorPayload,
    ProcessorResult,
    ProcessorSet,
)
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, StoreRef
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

from .base_reprocessing import prepare_base_reprocessing
from .execution_checkpoints import EntryCheckpointVerifier, VerifiedEntryCheckpoint
from .execution_evidence import failure_record, put_receipt
from .processor_rules import (
    flatten_processor_records,
    verify_processor_policies,
)
from .processor_runtime import ProcessorRuntime
from .store_state import load_latest_store
from .stage_identity import verify_extraction_identity, verify_segment_identity, verify_stage_implementations
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
        extractor: Extractor[ExtractionResult] | None,
        segmenter: Segmenter[RepresentationPayload, SegmentPayload] | None,
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
        self._base_reader: tuple[DocumentReleaseRef, DocumentCatalogReader] | None = None
        self._base_reader_lock = Lock()
        self._blobs = blobs
        self._fetcher = fetcher
        self._extractor = extractor
        self._segmenter = segmenter
        self._processor_runtime = ProcessorRuntime(
            plan_ref=plan_ref,
            controls=controls,
            processors=processors,
            retry_policy=retry_policy,
            processor_cache=processor_cache,
            sleep=sleep,
            monotonic=monotonic,
        )
        self._retry_policy = retry_policy
        self._accepted_failure_policy = accepted_failure_policy
        self._clock = clock
        self._sleep = sleep
        self._monotonic = monotonic
        self._checkpoints = EntryCheckpointVerifier(
            controls=controls,
            blobs=blobs,
            extractor=extractor,
            segmenter=segmenter,
            retry_policy=retry_policy,
        )

    def execute_store(self, planned_document_store_ref: StoreRef) -> StoreRef:
        plan = self.verify_configuration()
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
        budget.seed_verified_entries(
            store.entries,
            verified_processor_invocations,
            {entry_id: checkpoint.extraction_observations for entry_id, checkpoint in verified_checkpoints.items()},
        )
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
        if not any(entry.execution_mode is not EntryExecutionMode.FULL for entry in store.entries):
            return None
        if plan.base_release is None:
            raise IntegrityError("prefix reuse requires a pinned base release")
        # The prepared worker owns one admission of this immutable view. Each
        # task still verifies the record members, controls and blobs it uses.
        with self._base_reader_lock:
            if self._base_reader is None:
                reader = self._document_catalog.open_reader(plan.base_release)
                self._base_reader = (plan.base_release, reader)
            reference, reader = self._base_reader
            if reference != plan.base_release:
                raise IntegrityError("execution service base release differs from its admitted reference")
            return reader

    def close(self) -> None:
        """Release the admitted base view after workers stop; future work re-admits it."""
        with self._base_reader_lock:
            self._base_reader = None

    def verify_configuration(self) -> ProcessingPlan:
        """Check effective implementations before executing or reusing saved work."""

        self._controls.verify(self._plan_ref)
        plan = ProcessingPlan.from_dict(self._controls.load(self._plan_ref))
        verify_stage_implementations(plan.stages, extractor=self._extractor, segmenter=self._segmenter)
        if plan.retry_policy_digest != self._retry_policy.digest:
            raise IntegrityError("injected retry policy differs from the processing plan")
        if plan.accepted_failure_policy_digest != self._accepted_failure_policy.digest:
            raise IntegrityError("injected accepted-failure policy differs from the processing plan")
        if plan.limits.max_attempts != self._retry_policy.max_attempts:
            raise IntegrityError("work limits and retry policy disagree on maximum attempts")
        descriptions = tuple(self._processor_runtime.processor(identifier).description for identifier in plan.stages.processor_ids)
        processor_set = ProcessorSet(descriptions)
        if processor_set != plan.processors:
            raise IntegrityError("injected processor descriptions differ from the processing plan")
        verify_processor_policies(plan, descriptions)
        return plan

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
        if entry.execution_mode is not EntryExecutionMode.FULL:
            if base_reader is None:
                raise IntegrityError("prefix reuse requires a verified base release")
            seeded = prepare_base_reprocessing(
                entry,
                plan,
                base_reader,
                checkpoint,
                plan_ref=self._plan_ref,
                controls=self._controls,
                checkpoints=self._checkpoints,
            )
            checkpoint = self._checkpoints.verify_entry(seeded, plan)
            if seeded != entry:
                checkpoint_entry(seeded)
            entry = seeded

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
            failures.append(failure_record(stage, error, len(failures) + 1))
            return self._failed_entry(current_entry(), failures)

        with budget.materialization_scope() as memory:
            if not checkpoint.capture_complete or not checkpoint.extraction_complete:
                try:
                    if representations and plan.stages.requests_segmentation:
                        representation_payloads = self._load_representation_payloads(
                            entry,
                            plan,
                            memory,
                        )
                except Exception as error:
                    return failed("processing", error)
                start_index = len(representations) if plan.stages.requests_extraction else len(captured)
                for index in range(start_index, len(entry.source_item.candidates)):
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
                    if not plan.stages.requests_extraction:
                        continue
                    try:
                        if self._extractor is None:
                            raise IntegrityError("requested extraction has no implementation")
                        source_unit = budget.stage_unit_id(entry.entry_id, "source", captured_file.file_id)
                        source_memory = f"source:{source_unit}"
                        source_bytes = self._read_worker_bytes(
                            captured_file,
                            plan,
                            memory,
                            source_memory,
                        )
                        verify_stage_implementations(plan.stages, extractor=self._extractor, segmenter=self._segmenter)
                        extraction = self._extractor.extract(captured_file, source_bytes)
                        budget.check_duration()
                        verify_stage_implementations(plan.stages, extractor=self._extractor, segmenter=self._segmenter)
                        verify_extraction_identity(self._extractor, captured_file, extraction.payload.representation)
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
                        if plan.stages.requests_segmentation:
                            representation_payloads.append((representation_payload, representation_unit))
                        else:
                            memory.release(representation_memory)
                        representations.append(representation_payload.representation)
                        receipt_refs.append(
                            put_receipt(
                                self._controls,
                                "extraction-receipts",
                                "extraction-receipt",
                                extraction.receipt.to_dict(),
                            )
                        )
                        del extraction, source_bytes
                    except Exception as error:
                        return failed("processing", error)
                    checkpoint_entry(current_entry())
            elif plan.stages.requests_segmentation and not checkpoint.segmentation_complete:
                representation_payloads = self._load_representation_payloads(
                    entry,
                    plan,
                    memory,
                )

            if plan.stages.requests_segmentation and not checkpoint.segmentation_complete:
                try:
                    if self._segmenter is None:
                        raise IntegrityError("requested segmentation has no implementation")
                    while representation_payloads:
                        representation, representation_unit = representation_payloads.popleft()
                        budget.check_duration()
                        verify_stage_implementations(plan.stages, extractor=self._extractor, segmenter=self._segmenter)
                        selected_identity = self._segmenter.selected_identity(representation.representation)
                        results = self._segmenter.segment(representation)
                        verify_stage_implementations(plan.stages, extractor=self._extractor, segmenter=self._segmenter)
                        if self._segmenter.selected_identity(representation.representation) != selected_identity:
                            raise IntegrityError("segmenter selection changed during execution")
                        representation_id = representation.representation.representation_id
                        budget.charge_segments(representation_unit, len(results))
                        for result in results:
                            verify_segment_identity(result.segment, selected_identity)
                            verify_segment_representation(result, representation)
                            memory.reserve(
                                f"segment:{representation_unit}:{result.segment.segment_id}",
                                len(result.content),
                            )
                        for result in results:
                            persisted = self._persist_segment(result, plan)
                            segment_payloads.append(persisted)
                            segments.append(persisted.segment)
                        memory.release(f"representation:{representation_unit}")
                        segmentation_receipt = SegmentationReceipt(
                            representation_id,
                            *selected_identity,
                            tuple(result.segment.segment_id for result in results),
                        )
                        receipt_refs.append(
                            put_receipt(
                                self._controls,
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
                for identifier in entry.processor_ids_to_run
                if identifier not in checkpoint.completed_processors
            )
            if remaining_processors and not segment_payloads:
                segment_payloads = self._load_segment_payloads(current_entry(), plan, memory, budget)

            for identifier in remaining_processors:
                try:
                    self._processor_runtime.run_graph(
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
                failure = failure_record("acquisition", error, attempt)
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

    def _failed_entry(self, entry: DocumentEntry, failures: list[FailureRecord]) -> DocumentEntry:
        accepted = bool(failures) and self._accepted_failure_policy.accepts(failures[-1])
        disposition = (
            AcquisitionDisposition.ACCEPTED_FAILURE if accepted else AcquisitionDisposition.REJECTED_RUN
        )
        return replace(entry, disposition=disposition, failures=tuple(failures))
