"""Versioned corpus-state catalog boundary."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Protocol

from docspec.domain.plans import ProcessingPlan
from docspec.domain.release import DocumentRelease
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, StoreRef


class DocumentCatalogReader(Protocol):
    """A verified, immutable release view reusable for one application operation."""

    @property
    def release(self) -> DocumentRelease: ...

    def lookup(self, *, layer_kind: str, record_id: str) -> dict[str, Any] | None: ...

    def scan(self, *, layer_kind: str) -> Iterator[dict[str, Any]]: ...

    def scan_source(
        self,
        *,
        layer_kind: str,
        source_item_id: str,
    ) -> Iterator[dict[str, Any]]: ...


class DocumentCatalog(Protocol):
    """Open and conditionally advance complete release-described state."""

    def release_id(self, plan: ProcessingPlan, partition_policy: Mapping[str, object]) -> str: ...

    def open(self, reference: DocumentReleaseRef) -> DocumentRelease: ...

    def open_reader(self, reference: DocumentReleaseRef) -> DocumentCatalogReader: ...

    def current(self) -> DocumentReleaseRef | None: ...

    def lookup(self, reference: DocumentReleaseRef, *, layer_kind: str, record_id: str) -> dict[str, Any] | None: ...

    def scan(self, reference: DocumentReleaseRef, *, layer_kind: str) -> Iterator[dict[str, Any]]: ...

    def compare(
        self, older: DocumentReleaseRef, newer: DocumentReleaseRef, *, layer_kind: str
    ) -> Iterator[tuple[str, str]]: ...

    def stage(self, release: DocumentRelease) -> ArtifactRef: ...

    def commit(
        self,
        staged: ArtifactRef,
        *,
        expected_base: DocumentReleaseRef | None,
        stores: Iterable[StoreRef],
    ) -> DocumentReleaseRef: ...
