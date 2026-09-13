"""Small shared helpers for the bill and annual-CFR dataset examples."""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path

from spicy_docs.transport.capture import CapturedBodyResponse

from docspec.domain.identity import canonical_json_bytes, sha256_digest
from docspec.domain.policies import RetryPolicy
from docspec.domain.processors import ProcessorResourceIdentity, ProcessorResourceKind
from docspec.runtime import open_local_inspection
from examples.phrase_match_processor import PhraseMatchProcessor


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def phrase_processor(
    revision: str, phrases: tuple[str, ...], retry: RetryPolicy, output: Path, *, resource_id: str,
) -> PhraseMatchProcessor:
    raw = canonical_json_bytes({"terms": [{"id": phrase.replace(" ", "-"), "label": phrase,
                                            "phrases": [phrase]} for phrase in phrases]})
    (output / f"phrases-{revision}.json").write_bytes(raw)
    return PhraseMatchProcessor(ProcessorResourceIdentity(
        resource_id, ProcessorResourceKind.REFERENCE_DATA, revision, sha256_digest(raw),
    ), raw, retry_policy=retry)


def finish_run(prepared, workspace, source_producer, release_producer, name: str, output: Path, views: ExitStack):
    run = prepared.run()
    with open_local_inspection(prepared.plan, workspace, document_release_producer=release_producer,
                                 source_catalog_producer=source_producer, run_ref=run) as view:
        report = {
            "plan": prepared.plan.to_dict(), "run": run.to_dict(), "result": None,
            "handoff": prepared.handoff_ref.to_dict(), "inspection": view.summary(),
        }
        write_json(output / f"{name}.json", report)
        failures = list(view.records("failures"))
        if failures:
            write_json(output / f"{name}-failures.json", failures)
            raise RuntimeError(f"{name} recorded {len(failures)} failure(s); inspect {output / f'{name}-failures.json'} "
                               f"and {output / 'source-evidence'}")
    result = prepared.retain(run)
    view = views.enter_context(open_local_inspection(prepared.plan, workspace, document_release_producer=release_producer,
                                 source_catalog_producer=source_producer, release_ref=result))
    write_json(output / f"{name}.json", report | {"result": result.to_dict(), "inspection": view.summary()})
    return result, view


def matches(view, processor: PhraseMatchProcessor) -> list[dict]:
    return [match for row in view.records("derived:" + processor.description.processor_id)
            for match in row["payload"]["value"]["matches"]]


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
