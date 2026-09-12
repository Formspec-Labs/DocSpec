"""Shared processor request, input, result, and record-order rules.

Execution, checkpoint verification, and base reuse apply these same checks.
"""

from __future__ import annotations

from collections.abc import Mapping

from docspec.domain.content import DerivedRecord, Segment
from docspec.domain.identity import canonical_json_bytes, identity_digest
from docspec.domain.jobs import DocumentEntry
from docspec.domain.plans import ProcessingPlan
from docspec.domain.policies import DataUsePolicy, ProcessorExecutionScope
from docspec.domain.processors import ProcessorDescription, ProcessorRecordRef, ProcessorRequest, ProcessorResult
from docspec.domain.references import ArtifactRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.processing.artifacts import SegmentPayload


def verify_processor_policies(
    plan: ProcessingPlan,
    descriptions: tuple[ProcessorDescription, ...],
) -> None:
    """Reject incompatible processor policies before saving or executing work."""

    for description in descriptions:
        if description.data_use_policy_digest != plan.data_use_policy.digest:
            raise IntegrityError(f"processor {description.processor_id} differs from the plan data-use policy")
        if (
            description.execution_scope is ProcessorExecutionScope.DECLARED_EXTERNAL
            and not plan.data_use_policy.allows_external_processing
        ):
            raise IntegrityError(
                f"processor {description.processor_id} declares external execution under a local-only data-use policy"
            )
        if description.retry_policy_digest != plan.retry_policy_digest:
            raise IntegrityError(f"processor {description.processor_id} differs from the plan retry policy")


def flatten_processor_records(
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


def projected_segment_byte_size(
    segment: Segment,
    allowed_fields: tuple[str, ...],
) -> int:
    """Account for the same projected content bytes during execution and replay."""

    return segment.content.byte_size if "content" in allowed_fields else 0


def processor_request(
    plan_ref: ArtifactRef,
    entry: DocumentEntry,
    plan: ProcessingPlan,
    description: ProcessorDescription,
    segment: Segment,
    prerequisites: tuple[ArtifactRef, ...],
    invocation_id: str,
) -> ProcessorRequest:
    return ProcessorRequest(
        plan_ref,
        description.processor_id,
        identity_digest(description.to_dict()),
        entry.source_item.item_id,
        (ProcessorRecordRef.for_segment(segment),),
        prerequisites,
        plan.data_use_policy.allowed_fields,
        description.item_limits,
        description.cache_policy.key_schema_id or "docspec-cache-disabled/1",
        invocation_id,
    )


def require_segment_input(
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


def validate_processor_result(
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
