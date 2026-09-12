"""Persistence for small, immutable control-plane JSON artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from docspec.domain.references import ArtifactRef


class ControlRepository(Protocol):
    """Persist control values and admit their exact immutable references on read."""

    def put(self, *, kind: str, artifact_id: str, value: Mapping[str, Any]) -> ArtifactRef: ...

    def load(self, reference: ArtifactRef) -> dict[str, Any]:
        """Verify bounded bytes, the pin, canonical encoding and control identity/shape.

        Return the control value only after admission. Callers still validate
        its domain-specific type, semantic identity and links to other values.
        A preceding ``verify`` is unnecessary when the value will be loaded.
        """
        ...

    def verify(self, reference: ArtifactRef) -> None:
        """Perform the same admission as ``load`` without returning the value."""
        ...
