"""Replaceable extraction of one captured file into a representation."""

from __future__ import annotations

from typing import Protocol, TypeVar

from docspec.domain.content import CapturedFile

ExtractionResult_co = TypeVar("ExtractionResult_co", covariant=True)


class Extractor(Protocol[ExtractionResult_co]):
    """Extract one captured file into a representation under a pinned configuration."""

    @property
    def extractor_id(self) -> str: ...

    @property
    def configuration_digest(self) -> str: ...

    def selected_identity(self, captured_file: CapturedFile) -> tuple[str, str]:
        """Return the selected output extractor ID and configuration digest without reading bytes."""
        ...

    def extract(self, captured_file: CapturedFile, source_bytes: bytes) -> ExtractionResult_co: ...
