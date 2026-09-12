"""Two explicit content requirements over already verified active result rows."""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from typing import Any

from docspec.application.execution_evidence import (
    load_stage_receipts, verify_processor_receipts, verify_retained_stage_outputs,
    verify_unfinished_processor_attempts,
)
from docspec.domain.content import (
    AcquisitionDisposition, CapturedFile, DerivedRecord, Representation, Segment, SourceItem, SourceItemState,
)
from docspec.domain.dispositions import parse_disposition_payload
from docspec.domain.identity import canonical_json_bytes, identity_digest
from docspec.domain.plans import ProcessingPlan
from docspec.domain.processors import ProcessorRequest
from docspec.domain.references import ArtifactRef
from docspec.errors import DocSpecError, IntegrityError, LimitExceededError
from docspec.processing.artifacts import (
    IDENTITY_TRANSFORM, RepresentationPayload, SegmentPayload, verify_representation_mapping,
    verify_segment_representation,
)

from .io import EVIDENCE_BYTES, ITEM_BYTES
from .references import evidence_references


class ExportAdmissionError(DocSpecError):
    """A valid result does not meet the caller's explicit content requirement."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        super().__init__(f"result does not meet export admission: {report['reasonCounts']}")


def collection(item_id: str, kind: str) -> str:
    return identity_digest({"sourceItemId": item_id, "kind": kind})


def _text_observation(representation, content):
    if representation.kind not in {"text", "visible-text", "pdf-text"}:
        return None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return "invalid-utf8"
    return "nonempty" if text.strip() else "empty"


def _verify_output_bytes(view, files, representations, segments, counts):
    files_by_id = {value.file_id: value for value in files}
    by_representation = {}
    for segment in segments:
        by_representation.setdefault(segment.representation_id, []).append(segment)
    observations = []
    for representation in representations:
        payload = RepresentationPayload(representation, view.read_blob(representation.blob, max_bytes=EVIDENCE_BYTES))
        observations.append(_text_observation(representation, payload.content))
        identity_mappings = tuple(value for value in representation.evidence_mappings
            if value.transformation == IDENTITY_TRANSFORM)
        counts["derivedMappingsNotReplayed"] += len(representation.evidence_mappings) - len(identity_mappings)
        if identity_mappings:
            source = view.read_blob(files_by_id[representation.file_id].blob, max_bytes=EVIDENCE_BYTES)
            for mapping in identity_mappings:
                verify_representation_mapping(mapping,
                    payload.content[mapping.representation_start:mapping.representation_end], source)
                counts["verifiedIdentityMappings"] += 1
        for segment in by_representation.get(representation.representation_id, ()):
            verify_segment_representation(
                SegmentPayload(segment, view.read_blob(segment.content, max_bytes=EVIDENCE_BYTES)), payload,
            )
            counts["verifiedSegmentSlices"] += 1
    return observations


def _processor_completion(view, item, stages, outcome, segments, loaded, derived):
    receipts = tuple(pair for pair in loaded if pair[1]["format"] not in {
        "docspec-extraction-receipt", "docspec-segmentation-receipt",
    })
    invocations = tuple(raw for _, raw in receipts if raw["format"] == "docspec-processor-invocation-receipt")
    if not invocations:
        if derived:
            raise IntegrityError("export derived records lack processor invocation evidence")
        verify_unfinished_processor_attempts(receipts, outcome["entryId"], stages.processor_ids,
            tuple(segment.segment_id for segment in segments))
        return not stages.processor_ids or not segments
    requests = tuple(ProcessorRequest.from_dict(raw["request"]) for raw in invocations)
    plan_ref = requests[0].plan
    if any(request.plan != plan_ref for request in requests):
        raise IntegrityError("export item processor receipts disagree on their owning plan")
    owning_plan = ProcessingPlan.from_dict(view.read_evidence(plan_ref))
    if owning_plan.plan_id != plan_ref.artifact_id or owning_plan.stages != stages:
        raise IntegrityError("export processor plan differs from its retained item policy")
    processor_order = {identifier: ordinal for ordinal, identifier in enumerate(stages.processor_ids)}
    segment_order = {segment.segment_id: ordinal for ordinal, segment in enumerate(segments)}
    ordered = tuple(sorted(receipts, key=lambda pair: (
        processor_order.get(pair[1].get("processorId"), -1),
        segment_order.get(pair[1].get("segmentId"), -1),
        pair[1]["format"] == "docspec-processor-invocation-receipt", pair[1].get("attempt", 0),
    )))
    results, _ = verify_processor_receipts(view, owning_plan,
        {segment.segment_id: segment for segment in segments}, ordered,
        entry_id=outcome["entryId"], source_item_id=item.item_id, derived_records=derived,
        disposition=AcquisitionDisposition(outcome["disposition"]), max_attempts=owning_plan.limits.max_attempts)
    return len(results) == len(stages.processor_ids) * len(segments)


def observe_active_items(view, workspace, admission: str) -> dict[str, Any]:
    """Check promised receipt coverage and report simple text observations.

    Each source item is bounded independently. Completeness of declared work
    is an integrity requirement; semantic completeness of extracted text is
    not measurable from these records and is never claimed here.
    """
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    sample = []
    with closing(workspace.stream_records("sources")) as sources:
        for row in sources:
            item = SourceItem.from_dict(row["payload"])
            counts["sourceItems"] += 1
            used = len(canonical_json_bytes(row))

            def payloads(kind):
                nonlocal used
                with closing(workspace.stream_records(collection(item.item_id, kind))) as records:
                    for record in records:
                        used += len(canonical_json_bytes(record))
                        if used > ITEM_BYTES:
                            raise LimitExceededError("export item evidence exceeds its metadata byte limit")
                        yield record["payload"]

            dispositions = tuple(payloads("dispositions"))
            if len(dispositions) != 1:
                raise IntegrityError("export source requires one retained disposition")
            outcome = dispositions[0]
            stages, failure = parse_disposition_payload(outcome)
            if outcome["disposition"] not in {value.value for value in AcquisitionDisposition} - {
                AcquisitionDisposition.UNCHANGED.value, AcquisitionDisposition.REJECTED_RUN.value,
            }:
                raise IntegrityError("export contains an unfinished or rejected retained disposition")
            files = tuple(CapturedFile.from_dict(value) for value in payloads("files"))
            representations = tuple(Representation.from_dict(value) for value in payloads("representations"))
            segments = tuple(Segment.from_dict(value) for value in payloads("segments"))
            references = []
            for value in payloads("receipts"):
                if value["entryId"] != outcome["entryId"]:
                    raise IntegrityError("export receipt belongs to another retained item attempt")
                reference = ArtifactRef.from_dict(value["artifact"])
                used += reference.byte_size
                if used > ITEM_BYTES:
                    raise LimitExceededError("export item receipts exceed its metadata byte limit")
                references.append(reference)
            loaded = load_stage_receipts(view, tuple(references))
            charged = set(references)
            for _, value in loaded:
                for reference in evidence_references(value):
                    if reference not in charged:
                        charged.add(reference)
                        used += reference.byte_size
                        if used > ITEM_BYTES:
                            raise LimitExceededError("export item controls exceed its metadata byte limit")
            files, representations, segments, _, segmentation = verify_retained_stage_outputs(
                item, stages, files, representations, segments, loaded,
            )
            derived = tuple(DerivedRecord.from_dict(value) for kind in view.layer_kinds if kind.startswith("derived:")
                for value in payloads(kind))
            capture_complete = len(files) == len(item.candidates)
            extraction_complete = capture_complete and len(representations) == len(files)
            segmentation_complete = extraction_complete and len(segmentation) == len(representations)
            if any(raw["format"].startswith("docspec-processor-") for _, raw in loaded) and not segmentation_complete:
                raise IntegrityError("export processor evidence precedes complete segmentation")
            processing_complete = _processor_completion(view, item, stages, outcome, segments, loaded, derived)
            complete = capture_complete and (not stages.requests_extraction or extraction_complete) and (
                not stages.requests_segmentation or segmentation_complete) and processing_complete
            if outcome["disposition"] == AcquisitionDisposition.CAPTURED.value and not complete:
                raise IntegrityError("export successful item lacks requested-stage completion evidence")
            selected = item.state == SourceItemState.ACTIVE and outcome["disposition"] not in {"excluded", "deleted"}
            counts["selectedItems"] += int(selected)
            counts["failedItems"] += int(failure is not None)
            observations = _verify_output_bytes(view, files, representations, segments, counts)
            counts["nonemptyTextRepresentations"] += observations.count("nonempty")
            counts["emptyTextRepresentations"] += observations.count("empty")
            counts["invalidUtf8TextRepresentations"] += observations.count("invalid-utf8")
            counts["itemsWithNonemptyText"] += int("nonempty" in observations)
            item_reasons = []
            if admission == "nonempty-text" and selected:
                if failure is not None:
                    item_reasons.append("terminal-failure")
                if "nonempty" not in observations:
                    item_reasons.append("no-nonempty-text")
                if "invalid-utf8" in observations:
                    item_reasons.append("invalid-utf8-text")
            for reason in item_reasons:
                reasons[reason] += 1
                if len(sample) < 10:
                    sample.append({"sourceItemId": item.item_id, "reason": reason})
    report = {"admission": admission, "counts": dict(sorted(counts.items())),
        "reasonCounts": dict(sorted(reasons.items())), "sample": sample,
        "sampleTruncated": sum(reasons.values()) > len(sample),
        "semanticCompleteness": "not-established"}
    if reasons:
        raise ExportAdmissionError(report)
    return report
