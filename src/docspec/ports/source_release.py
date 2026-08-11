"""Admit one sealed source release by digest and stream its source items."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from docspec.domain.content import SourceItem
from docspec.domain.identity import require_relative_path, require_sha256
from docspec.domain.references import SourceCatalogRef
from docspec.ports.source_catalog import SourceCatalogSummary


@dataclass(frozen=True, slots=True)
class SourceReleasePin:
    """One checkout-free handle: where a sealed release root sits and which bytes it is."""

    root: str
    digest: str

    def __post_init__(self) -> None:
        require_relative_path(self.root, "source release root")
        require_sha256(self.digest, "source release digest")


@dataclass(frozen=True, slots=True)
class SourceReleaseAdmission:
    """The identity a sealed release proves for its pin before any item is read."""

    pin: SourceReleasePin
    reference: SourceCatalogRef
    summary: SourceCatalogSummary

    def __post_init__(self) -> None:
        if self.reference.locator != self.pin.root or self.reference.digest != self.pin.digest:
            raise ValueError("admitted source release differs from the pin it was read for")
        if self.summary.catalog_id != self.reference.catalog_id:
            raise ValueError("admitted source release identity differs from its reference")


@dataclass(slots=True)
class SourceReleaseRead:
    """One admitted sealed release and its single validating source-item stream."""

    admission: SourceReleaseAdmission
    items: Iterator[SourceItem]


class SourceReleaseReader(Protocol):
    """Open one sealed source release addressed only by its root locator and digest."""

    def admit(self, pin: SourceReleasePin) -> SourceReleaseAdmission: ...

    def open(self, pin: SourceReleasePin) -> SourceReleaseRead: ...
