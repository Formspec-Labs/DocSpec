"""Derive reusable failed-item work from existing retained rows and receipts."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from docspec.domain.content import AcquisitionDisposition, CapturedFile, DerivedRecord, Representation, Segment, SourceItem
from docspec.domain.identity import identity_digest
from docspec.domain.plans import ProcessingPlan, StagePolicy
from docspec.domain.processors import ProcessorRequest
from docspec.domain.references import ArtifactRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.control_repository import ControlRepository
from docspec.ports.document_catalog import DocumentCatalogReader
from docspec.ports.record_workspace import RecordWorkspace

from .execution_evidence import (
    load_stage_receipts, verify_processor_attempt_receipt, verify_processor_receipts, verify_stage_receipt_outputs,
)

_ROW_FIELDS = {"recordId", "sourceItemId", "idempotencyKey", "deleted", "payload"}
_STAGE_FORMATS = {"docspec-extraction-receipt", "docspec-segmentation-receipt"}


def _collection(source_item_id: str, kind: str) -> str:
    return "planner:failed-evidence:" + identity_digest({"sourceItemId": source_item_id, "kind": kind})


def spool_failed_evidence(
    reader: DocumentCatalogReader, workspace: RecordWorkspace, *, item_state_collection: str,
) -> None:
    """Read each retained layer once; keep only failed-source rows in bounded scratch."""
    for layer in reader.release.active_layers:
        kind = layer.layer_kind
        if kind not in {"files", "representations", "segments", "receipts"} and not kind.startswith("derived:"):
            continue
        with closing(reader.scan(layer_kind=kind)) as rows:
            for row in rows:
                if set(row) != _ROW_FIELDS or not isinstance(row["payload"], dict) or row["deleted"] is not False:
                    raise IntegrityError("base failure evidence has an invalid live record")
                state = workspace.lookup_record(item_state_collection, row["sourceItemId"])
                if state is None:
                    raise IntegrityError("base failure evidence has no source disposition")
                if state["terminalFailure"] is not None:
                    workspace.add_record(
                        _collection(row["sourceItemId"], kind), identity=row["recordId"],
                        source_item_id=row["sourceItemId"], record=row["payload"],
                    )


def _payloads(workspace: RecordWorkspace, item_id: str, kind: str, maximum: int) -> Iterator[dict[str, Any]]:
    with closing(workspace.stream_records(_collection(item_id, kind))) as values:
        for index, value in enumerate(values):
            if index >= maximum:
                raise LimitExceededError(f"base {kind} evidence exceeds the failed-item work bound")
            yield value


@dataclass(frozen=True, slots=True)
class FailedItemFrontier:
    capture_complete: bool
    extraction_complete: bool
    segmentation_complete: bool
    completed_processors: tuple[str, ...]
    unfinished_processor: str | None

    def changed_inputs(self, previous: StagePolicy, current: StagePolicy) -> bool:
        """A change must reach unfinished work, or remove that work, to admit repair."""
        if previous == current or (
            self.capture_complete
            and (not previous.requests_extraction or self.extraction_complete)
            and (not previous.requests_segmentation or self.segmentation_complete)
            and len(self.completed_processors) == len(previous.processor_ids)
        ):
            return False
        if not self.capture_complete:
            return False
        if not current.requests_extraction or (
            previous.extractor_id, previous.extractor_configuration_digest
        ) != (current.extractor_id, current.extractor_configuration_digest):
            return True
        if not self.extraction_complete:
            return False
        if not current.requests_segmentation or (
            previous.segmenter_id, previous.segmenter_policy_digest
        ) != (current.segmenter_id, current.segmenter_policy_digest):
            return True
        if not self.segmentation_complete:
            return False
        return self.unfinished_processor is not None and self.unfinished_processor not in current.processor_ids


def failed_item_frontier(
    item: SourceItem, stages: StagePolicy, entry_id: str, workspace: RecordWorkspace,
    controls: ControlRepository, plan: ProcessingPlan,
) -> FailedItemFrontier:
    """Verify recorded completion without live plugins or an invented saved job.

    Current work bounds also bound this unchanged item's retained evidence.
    Source or non-stage governing changes use ordinary full planning instead.
    Missing output is incomplete work; output without its promised receipt is
    contradictory evidence and refuses. The executor still verifies reused
    blobs and selected stage identities before consuming the chosen prefix.
    """
    candidates = {candidate.candidate_id: candidate for candidate in item.candidates}
    try:
        files_by_candidate = {}
        for raw in _payloads(workspace, item.item_id, "files", len(candidates)):
            captured = CapturedFile.from_dict(raw)
            if captured.candidate_id in files_by_candidate or captured.candidate_id not in candidates:
                raise IntegrityError("base capture repeats or names an unknown source candidate")
            if captured.source_item_id != item.item_id or captured.source_version != item.version:
                raise IntegrityError("base capture belongs to another source item")
            files_by_candidate[captured.candidate_id] = captured
        files = tuple(files_by_candidate[key] for key in candidates if key in files_by_candidate)
        file_order = {value.file_id: ordinal for ordinal, value in enumerate(files)}
        representations = tuple(Representation.from_dict(raw) for raw in _payloads(
            workspace, item.item_id, "representations", len(candidates),
        ))
        if any(value.file_id not in file_order or value.source_item_id != item.item_id for value in representations):
            raise IntegrityError("base representation has no matching captured source")
        representations = tuple(sorted(representations, key=lambda value: file_order[value.file_id]))
        if len({value.file_id for value in representations}) != len(representations):
            raise IntegrityError("base repeats a representation for one captured file")
        representation_order = {value.representation_id: ordinal for ordinal, value in enumerate(representations)}
        segments = tuple(Segment.from_dict(raw) for raw in _payloads(
            workspace, item.item_id, "segments", plan.limits.max_segments,
        ))
        if any(value.representation_id not in representation_order or value.source_item_id != item.item_id for value in segments):
            raise IntegrityError("base segment has no matching source representation")
        segments = tuple(sorted(segments, key=lambda value: (representation_order[value.representation_id], value.ordinal)))
        maximum_receipts = 2 * len(candidates) + plan.limits.max_segments * len(stages.processor_ids) * (plan.limits.max_attempts + 1)
        references = []
        for raw in _payloads(workspace, item.item_id, "receipts", maximum_receipts):
            if set(raw) != {"entryId", "artifact"} or raw["entryId"] != entry_id:
                raise IntegrityError("base receipt belongs to another source-item attempt")
            references.append(ArtifactRef.from_dict(raw["artifact"]))
        loaded = load_stage_receipts(controls, tuple(references))
        stage_receipts = sorted(
            (pair for pair in loaded if pair[1]["format"] in _STAGE_FORMATS),
            key=lambda pair: (pair[1]["format"], representation_order.get(pair[1].get("representationId"), -1)),
        )
        extraction, segmentation = verify_stage_receipt_outputs(files, representations, segments, tuple(stage_receipts))
        if not stages.requests_extraction and (representations or extraction):
            raise IntegrityError("base contains unrequested extraction")
        if not stages.requests_segmentation and (segments or segmentation):
            raise IntegrityError("base contains unrequested segmentation")
        capture_complete = len(files) == len(candidates)
        extraction_complete = capture_complete and len(representations) == len(files)
        segmentation_complete = extraction_complete and len(segmentation) == len(representations)
        processors = tuple(pair for pair in loaded if pair[1]["format"] not in _STAGE_FORMATS)
        if processors and not segmentation_complete:
            raise IntegrityError("base processor work precedes complete segmentation")
        completed = _completed_processors(
            item.item_id, entry_id, stages, segments, processors, workspace, controls,
        ) if segmentation_complete else ()
        return FailedItemFrontier(
            capture_complete, extraction_complete, segmentation_complete, completed,
            next((identifier for identifier in stages.processor_ids if identifier not in completed), None),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise IntegrityError(f"base failed-item evidence is invalid: {error}") from error


def _completed_processors(
    item_id: str, entry_id: str, stages: StagePolicy, segments: tuple[Segment, ...],
    receipts: tuple[tuple[ArtifactRef, dict[str, Any]], ...], workspace: RecordWorkspace,
    controls: ControlRepository,
) -> tuple[str, ...]:
    invocations = tuple(raw for _, raw in receipts if raw["format"] == "docspec-processor-invocation-receipt")
    segment_order = {segment.segment_id: ordinal for ordinal, segment in enumerate(segments)}
    processor_order = {identifier: ordinal for ordinal, identifier in enumerate(stages.processor_ids)}
    if not invocations:
        for identifier in stages.processor_ids:
            with closing(workspace.stream_records(_collection(item_id, f"derived:{identifier}"))) as outputs:
                if next(outputs, None) is not None:
                    raise IntegrityError("base derived output has no processor invocation receipt")
        _verify_unfinished_attempts(receipts, entry_id, processor_order, segment_order)
        return stages.processor_ids if not segments else ()
    requests = tuple(ProcessorRequest.from_dict(raw["request"]) for raw in invocations)
    plan_ref = requests[0].plan
    if any(request.plan != plan_ref for request in requests):
        raise IntegrityError("base processor receipts disagree on their owning plan")
    owning_plan = ProcessingPlan.from_dict(controls.load(plan_ref))
    if owning_plan.plan_id != plan_ref.artifact_id or owning_plan.stages != stages:
        raise IntegrityError("base processor plan differs from the retained item policy")
    derived = tuple(
        DerivedRecord.from_dict(raw)
        for description in owning_plan.processors.execution_order
        for raw in _payloads(workspace, item_id, f"derived:{description.processor_id}",
            len(segments) * description.item_limits.max_output_records)
    )
    ordered = tuple(sorted(receipts, key=lambda pair: (
        processor_order.get(pair[1].get("processorId"), -1),
        segment_order.get(pair[1].get("segmentId"), -1),
        pair[1]["format"] == "docspec-processor-invocation-receipt",
        pair[1].get("attempt", 0),
    )))
    results, _ = verify_processor_receipts(
        controls, owning_plan, {segment.segment_id: segment for segment in segments}, ordered,
        entry_id=entry_id, source_item_id=item_id, derived_records=derived,
        disposition=AcquisitionDisposition.ACCEPTED_FAILURE, max_attempts=owning_plan.limits.max_attempts,
    )
    return tuple(identifier for identifier in stages.processor_ids if all(
        (identifier, segment.segment_id) in results for segment in segments
    ))


def _verify_unfinished_attempts(
    receipts: tuple[tuple[ArtifactRef, dict[str, Any]], ...], entry_id: str,
    processors: dict[str, int], segments: dict[str, int],
) -> None:
    """An attempt with no result supplies no owning-plan reference or reusable output."""
    grouped: dict[tuple[str, str, str], set[int]] = {}
    for _, raw in receipts:
        processor, segment, request, _, attempt, outcome = verify_processor_attempt_receipt(
            raw, entry_id=entry_id, processor_ids=processors, segment_ids=segments, max_attempts=None,
        )
        if outcome != "failed":
            raise IntegrityError("base unfinished processor attempt has no result or failure")
        key = processor, segment, request
        if attempt in grouped.setdefault(key, set()):
            raise IntegrityError("base repeats an unfinished processor attempt")
        grouped[key].add(attempt)
    if any(sorted(attempts) != list(range(1, len(attempts) + 1)) for attempts in grouped.values()):
        raise IntegrityError("base unfinished processor attempts are not contiguous")
