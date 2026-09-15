"""Frozen DocSpec page policy before shared-reader adoption.

Copied from extraction.py at 560af84a11af801b95a65bbc2154c1d775b42eba.
Keep this independent of the shared provider; it is a parity oracle, not runtime code.
"""

from importlib import import_module
from importlib.metadata import version
from io import BytesIO

from docspec.processing.extraction import ExtractionError


class FrozenPypdfExtractor:
    def __init__(self, *, strip_page_whitespace: bool = False) -> None:
        self.strip_page_whitespace = strip_page_whitespace

    def _require_provider_version(self) -> str:
        return version("pypdf")

    def _read_pages(self, source_bytes: bytes) -> tuple[tuple[str, ...], str]:
        expected_version = self._require_provider_version()
        try:
            provider = import_module("pypdf")
        except (ImportError, ModuleNotFoundError) as error:
            raise ExtractionError("the pypdf extraction profile requires the docspec[pdf] extra") from error
        provider_version = getattr(provider, "__version__", None)
        if provider_version != expected_version:
            raise ExtractionError("loaded pypdf version differs from the configured distribution version")
        try:
            reader = provider.PdfReader(BytesIO(source_bytes), strict=False)
            if bool(getattr(reader, "is_encrypted", False)):
                raise ExtractionError("encrypted PDF requires an explicit decryption profile")
            extracted = tuple((page.extract_text() or "") for page in reader.pages)
        except ExtractionError:
            raise
        except Exception as error:  # optional provider failures are normalized at this boundary
            raise ExtractionError(f"pypdf cannot extract the captured PDF: {error}") from error
        if self.strip_page_whitespace:
            extracted = tuple(page.strip() for page in extracted)
        return extracted, provider_version

