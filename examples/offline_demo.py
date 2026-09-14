"""Capture, repair, reuse, and compare local documents through Core.

Run: python -m examples.offline_demo --output /absolute/new-experiment
The example needs no network or optional provider.
"""

import argparse
import json
from contextlib import closing
from pathlib import Path

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.application.document_processors import segment_rows
from docspec.domain import core
from docspec.domain.content import CandidateFile, SourceItem, SourceItemState
from docspec.domain.identity import canonical_json_bytes, sha256_digest
from docspec.runtime import CoreWorkspace
from examples.dataset_example_support import document_results, output_value, phrase_processor, processor_values, run_documents, write_json

INPUT_ROOT = Path(__file__).with_name("offline")
DOCUMENTS = ("privacy", "security", "late-arrival", "out-of-scope")


def _sources(names):
    for index, name in enumerate(names):
        yield SourceItem(f"{index:02}:{name}", "1", (CandidateFile("text", name + ".txt", "text/plain"),),
                         state=SourceItemState.EXCLUDED if name == "out-of-scope" else SourceItemState.ACTIVE,
                         metadata={"documentId": name, "synthetic": True})


def _values(workspace, pipeline, state_id):
    """Validate every literal quote against its retained segment and source."""
    values = []
    for _, stages in document_results(workspace, pipeline, state_id):
        if not stages:
            continue
        source = output_value(workspace, stages[0], "content")
        segments_id = next(binding.entity_id for binding in stages[2].outcome.outputs if binding.label == "segments")
        with workspace.publisher.session() as session, closing(segment_rows(session, segments_id)) as segments:
            contents = {}
            for _, segment, reference in segments:
                with closing(workspace.blobs.read(reference, max_bytes=64 * 1024)) as chunks:
                    content = b"".join(chunks)
                evidence = segment.evidence
                assert source[evidence.start:evidence.end] == content
                contents[sha256_digest(content)] = (content, evidence.to_dict())
        for value in processor_values(workspace, pipeline, stages[-1]):
            content, evidence = contents[value["segmentDigest"]]
            assert evidence == value["enclosingSourceEvidence"]
            for match in value["matches"]:
                assert content[match["segmentByteStart"]:match["segmentByteEnd"]].decode() == match["quote"]
            values.append(value)
    return sorted(values, key=canonical_json_bytes)


def _failures(workspace):
    # This fixture has fewer than 100 attempts. General callers stream and filter
    # ledger batches rather than collecting a population for a report.
    failures = []
    for batch in workspace.ledger.retained_records():
        for row in batch:
            if isinstance(row.value, core.Execution):
                history = workspace.inspect("execution", row.value.execution_id)
                if history["progress"] and history["progress"][-1]["status"] == "failed":
                    failures.append(history)
    return failures


def run_example(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / "inputs"
    inputs.mkdir()
    for name in ("privacy", "security"):
        (inputs / f"{name}.txt").write_bytes((INPUT_ROOT / f"{name}.txt").read_bytes())
    values, comparisons = {}, {}
    def processor(revision, *, case_sensitive=False):
        return phrase_processor(revision, resource_id="urn:docspec:example:review-vocabulary",
            resource_bytes=(INPUT_ROOT / f"vocabulary-{revision}.json").read_bytes(), case_sensitive=case_sensitive)
    with CoreWorkspace(output / "workspace") as workspace:
        pipeline = workspace.documents(fetcher=LocalFileContentFetcher(inputs))
        pipeline.import_sources(_sources(DOCUMENTS), state_id="catalog")
        write_json(output / "catalog-preview.json", {"items": [{"documentId": name, "selectedInRun": name != "out-of-scope"} for name in DOCUMENTS]})
        try:
            pipeline.run("catalog", run_id="initial-capture", extract=False, segment=False)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("missing input unexpectedly captured")
        failures = _failures(workspace)
        assert len(failures) == 1
        write_json(output / "initial-failures.json", failures)
        (inputs / "late-arrival.txt").write_bytes((INPUT_ROOT / "late-arrival.txt").read_bytes())
        pipeline.run("catalog", run_id="repaired-capture", extract=False, segment=False)
        original = processor("v1")
        run_documents(workspace, pipeline, "catalog", name="processed", processors=(original,), output=output)
        values["original"] = _values(workspace, pipeline, "processed")
        original_results = dict(document_results(workspace, pipeline, "processed"))
    # A new process can recover the exact same selections without producers.
    with CoreWorkspace(output / "workspace") as workspace:
        pipeline = workspace.documents(fetcher=LocalFileContentFetcher(inputs))
        run_documents(workspace, pipeline, "catalog", name="processed", processors=(original,), output=output)
        assert dict(document_results(workspace, pipeline, "processed")) == original_results
        for name, alternative in (("case-sensitive", processor("v1", case_sensitive=True)), ("resource-v2", processor("v2"))):
            run_documents(workspace, pipeline, "catalog", name=name, processors=(alternative,), output=output)
            current = dict(document_results(workspace, pipeline, name))
            assert all(stages[:3] == original_results[key][:3] for key, stages in current.items())
            values[name] = _values(workspace, pipeline, name)
            comparisons[name] = workspace.compare("processed", name)
        names = (*DOCUMENTS, "added-note")
        (inputs / "added-note.txt").write_bytes((INPUT_ROOT / "added-note.txt").read_bytes())
        pipeline.import_sources(_sources(names), state_id="grown-catalog")
        run_documents(workspace, pipeline, "grown-catalog", name="grown", processors=(alternative,), output=output)
        values["grown"] = _values(workspace, pipeline, "grown")
        assert workspace.inspect("execution", failures[0]["key"][1])["progress"] == failures[0]["progress"]
    with CoreWorkspace(output / "clean-comparison") as workspace:
        pipeline = workspace.documents(fetcher=LocalFileContentFetcher(inputs))
        pipeline.import_sources(_sources(names), state_id="catalog")
        pipeline.run("catalog", run_id="clean", processors=(alternative,))
        assert _values(workspace, pipeline, "clean") == values["grown"]
    summary = {"verdict": "pass", "initialCatalogItems": len(DOCUMENTS), "catalogItems": len(names),
        "excludedDocuments": 1, "addedDocuments": 1, "initialFailures": len(failures), "repairedFailures": 0,
        "processedSegments": len(values["grown"]), "matchCounts": {name: sum(len(value["matches"]) for value in rows) for name, rows in values.items()},
        "exactSelectionsRecovered": True, "originalFailureStillInspectable": True,
        "alternativesReuseUpstream": True, "cleanOutputValuesAgree": True}
    write_json(output / "matches.json", values)
    write_json(output / "comparisons.json", comparisons)
    write_json(output / "experiment-summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        summary = run_example(arguments.output)
    except FileExistsError:
        parser.error("--output must name a new directory")
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
