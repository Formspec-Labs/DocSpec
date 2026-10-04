"""Select pinned Docling OCR models for the existing retained document pipeline."""

from __future__ import annotations

from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path

from docspec.domain.identity import sha256_digest
from docspec.processing.ocr import OcrExtractor, _observation_bytes
from docspec.processing.reader_identity import installed_reader_identity, reader_configuration, require_reader_identity

DOCLING_OCR_MODELS = ("ovisocr2", "glm-ocr", "nuextract3")
_READER_MODULES = (
    "spicy_docs.extraction.api",
    "spicy_docs.extraction.model",
    "spicy_docs.extraction.pages",
    "spicy_docs.extraction.docling",
    "spicy_docs.extraction.docling_assets",
)


def create_docling_ocr_extractor(
    model: str,
    *,
    artifacts_path: str | Path | None = None,
    pages: Sequence[int] | None = None,
    dpi: int = 300,
    max_tokens: int = 16_384,
    document_timeout: float | None = 120,
    local_files_only: bool = False,
) -> OcrExtractor:
    """Prepare one local MLX model and bind its identity to retained OCR results.

    Requires the ``ocr-vlm`` extra and Apple Silicon macOS. Preparation downloads
    pinned weights when needed unless ``local_files_only`` is true. NuExtract3
    uses content transcription; its experimental structured JSON mode is not
    selected here. No existing default extractor changes.
    """
    if model not in DOCLING_OCR_MODELS:
        raise ValueError(f"choose a Docling OCR model from {DOCLING_OCR_MODELS}")

    from spicy_docs.extraction import DefaultReader, Docling, DoclingVlmSettings, DocumentExtractor, FullPage

    root = Path(artifacts_path) if artifacts_path is not None else Path.home() / ".cache" / "docspec" / "docling-mlx"
    settings = DoclingVlmSettings(
        model=model,
        max_tokens=max_tokens,
        image_scale=dpi / 72,
        document_timeout=document_timeout,
        artifacts_path=str(root.resolve()),
    )
    settings.check_platform()
    reader = DefaultReader(dpi=dpi)
    reader_identity = installed_reader_identity(_READER_MODULES)
    require_reader_identity(reader_identity, _READER_MODULES)
    renderer = {"dpi": dpi, "pymupdf": version("pymupdf"), "pillow": version("pillow")}
    model_identity = settings.prepare_artifacts(local_files_only=local_files_only)
    backend = Docling(settings=settings, model_identity=model_identity)
    configuration = backend.require_reproducible_identity()
    return OcrExtractor(
        DocumentExtractor(FullPage(backend), reader=reader),
        pages=pages,
        processing_identity={
            "reader": reader_configuration(reader_identity),
            "strategy": "FullPage",
            "model": model,
            "renderer": renderer,
            # Provider options contain measured floats. Hash the same ordinary
            # JSON encoding retained in OCR evidence, keeping Core identities exact.
            "providerConfigurationDigest": sha256_digest(_observation_bytes(configuration)),
        },
    )
