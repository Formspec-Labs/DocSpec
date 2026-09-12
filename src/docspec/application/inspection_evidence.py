"""Bounded explanations of saved entry work, independent of executable plugins."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from docspec.domain.content import AcquisitionDisposition, SourceItemState
from docspec.domain.jobs import DocumentEntry, EntryExecutionMode
from docspec.domain.plans import ProcessingPlan
from docspec.domain.processors import ProcessorResult
from docspec.domain.references import ArtifactRef
from docspec.errors import IntegrityError
from docspec.ports.control_repository import ControlRepository

from .execution_evidence import load_stage_receipts, verify_processor_receipts, verify_stage_receipt_outputs
from .work_budget import WorkBudget


ENTRY_COUNT_KEYS = (
    "capturedFiles", "capturedBytes", "representations", "representationBytes", "segments", "derivedRecords",
    "newCapturedFiles", "newCapturedBytes", "reusedCapturedFiles", "reusedCapturedBytes",
    "newRepresentations", "reusedRepresentations", "newSegments", "reusedSegments",
    "processorInvocations", "processorBaseReuses", "processorCacheHits", "recordedProcessorAttempts",
    "recordedProcessorFailures", "recordedProcessorElapsedMilliseconds", "failures",
)


def _stage(*, requested: bool, applicable: bool, complete: bool, started: bool, terminal: bool, reused: bool) -> dict[str, Any]:
    if not applicable:
        status = "not-applicable"
    elif not requested:
        status = "not-requested"
    elif complete:
        status = "recorded-complete"
    elif terminal:
        status = "not-completed"
    else:
        status = "partial" if started else "pending"
    origin = None
    if applicable and requested and (started or complete):
        origin = "reused-base" if reused else "this-run"
    return {"requested": requested, "status": status, "origin": origin}


def _processor_evidence(
    receipts: tuple[tuple[ArtifactRef, dict[str, Any]], ...],
    results: Mapping[tuple[str, str], tuple[ArtifactRef, ProcessorResult]],
    counts: dict[str, int],
    *,
    sample_limit: int,
) -> dict[str, Any]:
    sample: list[dict[str, Any]] = []
    attempts_by_request: dict[str, int] = {}
    attempt_sample: list[dict[str, Any]] = []
    reported: dict[str, dict[str, int]] = {
        origin: {"results": 0, "inputBytes": 0, "outputBytes": 0, "durationMilliseconds": 0, "externalRequestCount": 0}
        for origin in ("this-run", "cache", "reused-base")
    }
    for reference, receipt in receipts:
        if receipt["format"] != "docspec-processor-attempt-receipt":
            continue
        counts["recordedProcessorAttempts"] += 1
        counts["recordedProcessorFailures"] += receipt["outcome"] == "failed"
        counts["recordedProcessorElapsedMilliseconds"] += receipt["elapsedMilliseconds"]
        request_id = receipt["requestId"]
        attempts_by_request[request_id] = attempts_by_request.get(request_id, 0) + 1
        if len(attempt_sample) < sample_limit:
            attempt_sample.append({"receipt": reference.to_dict(), **receipt})
    for reference, receipt in receipts:
        if receipt["format"] != "docspec-processor-invocation-receipt":
            continue
        counts["processorInvocations"] += 1
        cache = receipt["cacheDisposition"]
        counts["processorCacheHits"] += cache == "hit"
        counts["processorBaseReuses"] += cache == "reused-base"
        origin = "reused-base" if cache == "reused-base" else "cache" if cache == "hit" else "this-run"
        result_ref, result = results[(receipt["processorId"], receipt["segmentId"])]
        reported[origin]["results"] += 1
        for name, value in result.resource_use.to_dict().items():
            reported[origin][name] += value
        if len(sample) < sample_limit:
            sample.append({
                "processorId": receipt["processorId"], "segmentId": receipt["segmentId"],
                "requestId": receipt["request"]["requestId"], "result": result_ref.to_dict(),
                "receipt": reference.to_dict(), "cacheDisposition": cache,
                "recordedAttempts": attempts_by_request.get(receipt["request"]["requestId"], 0),
                "resultDisposition": result.disposition.value, "derivedRecords": len(result.derived_records),
                "resultReportedResourceUse": result.resource_use.to_dict(),
            })
    return {
        "sample": sample, "sampleTruncated": counts["processorInvocations"] > len(sample),
        "attemptSample": attempt_sample,
        "attemptSampleTruncated": counts["recordedProcessorAttempts"] > len(attempt_sample),
        "resultReportedResourceUseByOrigin": reported,
        "scope": "saved-invocations-and-attempts",
        "cacheMeaning": "A cache hit identifies the retained result; recorded attempts may still show local execution.",
    }


def entry_evidence(
    entry: DocumentEntry,
    plan: ProcessingPlan,
    controls: ControlRepository,
    *,
    sample_limit: int = 5,
) -> dict[str, Any]:
    """Explain one saved entry without invoking stages or reading content blobs.

    The owning plan and entry must already come from the caller's pinned store
    view. Receipt identity, output links and processor result relationships use
    the same checks as recovery. This is not a proof of replayability: selected
    plugin identity and blob content verification remain the executor's job.
    Counts cover durable observations across this entry's attempts; operations
    lost before a checkpoint cannot be reconstructed. Samples alone are bounded
    by ``sample_limit``; reading all evidence is bounded by the owning store.
    """

    if type(sample_limit) is not int or sample_limit < 0:
        raise ValueError("inspection sample limit must be a non-negative integer")
    if entry.requested_stages != plan.stages:
        raise IntegrityError("inspection entry stages differ from its owning plan")
    loaded = load_stage_receipts(controls, entry.stage_receipts)
    extraction, segmentation = verify_stage_receipt_outputs(
        entry.captured_files, entry.representations, entry.segments, loaded,
    )
    if not plan.stages.requests_extraction and entry.representations:
        raise IntegrityError("inspection entry has unrequested extraction output")
    if not plan.stages.requests_segmentation and (entry.segments or segmentation):
        raise IntegrityError("inspection entry has unrequested segmentation output")
    results, _ = verify_processor_receipts(
        controls, plan, {item.segment_id: item for item in entry.segments}, loaded,
        entry_id=entry.entry_id, source_item_id=entry.source_item.item_id,
        derived_records=entry.derived_records, disposition=entry.disposition,
        max_attempts=plan.limits.max_attempts,
    )
    counts = dict.fromkeys(ENTRY_COUNT_KEYS, 0)
    counts.update({
        "capturedFiles": len(entry.captured_files),
        "capturedBytes": sum(item.blob.byte_size for item in entry.captured_files),
        "representations": len(entry.representations),
        "representationBytes": sum(item.blob.byte_size for item in entry.representations),
        "segments": len(entry.segments), "derivedRecords": len(entry.derived_records), "failures": len(entry.failures),
    })
    reuse_capture = entry.execution_mode is not EntryExecutionMode.FULL
    reuse_extraction = entry.execution_mode in {EntryExecutionMode.FROM_REPRESENTATIONS, EntryExecutionMode.FROM_SEGMENTS}
    reuse_segmentation = entry.execution_mode is EntryExecutionMode.FROM_SEGMENTS
    counts[("reused" if reuse_capture else "new") + "CapturedFiles"] = counts["capturedFiles"]
    counts[("reused" if reuse_capture else "new") + "CapturedBytes"] = counts["capturedBytes"]
    counts[("reused" if reuse_extraction else "new") + "Representations"] = counts["representations"]
    counts[("reused" if reuse_segmentation else "new") + "Segments"] = counts["segments"]
    processors = _processor_evidence(loaded, results, counts, sample_limit=sample_limit)
    capture_complete = len(entry.captured_files) == len(entry.source_item.candidates)
    extraction_complete = capture_complete and len(extraction) == len(entry.captured_files)
    segmentation_complete = extraction_complete and len(segmentation) == len(entry.representations)
    applicable = entry.source_item.state is SourceItemState.ACTIVE
    common = {"applicable": applicable, "terminal": entry.terminal}
    stages = {
        "capture": _stage(**common, requested=True, complete=capture_complete, started=bool(entry.captured_files), reused=reuse_capture),
        "extraction": _stage(**common, requested=plan.stages.requests_extraction, complete=extraction_complete,
                             started=bool(extraction), reused=reuse_extraction),
        "segmentation": _stage(**common, requested=plan.stages.requests_segmentation, complete=segmentation_complete,
                               started=bool(segmentation), reused=reuse_segmentation),
        "processing": _stage(**common, requested=bool(plan.stages.processor_ids),
                             complete=segmentation_complete and len(results) == len(plan.stages.processor_ids) * len(entry.segments),
                             started=bool(results) or bool(counts["recordedProcessorAttempts"]),
                             reused=entry.execution_mode is EntryExecutionMode.FROM_SEGMENTS and not entry.processor_ids_to_run),
    }
    observations: dict[str, int] = {"pagesOrFrames": 0, "representationsWithObservation": 0, "representationsWithoutObservation": 0}
    for receipt in extraction:
        observed = WorkBudget.extraction_observation(representation_kind=receipt.kind, metadata=receipt.metadata)
        if receipt.metadata.get("pageCount") is None and receipt.metadata.get("frameCount") is None:
            observations["representationsWithoutObservation"] += 1
        else:
            observations["pagesOrFrames"] += observed
            observations["representationsWithObservation"] += 1
    stages["extraction"]["receiptObservations"] = observations
    if entry.disposition is AcquisitionDisposition.CAPTURED and any(
        value["requested"] and value["status"] != "recorded-complete" for value in stages.values()
    ):
        raise IntegrityError("completed inspection entry lacks requested stage evidence")
    return {
        "sourceItemId": entry.source_item.item_id, "sourceVersion": entry.source_item.version,
        "entryId": entry.entry_id, "executionMode": entry.execution_mode.value,
        "processorIdsToRun": list(entry.processor_ids_to_run), "requestedStages": entry.requested_stages.to_dict(),
        "disposition": None if entry.disposition is None else entry.disposition.value,
        "counts": counts, "stages": stages, "processors": processors,
        "failures": {"sample": [failure.to_dict() for failure in entry.failures[:sample_limit]],
                     "sampleTruncated": len(entry.failures) > sample_limit},
        "verificationScope": "saved-entry-receipt-identities-and-output-relationships",
        "unavailable": {
            "monetaryCost": "No monetary cost is recorded.",
            "totalExecutionMilliseconds": "Only saved processor attempts record measured elapsed time.",
            "physicalWork": "Transfers, calls or work lost before checkpointing are not reconstructed.",
            "replayability": "Inspection does not load selected plugins or verify content blob bytes.",
        },
    }
