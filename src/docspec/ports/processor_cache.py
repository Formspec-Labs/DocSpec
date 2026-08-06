"""Disposable exact-result lookup behind a processor-neutral port."""

from __future__ import annotations

from typing import Protocol

from docspec.domain.references import ArtifactRef


class ProcessorResultCache(Protocol):
    def lookup(self, reuse_key: str) -> ArtifactRef | None: ...

    def put_if_absent(self, reuse_key: str, result: ArtifactRef) -> ArtifactRef: ...

    def discard(self, reuse_key: str, expected: ArtifactRef) -> bool: ...
