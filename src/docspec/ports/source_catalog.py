"""Read fixed source-catalog distributions through DocSpec-owned records."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from docspec.domain.content import SourceItem
from docspec.domain.identity import freeze_json, require_text, thaw_json
from docspec.domain.references import SourceCatalogRef


@dataclass(frozen=True, slots=True)
class SourceCatalogSummary:
    catalog_id: str
    kind: str
    item_count: int
    partitions: tuple[str, ...]
    state_counts: Mapping[str, int]
    coverage: Mapping[str, Any]
    base_catalog: SourceCatalogRef | None = None

    def __post_init__(self) -> None:
        require_text(self.catalog_id, "catalog_id")
        if self.kind not in {"snapshot", "change-set"}:
            raise ValueError("source catalog kind must be snapshot or change-set")
        if self.item_count < 0 or any(count < 0 for count in self.state_counts.values()):
            raise ValueError("source catalog counts must be non-negative")
        object.__setattr__(self, "coverage", thaw_json(freeze_json(self.coverage, label="catalog coverage")))


@dataclass(slots=True)
class SourceCatalogRead:
    """One verified distribution root and its single validating item stream."""

    summary: SourceCatalogSummary
    items: Iterator[SourceItem]


class SourceCatalog(Protocol):
    """Verify and stream one immutable catalog snapshot or change set."""

    def open(self, reference: SourceCatalogRef) -> SourceCatalogRead: ...

    def verify(self, reference: SourceCatalogRef) -> SourceCatalogSummary: ...

    def describe(self, reference: SourceCatalogRef) -> SourceCatalogSummary: ...

    def stream(self, reference: SourceCatalogRef) -> Iterator[SourceItem]: ...


SourceCatalogReader = SourceCatalog
