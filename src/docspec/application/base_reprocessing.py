"""Prepare exact pinned base content and reusable results for processor-only work."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any

from docspec.domain.content import CapturedFile, DerivedRecord, Representation, Segment, SourceItem
from docspec.domain.jobs import DocumentEntry
from docspec.domain.plans import ProcessingPlan
from docspec.domain.processors import ProcessorResult
from docspec.domain.references import ArtifactRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.control_repository import ControlRepository
from docspec.ports.document_catalog import DocumentCatalogReader

from .execution_checkpoints import EntryCheckpointVerifier, VerifiedEntryCheckpoint
from .execution_evidence import put_receipt
from .processor_rules import (
    flatten_processor_records,
    processor_request,
    projected_segment_byte_size,
    validate_processor_result,
)
from .work_budget import WorkBudget


@dataclass(frozen=True, slots=True)
class PreparedReprocessing:
    """Immutable base content and mutable progress transferred to the caller.

    Receipt, result, and record collections must survive later processor failure.
    """

    captured: tuple[CapturedFile, ...]
    representations: tuple[Representation, ...]
    segments: tuple[Segment, ...]
    warnings: tuple[str, ...]
    derived_by_processor: dict[str, list[DerivedRecord]]
    result_by_processor_segment: dict[tuple[str, str], tuple[ArtifactRef, ProcessorResult]]
    receipt_refs: list[ArtifactRef]


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


def prepare_base_reprocessing(
    entry: DocumentEntry,
    plan: ProcessingPlan,
    reader: DocumentCatalogReader,
    checkpoint: VerifiedEntryCheckpoint,
    *,
    plan_ref: ArtifactRef,
    controls: ControlRepository,
    checkpoints: EntryCheckpointVerifier,
) -> PreparedReprocessing:
    """Verify retained content and persist current-plan receipts for base reuse."""

    source_item_id = entry.source_item.item_id
    requested = entry.requested_stages.processor_ids
    requested_set = set(requested)
    current_processor_ids = set(plan.stages.processor_ids)
    expected_order = tuple(identifier for identifier in plan.stages.processor_ids if identifier in requested_set)
    if requested != expected_order:
        raise IntegrityError("processor-only stages are not an ordered subset of the processing plan")
    if replace(entry.requested_stages, processor_ids=plan.stages.processor_ids) != plan.stages:
        raise IntegrityError("processor-only stages changed extraction or segmentation policy")

    layer_kinds = {layer.layer_kind for layer in reader.release.active_layers}
    try:
        source_payloads = tuple(
            _base_payloads(reader, layer_kind="source-items", source_item_id=source_item_id)
        )
        if len(source_payloads) != 1 or SourceItem.from_dict(source_payloads[0]) != entry.source_item:
            raise IntegrityError("processor-only source item differs from the pinned base release")
        captured = tuple(
            CapturedFile.from_dict(value)
            for value in _base_payloads(reader, layer_kind="files", source_item_id=source_item_id)
        )
        representations = tuple(
            Representation.from_dict(value)
            for value in _base_payloads(
                reader,
                layer_kind="representations",
                source_item_id=source_item_id,
            )
        )
        segments = tuple(
            Segment.from_dict(value)
            for value in _base_payloads(reader, layer_kind="segments", source_item_id=source_item_id)
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
                for value in _base_payloads(
                    reader,
                    layer_kind=layer_kind,
                    source_item_id=source_item_id,
                )
            ]

        disposition_payloads = tuple(
            _base_payloads(reader, layer_kind="dispositions", source_item_id=source_item_id)
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
        for value in _base_payloads(reader, layer_kind="receipts", source_item_id=source_item_id):
            if set(value) != {"entryId", "artifact"} or not isinstance(value["artifact"], dict):
                raise IntegrityError("base stage receipt record has an invalid payload")
            receipt_ref = ArtifactRef.from_dict(value["artifact"])
            receipt = controls.load(receipt_ref)
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
            result = ProcessorResult.from_dict(controls.load(result_ref))
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
                    plan_ref,
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
                    put_receipt(
                        controls,
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
        result_by_processor_segment, _ = checkpoints.verify_processor_receipts(
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

    return PreparedReprocessing(
        captured=captured,
        representations=representations,
        segments=segments,
        warnings=warnings,
        derived_by_processor=derived_by_processor,
        result_by_processor_segment=result_by_processor_segment,
        receipt_refs=receipt_refs,
    )
