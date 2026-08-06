"""Provider-owned traversal of blob references named by profile state."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Protocol

from docspec.domain.references import ArtifactRef, BlobRef


class ProfileStateBlobReachability(Protocol):
    """Expose blob members, if any, behind one verified profile-state root."""

    def references(
        self,
        reference: ArtifactRef,
        state: Mapping[str, Any],
    ) -> Iterator[BlobRef]: ...


__all__ = ["ProfileStateBlobReachability"]
