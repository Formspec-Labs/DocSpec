"""Seed ordinary execution with the verified reusable prefix of a pinned result."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from docspec.domain.content import AcquisitionDisposition, CapturedFile, DerivedRecord, Representation, Segment, SourceItem
from docspec.domain.jobs import DocumentEntry, EntryExecutionMode
from docspec.domain.plans import ProcessingPlan
from docspec.domain.dispositions import parse_disposition_payload
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


def _base_payloads(
    reader: DocumentCatalogReader,
    *,
    layer_kind: str,
    source_item_id: str,
    max_records: int | None = None,
) -> Iterator[dict[str, Any]]:
    expected = {"recordId", "sourceItemId", "idempotencyKey", "deleted", "payload"}
    for index, row in enumerate(reader.scan_source(layer_kind=layer_kind, source_item_id=source_item_id)):
        if max_records is not None and index >= max_records:
            raise LimitExceededError(f"base {layer_kind!r} records exceed the reusable input bound")
        if set(row) != expected or row["sourceItemId"] != source_item_id:
            raise IntegrityError(f"base {layer_kind!r} record has an invalid closed shape")
        if row["deleted"] is not False or not isinstance(row["payload"], dict):
            raise IntegrityError(f"base {layer_kind!r} record has an invalid live payload")
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
) -> DocumentEntry:
    """Verify a retained prefix, preserving current-plan progress on recovery."""

    mode = entry.execution_mode
    if mode is EntryExecutionMode.FULL:
        raise IntegrityError("base reprocessing requires a retained-input execution mode")
    if plan.base_release is None or reader.release.release_id != plan.base_release.release_id:
        raise IntegrityError("reprocessing reader differs from the pinned base release")
    if entry.requested_stages != plan.stages:
        raise IntegrityError("reprocessing stages differ from the processing plan")
    requested = entry.processor_ids_to_run
    requested_set = set(requested)
    current_processor_ids = set(plan.stages.processor_ids)
    if requested != tuple(identifier for identifier in plan.stages.processor_ids if identifier in requested_set):
        raise IntegrityError("reprocessing processors are not an ordered subset of the processing plan")
    reuse_representations = mode in {EntryExecutionMode.FROM_REPRESENTATIONS, EntryExecutionMode.FROM_SEGMENTS}
    reuse_segments = mode is EntryExecutionMode.FROM_SEGMENTS
    if reuse_representations and not plan.stages.requests_extraction:
        raise IntegrityError("representation reuse requires requested extraction")
    if reuse_segments and not plan.stages.requests_segmentation:
        raise IntegrityError("segment reuse requires requested segmentation")

    source_item_id = entry.source_item.item_id
    layer_kinds = {layer.layer_kind for layer in reader.release.active_layers}

    def payloads(kind: str, maximum: int | None = None) -> Iterator[dict[str, Any]]:
        return _base_payloads(reader, layer_kind=kind, source_item_id=source_item_id, max_records=maximum)

    try:
        source_payloads = tuple(payloads("source-items", 1))
        if len(source_payloads) != 1 or SourceItem.from_dict(source_payloads[0]) != entry.source_item:
            raise IntegrityError("reprocessing source item differs from the pinned base release")
        dispositions = tuple(payloads("dispositions", 1))
        if len(dispositions) != 1:
            raise IntegrityError("base source item requires exactly one disposition record")
        disposition = dispositions[0]
        base_stages, _terminal_failure = parse_disposition_payload(disposition)
        if disposition["disposition"] not in {
            AcquisitionDisposition.CAPTURED.value, AcquisitionDisposition.ACCEPTED_FAILURE.value,
        }:
            raise IntegrityError("base reuse requires a captured or accepted-failure source item")
        if reuse_representations and (
            base_stages.extractor_id, base_stages.extractor_configuration_digest
        ) != (plan.stages.extractor_id, plan.stages.extractor_configuration_digest):
            raise IntegrityError("base extraction settings differ from the reusable prefix")
        if reuse_segments and (
            base_stages.segmenter_id, base_stages.segmenter_policy_digest
        ) != (plan.stages.segmenter_id, plan.stages.segmenter_policy_digest):
            raise IntegrityError("base segmentation settings differ from the reusable prefix")
        warnings = tuple(disposition["warnings"])

        # Layer scans sort by record ID. Restore source/processing order before
        # applying the same checkpoint invariants as ordinary execution.
        files_by_candidate: dict[str, CapturedFile] = {}
        for value in payloads("files", len(entry.source_item.candidates)):
            captured_file = CapturedFile.from_dict(value)
            if captured_file.candidate_id in files_by_candidate:
                raise IntegrityError("base source item repeats a captured candidate")
            files_by_candidate[captured_file.candidate_id] = captured_file
        if set(files_by_candidate) != {item.candidate_id for item in entry.source_item.candidates}:
            raise IntegrityError("base capture population differs from the complete source candidates")
        captured = tuple(files_by_candidate[item.candidate_id] for item in entry.source_item.candidates)

        representations: tuple[Representation, ...] = ()
        if reuse_representations:
            by_file: dict[str, Representation] = {}
            for value in payloads("representations", len(captured)):
                representation = Representation.from_dict(value)
                if representation.file_id in by_file:
                    raise IntegrityError("base repeats a representation for one captured file")
                by_file[representation.file_id] = representation
            if set(by_file) != {item.file_id for item in captured}:
                raise IntegrityError("base representations do not cover the complete captured files")
            representations = tuple(by_file[item.file_id] for item in captured)

        segments: tuple[Segment, ...] = ()
        if reuse_segments:
            representation_order = {item.representation_id: index for index, item in enumerate(representations)}
            raw_segments = tuple(Segment.from_dict(value) for value in payloads("segments", plan.limits.max_segments))
            if any(item.representation_id not in representation_order for item in raw_segments):
                raise IntegrityError("base segment names an unavailable representation")
            segments = tuple(sorted(
                raw_segments, key=lambda item: (representation_order[item.representation_id], item.ordinal)
            ))

        extraction_refs: dict[str, ArtifactRef] = {}
        segmentation_refs: dict[str, ArtifactRef] = {}
        base_result_candidates: dict[tuple[str, str], tuple[ArtifactRef, ProcessorResult]] = {}
        for value in payloads("receipts"):
            if set(value) != {"entryId", "artifact"} or not isinstance(value["artifact"], dict):
                raise IntegrityError("base stage receipt record has an invalid payload")
            if value["entryId"] != disposition["entryId"]:
                raise IntegrityError("base receipt belongs to another source-item attempt")
            reference = ArtifactRef.from_dict(value["artifact"])
            receipt = controls.load(reference)
            receipt_format = receipt.get("format")
            if receipt_format in {"docspec-extraction-receipt", "docspec-segmentation-receipt"}:
                retained = reuse_representations if receipt_format == "docspec-extraction-receipt" else reuse_segments
                if retained:
                    selected = extraction_refs if receipt_format == "docspec-extraction-receipt" else segmentation_refs
                    identifier = receipt["representationId"]
                    if identifier in selected:
                        raise IntegrityError("base repeats a stage receipt for one representation")
                    selected[identifier] = reference
                    if len(selected) > len(representations):
                        raise LimitExceededError("base stage receipt population exceeds the reusable prefix")
                continue
            if receipt_format == "docspec-processor-attempt-receipt":
                continue
            if receipt_format != "docspec-processor-invocation-receipt":
                raise IntegrityError("base contains an unknown stage receipt format")
            processor_id = receipt["processorId"]
            if not reuse_segments or processor_id in requested_set or processor_id not in current_processor_ids:
                continue
            if set(receipt) != {
                "format", "formatVersion", "processorId", "segmentId", "request", "result", "cacheDisposition",
            } or receipt["formatVersion"] != "1.0":
                raise IntegrityError("base processor invocation receipt has an invalid closed shape")
            result_ref = ArtifactRef.from_dict(receipt["result"])
            result = ProcessorResult.from_dict(controls.load(result_ref))
            key = (processor_id, receipt["segmentId"])
            if result.result_id != result_ref.artifact_id or key in base_result_candidates:
                raise IntegrityError("base processor result has an invalid or repeated identity")
            base_result_candidates[key] = (result_ref, result)
            if len(base_result_candidates) > len(segments) * len(current_processor_ids - requested_set):
                raise LimitExceededError("base processor result population exceeds the reusable graph")

        representation_ids = {item.representation_id for item in representations}
        if set(extraction_refs) != representation_ids or (reuse_segments and set(segmentation_refs) != representation_ids):
            raise IntegrityError("base stage receipts do not cover the reusable representations")
        receipt_refs = [extraction_refs[item.representation_id] for item in representations]
        if reuse_segments:
            receipt_refs.extend(segmentation_refs[item.representation_id] for item in representations)
        seeded = replace(
            entry, captured_files=captured, representations=representations, segments=segments,
            derived_records=(), stage_receipts=tuple(receipt_refs), warnings=warnings,
        )
        prefix = checkpoints.verify_entry(seeded, plan)
        if not prefix.capture_complete:
            raise IntegrityError("base capture is incomplete")
        if reuse_representations and not prefix.extraction_complete:
            raise IntegrityError("base extraction is incomplete")
        if reuse_segments and not prefix.segmentation_complete:
            raise IntegrityError("base segmentation is incomplete")

        recovering = bool(entry.captured_files or entry.representations or entry.segments or entry.stage_receipts)
        if recovering and (
            entry.captured_files != captured
            or (reuse_representations and entry.representations != representations)
            or (reuse_segments and entry.segments != segments)
            or entry.warnings != warnings
            or not set(receipt_refs).issubset(entry.stage_receipts)
        ):
            raise IntegrityError("reprocessing checkpoint content differs from the pinned base prefix")

        derived_by_processor: dict[str, list[DerivedRecord]] = {}
        result_by_processor_segment: dict[tuple[str, str], tuple[ArtifactRef, ProcessorResult]] = {}
        invocation_receipts: list[dict[str, Any]] = []
        for description in plan.processors.execution_order:
            processor_id = description.processor_id
            if processor_id in requested_set:
                continue
            if not reuse_segments or processor_id not in base_stages.processor_ids:
                raise IntegrityError("unrequested processor has no matching retained base policy")
            layer_kind = f"derived:{processor_id}"
            records = {
                record.derived_id: record
                for record in (
                    DerivedRecord.from_dict(value)
                    for value in payloads(layer_kind, len(segments) * description.item_limits.max_output_records)
                )
            } if layer_kind in layer_kinds else {}
            covered_records: set[str] = set()
            for segment in segments:
                key = (processor_id, segment.segment_id)
                if key not in base_result_candidates:
                    raise IntegrityError("base release is missing an unaffected processor result")
                result_ref, result = base_result_candidates[key]
                prerequisite_pairs = []
                for dependency in description.dependencies:
                    pair = result_by_processor_segment.get((dependency, segment.segment_id))
                    if pair is None:
                        raise IntegrityError("base processor result is missing a prerequisite result")
                    prerequisite_pairs.append(pair)
                request = processor_request(
                    plan_ref, entry, plan, description, segment,
                    tuple(reference for reference, _ in prerequisite_pairs),
                    WorkBudget.processor_invocation_id(entry.entry_id, processor_id, (segment.segment_id,)),
                )
                validate_processor_result(
                    result, request, description, segment,
                    projected_segment_byte_size(segment, request.allowed_fields),
                    tuple(value for _, value in prerequisite_pairs),
                    data_use_policy=plan.data_use_policy, require_current_request=False,
                )
                for record in result.derived_records:
                    if records.get(record.derived_id) != record or record.derived_id in covered_records:
                        raise IntegrityError("base processor result differs from its durable derived layer")
                    covered_records.add(record.derived_id)
                result_by_processor_segment[key] = (result_ref, result)
                if recovering and checkpoint.processor_results.get(key) != (result_ref, result):
                    raise IntegrityError("reprocessing checkpoint changed an unaffected base result")
                invocation_receipts.append({
                    "format": "docspec-processor-invocation-receipt", "formatVersion": "1.0",
                    "processorId": processor_id, "segmentId": segment.segment_id,
                    "request": request.to_dict(), "result": result_ref.to_dict(), "cacheDisposition": "reused-base",
                })
            if covered_records != set(records):
                raise IntegrityError("base derived layer is not covered by exact processor results")
            derived_by_processor[processor_id] = list(records.values())
        if set(base_result_candidates) != set(result_by_processor_segment):
            raise IntegrityError("base processor receipts include an unused result")
        if recovering:
            return entry
        # All reusable content and unaffected results are verified before writes.
        receipt_refs.extend(
            put_receipt(controls, "processor-invocation-receipts", "processor-invocation-receipt", value)
            for value in invocation_receipts
        )
        seeded = replace(
            seeded, derived_records=flatten_processor_records(plan, derived_by_processor),
            stage_receipts=tuple(receipt_refs),
        )
        checkpoints.verify_entry(seeded, plan)
        return seeded
    except (KeyError, TypeError, ValueError) as error:
        raise IntegrityError(f"reprocessing base content is invalid: {error}") from error
