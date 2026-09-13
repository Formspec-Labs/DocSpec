"""Local storage locations shared by dataset commands and Python callers.

A workspace chooses locations, not a new dataset identity or run ledger. Plans,
catalog references, and retained store revisions keep their existing identities.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from docspec.profile_registry import BUILTIN_PROFILE_DIRECTORY

STORAGE_ROOT_NAMES = frozenset({
    "blobStorage", "controlRepository", "documentCatalog", "documentStores",
    "reconciliation", "recordStorage", "sourceCatalog", "sourceContent",
})


def _absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError(f"{label} must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return path


@dataclass(frozen=True)
class LocalWorkspace:
    """Derive local storage roots without creating directories or selecting inputs.

    Overrides support independently located catalogs, source bytes, or storage.
    Profile limits and descriptions are still checked when the plan is opened.
    Producer and verifier acceptance remains an explicit caller choice.
    """

    root: Path
    overrides: Mapping[str, Path] = field(default_factory=dict)
    profile_directory: Path = BUILTIN_PROFILE_DIRECTORY

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", _absolute_path(self.root, "workspace"))
        object.__setattr__(self, "profile_directory", _absolute_path(self.profile_directory, "profile directory"))
        if not isinstance(self.overrides, Mapping) or not set(self.overrides) <= STORAGE_ROOT_NAMES:
            raise ValueError("workspace root overrides contain unknown storage names")
        object.__setattr__(self, "overrides", MappingProxyType({
            name: _absolute_path(path, f"workspace {name} root")
            for name, path in self.overrides.items()
        }))

    @property
    def roots(self) -> dict[str, Path]:
        return {name: self.overrides.get(name, self.root / name) for name in sorted(STORAGE_ROOT_NAMES)}
