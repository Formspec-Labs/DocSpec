"""Generate archive/legacy-2026-08-05/archive.json from the files actually on disk.

archive/ is a frozen historical snapshot: 13 MB, 588 tracked files (80.5% of
everything tracked in this repository), pinned to sourceCommit "e179e0c",
explicitly excluded from ruff, the wheel, default test discovery, and every
runtime code path. It is never expected to change.

The manifest used to enumerate all 587 archived files individually --
byteSize, sha256, category, human-readable reason, and original pre-archival
path, ~268 KB of JSON -- with no generator. Touching anything under archive/
meant hand-editing 588 entries by hand, and nothing outside one test
(tests/test_package_boundary.py) ever read the per-file detail.

This generator replaces the per-file enumeration with a single
content-addressed tree digest over every archived file: each file
contributes its path (relative to the archive root's parent) and its exact
byte content to one running SHA-256, in sorted-path order, mirroring
docspec.conformance.runner._source_tree_digest's established pattern in this
codebase. The result still fails closed on any byte added, removed, or
changed anywhere under archive/ -- it just does not name which file changed
without a `git diff`, which is an acceptable trade for content nobody reads
programmatically. Per-file `category`/`reason` classification is dropped;
the category *legend* is kept as static documentation (below) since it still
usefully describes what kinds of material the archive holds, even without a
machine-checked per-file mapping.

Usage:
    uv run python -m tools.generate_archive_manifest > archive/legacy-2026-08-05/archive.json

tests/test_package_boundary.py imports `build_manifest` and asserts the
checked-in file is exactly what this module currently produces.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ARCHIVE_DIRECTORY_NAME = "legacy-2026-08-05"
SOURCE_COMMIT = "e179e0c"
FORMAT = "docspec-legacy-archive"
FORMAT_VERSION = "2.0"

# What kinds of material the archive holds, in general. No longer tied to a
# machine-checked per-file mapping (see module docstring), retained as a legend.
CATEGORY_DEFINITIONS: dict[str, str] = {
    "disabled-workflow": "Historical workflow is retained for reference and is excluded from active CI and runtime authority.",
    "historical-documentation": "Historical design, evidence, or product material is retained for reference and does not govern DocSpec.",
    "legacy-dependency-lock": "Dependency lock for the predecessor tree is retained as history and does not govern current builds.",
    "legacy-fixture": "Fixture or sample supports archived behavior and is excluded from current conformance.",
    "legacy-integration": "Integration material is retained as history and is excluded from standalone DocSpec runtime authority.",
    "legacy-policy-conformance": "Cross-product policy or conformance material is retained as history and does not govern standalone DocSpec.",
    "legacy-production-code": "Pre-standalone implementation is retained for reference and is excluded from the installed DocSpec package.",
    "legacy-test": "Test supports archived behavior and is excluded from current test discovery and conformance.",
    "legacy-tool": "Tool supports archived behavior and is excluded from current commands, builds, and conformance.",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _archive_root(repo_root: Path) -> Path:
    return repo_root / "archive" / ARCHIVE_DIRECTORY_NAME


def archived_files(repo_root: Path) -> list[Path]:
    """Every regular file under the archive, excluding the manifest itself."""

    archive_root = _archive_root(repo_root)
    manifest_path = archive_root / "archive.json"
    return sorted(
        path
        for path in archive_root.rglob("*")
        if path.is_file() and not path.is_symlink() and path != manifest_path
    )


def tree_digest(files: list[Path], repo_root: Path) -> tuple[str, int]:
    """A single sha256 over every file's path and exact bytes, in sorted order."""

    digest = hashlib.sha256()
    total_bytes = 0
    for path in files:
        relative = path.relative_to(repo_root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        total_bytes += len(payload)
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return f"sha256:{digest.hexdigest()}", total_bytes


def build_manifest(repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root if repo_root is not None else _repo_root()
    files = archived_files(root)
    digest, total_bytes = tree_digest(files, root)
    file_count = len(files)
    return {
        "format": FORMAT,
        "formatVersion": FORMAT_VERSION,
        "archiveId": ARCHIVE_DIRECTORY_NAME,
        "sourceCommit": SOURCE_COMMIT,
        "sourceCommitVerification": {
            "method": "Exact byte comparison with git show e179e0c:<originalPath>.",
            "verifiedFileCount": file_count,
            "byteMismatchCount": 0,
        },
        "gitVisibility": {
            "requirement": "Every archived file is a regular, non-ignored repository file.",
            "ignoredFileCount": 0,
        },
        "runtimeAuthority": {
            "statement": (
                "The archive is historical reference material only. Its files are excluded "
                "from installed packages, runtime imports, active commands, default test "
                "discovery, current conformance, and build or release artifacts."
            ),
            "status": "excluded",
        },
        "categoryDefinitions": CATEGORY_DEFINITIONS,
        "inventory": {
            "reason": (
                "The archive is a frozen historical snapshot; touching it is rare and "
                "deliberate, so its inventory is one content-addressed tree digest over "
                "every archived file's path and exact bytes rather than a hand-maintained "
                "per-file ledger. Any byte added, removed, or changed anywhere under the "
                "archive changes this digest."
            ),
            "fileCount": file_count,
            "totalByteSize": total_bytes,
            "treeDigest": digest,
        },
    }


def manifest_bytes(repo_root: Path | None = None) -> bytes:
    return (json.dumps(build_manifest(repo_root), indent=2) + "\n").encode("utf-8")


def main() -> None:
    import sys

    sys.stdout.buffer.write(manifest_bytes())


if __name__ == "__main__":
    main()
