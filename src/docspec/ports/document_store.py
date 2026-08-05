"""Storage boundary for exact source files and DocSpec-derived artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class DocumentStore(Protocol):
    """Fetch and publish files without exposing a storage provider to DocSpec."""

    def fetch(self, object_key: str, local_path: Path) -> bool: ...

    def publish(self, local_path: Path, *, object_key: str, **options: Any) -> None: ...
