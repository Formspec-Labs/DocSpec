"""Persistence for small, immutable control-plane JSON artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from docspec.domain.references import ArtifactRef


class ControlRepository(Protocol):
    def put(self, *, kind: str, artifact_id: str, value: Mapping[str, Any]) -> ArtifactRef: ...

    def load(self, reference: ArtifactRef) -> dict[str, Any]: ...

    def verify(self, reference: ArtifactRef) -> None: ...
