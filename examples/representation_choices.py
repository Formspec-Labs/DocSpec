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

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.domain.identity import sha256_digest
from docspec.domain.plans import WorkLimits
from docspec.domain.references import BlobRef
from docspec.processing import HtmlExtractor, ParagraphSegmenter
from docspec.processing.visible_text_runtime import VisibleTextBlockSegmenter, VisibleTextExtractor
from docspec.runtime import build_local_catalog, open_local_inspection, prepare_local_experiment
from docspec.source_catalog import (
    SourceCatalogCandidate, SuppliedRecordCatalogPolicy, SuppliedRecordSource,
)
from docspec.workspace import LocalWorkspace
COMPLETED_AT = "2026-09-11T12:00:00Z"
INPUT_ROOT = Path(__file__).with_name("offline")


def run_example(output: Path, representation: str = "visible-text") -> dict[str, Any]:
    """Build one bounded experiment and return its inspected bytes and coordinates."""
    if representation not in {"markup", "visible-text"}:
        raise ValueError("choose markup or visible-text")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    payload = (INPUT_ROOT / "notice.html").read_bytes()
    namespace = "urn:docspec:example:representation-source"
    source = SuppliedRecordSource(({
        "recordId": "notice", "sourceIssuedVersion": "fixture1", "title": "Local contributor example",
        "metadata": {"synthetic": True}, "candidateRenditions": [SourceCatalogCandidate(
            "body", "text/html", "immutable-object", "notice.html",
            expected_sha256=sha256_digest(payload), expected_byte_size=len(payload),
        ).to_dict()],
    },), source_system_id=namespace, source_system_version="1", source_state_scope="complete-snapshot",
        max_records=1, max_bytes=4096)
    implementation = "urn:docspec:example:representation-choices:" + sha256_digest(Path(__file__).read_bytes())
    source_producer = Producer(
        "docspec", implementation, "urn:docspec:verifier:source-catalog", "1.0.0", implementation,
    )
    release_producer = replace(source_producer, verifier_id="urn:docspec:verifier:document-release")
    workspace = LocalWorkspace(output, {"sourceContent": INPUT_ROOT})
    catalog = build_local_catalog((source,), workspace, policy=SuppliedRecordCatalogPolicy(namespace, "1"),
        catalog_id="urn:docspec:example:representation-catalog", producer=source_producer, max_scratch_bytes=8 * 1024**2)
    extractor = VisibleTextExtractor() if representation == "visible-text" else HtmlExtractor()
    segmenter = VisibleTextBlockSegmenter() if representation == "visible-text" else ParagraphSegmenter()
    with prepare_local_experiment(
        catalog.reference, workspace,
        limits=WorkLimits(1, 1024 * 1024, 10, 10, 100, 1024 * 1024, 60, 1),
        source_catalog_producer=source_producer, document_release_producer=release_producer,
        deadline_epoch_seconds=4_000_000_000, completed_at=COMPLETED_AT,
        stop_after="segmentation",
        content_fetcher=LocalFileContentFetcher(INPUT_ROOT), extractor=extractor, segmenter=segmenter,
    ) as prepared:
        release = prepared.retain(prepared.run())
        plan = prepared.plan

    with open_local_inspection(
        plan, workspace, document_release_producer=release_producer, release_ref=release,
    ) as view:
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
