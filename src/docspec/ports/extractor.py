"""Replaceable representation extraction."""

from __future__ import annotations

from typing import Protocol, TypeVar

from docspec.domain.content import CapturedFile

ExtractionResult_co = TypeVar("ExtractionResult_co", covariant=True)


class Extractor(Protocol[ExtractionResult_co]):
    def extract(self, captured_file: CapturedFile, source_bytes: bytes) -> ExtractionResult_co: ...
