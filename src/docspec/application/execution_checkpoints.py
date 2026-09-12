"""Read-only verification of durable entry checkpoints before reuse."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from docspec.domain.content import AcquisitionDisposition, Segment
from docspec.domain.identity import identity_digest, stable_urn
from docspec.domain.jobs import DocumentEntry, EntryExecutionMode, FailureRecord
from docspec.domain.plans import ProcessingPlan
from docspec.domain.policies import RetryPolicy
from docspec.domain.processors import ProcessorRecordRef, ProcessorRequest, ProcessorResult
from docspec.domain.references import ArtifactRef
from docspec.errors import IntegrityError
from docspec.ports.blob_store import BlobStore
from docspec.ports.control_repository import ControlRepository
from docspec.ports.extractor import Extractor
from docspec.ports.segmenter import Segmenter
from docspec.processing.artifacts import RepresentationPayload, SegmentPayload
from docspec.processing.extraction import ExtractionReceipt, ExtractionResult
from docspec.processing.segmentation import SegmentationReceipt

from .processor_rules import projected_segment_byte_size, validate_processor_result
from .stage_identity import verify_extraction_identity, verify_segment_identity, verify_stage_implementations
from .work_budget import WorkBudget


@dataclass(frozen=True, slots=True)
class VerifiedEntryCheckpoint:
    """Verified durable frontier for one entry; never serialized as a cursor."""

    capture_complete: bool
    extraction_complete: bool
    segmentation_complete: bool
    completed_processors: tuple[str, ...]
    processor_results: Mapping[tuple[str, str], tuple[ArtifactRef, ProcessorResult]]
    processor_invocations: tuple[str, ...]
    extraction_observations: Mapping[str, int]


class EntryCheckpointVerifier:
    """Read artifacts and prove a restart frontier without saving or running work."""

    def __init__(
        self,
        *,
        controls: ControlRepository,
        blobs: BlobStore,
        extractor: Extractor[ExtractionResult] | None,
        segmenter: Segmenter[RepresentationPayload, SegmentPayload] | None,
        retry_policy: RetryPolicy,
    ) -> None:
        self._controls = controls
        self._blobs = blobs
        self._extractor = extractor
        self._segmenter = segmenter
        self._retry_policy = retry_policy

    def verify_terminal_entry(
        self,
        entry: DocumentEntry,
        plan: ProcessingPlan,
    ) -> tuple[str, ...]:
        """Verify every immutable object and relationship before checkpoint reuse."""

        if not entry.terminal:
            raise IntegrityError("only a terminal document entry may be reused")
        return self.verify_entry(entry, plan).processor_invocations

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

    def verify_entry(
        self,
        entry: DocumentEntry,
        plan: ProcessingPlan,
    ) -> VerifiedEntryCheckpoint:
        """Verify a terminal entry or a coarse, restartable processing frontier."""

        verify_stage_implementations(plan.stages, extractor=self._extractor, segmenter=self._segmenter)
        if entry.requested_stages != plan.stages:
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
        for representation in entry.representations:
            captured = files.get(representation.file_id)
            if (
                captured is None
                or representation.source_item_id != entry.source_item.item_id
                or representation.file_digest != captured.blob.digest
            ):
                raise IntegrityError("checkpoint representation has broken source-file lineage")
            verify_extraction_identity(self._extractor, captured, representation)
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
        extraction_observations: dict[str, int] = {}
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
            extraction_observations[representation.representation_id] = WorkBudget.extraction_observation(
                representation_kind=receipt.kind,
                metadata=receipt.metadata,
            )
        if not plan.stages.requests_extraction and (entry.representations or extraction_receipts):
            raise IntegrityError("checkpoint contains extraction output for an unrequested stage")
        if not plan.stages.requests_segmentation and (entry.segments or segmentation_receipts):
            raise IntegrityError("checkpoint contains segmentation output for an unrequested stage")
        capture_complete = len(entry.captured_files) == len(candidates)
        extraction_complete = capture_complete and (
            not plan.stages.requests_extraction
            or len(entry.representations) == len(entry.captured_files)
        )

        segments = {item.segment_id: item for item in entry.segments}
        if len(segments) != len(entry.segments):
            raise IntegrityError("checkpoint repeats a segment identity")
        segmented_representations = {segment.representation_id for segment in entry.segments} | {
            receipt.representation_id for receipt in segmentation_receipts
        }
        selected_segmenters = {
            identifier: self._segmenter.selected_identity(representation)
            for identifier, representation in representations.items()
            if identifier in segmented_representations and self._segmenter is not None
        }
        for segment in entry.segments:
            representation = representations.get(segment.representation_id)
            if (
                representation is None
                or segment.source_item_id != entry.source_item.item_id
                or segment.file_id != representation.file_id
                or segment.evidence.source_digest != representation.file_digest
            ):
                raise IntegrityError("checkpoint segment has broken representation or source lineage")
            verify_segment_identity(segment, selected_segmenters[segment.representation_id])
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
            if (receipt.segmenter_id, receipt.policy_digest) != selected_segmenters[receipt.representation_id]:
                raise IntegrityError("checkpoint segmentation receipt differs from the selected segmenter policy")
            if any(segments[segment_id].representation_id != receipt.representation_id for segment_id in receipt.segment_ids):
                raise IntegrityError("checkpoint segmentation receipt includes an unrelated segment")
        segmentation_complete = extraction_complete and (
            not plan.stages.requests_segmentation
            or len(segmentation_receipts) == len(entry.representations)
        )

        available_inputs = set(segments)
        for record in entry.derived_records:
            if record.source_item_id != entry.source_item.item_id or not set(record.input_ids).issubset(available_inputs):
                raise IntegrityError("checkpoint processor record has unavailable source inputs")
            available_inputs.add(record.derived_id)
        processor_results, invocation_ids = self.verify_processor_receipts(
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
            entry.execution_mode is not EntryExecutionMode.FROM_SEGMENTS
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
                elif entry.execution_mode is not EntryExecutionMode.FROM_SEGMENTS:
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
            if entry.execution_mode is not EntryExecutionMode.FROM_SEGMENTS:
                completed_node_count = len(completed_processors) * len(segments)
                if len(actual_nodes) != completed_node_count:
                    raise IntegrityError("nonterminal checkpoint stops inside a processor layer")
            elif has_processor_progress:
                requested = entry.processor_ids_to_run
                completed_requested = tuple(
                    identifier for identifier in requested if identifier in completed_processors
                )
                if completed_requested != requested[: len(completed_requested)]:
                    raise IntegrityError("segment-reuse checkpoint is not a requested-layer prefix")
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
                    raise IntegrityError("segment-reuse checkpoint stops inside a requested processor layer")
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
                if entry.execution_mode is not EntryExecutionMode.FROM_SEGMENTS
                else set(actual_nodes) == set(expected_nodes)
            )
            if not capture_complete or not extraction_complete or not segmentation_complete or not processor_complete:
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
        if entry.execution_mode is EntryExecutionMode.FROM_SEGMENTS:
            requested_processors = set(entry.processor_ids_to_run)
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

        return VerifiedEntryCheckpoint(
            capture_complete,
            extraction_complete,
            segmentation_complete,
            tuple(completed_processors),
            processor_results,
            checkpoint_invocations,
            extraction_observations,
        )

    def verify_processor_receipts(
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
        allowed_fields = plan.data_use_policy.allowed_fields
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
