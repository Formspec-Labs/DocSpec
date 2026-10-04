# Retain scanned document reads

`OcrExtractor` adapts SpicyDocs' `DocumentExtractor` to the existing document
pipeline. Supply the reader and page strategy directly, plus an explicit
`processing_identity` describing resolved settings and model/package versions.
For arbitrary injected objects, this identity is a caller attestation. Pin remote
model artifacts where their names do not identify immutable versions.

The extraction stage retains searchable page text, an `ocr-observations` JSON
blob containing page metadata and provider observations, and separate blobs for
provider/raster bytes. Ordinary JSON evidence preserves measured float values;
Core receipts keep their canonical identity rules. The receipt records each
evidence blob's digest and size. Source citations identify inspected pages; they
do not imply measured text boxes.
Text boxes remain available in the retained observations when a provider measures
them. `evidence_resolver(result)` checks text against retained observations without
another recognition call; this proves consistency with retained readings, not
that the recognized words match the source image.

A result becomes complete only after iteration ends and every requested page
appears in the requested order. Failures retain an `ocr-failure` blob with
completed page observations and supported provider diagnostics on the existing
failed execution; they never publish a successful text representation. A retry
can reuse its captured source. Successful repeated runs reuse the same selected
result when the processing identity and inputs match.

See `examples/scanned_document.py` for an explicitly bounded Apple Vision example
on macOS and `tests/test_ocr_extraction.py` for failure, truncation, retention and
reopen/reuse checks. The example requires the SpicyDocs PDF and Apple Vision
optional dependencies in the running environment.

## Choose a Docling model

Install the optional local vision-language model dependencies on Apple Silicon
macOS with `uv sync --extra ocr-vlm`. These models read each rendered page through
Docling and replace the separate OCR, layout and table stages for that run.
Docling's RapidOCR dependency pins an ANTLR runtime that Dagster cannot use, so
`ocr-vlm` and `dagster` are declared conflicting extras and need separate
environments.

| Model option | Pinned local model | Output |
| --- | --- | --- |
| `ovisocr2` | `mlx-community/OvisOCR2-8bit` | Content and tables exported as Markdown |
| `glm-ocr` | `mlx-community/GLM-OCR-bf16` | Content exported as Markdown |
| `nuextract3` | `numind/NuExtract3-mlx-4bits` | Content and tables exported as Markdown |

```python
from docspec.adapters import create_docling_ocr_extractor

extractor = create_docling_ocr_extractor(
    "ovisocr2",  # or "glm-ocr", "nuextract3"
    pages=(1, 2),
    local_files_only=True,
)
pipeline = workspace.documents(fetcher=fetcher, extractor=extractor)
```

The factory prepares the selected pinned revision and hashes its model files.
By default it downloads missing files into the Hugging Face cache; use
`local_files_only=True` to require cached weights. Its prepared Docling directory
defaults to `~/.cache/docspec/docling-mlx`; set `artifacts_path` to choose another
directory. Ovis and NuExtract use the tested processor-class compatibility
adjustment in a separate prepared copy. The original Hugging Face files remain
unchanged. An existing prepared directory with different bytes is refused.

The processing identity includes installed reader hashes, renderer versions and
DPI, plus a digest of the resolved provider configuration and prepared model
files. Selecting a different model or changing generation settings therefore
creates a different extraction identity. Full settings and provider responses
remain in the ordinary OCR evidence blobs. These routes default to 300 DPI,
a 16,384-token ceiling and a 120-second Docling document timeout; the latter is
not a hard process-kill deadline. Callers may set `dpi`, `max_tokens` and
`document_timeout`. Detected incomplete or refused provider results fail through
the existing OCR failure-retention path. An unspecified generation stop below
the token ceiling does not prove completion; its metadata remains in observations.

The bounded example exposes the same choices:

```sh
uv run --extra ocr-vlm python -m examples.scanned_document scan.pdf \
  --workspace ./scan-state --pages 1 2 --model ovisocr2 --local-files-only
```

Without `--model`, the example still selects its existing direct Apple Vision
reader. Ordinary DocSpec extraction defaults also remain unchanged. NuExtract's
experimental structured-template JSON mode is not included: the tested content
mode uses the same Docling retention path as the other choices.
