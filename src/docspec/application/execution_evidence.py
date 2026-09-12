"""Persist stage receipts and classify execution failures consistently."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from docspec.domain.content import AcquisitionDisposition, Segment
from docspec.domain.identity import identity_digest, stable_urn
from docspec.domain.jobs import DocumentEntry, FailureClass, FailureRecord
from docspec.domain.plans import ProcessingPlan
from docspec.domain.processors import ProcessorRecordRef, ProcessorRequest, ProcessorResult
from docspec.domain.references import ArtifactRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.control_repository import ControlRepository
from docspec.processing.extraction import ExtractionReceipt
from docspec.processing.segmentation import SegmentationReceipt

from .processor_rules import projected_segment_byte_size, validate_processor_result
from .work_budget import WorkBudget


def put_receipt(controls: ControlRepository, kind: str, identity_kind: str, value: Mapping[str, Any]) -> ArtifactRef:
    artifact_id = stable_urn(identity_kind, value)
    return controls.put(kind=kind, artifact_id=artifact_id, value=value)


def failure_record(stage: str, error: Exception, attempt: int) -> FailureRecord:
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


def load_stage_receipts(
    controls: ControlRepository,
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
        value = controls.load(reference)
        receipt_format = value.get("format")
        identity_kind = identity_kinds.get(receipt_format)
        if identity_kind is None:
            raise IntegrityError("checkpoint contains an unknown stage receipt format")
        if reference.artifact_id != stable_urn(identity_kind, value):
            raise IntegrityError("stage receipt semantic identity differs from its reference")
        loaded.append((reference, value))
    return tuple(loaded)


def verify_stage_receipt_outputs(
    entry: DocumentEntry,
    loaded_receipts: tuple[tuple[ArtifactRef, dict[str, Any]], ...],
) -> tuple[list[ExtractionReceipt], list[SegmentationReceipt]]:
    """Bind persisted extraction/segmentation receipts to outputs, without plugins or blob reads."""

    files = {item.file_id: item for item in entry.captured_files}
    representations = {item.representation_id: item for item in entry.representations}
    segments = {item.segment_id: item for item in entry.segments}
    if (len(files) != len(entry.captured_files) or len(representations) != len(entry.representations)
            or len(segments) != len(entry.segments)):
        raise IntegrityError("entry repeats a persisted output identity")
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
        captured = files.get(representation.file_id)
        if captured is None:
            raise IntegrityError("checkpoint representation has no captured file")
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
        if any(segments[segment_id].representation_id != receipt.representation_id for segment_id in receipt.segment_ids):
            raise IntegrityError("checkpoint segmentation receipt includes an unrelated segment")
        if any(
            (segments[segment_id].segmenter_id, segments[segment_id].policy_digest)
            != (receipt.segmenter_id, receipt.policy_digest)
            for segment_id in receipt.segment_ids
        ):
            raise IntegrityError("checkpoint segmentation receipt differs from its segment policy")
    return extraction_receipts, segmentation_receipts


def verify_processor_receipts(
    controls: ControlRepository,
    entry: DocumentEntry,
    plan: ProcessingPlan,
    segments: Mapping[str, Segment],
    loaded_receipts: tuple[tuple[ArtifactRef, dict[str, Any]], ...] | None = None,
    *,
    max_attempts: int,
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
    receipts = loaded_receipts if loaded_receipts is not None else load_stage_receipts(controls, entry)
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
                or not 1 <= attempt <= max_attempts
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
            result = ProcessorResult.from_dict(controls.load(result_ref))
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
