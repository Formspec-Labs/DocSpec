"""Retain markup or visible text while keeping exact source evidence.

Run: python -m examples.representation_choices --representation visible-text \
    --output /absolute/new-workspace
No network or optional parser is used.
"""

import argparse
import json
from contextlib import closing
from pathlib import Path

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.application.document_processors import segment_rows
from docspec.domain.content import CandidateFile, SourceItem
from docspec.processing import HtmlExtractor, ParagraphSegmenter
from docspec.processing.visible_text_runtime import VisibleTextBlockSegmenter, VisibleTextExtractor
from docspec.runtime import CoreWorkspace
from examples.dataset_example_support import document_results, output_value

INPUT_ROOT = Path(__file__).with_name("offline")


def run_example(output: Path, representation="visible-text"):
    if representation not in {"markup", "visible-text"}:
        raise ValueError("choose markup or visible-text")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    extractor = VisibleTextExtractor() if representation == "visible-text" else HtmlExtractor()
    segmenter = VisibleTextBlockSegmenter() if representation == "visible-text" else ParagraphSegmenter()
    with CoreWorkspace(output) as workspace:
        pipeline = workspace.documents(fetcher=LocalFileContentFetcher(INPUT_ROOT), extractor=extractor, segmenter=segmenter)
        pipeline.import_sources([SourceItem("notice", "1", (CandidateFile("body", "notice.html", "text/html"),))], state_id="catalog")
        pipeline.run("catalog", run_id="processed")
    with CoreWorkspace(output) as workspace:
        pipeline = workspace.documents(fetcher=LocalFileContentFetcher(INPUT_ROOT), extractor=extractor, segmenter=segmenter)
        _, stages = next(document_results(workspace, pipeline, "processed"))
        outputs = [{item.label: item.entity_id for item in stage.outcome.outputs} for stage in stages]
        captured = output_value(workspace, stages[0], "capture")
        derived = output_value(workspace, stages[1], "representation")["representation"]
        with workspace.publisher.session() as session, closing(segment_rows(session, outputs[2]["segments"])) as rows:
            segments = [{"representationStart": segment.representation_start, "representationEnd": segment.representation_end,
                         "evidence": segment.evidence.to_dict()} for _, segment, _ in rows]
        result = {"representationKind": derived["kind"], "capturedDigest": captured["blob"]["digest"],
                  "representationDigest": derived["blob"]["digest"], "capturedText": output_value(workspace, stages[0], "content").decode(),
                  "representationText": output_value(workspace, stages[1], "content").decode(), "segments": segments,
                  "counts": {"files": 1, "representations": 1, "segments": len(segments), "failures": 0}}
        (output / "representation-inspection.json").write_text(json.dumps(result, indent=2) + "\n")
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--representation", choices=("markup", "visible-text"), default="visible-text")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = run_example(arguments.output, arguments.representation)
    print(json.dumps({key: value for key, value in result.items() if key != "capturedText"}, indent=2))


if __name__ == "__main__":
    main()
