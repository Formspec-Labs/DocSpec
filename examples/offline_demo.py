"""Capture, repair, process, and compare a small local document experiment.

Run: python -m examples.offline_demo --output /absolute/new-experiment
The example uses the installed public API and needs no network or provider.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

from rulespec_artifacts import Producer

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.domain.identity import canonical_json_bytes, canonical_json_file_bytes, identity_digest, sha256_digest
from docspec.domain.jobs import FailureClass
from docspec.domain.plans import WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.processors import ProcessorResourceIdentity, ProcessorResourceKind
from docspec.domain.references import BlobRef
from docspec.processing import ParagraphSegmenter, TextExtractor
from docspec.runtime import build_local_catalog, open_local_catalog, open_local_inspection, prepare_local_experiment
from docspec.source_catalog import SourceCatalogCandidate, SuppliedRecordCatalogPolicy, SuppliedRecordSource
from docspec.workspace import LocalWorkspace
from examples.phrase_match_processor import PhraseMatchProcessor

INPUT_ROOT = Path(__file__).with_name("offline")
COMPLETED_AT = "2026-09-11T12:00:00Z"
DOCUMENTS = ("privacy", "security", "late-arrival", "out-of-scope")


def _write(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_file_bytes(value))


def _phrase_values(view, processor) -> list[dict]:
    """Check quote slices and source links for this bounded four-document fixture."""
    files = {row["payload"]["blob"]["digest"]: row["payload"] for row in view.records("files")}
    segments = {row["recordId"]: row["payload"] for row in view.records("segments")}
    values = []
    for row in view.records(f"derived:{processor.description.processor_id}"):
        value = row["payload"]["value"]
        segment = segments[value["segmentId"]]
        content = b"".join(view.read_blob(BlobRef.from_dict(segment["content"]), max_bytes=64 * 1024))
        assert sha256_digest(content) == value["segmentDigest"]
        evidence = value["enclosingSourceEvidence"]
        assert evidence == segment["evidence"]
        captured = files[evidence["sourceDigest"]]
        source = b"".join(view.read_blob(BlobRef.from_dict(captured["blob"]), max_bytes=64 * 1024))
        # This walkthrough uses source-native plain text. Other representations
        # can supply enclosing block/page evidence rather than exact slices.
        assert source[evidence["start"]:evidence["end"]] == content
        for match in value["matches"]:
            assert content[match["segmentByteStart"]:match["segmentByteEnd"]].decode("utf-8") == match["quote"]
        values.append(value)
    return sorted(values, key=canonical_json_bytes)


def run_example(output: Path) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    implementation = {
        "docspecVersion": version("docspec"),
        "example": sha256_digest(Path(__file__).read_bytes()),
        "processor": sha256_digest(Path(__file__).with_name("phrase_match_processor.py").read_bytes()),
    }
    _write(output / "implementation.json", implementation)
    implementation_id = "urn:docspec:example:implementation:" + identity_digest(implementation)
    source_producer = Producer(
        "docspec-example", implementation_id, "urn:docspec:verifier:source-catalog", "1.0.0", implementation_id,
    )
    release_producer = replace(source_producer, verifier_id="urn:docspec:verifier:document-release")
    workspace = LocalWorkspace(output)
    payloads = {name: (INPUT_ROOT / f"{name}.txt").read_bytes() for name in DOCUMENTS}
    namespace = "urn:docspec:example:review-notes"
    source = SuppliedRecordSource(({
        "recordId": name, "sourceIssuedVersion": "fixture-1", "title": name.replace("-", " ").title(),
        "metadata": {"synthetic": True},
        "candidateRenditions": [SourceCatalogCandidate(
            "body", "text/plain", "immutable-object", f"{name}.txt",
            expected_sha256=sha256_digest(content), expected_byte_size=len(content),
        ).to_dict()],
    } for name, content in payloads.items()), source_system_id=namespace, source_system_version="1",
        source_state_scope="complete-snapshot", max_records=4, max_bytes=16 * 1024)
    catalog = build_local_catalog((source,), workspace, policy=SuppliedRecordCatalogPolicy(namespace, "1"),
        catalog_id="urn:docspec:example:review-catalog", producer=source_producer, max_scratch_bytes=8 * 1024**2)
    rows = tuple(open_local_catalog(catalog.reference, workspace, producer=source_producer).iter_mappings())
    excluded = next(row["sourceItemId"] for row in rows if row["documentId"] == "out-of-scope")
    preview = [{"documentId": row["documentId"], "sourceItemId": row["sourceItemId"],
                "catalogSelection": row["selection"], "selectedInRun": row["sourceItemId"] != excluded} for row in rows]
    _write(output / "catalog-preview.json", {"reference": catalog.reference.to_dict(), "items": preview})
    input_root = workspace.roots["sourceContent"]
    input_root.mkdir()
    for name in ("privacy", "security"):
        (input_root / f"{name}.txt").write_bytes(payloads[name])
    resource_root = output / "reference-inputs"
    resource_root.mkdir()
    resources = {}
    for revision in ("v1", "v2"):
        raw = (INPUT_ROOT / f"vocabulary-{revision}.json").read_bytes()
        (resource_root / f"vocabulary-{revision}.json").write_bytes(raw)
        resources[revision] = (ProcessorResourceIdentity(
            "urn:docspec:example:review-vocabulary", ProcessorResourceKind.REFERENCE_DATA, revision, sha256_digest(raw),
        ), raw)
    retry = RetryPolicy(max_attempts=1, base_delay_milliseconds=0)
    selection = {"excludeItemIds": [excluded]}
    settings = {
        "limits": WorkLimits(4, 64 * 1024, 16, 16, 16, 1024 * 1024, 60, 1),
        "source_catalog_producer": source_producer, "document_release_producer": release_producer,
        "completed_at": COMPLETED_AT, "deadline_epoch_seconds": 4_000_000_000,
        "content_fetcher": LocalFileContentFetcher(input_root), "retry_policy": retry,
        "accepted_failure_policy": AcceptedFailurePolicy(accepted_classes=(FailureClass.TRANSIENT_EXTERNAL,)),
        "selection": selection,
    }

    def finish(prepared, name):
        run = prepared.run()
        release = prepared.retain(run)
        view = open_local_inspection(prepared.plan, workspace,
            document_release_producer=release_producer, source_catalog_producer=source_producer, release_ref=release)
        report = {"plan": prepared.plan.to_dict(), "run": run.to_dict(), "release": release.to_dict(),
                  "handoff": prepared.handoff_ref.to_dict(), "inspection": view.summary()}
        _write(output / f"{name}.json", report)
        return release, view, report

    with prepare_local_experiment(catalog.reference, workspace, stop_after="capture", **settings) as prepared:
        failed_base, failed_view, failed = finish(prepared, "initial-capture")
    assert failed["inspection"]["result"]["layers"]["files"] == 2
    assert failed["inspection"]["result"]["layers"]["failures"] == 1
    (input_root / "late-arrival.txt").write_bytes(payloads["late-arrival"])
    repair_settings = settings | {"selection": selection | {"retryFailures": "transient"}}
    with prepare_local_experiment(catalog.reference, workspace, stop_after="capture", base_release=failed_base,
                                  **repair_settings) as prepared:
        captured_base, _, repaired = finish(prepared, "repaired-capture")
    assert repaired["inspection"]["work"]["counts"]["newCapturedFiles"] == 1
    assert repaired["inspection"]["result"]["layers"]["files"] == 3
    assert repaired["inspection"]["result"]["layers"]["failures"] == 0
    assert len(tuple(failed_view.records("failures"))) == 1
    processor = PhraseMatchProcessor(*resources["v1"], retry_policy=retry)
    stages = {"extractor": TextExtractor(), "segmenter": ParagraphSegmenter()}
    processing_settings = settings | stages | {"processors": (processor,), "base_release": captured_base}
    with prepare_local_experiment(catalog.reference, workspace, **processing_settings) as prepared:
        processed_base, processed_view, processed = finish(prepared, "processed")
        handoff = prepared.handoff_ref
        run = prepared.run()
    with prepare_local_experiment(catalog.reference, workspace, handoff_ref=handoff, **processing_settings) as recovered:
        assert recovered.run() == run
    values = {"original": _phrase_values(processed_view, processor)}
    comparisons = {}
    base_prefix = {
        kind: tuple(row["payload"] for row in processed_view.records(kind))
        for kind in ("files", "representations", "segments")
    }
    for name, candidate in (
        ("case-sensitive", PhraseMatchProcessor(*resources["v1"], case_sensitive=True, retry_policy=retry)),
        ("resource-v2", PhraseMatchProcessor(*resources["v2"], retry_policy=retry)),
    ):
        with prepare_local_experiment(catalog.reference, workspace, **(settings | stages),
                                      processors=(candidate,), base_release=processed_base) as prepared:
            _, view, report = finish(prepared, name)
        assert all(tuple(row["payload"] for row in view.records(kind)) == records for kind, records in base_prefix.items())
        counts = report["inspection"]["work"]["counts"]
        assert counts["newCapturedFiles"] == counts["newRepresentations"] == counts["newSegments"] == 0
        values[name], comparisons[name] = _phrase_values(view, candidate), processed_view.compare(view)
    clean_workspace = LocalWorkspace(output / "clean-comparison", {
        "sourceCatalog": workspace.roots["sourceCatalog"], "sourceContent": input_root,
    })
    with prepare_local_experiment(catalog.reference, clean_workspace, **(settings | stages),
                                  processors=(candidate,)) as clean:
        clean_release = clean.retain(clean.run())
        clean_view = open_local_inspection(clean.plan, clean_workspace,
            document_release_producer=release_producer, release_ref=clean_release)
    assert _phrase_values(clean_view, candidate) == values["resource-v2"]
    _write(output / "matches.json", values)
    _write(output / "comparisons.json", comparisons)
    summary = {
        "verdict": "pass", "catalogItems": len(rows), "selectedDocuments": 3, "excludedDocuments": 1,
        "initialFailures": 1, "repairedFailures": 0, "originalFailureStillInspectable": True,
        "capturedDocuments": 3, "processedSegments": len(values["original"]),
        "matchCounts": {name: sum(len(value["matches"]) for value in result) for name, result in values.items()},
        "savedHandoffRecovered": True, "alternativesReuseUpstream": True, "cleanOutputValuesAgree": True,
        "limitations": "Synthetic literal-mention examples; no semantic classification or live-resource verification.",
    }
    _write(output / "experiment-summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new directory for retained experiment artifacts")
    arguments = parser.parse_args()
    try:
        summary = run_example(arguments.output)
    except FileExistsError:
        parser.error("--output must name a new directory")
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
