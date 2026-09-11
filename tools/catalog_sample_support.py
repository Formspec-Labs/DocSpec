"""Read retained catalog sampling frames from a detached, flat blob store.

These helpers preserve the research tools' input order and decoding. They do
not admit a catalog for publication; that belongs to the package's verifier.
Sampling predicates and receipt fields stay with the tool asking the question.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def source_item_member_paths(catalog_root: Path, blob_store: Path) -> list[str]:
    """Locate source-item blobs in the manifest's stable blob-reference order."""
    manifest = json.loads((catalog_root / "manifests" / "catalog.json").read_text())
    members = sorted(
        (member for member in manifest["members"] if member["role"] == "source-items"),
        key=lambda member: member["blobRef"],
    )
    return [str(blob_store / member["blobRef"].split(":", 1)[1]) for member in members]


def iter_source_item_rows(member_path: str) -> Iterator[dict[str, Any]]:
    """Decode one plain or gzip JSONL blob, preserving values and row order."""
    raw = Path(member_path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    for line in raw.splitlines():
        if line.strip():
            yield json.loads(line)


def report_catalog_root(catalog_root: Path) -> str:
    """Use the existing home-relative receipt spelling when it applies."""
    try:
        return "~/" + str(catalog_root.relative_to(Path.home()))
    except ValueError:
        return str(catalog_root)
