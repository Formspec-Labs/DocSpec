"""Read-only verification of durable entry checkpoints before reuse."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from docspec.domain.content import AcquisitionDisposition
from docspec.domain.jobs import DocumentEntry, EntryExecutionMode
from docspec.domain.plans import ProcessingPlan
from docspec.domain.policies import RetryPolicy
from docspec.domain.processors import ProcessorResult
from docspec.domain.references import ArtifactRef
from docspec.errors import IntegrityError
from docspec.ports.blob_store import BlobStore
from docspec.ports.control_repository import ControlRepository
from docspec.ports.extractor import Extractor
from docspec.ports.segmenter import Segmenter
from docspec.processing.artifacts import RepresentationPayload, SegmentPayload
from docspec.processing.extraction import ExtractionResult

from .execution_evidence import load_stage_receipts, verify_processor_receipts, verify_stage_receipt_outputs
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

    def verify_entry(
        self,
        entry: DocumentEntry,
        plan: ProcessingPlan,
    ) -> VerifiedEntryCheckpoint:
        """Verify a terminal entry or a coarse, restartable processing frontier."""

        verify_stage_implementations(plan.stages, extractor=self._extractor, segmenter=self._segmenter)
        if entry.requested_stages != plan.stages:
            raise IntegrityError("document entry stages differ from the processing plan")
        loaded_receipts = load_stage_receipts(self._controls, entry.stage_receipts)

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

        extraction_receipts, segmentation_receipts = verify_stage_receipt_outputs(
            entry.captured_files, entry.representations, entry.segments, loaded_receipts,
        )
        extraction_observations = {
            receipt.representation_id: WorkBudget.extraction_observation(
                representation_kind=receipt.kind, metadata=receipt.metadata,
            )
            for receipt in extraction_receipts
        }
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

        for receipt in segmentation_receipts:
            if (receipt.segmenter_id, receipt.policy_digest) != selected_segmenters[receipt.representation_id]:
                raise IntegrityError("checkpoint segmentation receipt differs from the selected segmenter policy")
        segmentation_complete = extraction_complete and (
            not plan.stages.requests_segmentation
            or len(segmentation_receipts) == len(entry.representations)
        )

        available_inputs = set(segments)
        for record in entry.derived_records:
            if record.source_item_id != entry.source_item.item_id or not set(record.input_ids).issubset(available_inputs):
                raise IntegrityError("checkpoint processor record has unavailable source inputs")
            available_inputs.add(record.derived_id)
        processor_results, invocation_ids = verify_processor_receipts(
            self._controls,
            plan,
            segments,
            loaded_receipts,
            entry_id=entry.entry_id,
            source_item_id=entry.source_item.item_id,
            derived_records=entry.derived_records,
            disposition=entry.disposition,
            max_attempts=self._retry_policy.max_attempts,
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
