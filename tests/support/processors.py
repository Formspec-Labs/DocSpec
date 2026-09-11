"""Shared processors fixtures, extracted from tests.test_processor_reprocessing."""

from __future__ import annotations

from typing import Any

from docspec.domain.content import DerivedRecord, ProcessorDisposition
from docspec.domain.identity import canonical_json_bytes, identity_digest
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import (
    AcceptedFailurePolicy,
    DataUsePolicy,
    ProcessorExecutionScope,
    RetentionPolicy,
    RetryPolicy,
)
from docspec.domain.processors import (
    ProcessorCacheMode,
    ProcessorCachePolicy,
    ProcessorDescription,
    ProcessorInput,
    ProcessorItemLimits,
    ProcessorPayload,
    ProcessorRequest,
    ProcessorResourceIdentity,
    ProcessorResourceUse,
    ProcessorResult,
    ProcessorSet,
)
from docspec.errors import IntegrityError
from docspec.processing.extraction import DefaultExtractorRegistry
from docspec.processing.segmentation import DefaultSegmenterRegistry
from tests.helpers import (
    local_profile_set,
)


class _CountingFetcher:
    def __init__(self, delegate) -> None:  # type: ignore[no-untyped-def]
        self.delegate = delegate
        self.calls: list[str] = []

    def fetch(self, candidate, *, max_bytes: int, task_id: str, attempt_id: str):
        self.calls.append(candidate.candidate_id)
        return self.delegate.fetch(
            candidate,
            max_bytes=max_bytes,
            task_id=task_id,
            attempt_id=attempt_id,
        )


class _CountingExtractor:
    extractor_id = DefaultExtractorRegistry.extractor_id

    def __init__(self) -> None:
        self.delegate = DefaultExtractorRegistry()
        self.calls = 0

    def extract(self, source, content):
        self.calls += 1
        return self.delegate.extract(source, content)


class _CountingSegmenter:
    segmenter_id = DefaultSegmenterRegistry.segmenter_id

    def __init__(self) -> None:
        self.delegate = DefaultSegmenterRegistry()
        self.calls = 0

    def segment(self, representation):
        self.calls += 1
        return self.delegate.segment(representation)


class _CountingProcessor:
    def __init__(self, description: ProcessorDescription) -> None:
        self.description = description
        self.calls: list[str] = []
        self.representation_ranges: list[tuple[int, int]] = []

    def process(
        self,
        request: ProcessorRequest,
        payload: ProcessorPayload,
        prerequisite_results: tuple[ProcessorResult, ...],
    ) -> ProcessorResult:
        payload.require("content")
        payload.require("representationCoordinates")
        content = payload.content
        representation_coordinates = payload.representation_coordinates
        if content is None or representation_coordinates is None:
            raise IntegrityError("test processor received an incomplete projected payload")
        input_record = payload.input_record
        self.calls.append(input_record.record_id)
        self.representation_ranges.append(representation_coordinates)
        value: dict[str, Any] = {
            "processorName": self.description.name,
            "processorVersion": self.description.version,
            "segmentId": input_record.record_id,
            "contentDigest": input_record.record_digest,
        }
        input_ids = (
            input_record.record_id,
            *(record.derived_id for prerequisite in prerequisite_results for record in prerequisite.derived_records),
        )
        receipt: dict[str, Any] = {
            "executionKind": "test-deterministic",
            "requestId": request.request_id,
            "reuseKey": request.reuse_key,
            "processorId": self.description.processor_id,
            "processorDescriptionDigest": identity_digest(self.description.to_dict()),
            "inputIds": list(input_ids),
            "outputDigest": identity_digest(value),
            "outputSchemaId": self.description.output_schema_id,
            "outputMediaType": self.description.output_media_types[0],
            "configurationDigest": self.description.configuration_digest,
            "dataUsePolicyDigest": self.description.data_use_policy_digest,
            "retryPolicyDigest": self.description.retry_policy_digest,
        }
        record = DerivedRecord.create(
            source_item_id=request.source_item_id,
            processor_id=self.description.processor_id,
            input_ids=input_ids,
            schema_id=self.description.output_schema_id,
            value=value,
            provider_receipt_digest=identity_digest(receipt),
            disposition=ProcessorDisposition.PRODUCED,
        )
        return ProcessorResult(
            request.request_id,
            request.reuse_key,
            ProcessorDisposition.PRODUCED,
            self.description.output_media_types[0],
            self.description.external_resources,
            (record,),
            ProcessorResourceUse(
                len(content) + sum(len(canonical_json_bytes(result.to_dict())) for result in prerequisite_results),
                len(canonical_json_bytes(value)),
                0,
            ),
            (),
            receipt,
        )


def _description(
    name: str,
    version: str,
    retry: RetryPolicy,
    *,
    dependencies: tuple[str, ...] = (),
    external_resources: tuple[ProcessorResourceIdentity, ...] = (),
    output_media_types: tuple[str, ...] = ("application/json",),
) -> ProcessorDescription:
    return ProcessorDescription.create(
        name=name,
        version=version,
        implementation_id=f"tests.{name}/{version}",
        accepted_inputs=(ProcessorInput("segment", ("docspec-segment/1",), ("*/*",)),),
        output_schema_id=f"tests-{name}-output/1",
        output_media_types=output_media_types,
        execution_scope=ProcessorExecutionScope.LOCAL_ONLY,
        external_resources=external_resources,
        dependencies=dependencies,
        deterministic=True,
        cache_policy=ProcessorCachePolicy(ProcessorCacheMode.EXACT_INPUTS, "tests-exact-inputs/1"),
        configuration_digest=identity_digest({"name": name, "version": version}),
        data_use_policy_digest=DataUsePolicy.local_content().digest,
        item_limits=ProcessorItemLimits(10, 1024 * 1024, 1, 1024 * 1024, 60),
        retry_policy_digest=retry.digest,
        capabilities=("test-output",),
    )


def _plan(
    source,
    base,
    processors: tuple[_CountingProcessor, ...],
    retry: RetryPolicy,
    accepted: AcceptedFailurePolicy,
) -> ProcessingPlan:
    declared = ProcessorSet(tuple(item.description for item in processors))
    processor_set = ProcessorSet(declared.execution_order)
    stages = StagePolicy(
        (DefaultExtractorRegistry.extractor_id,),
        DefaultSegmenterRegistry.segmenter_id,
        tuple(item.processor_id for item in processor_set.execution_order),
    )
    return ProcessingPlan.create(
        source_catalog=source,
        base_release=base,
        profiles=local_profile_set(),
        limits=WorkLimits(2, 1024 * 1024, 100, 100, 1000, 1024 * 1024, 60, retry.max_attempts),
        stages=stages,
        processors=processor_set,
        partition_count=8,
        selection={},
        retention_policy=RetentionPolicy.retain_all(),
        data_use_policy=DataUsePolicy.local_content(),
        retry_policy_digest=retry.digest,
        accepted_failure_policy_digest=accepted.digest,
    )
