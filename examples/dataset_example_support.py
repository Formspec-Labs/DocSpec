"""Small shared helpers for the bill and annual-CFR dataset examples."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spicy_docs.transport.capture import CapturedBodyResponse

from docspec.domain.identity import canonical_json_bytes, sha256_digest, stable_urn
from docspec.domain import core
from docspec.domain.core_admission import record_value
from docspec.domain.references import BlobRef
from docspec.domain.processor_policy import DataUsePolicy, ProcessorExecutionScope, ProcessorLimits
from docspec.adapters.document_processor import provider_processor
from examples.phrase_match_processor import PhraseMatcher


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def phrase_processor(revision, phrases=None, output=None, *, resource_id, resource_bytes=None, case_sensitive=False):
    raw = resource_bytes if resource_bytes is not None else canonical_json_bytes({"terms": [
        {"id": phrase.replace(" ", "-"), "label": phrase, "phrases": [phrase]} for phrase in phrases]})
    if output is not None:
        (output / f"phrases-{revision}.json").write_bytes(raw)
    resource = core.Resource(label="vocabulary", certainty="established",
        description={"resourceId": resource_id, "revision": revision, "digest": sha256_digest(raw)})
    limits = ProcessorLimits(max_input_bytes=64 * 1024, max_output_bytes=64 * 1024, max_output_records=1, max_duration_seconds=5)
    configuration = {"caseSensitive": case_sensitive, "matching": "literal-unicode-boundaries",
        "overlap": "all-phrases-all-starts", "ordering": "byte-start-end-term-id-phrase"}
    definition = core.OperationDefinition(format_version=1,
        definition_id=stable_urn("phrase-matcher", [configuration, resource.description]),
        implementation_id="docspec.example.PhraseMatcher", implementation_version="1", operation_kind="transformation",
        configuration=configuration, resources=(resource,))
    return provider_processor("phrase-matches", definition=definition,
        process=PhraseMatcher(resource, raw, case_sensitive=case_sensitive, limits=limits), limits=limits,
        data_use_policy=DataUsePolicy.create(execution_scope=ProcessorExecutionScope.LOCAL_ONLY, allowed_fields=("content", "evidence")),
        input_media_types=("text/*",), output_schema={"type": "object", "required": ["matches"],
            "properties": {"matches": {"type": "array"}}})


def run_documents(workspace, pipeline, source_state_id, *, name, processors, output):
    try:
        state = pipeline.run(source_state_id, run_id=name, processors=processors)
    except Exception as error:
        write_json(output / f"{name}-failures.json", {"errorType": type(error).__name__, "message": str(error),
            "ledger": str(workspace.path / "ledger.sqlite")})
        raise
    results = tuple(document_results(workspace, pipeline, state.state_id))
    write_json(output / f"{name}.json", {"state": record_value(state), "documents": [
        {"key": key, "results": [record_value(result) for result in stages]} for key, stages in results]})
    return state, results


def document_results(workspace, pipeline, state_id):
    with closing(pipeline.rows(state_id)) as summaries:
        for key, _, summary in summaries:
            selected = [row.value for batch in workspace.ledger.read_records(("selection", identifier)
                for identifier in summary["selections"]) for row in batch]
            results = tuple(row.value for batch in workspace.ledger.read_records(("result", selection.selected_result_id)
                for selection in selected) for row in batch)
            yield key, results


def processor_values(workspace, pipeline, result):
    for binding in result.outcome.outputs:
        if binding.label == "records":
            with closing(pipeline.rows(binding.entity_id)) as rows:
                for key, _, value in rows:
                    if ":output:" in key:
                        yield value


def matches(workspace, pipeline, state_id):
    return [match for _, results in document_results(workspace, pipeline, state_id)
        for value in processor_values(workspace, pipeline, results[-1]) for match in value["matches"]]


def output_value(workspace, result, label):
    identifier = next(binding.entity_id for binding in result.outcome.outputs if binding.label == label)
    with workspace.publisher.session() as session:
        with closing(session.read_records([("entity", identifier)])) as rows:
            entity = next(rows)[0].value
        if isinstance(entity.value, core.InlineValue):
            return entity.value.value
        if entity.value.codec == "json-v1":
            return session.read_json(entity.value)
        reference = BlobRef(entity.value.locator, entity.value.digest, entity.value.byte_size, entity.value.media_type)
        with closing(session.blobs.read(reference, max_bytes=64 * 1024**2)) as chunks:
            return b"".join(chunks)


def capture_facts(capture: CapturedBodyResponse) -> dict:
    return {
        "requestedUrl": capture.requested_url, "resolvedUrl": capture.resolved_url,
        "statusCode": capture.status_code, "contentType": capture.content_type,
        "observedAt": capture.observed_at, "byteSize": capture.byte_size, "sha256": capture.sha256,
    }


def retain_refusal(error: Exception, destination: Path, *, source: str = "bill") -> None:
    """Keep bounded publisher evidence before normal exception handling continues."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    receipt = {"errorType": type(error).__name__, "message": str(error),
               "acquisition": getattr(error, source + "_acquisition", None)}
    refused = getattr(error, "refused_response", None)
    if refused is not None:
        receipt["response"] = {"requestKey": refused.request_key, "stage": refused.stage,
                               "mediaType": refused.media_type, "unavailableReason": refused.unavailable_reason,
                               "observedByteSize": refused.observed_byte_size}
        if refused.response_bytes is not None:
            body = destination.with_suffix(".body")
            body.write_bytes(refused.response_bytes)
            receipt["response"].update({"bodyFile": body.name, "sha256": sha256_digest(refused.response_bytes)})
    destination.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
