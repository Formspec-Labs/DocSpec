"""Provider-neutral processor descriptions and one deterministic example."""

from __future__ import annotations

from time import monotonic
from typing import Any

from docspec.domain.content import DerivedRecord, ProcessorDisposition
from docspec.domain.identity import canonical_json_bytes, identity_digest
from docspec.domain.policies import RetryPolicy
from docspec.domain.processors import (
    ProcessorCacheMode,
    ProcessorCachePolicy,
    ProcessorDescription,
    ProcessorInput,
    ProcessorItemLimits,
    ProcessorRecordRef,
    ProcessorRequest,
    ProcessorResourceUse,
    ProcessorResult,
    processor_receipt_digest,
)
from docspec.errors import IntegrityError
from docspec.processing.artifacts import SegmentPayload

class ContentStatisticsProcessor:
    """Produce local content statistics without assigning document meaning."""

    def __init__(
        self,
        *,
        item_limits: ProcessorItemLimits | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        configuration_digest = identity_digest({"wordRule": "unicode-whitespace-separated"})
        data_use_policy_digest = identity_digest({"dataUse": "local-bytes-only"})
        retry_policy_digest = (retry_policy or RetryPolicy()).digest
        limits = item_limits or ProcessorItemLimits(
            max_input_records=1,
            max_input_bytes=64 * 1024 * 1024,
            max_output_records=1,
            max_output_bytes=256 * 1024,
            max_duration_seconds=60,
        )
        self.description = ProcessorDescription.create(
            name="content-statistics",
            version="1.0",
            implementation_id="docspec.processing.ContentStatisticsProcessor/v1",
            accepted_inputs=(ProcessorInput("segment", ("docspec-segment/1",), ("*/*",)),),
            output_schema_id="docspec-content-statistics/1",
            output_media_types=("application/vnd.docspec.content-statistics+json",),
            external_resources=(),
            dependencies=(),
            deterministic=True,
            cache_policy=ProcessorCachePolicy(
                ProcessorCacheMode.EXACT_INPUTS,
                "docspec-exact-processor-cache-key/1",
            ),
            configuration_digest=configuration_digest,
            data_use_policy_digest=data_use_policy_digest,
            item_limits=limits,
            retry_policy_digest=retry_policy_digest,
            capabilities=("content-digest", "content-statistics", "source-evidence"),
        )

    def process(
        self,
        request: ProcessorRequest,
        segment: SegmentPayload,
        prerequisite_results: tuple[ProcessorResult, ...],
    ) -> ProcessorResult:
        started_at = monotonic()
        # Re-run the immutable payload checks at the injection boundary so a
        # custom caller cannot bypass digest or identity validation.
        SegmentPayload(segment.segment, segment.content)
        expected_input = ProcessorRecordRef.for_segment(segment.segment)
        if (
            request.processor_id != self.description.processor_id
            or request.processor_description_digest != identity_digest(self.description.to_dict())
            or request.source_item_id != segment.segment.source_item_id
            or request.input_records != (expected_input,)
            or request.prerequisite_results
            or prerequisite_results
            or request.item_limits != self.description.item_limits
        ):
            raise IntegrityError("processor request differs from the pinned content-statistics invocation")
        if len(segment.content) > self.description.item_limits.max_input_bytes:
            raise IntegrityError("processor input exceeds its declared item byte limit")
        try:
            text = segment.content.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None:
            line_count = None
        else:
            line_count = len(text.splitlines()) if text else 0
        value: dict[str, Any] = {
            "segmentId": segment.segment.segment_id,
            "contentDigest": segment.segment.content.digest,
            "byteCount": len(segment.content),
            "utf8CodepointCount": len(text) if text is not None else None,
            "lineCount": line_count,
            "wordCount": len(text.split()) if text is not None else None,
            "evidence": segment.segment.evidence.to_dict(),
        }
        if len(canonical_json_bytes(value)) > self.description.item_limits.max_output_bytes:
            raise IntegrityError("processor output exceeds its declared item byte limit")
        if monotonic() - started_at > self.description.item_limits.max_duration_seconds:
            raise IntegrityError("processor execution exceeds its declared item duration limit")
        output_digest = identity_digest(value)
        receipt: dict[str, Any] = {
            "executionKind": "local-deterministic",
            "requestId": request.request_id,
            "reuseKey": request.reuse_key,
            "processorId": self.description.processor_id,
            "processorDescriptionDigest": identity_digest(self.description.to_dict()),
            "inputIds": [segment.segment.segment_id],
            "outputDigest": output_digest,
            "outputSchemaId": self.description.output_schema_id,
            "outputMediaType": self.description.output_media_types[0],
            "configurationDigest": self.description.configuration_digest,
            "dataUsePolicyDigest": self.description.data_use_policy_digest,
            "retryPolicyDigest": self.description.retry_policy_digest,
        }
        receipt_digest = processor_receipt_digest(receipt)
        record = DerivedRecord.create(
            source_item_id=segment.segment.source_item_id,
            processor_id=self.description.processor_id,
            input_ids=(segment.segment.segment_id,),
            schema_id=self.description.output_schema_id,
            value=value,
            provider_receipt_digest=receipt_digest,
            disposition=ProcessorDisposition.PRODUCED,
        )
        elapsed_milliseconds = max(0, int((monotonic() - started_at) * 1000))
        return ProcessorResult(
            request.request_id,
            request.reuse_key,
            ProcessorDisposition.PRODUCED,
            self.description.output_media_types[0],
            self.description.external_resources,
            (record,),
            ProcessorResourceUse(
                len(segment.content),
                len(canonical_json_bytes(value)),
                elapsed_milliseconds,
            ),
            (),
            receipt,
        )
