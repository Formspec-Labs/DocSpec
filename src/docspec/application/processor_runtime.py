"""Execute declared processor work with bounded retries and verified cache reuse."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from docspec.domain.content import DerivedRecord
from docspec.domain.jobs import DocumentEntry
from docspec.domain.plans import ProcessingPlan
from docspec.domain.policies import DataUsePolicy, RetryPolicy
from docspec.domain.processors import (
    ProcessorCacheMode,
    ProcessorDescription,
    ProcessorPayload,
    ProcessorRequest,
    ProcessorResult,
)
from docspec.domain.references import ArtifactRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.control_repository import ControlRepository
from docspec.ports.processor import Processor
from docspec.ports.processor_cache import ProcessorResultCache
from docspec.processing.artifacts import SegmentPayload

from .execution_evidence import failure_record, put_receipt
from .processor_rules import processor_request, require_segment_input, validate_processor_result
from .work_budget import WorkBudget


class ProcessorRuntime:
    """Run processors while appending evidence to the caller's current progress.

    The caller retains result, record, and receipt collections even on failure.
    This object neither owns a store nor creates another work budget.
    """

    def __init__(
        self,
        *,
        plan_ref: ArtifactRef,
        controls: ControlRepository,
        processors: Mapping[str, Processor[ProcessorPayload, ProcessorResult]],
        retry_policy: RetryPolicy,
        processor_cache: ProcessorResultCache | None,
        sleep: Callable[[float], None],
        monotonic: Callable[[], float],
    ) -> None:
        self._plan_ref = plan_ref
        self._controls = controls
        self._processors = dict(processors)
        self._retry_policy = retry_policy
        self._processor_cache = processor_cache
        self._sleep = sleep
        self._monotonic = monotonic

    def processor(self, identifier: str) -> Processor[ProcessorPayload, ProcessorResult]:
        try:
            processor = self._processors[identifier]
        except KeyError as error:
            raise IntegrityError(f"processing plan names unknown processor {identifier}") from error
        if processor.description.processor_id != identifier:
            raise IntegrityError("processor registry key differs from its description")
        return processor

    def run_graph(
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
            processor = self.processor(identifier)
            records = derived_by_processor.setdefault(identifier, [])
            for segment in segments:
                require_segment_input(processor.description, segment)
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
                request = processor_request(
                    self._plan_ref,
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
                    put_receipt(
                        self._controls,
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
                validate_processor_result(
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
                failure = failure_record("processor", error, attempt)
                receipt_refs.append(
                    put_receipt(
                        self._controls,
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
                put_receipt(
                    self._controls,
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
        validate_processor_result(
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
