"""Read selected scan pages, retain OCR evidence, and prove lifecycle reuse.

Run on macOS with SpicyDocs' PDF and Apple Vision extras installed:
    uv run --extra ocr python -m examples.scanned_document scan.pdf --workspace ./scan-state --pages 1 2

For a Docling model on Apple Silicon, use --extra ocr-vlm and select
--model ovisocr2, --model glm-ocr, or --model nuextract3.
"""

from __future__ import annotations

import argparse
import platform
from importlib.metadata import version
from pathlib import Path

from spicy_docs.extraction.api import DocumentExtractor, FullPage
from spicy_docs.extraction.ocr import AppleVision

from docspec.adapters.content_fetchers.local_file import LocalFileContentFetcher
from docspec.adapters.docling_ocr import DOCLING_OCR_MODELS, create_docling_ocr_extractor
from docspec.domain.content import CandidateFile, SourceItem
from docspec.processing.ocr import OcrExtractor
from docspec.processing.reader_identity import installed_reader_identity, reader_configuration
from docspec.runtime.core import CoreWorkspace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scan", type=Path)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--pages", type=int, nargs="+", required=True)
    parser.add_argument("--model", choices=("apple-vision", *DOCLING_OCR_MODELS), default="apple-vision")
    parser.add_argument("--artifacts", type=Path, help="Prepared Docling model cache directory")
    parser.add_argument("--local-files-only", action="store_true", help="Require already cached model weights")
    parser.add_argument("--max-tokens", type=int, default=16_384, help="Docling VLM output ceiling")
    args = parser.parse_args()
    path = args.scan.resolve()
    if args.model != "apple-vision":
        extractor = create_docling_ocr_extractor(
            args.model,
            artifacts_path=args.artifacts,
            pages=args.pages,
            max_tokens=args.max_tokens,
            local_files_only=args.local_files_only,
        )
    else:
        extractor = apple_vision_extractor(args.pages)
    with CoreWorkspace(args.workspace) as workspace:
        pipeline = workspace.documents(fetcher=LocalFileContentFetcher(path.parent), extractor=extractor)
        pipeline.import_sources(
            [SourceItem(path.name, "1", (CandidateFile("pdf", path.name, "application/pdf"),))], state_id="scan-catalog"
        )
        pipeline.run("scan-catalog", run_id="scan-read")
        pipeline.run("scan-catalog", run_id="scan-reuse")
        print(f"Retained OCR observations and searchable page text: {args.workspace.resolve()}")
        print("Read scan-read and scan-reuse selections to verify shared result identities.")


def apple_vision_extractor(pages):
    backend = AppleVision(recognition_level="accurate", languages=("en-US",))
    modules = (
        "spicy_docs.extraction.api",
        "spicy_docs.extraction.model",
        "spicy_docs.extraction.pages",
        "spicy_docs.extraction.ocr",
    )
    return OcrExtractor(
        DocumentExtractor(FullPage(backend)),
        pages=pages,
        processing_identity={
            "reader": reader_configuration(installed_reader_identity(modules)),
            "strategy": "FullPage",
            "backend": "apple-vision",
            "macos": platform.mac_ver()[0],
            "ocrmac": version("ocrmac"),
            "pymupdf": version("pymupdf"),
            "pillow": version("pillow"),
            "recognitionLevel": backend.level,
            "languages": backend.languages,
            "modelIdentity": "Apple Vision bundled with recorded macOS version",
        },
    )


if __name__ == "__main__":
    main()
