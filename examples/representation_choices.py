"""Retain and inspect markup or visible text from one synthetic local document.

Run: uv run --frozen --extra dagster python -m examples.representation_choices \
    --representation visible-text --output /tmp/docspec-visible-text
The output directory must not exist. No network or optional parser is used.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from rulespec_artifacts import Producer

from docspec.domain.identity import sha256_digest
from docspec.domain.plans import WorkLimits
from docspec.domain.references import BlobRef
from docspec.processing import HtmlExtractor, ParagraphSegmenter
from docspec.processing.visible_text_runtime import VisibleTextBlockSegmenter, VisibleTextExtractor
from docspec.runtime import open_local_inspection, prepare_local_experiment
from docspec.source_catalog import (
    FederalRegisterCatalogPolicy,
    LocalSourceCatalogStore,
    SourceCatalogBuilder,
    SourceCatalogBuildRequest,
    SqliteCatalogPolicyWorkspace,
)
from docspec.workspace import LocalWorkspace
from examples.offline_demo import COMPLETED_AT, INPUT_ROOT, SOURCE_SYSTEM, ExampleFetcher, ExampleSource


def run_example(output: Path, representation: str = "visible-text") -> dict[str, Any]:
    """Build one bounded experiment and return its inspected bytes and coordinates."""
    if representation not in {"markup", "visible-text"}:
        raise ValueError("choose markup or visible-text")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = ExampleSource()
    implementation = "urn:docspec:example:representation-choices:" + sha256_digest(Path(__file__).read_bytes())
    source_producer = Producer(
        "docspec", implementation, "urn:docspec:verifier:source-catalog", "1.0.0", implementation,
    )
    release_producer = replace(source_producer, verifier_id="urn:docspec:verifier:document-release")
    workspace = LocalWorkspace(output, {"sourceContent": INPUT_ROOT})
    catalog = SourceCatalogBuilder(
        store=LocalSourceCatalogStore(workspace.roots["sourceCatalog"]),
        policy=FederalRegisterCatalogPolicy(SOURCE_SYSTEM),
        request=SourceCatalogBuildRequest("urn:docspec:example:representation-catalog", source_producer),
        workspace_factory=SqliteCatalogPolicyWorkspace,
    ).build((source,))
    extractor = VisibleTextExtractor() if representation == "visible-text" else HtmlExtractor()
    segmenter = VisibleTextBlockSegmenter() if representation == "visible-text" else ParagraphSegmenter()
    with prepare_local_experiment(
        catalog.reference, workspace,
        limits=WorkLimits(1, 1024 * 1024, 10, 10, 100, 1024 * 1024, 60, 1),
        source_catalog_producer=source_producer, document_release_producer=release_producer,
        deadline_epoch_seconds=4_000_000_000, completed_at=COMPLETED_AT,
        stop_after="segmentation",
        content_fetcher=ExampleFetcher(source), extractor=extractor, segmenter=segmenter,
    ) as prepared:
        release = prepared.retain(prepared.run())
        plan = prepared.plan

    view = open_local_inspection(
        plan, workspace, document_release_producer=release_producer, release_ref=release,
    )
    # This fixture has exactly one file and representation. Dataset callers
    # should stream these iterators instead of collecting a whole population.
    captured = tuple(view.records("files"))[0]["payload"]
    derived = tuple(view.records("representations"))[0]["payload"]
    capture_bytes = b"".join(view.read_blob(BlobRef.from_dict(captured["blob"]), max_bytes=1024 * 1024))
    text_bytes = b"".join(view.read_blob(BlobRef.from_dict(derived["blob"]), max_bytes=1024 * 1024))
    result = {
        "representationKind": derived["kind"],
        "capturedDigest": captured["blob"]["digest"],
        "representationDigest": derived["blob"]["digest"],
        "capturedText": capture_bytes.decode("utf-8"),
        "representationText": text_bytes.decode("utf-8"),
        "segments": [
            {
                "representationStart": row["payload"]["representationStart"],
                "representationEnd": row["payload"]["representationEnd"],
                "evidence": row["payload"]["evidence"],
            }
            for row in view.records("segments")
        ],
        "counts": view.summary()["result"]["layers"],
    }
    (output / "representation-inspection.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--representation", choices=("markup", "visible-text"), default="visible-text")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = run_example(arguments.output, arguments.representation)
    print(json.dumps({key: value for key, value in result.items() if key != "capturedText"}, indent=2))


if __name__ == "__main__":
    main()
