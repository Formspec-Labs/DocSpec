"""Resumable acquisition and processing of one bounded DocumentStore reference."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Any

from docspec.domain.content import (
    AcquisitionDisposition,
    CapturedFile,
    DerivedRecord,
    Representation,
    Segment,
    SourceItem,
)
from docspec.domain.identity import canonical_json_bytes, identity_digest, stable_urn
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
    ProcessorRecordRef,
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
from docspec.processing.extraction import ExtractionReceipt, ExtractionResult
from docspec.processing.segmentation import SegmentationReceipt

from .store_state import load_latest_store
from .work_budget import MemoryScope, WorkBudget


@dataclass(frozen=True, slots=True)
class _VerifiedEntryCheckpoint:
    """Verified durable frontier for one entry; never serialized as a cursor."""

    extraction_complete: bool
    segmentation_complete: bool
    completed_processors: tuple[str, ...]
    processor_results: Mapping[tuple[str, str], tuple[ArtifactRef, ProcessorResult]]
    processor_invocations: tuple[str, ...]


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

    def execute_store(self, planned_document_store_ref: StoreRef) -> StoreRef:
        plan = self._load_plan()
        current_ref, store = load_latest_store(self._stores, planned_document_store_ref)
        if store.plan_id != plan.plan_id:
            raise IntegrityError("document store belongs to another processing plan")
        if store.state == StoreState.SEALED:
            for entry in store.entries:
                self._verify_terminal_entry(entry, plan)
            if store.delivery_receipt is None:
                raise IntegrityError("sealed document store has no delivery receipt")
            self._controls.verify(store.delivery_receipt)
            return current_ref
        budget = WorkBudget(plan.limits, monotonic=self._monotonic)
        verified_checkpoints: dict[str, _VerifiedEntryCheckpoint] = {}
        verified_processor_invocations: dict[str, tuple[str, ...]] = {}
        for entry in store.entries:
            checkpoint = self._verify_entry_checkpoint(entry, plan)
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
                self._verify_entry_checkpoint(partial, plan)
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
            self._verify_terminal_entry(entries[index], plan)
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

    def _verify_terminal_entry(
        self,
        entry: DocumentEntry,
        plan: ProcessingPlan,
    ) -> tuple[str, ...]:
        """Verify every immutable object and relationship before checkpoint reuse."""

        if not entry.terminal:
            raise IntegrityError("only a terminal document entry may be reused")
        return self._verify_entry_checkpoint(entry, plan).processor_invocations

    def _load_stage_receipts(
        self,
        entry: DocumentEntry,
    ) -> tuple[tuple[ArtifactRef, dict[str, Any]], ...]:
        """Load each closed receipt once and verify its semantic artifact identity."""

        identity_kinds = {
            "docspec-extraction-receipt": "extraction-receipt",
            "docspec-segmentation-receipt": "segmentation-receipt",
            "docspec-processor-attempt-receipt": "processor-attempt-receipt",
            "docspec-processor-invocation-receipt": "processor-invocation-receipt",
        }
        loaded: list[tuple[ArtifactRef, dict[str, Any]]] = []
        seen: set[ArtifactRef] = set()
        for reference in entry.stage_receipts:
            if reference in seen:
                raise IntegrityError("checkpoint repeats a stage receipt reference")
            seen.add(reference)
            value = self._controls.load(reference)
            receipt_format = value.get("format")
            identity_kind = identity_kinds.get(receipt_format)
            if identity_kind is None:
                raise IntegrityError("checkpoint contains an unknown stage receipt format")
            if reference.artifact_id != stable_urn(identity_kind, value):
                raise IntegrityError("stage receipt semantic identity differs from its reference")
            loaded.append((reference, value))
        return tuple(loaded)

    def _verify_entry_checkpoint(
        self,
        entry: DocumentEntry,
        plan: ProcessingPlan,
    ) -> _VerifiedEntryCheckpoint:
        """Verify a terminal entry or a coarse, restartable processing frontier."""

        if entry.requested_stages != plan.stages and entry.execution_mode is EntryExecutionMode.FULL:
            raise IntegrityError("document entry stages differ from the processing plan")
        loaded_receipts = self._load_stage_receipts(entry)

        files = {item.file_id: item for item in entry.captured_files}
        if len(files) != len(entry.captured_files):
            raise IntegrityError("checkpoint repeats a captured-file identity")
        candidates = entry.source_item.candidates
        captured_candidate_ids = tuple(item.candidate_id for item in entry.captured_files)
        expected_candidate_ids = tuple(item.candidate_id for item in candidates)
        if captured_candidate_ids != expected_candidate_ids[: len(captured_candidate_ids)]:
            raise IntegrityError("checkpoint captured files are not an ordered source-candidate prefix")
        for captured, candidate in zip(entry.captured_files, candidates, strict=False):
            if (
                captured.source_item_id != entry.source_item.item_id
                or captured.source_version != entry.source_item.version
                or captured.candidate_id != candidate.candidate_id
                or captured.media_type != candidate.media_type
                or captured.transport_version != candidate.transport_version
                or (candidate.expected_digest is not None and captured.blob.digest != candidate.expected_digest)
                or (candidate.expected_size is not None and captured.blob.byte_size != candidate.expected_size)
            ):
                raise IntegrityError("checkpoint captured file names a different source item")
            self._blobs.verify(captured.blob)

        representations = {item.representation_id: item for item in entry.representations}
        if len(representations) != len(entry.representations):
            raise IntegrityError("checkpoint repeats a representation identity")
        if tuple(item.file_id for item in entry.representations) != tuple(files)[: len(entry.representations)]:
            raise IntegrityError("checkpoint representations are not an ordered captured-file prefix")
        if len(entry.captured_files) - len(entry.representations) not in {0, 1}:
            raise IntegrityError("checkpoint must stop at a candidate capture or extraction frontier")
        for representation in entry.representations:
            captured = files.get(representation.file_id)
            extractor_registry_id = getattr(self._extractor, "extractor_id", None)
            if (
                captured is None
                or representation.source_item_id != entry.source_item.item_id
                or representation.file_digest != captured.blob.digest
                or (
                    representation.extractor_id not in plan.stages.extractor_ids
                    and extractor_registry_id not in plan.stages.extractor_ids
                )
            ):
                raise IntegrityError("checkpoint representation has broken source-file lineage")
            if any(
                mapping.evidence.end is not None and mapping.evidence.end > captured.blob.byte_size
                for mapping in representation.evidence_mappings
            ):
                raise IntegrityError("checkpoint representation evidence exceeds its captured file")
            self._blobs.verify(representation.blob)

        extraction_receipts: list[ExtractionReceipt] = []
        segmentation_receipts: list[SegmentationReceipt] = []
        for _, raw in loaded_receipts:
            try:
                if raw["format"] == "docspec-extraction-receipt":
                    extraction_receipts.append(ExtractionReceipt.from_dict(raw))
                elif raw["format"] == "docspec-segmentation-receipt":
                    segmentation_receipts.append(SegmentationReceipt.from_dict(raw))
            except (KeyError, TypeError, ValueError) as error:
                raise IntegrityError(f"checkpoint stage receipt is invalid: {error}") from error
        if len(extraction_receipts) != len(entry.representations):
            raise IntegrityError("checkpoint extraction receipts do not cover its representations")
        for receipt, representation in zip(extraction_receipts, entry.representations, strict=True):
            captured = files[representation.file_id]
            if (
                receipt.file_id != captured.file_id
                or receipt.input_digest != captured.blob.digest
                or receipt.representation_id != representation.representation_id
                or receipt.output_digest != representation.blob.digest
                or receipt.output_byte_size != representation.blob.byte_size
                or receipt.kind != representation.kind
                or receipt.extractor_id != representation.extractor_id
                or receipt.configuration_digest != representation.configuration_digest
                or receipt.warnings != representation.warnings
            ):
                raise IntegrityError("checkpoint extraction receipt differs from its immutable output")
        extraction_complete = (
            len(entry.captured_files) == len(candidates)
            and len(entry.representations) == len(entry.captured_files)
        )

        segments = {item.segment_id: item for item in entry.segments}
        if len(segments) != len(entry.segments):
            raise IntegrityError("checkpoint repeats a segment identity")
        for segment in entry.segments:
            representation = representations.get(segment.representation_id)
            if (
                representation is None
                or segment.source_item_id != entry.source_item.item_id
                or segment.file_id != representation.file_id
                or segment.evidence.source_digest != representation.file_digest
            ):
                raise IntegrityError("checkpoint segment has broken representation or source lineage")
            try:
                expected_evidence = representation.evidence_for_range(
                    segment.representation_start,
                    segment.representation_end,
                )
            except ValueError as error:
                raise IntegrityError("checkpoint segment has no reversible representation mapping") from error
            if segment.evidence != expected_evidence:
                raise IntegrityError("checkpoint segment evidence differs from its representation mapping")
            self._blobs.verify(segment.content)

        if tuple(item.representation_id for item in segmentation_receipts) != tuple(representations)[
            : len(segmentation_receipts)
        ]:
            raise IntegrityError("checkpoint segmentation receipts are not an ordered representation prefix")
        receipted_segment_ids = tuple(
            segment_id
            for receipt in segmentation_receipts
            for segment_id in receipt.segment_ids
        )
        if receipted_segment_ids != tuple(segments):
            raise IntegrityError("checkpoint segmentation receipts differ from its ordered segments")
        for receipt in segmentation_receipts:
            if receipt.segmenter_id != plan.stages.segmenter_id:
                raise IntegrityError("checkpoint segmentation receipt differs from the processing plan")
            if any(segments[segment_id].representation_id != receipt.representation_id for segment_id in receipt.segment_ids):
                raise IntegrityError("checkpoint segmentation receipt includes an unrelated segment")
        segmentation_complete = extraction_complete and len(segmentation_receipts) == len(entry.representations)

        available_inputs = set(segments)
        for record in entry.derived_records:
            if record.source_item_id != entry.source_item.item_id or not set(record.input_ids).issubset(available_inputs):
                raise IntegrityError("checkpoint processor record has unavailable source inputs")
            available_inputs.add(record.derived_id)
        processor_results, invocation_ids = self._verify_processor_receipts(
            entry,
            plan,
            segments,
            loaded_receipts,
        )
        expected_nodes = tuple(
            (description.processor_id, segment_id)
            for description in plan.processors.execution_order
            for segment_id in segments
        )
        actual_nodes = tuple(processor_results)
        if (
            entry.execution_mode is EntryExecutionMode.FULL
            and actual_nodes != expected_nodes[: len(actual_nodes)]
        ):
            raise IntegrityError("checkpoint processor results are not an ordered graph prefix")
        completed_processors: list[str] = []
        if segmentation_complete:
            for description in plan.processors.execution_order:
                expected = {(description.processor_id, segment_id) for segment_id in segments}
                actual = expected.intersection(processor_results)
                if actual == expected:
                    completed_processors.append(description.processor_id)
                elif entry.execution_mode is EntryExecutionMode.FULL:
                    break

        has_segmentation_progress = bool(entry.segments or segmentation_receipts)
        has_processor_progress = any(
            raw["format"].startswith("docspec-processor-") for _, raw in loaded_receipts
        )
        if has_processor_progress and not segmentation_complete:
            raise IntegrityError("checkpoint has processor work before segmentation completes")
        if not entry.terminal:
            if has_segmentation_progress and not segmentation_complete:
                raise IntegrityError("nonterminal checkpoint stops inside segmentation")
            if entry.execution_mode is EntryExecutionMode.FULL:
                completed_node_count = len(completed_processors) * len(segments)
                if len(actual_nodes) != completed_node_count:
                    raise IntegrityError("nonterminal checkpoint stops inside a processor layer")
            elif has_processor_progress:
                requested = entry.requested_stages.processor_ids
                completed_requested = tuple(
                    identifier for identifier in requested if identifier in completed_processors
                )
                if completed_requested != requested[: len(completed_requested)]:
                    raise IntegrityError("processor-only checkpoint is not a requested-layer prefix")
                completed_ids = (
                    set(plan.stages.processor_ids).difference(requested)
                    | set(completed_requested)
                )
                expected_completed_nodes = {
                    (processor_id, segment_id)
                    for processor_id in completed_ids
                    for segment_id in segments
                }
                if set(actual_nodes) != expected_completed_nodes:
                    raise IntegrityError("processor-only checkpoint stops inside a requested processor layer")
            result_request_ids = {
                raw["request"]["requestId"]
                for _, raw in loaded_receipts
                if raw["format"] == "docspec-processor-invocation-receipt"
            }
            for _, raw in loaded_receipts:
                if (
                    raw["format"] == "docspec-processor-attempt-receipt"
                    and raw["requestId"] not in result_request_ids
                ):
                    raise IntegrityError("nonterminal checkpoint contains an incomplete processor attempt")

        if entry.disposition is AcquisitionDisposition.CAPTURED:
            processor_complete = (
                actual_nodes == expected_nodes
                if entry.execution_mode is EntryExecutionMode.FULL
                else set(actual_nodes) == set(expected_nodes)
            )
            if not extraction_complete or not segmentation_complete or not processor_complete:
                raise IntegrityError("captured entry does not cover every planned processing stage")
        elif entry.disposition in {
            AcquisitionDisposition.UNCHANGED,
            AcquisitionDisposition.DELETED,
            AcquisitionDisposition.EXCLUDED,
        } and (
            entry.captured_files
            or entry.representations
            or entry.segments
            or entry.derived_records
            or entry.stage_receipts
        ):
            raise IntegrityError("metadata-only terminal entry unexpectedly contains processing output")

        checkpoint_invocations = invocation_ids
        if entry.execution_mode is EntryExecutionMode.PROCESSORS_ONLY:
            requested_processors = set(entry.requested_stages.processor_ids)
            checkpoint_invocations = tuple(
                sorted(
                    {
                        (
                            raw["invocationId"]
                            if raw["format"] == "docspec-processor-attempt-receipt"
                            else raw["request"]["invocationId"]
                        )
                        for _, raw in loaded_receipts
                        if raw["format"].startswith("docspec-processor-")
                        and raw["processorId"] in requested_processors
                    }
                )
            )

        return _VerifiedEntryCheckpoint(
            extraction_complete,
            segmentation_complete,
            tuple(completed_processors),
            processor_results,
            checkpoint_invocations,
        )

    def _verify_processor_receipts(
        self,
        entry: DocumentEntry,
        plan: ProcessingPlan,
        segments: Mapping[str, Segment],
        loaded_receipts: tuple[tuple[ArtifactRef, dict[str, Any]], ...] | None = None,
    ) -> tuple[
        dict[tuple[str, str], tuple[ArtifactRef, ProcessorResult]],
        tuple[str, ...],
    ]:
        """Verify one entry's complete processor subgraph without reading bulk bytes."""

        derived_by_id = {record.derived_id: record for record in entry.derived_records}
        if len(derived_by_id) != len(entry.derived_records):
            raise IntegrityError("entry repeats a processor-derived record identity")
        receipted_derived_ids: set[str] = set()
        invocation_ids: set[str] = set()
        processor_results: dict[tuple[str, str], tuple[ArtifactRef, ProcessorResult]] = {}
        processor_attempts: dict[tuple[str, str, str], dict[int, str]] = {}
        settled_attempt_keys: set[tuple[str, str, str]] = set()
        descriptions = {item.processor_id: item for item in plan.processors.execution_order}
        allowed_fields = self._allowed_processor_fields(plan)
        receipts = loaded_receipts if loaded_receipts is not None else self._load_stage_receipts(entry)
        for _, receipt in receipts:
            receipt_format = receipt.get("format")
            if receipt_format == "docspec-processor-attempt-receipt":
                expected_attempt = {
                    "format",
                    "formatVersion",
                    "processorId",
                    "segmentId",
                    "requestId",
                    "invocationId",
                    "attempt",
                    "outcome",
                    "elapsedMilliseconds",
                    "failure",
                }
                processor_id = receipt.get("processorId")
                segment_id = receipt.get("segmentId")
                request_id = receipt.get("requestId")
                invocation_id = receipt.get("invocationId")
                attempt = receipt.get("attempt")
                outcome = receipt.get("outcome")
                elapsed = receipt.get("elapsedMilliseconds")
                if (
                    set(receipt) != expected_attempt
                    or receipt.get("formatVersion") != "1.0"
                    or not all(
                        isinstance(value, str) and value
                        for value in (processor_id, segment_id, request_id, invocation_id)
                    )
                    or processor_id not in descriptions
                    or segment_id not in segments
                    or type(attempt) is not int
                    or not 1 <= attempt <= self._retry_policy.max_attempts
                    or outcome not in {"failed", "succeeded"}
                    or type(elapsed) is not int
                    or elapsed < 0
                    or invocation_id
                    != WorkBudget.processor_invocation_id(
                        entry.entry_id,
                        processor_id,
                        (segment_id,),
                    )
                ):
                    raise IntegrityError("processor attempt receipt has an invalid closed shape or identity")
                if outcome == "failed":
                    try:
                        failure = FailureRecord.from_dict(receipt["failure"])
                    except (TypeError, ValueError) as error:
                        raise IntegrityError("processor attempt receipt has an invalid failure") from error
                    if failure.attempt != attempt:
                        raise IntegrityError("processor attempt failure names a different attempt")
                elif receipt["failure"] is not None:
                    raise IntegrityError("successful processor attempt receipt contains a failure")
                attempt_key = (processor_id, segment_id, request_id)
                attempts = processor_attempts.setdefault(attempt_key, {})
                if attempt in attempts:
                    raise IntegrityError("entry repeats a processor attempt")
                attempts[attempt] = outcome
                invocation_ids.add(invocation_id)
                continue
            if receipt_format != "docspec-processor-invocation-receipt":
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
            if (
                set(receipt) != expected
                or receipt["formatVersion"] != "1.0"
                or receipt["cacheDisposition"]
                not in {"hit", "miss", "bypassed", "invalid", "unavailable", "reused-base"}
            ):
                raise IntegrityError("processor invocation receipt has an invalid closed shape")
            try:
                request = ProcessorRequest.from_dict(receipt["request"])
                result_ref = ArtifactRef.from_dict(receipt["result"])
                result = ProcessorResult.from_dict(self._controls.load(result_ref))
            except (TypeError, ValueError) as error:
                raise IntegrityError(f"processor invocation receipt is invalid: {error}") from error
            processor_id = receipt["processorId"]
            segment_id = receipt["segmentId"]
            if not isinstance(processor_id, str) or not isinstance(segment_id, str):
                raise IntegrityError("processor invocation receipt identities must be strings")
            description = descriptions.get(processor_id)
            segment = segments.get(segment_id)
            key = (processor_id, segment_id)
            if description is None or key in processor_results:
                raise IntegrityError("processor invocation receipt names an unknown or repeated graph node")
            prerequisite_pairs: list[tuple[ArtifactRef, ProcessorResult]] = []
            for dependency in description.dependencies:
                pair = processor_results.get((dependency, segment_id))
                if pair is None:
                    raise IntegrityError("processor invocation receipt is missing a prerequisite result")
                prerequisite_pairs.append(pair)
            expected_invocation_id = WorkBudget.processor_invocation_id(
                entry.entry_id,
                processor_id,
                (segment_id,),
            )
            if (
                request.processor_id != processor_id
                or request.processor_description_digest != identity_digest(description.to_dict())
                or request.source_item_id != entry.source_item.item_id
                or segment is None
                or request.input_records
                != (ProcessorRecordRef.for_segment(segment),)
                or request.prerequisite_results
                != tuple(reference for reference, _ in prerequisite_pairs)
                or request.allowed_fields != allowed_fields
                or request.item_limits != description.item_limits
                or request.cache_key_schema_id
                != (description.cache_policy.key_schema_id or "docspec-cache-disabled/1")
                or request.invocation_id != expected_invocation_id
                or result.result_id != result_ref.artifact_id
                or result.reuse_key != request.reuse_key
            ):
                raise IntegrityError("processor invocation receipt differs from its entry or result")
            invocation_ids.add(request.invocation_id)
            self._validate_processor_result(
                result,
                request,
                description,
                segment,
                self._projected_segment_byte_size(segment, request.allowed_fields),
                tuple(value for _, value in prerequisite_pairs),
                data_use_policy=plan.data_use_policy,
                require_current_request=False,
            )
            processor_results[key] = (result_ref, result)
            attempt_key = (processor_id, segment_id, request.request_id)
            settled_attempt_keys.add(attempt_key)
            attempts = processor_attempts.get(attempt_key, {})
            if receipt["cacheDisposition"] == "reused-base" and attempts:
                raise IntegrityError("base-reused processor result contains local attempt receipts")
            if attempts and attempts[max(attempts)] != "succeeded":
                raise IntegrityError("processor result follows an unsuccessful final attempt")
            if receipt["cacheDisposition"] in {"miss", "bypassed", "invalid", "unavailable"}:
                if not attempts:
                    raise IntegrityError("executed processor result lacks a successful attempt receipt")
            for record in result.derived_records:
                if derived_by_id.get(record.derived_id) != record:
                    raise IntegrityError("processor invocation result differs from the entry derived records")
                if record.derived_id in receipted_derived_ids:
                    raise IntegrityError("entry repeats a processor-derived result across receipts")
                receipted_derived_ids.add(record.derived_id)
        for key, attempts in processor_attempts.items():
            ordered = sorted(attempts)
            if ordered != list(range(1, len(ordered) + 1)):
                raise IntegrityError("processor attempt receipts are not a contiguous retry sequence")
            if any(attempts[number] == "succeeded" for number in ordered[:-1]):
                raise IntegrityError("processor attempt sequence continued after success")
            if key not in settled_attempt_keys and (
                entry.disposition
                not in {AcquisitionDisposition.ACCEPTED_FAILURE, AcquisitionDisposition.REJECTED_RUN}
                or attempts[ordered[-1]] != "failed"
            ):
                raise IntegrityError("processor attempt receipt is not settled by a result or terminal failure")
        if receipted_derived_ids != set(derived_by_id):
            raise IntegrityError("entry derived records are not covered by exact processor results")
        if entry.disposition is AcquisitionDisposition.CAPTURED:
            expected_nodes = {
                (description.processor_id, segment_id)
                for description in plan.processors.execution_order
                for segment_id in segments
            }
            if set(processor_results) != expected_nodes:
                raise IntegrityError("captured entry does not cover the complete processor graph")
        return processor_results, tuple(sorted(invocation_ids))

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
        checkpoint: _VerifiedEntryCheckpoint,
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
        if self._flatten_processor_records(plan, derived_by_processor) != entry.derived_records:
            raise IntegrityError("verified processor results differ from checkpoint records")
        failures = list(entry.failures)
        receipt_refs = list(entry.stage_receipts)

        def current_entry() -> DocumentEntry:
            return replace(
                entry,
                captured_files=tuple(captured),
                representations=tuple(representations),
                segments=tuple(segments),
                derived_records=self._flatten_processor_records(plan, derived_by_processor),
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

    @staticmethod
    def _flatten_processor_records(
        plan: ProcessingPlan,
        records: Mapping[str, list[DerivedRecord]],
    ) -> tuple[DerivedRecord, ...]:
        return tuple(
            record
            for description in plan.processors.execution_order
            for record in sorted(
                records.get(description.processor_id, ()),
                key=lambda item: item.derived_id,
            )
        )

    @staticmethod
    def _allowed_processor_fields(plan: ProcessingPlan) -> tuple[str, ...]:
        return plan.data_use_policy.allowed_fields

    @staticmethod
    def _projected_segment_byte_size(
        segment: Segment,
        allowed_fields: tuple[str, ...],
    ) -> int:
        """Account for the same projected content bytes during execution and replay."""

        return segment.content.byte_size if "content" in allowed_fields else 0

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
                self._require_segment_input(processor.description, segment)
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
                request = self._processor_request(
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

    def _processor_request(
        self,
        entry: DocumentEntry,
        plan: ProcessingPlan,
        description: ProcessorDescription,
        segment: Segment,
        prerequisites: tuple[ArtifactRef, ...],
        invocation_id: str,
    ) -> ProcessorRequest:
        return ProcessorRequest(
            self._plan_ref,
            description.processor_id,
            identity_digest(description.to_dict()),
            entry.source_item.item_id,
            (ProcessorRecordRef.for_segment(segment),),
            prerequisites,
            self._allowed_processor_fields(plan),
            description.item_limits,
            description.cache_policy.key_schema_id or "docspec-cache-disabled/1",
            invocation_id,
        )

    @staticmethod
    def _require_segment_input(
        description: ProcessorDescription,
        segment: SegmentPayload,
    ) -> None:
        declaration = next(
            (item for item in description.accepted_inputs if item.record_kind == "segment"),
            None,
        )
        if declaration is None or "docspec-segment/1" not in declaration.schema_ids:
            raise IntegrityError(
                f"processor {description.processor_id} does not accept docspec-segment/1 inputs"
            )
        actual = segment.segment.content.media_type
        accepted = any(
            pattern == "*/*"
            or pattern == actual
            or (pattern.endswith("/*") and actual.startswith(pattern[:-1]))
            for pattern in declaration.media_types
        )
        if not accepted:
            raise IntegrityError(
                f"processor {description.processor_id} does not accept segment media type {actual}"
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
                self._validate_processor_result(
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
        self._validate_processor_result(
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
    def _validate_processor_result(
        result: ProcessorResult,
        request: ProcessorRequest,
        description: ProcessorDescription,
        segment: Segment,
        segment_byte_size: int,
        prerequisites: tuple[ProcessorResult, ...],
        *,
        data_use_policy: DataUsePolicy,
        require_current_request: bool,
    ) -> None:
        if not isinstance(result, ProcessorResult):
            raise IntegrityError("processor returned a non-DocSpec ProcessorResult")
        if result.reuse_key != request.reuse_key or (
            require_current_request and result.request_id != request.request_id
        ):
            raise IntegrityError("processor result differs from its request identity")
        if result.output_media_type not in description.output_media_types:
            raise IntegrityError("processor result media type is not declared by its description")
        if result.resource_identities != description.external_resources:
            raise IntegrityError("processor result resources differ from its description")
        try:
            external_processing = (
                description.execution_scope is ProcessorExecutionScope.DECLARED_EXTERNAL
            )
            data_use_policy.require_provider_evidence(
                result.provider_evidence,
                external=external_processing,
            )
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"processor provider evidence differs from its data-use policy: {error}") from error
        if external_processing != (result.resource_use.external_request_count > 0):
            raise IntegrityError("processor external-request count differs from its declared execution scope")
        expected_inputs = (
            segment.segment_id,
            *(record.derived_id for prerequisite in prerequisites for record in prerequisite.derived_records),
        )
        receipt = result.provider_receipt
        if (
            receipt["requestId"] != result.request_id
            or receipt["reuseKey"] != result.reuse_key
            or receipt["processorId"] != description.processor_id
            or receipt["processorDescriptionDigest"] != identity_digest(description.to_dict())
            or tuple(receipt["inputIds"]) != expected_inputs
            or receipt["outputSchemaId"] != description.output_schema_id
            or receipt["outputMediaType"] != result.output_media_type
            or receipt["configurationDigest"] != description.configuration_digest
            or receipt["dataUsePolicyDigest"] != description.data_use_policy_digest
            or receipt["retryPolicyDigest"] != description.retry_policy_digest
        ):
            raise IntegrityError("processor provider receipt differs from its request or description")
        if len(result.derived_records) > request.item_limits.max_output_records:
            raise LimitExceededError("processor result exceeds its output-record limit")
        output_bytes = sum(len(canonical_json_bytes(record.value)) for record in result.derived_records)
        if output_bytes > request.item_limits.max_output_bytes:
            raise LimitExceededError("processor result exceeds its output-byte limit")
        input_bytes = segment_byte_size + sum(
            len(canonical_json_bytes(prerequisite.to_dict())) for prerequisite in prerequisites
        )
        if input_bytes > request.item_limits.max_input_bytes:
            raise LimitExceededError("processor request exceeds its input-byte limit")
        if 1 + len(prerequisites) > request.item_limits.max_input_records:
            raise LimitExceededError("processor request exceeds its input-record limit")
        if result.resource_use.input_bytes != input_bytes or result.resource_use.output_bytes != output_bytes:
            raise IntegrityError("processor resource use differs from its verified inputs or outputs")
        if result.resource_use.duration_milliseconds > request.item_limits.max_duration_seconds * 1000:
            raise LimitExceededError("processor result exceeds its duration limit")
        for record in result.derived_records:
            if (
                record.processor_id != description.processor_id
                or record.source_item_id != request.source_item_id
                or record.schema_id != description.output_schema_id
                or record.input_ids != expected_inputs
                or record.disposition is not result.disposition
            ):
                raise IntegrityError("processor derived record differs from its request or result")

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
        checkpoint: _VerifiedEntryCheckpoint,
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
                    request = self._processor_request(
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
                    self._validate_processor_result(
                        result,
                        request,
                        description,
                        segment,
                        self._projected_segment_byte_size(segment, request.allowed_fields),
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
                derived_records=self._flatten_processor_records(plan, derived_by_processor),
                stage_receipts=tuple(receipt_refs),
                warnings=warnings,
            )
            result_by_processor_segment, _ = self._verify_processor_receipts(
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
                checkpoint_records = self._flatten_processor_records(plan, derived_by_processor)
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
                derived_records=self._flatten_processor_records(plan, derived_by_processor),
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
            self._verify_terminal_entry(completed, plan)
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
