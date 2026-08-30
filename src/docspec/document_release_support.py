"""Byte, digest, and path primitives for the DocumentRelease 2.0 wire contract.

Moved under REF-048 -- DocumentRelease is DocSpec's record -- from the
`rulespec_conformance` package at source commit
c584a1d9fcb89fb8c4253b5bb6879741b0e24c1c, where the verifier imported them from
its `source_catalog_release.py` sibling so one implementation served both
release roots.

Nothing here restates a rule DocSpec already owns. The canonical JSON encoding,
the duplicate-key refusal, and the exact-bytes check delegate to
`docspec.domain.identity`; the containment test delegates to
`require_relative_path`. What is written out is only what DocSpec had no
equivalent for: the unqualified hexadecimal digest spelling this wire contract
uses, the streamed file digest, the bundle tree inventory, the set digest, the
JSON safe-integer bound, and the logical-content projection a content-derived
identity is taken over.

Failures raise `ValueError` rather than `IntegrityError`. The verifier reports a
diagnostic code for every refusal and must catch one exception type across
manifest bytes, member bytes, and row bytes alike; `IntegrityError` descends
from `DocSpecError`, not from `ValueError`, so translating here keeps the
verifier's `except` clauses exactly as they were written.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any

from docspec.domain.identity import (
    canonical_json_bytes as _identity_canonical_json_bytes,
)
from docspec.domain.identity import (
    parse_canonical_json,
    require_relative_path,
    thaw_json,
)
from docspec.errors import IntegrityError

# The largest integer a conforming JSON reader is guaranteed to carry without
# loss. A release whose counts or byte offsets exceed it cannot be exchanged.
MAX_SAFE_INTEGER = (1 << 53) - 1

# The shared member-manifest protocol both release roots write. These are wire
# facts, not DocSpec preferences, so they are stated once and compared, never
# derived.
MEMBER_MANIFEST_FORMAT = "spicy-artifact-member-manifest"
MEMBER_MANIFEST_VERSION = "1.0"
MEMBER_DESCRIPTOR_FIELDS = frozenset(
    {"objectKey", "role", "mediaType", "byteSize", "sha256", "recordCount", "schemaId"}
)
MANIFEST_REFERENCE_FIELDS = frozenset(
    {"manifestId", "scopeKind", "scopeId", "objectKey", "byteSize", "sha256"}
)
SUBORDINATE_MANIFEST_FIELDS = frozenset(
    {"format", "formatVersion", "manifestId", "scope", "members", "counts"}
)


def packaged_schema_root(format_version: str = "2.0") -> Path:
    """Locate the DocumentRelease schemas shipped inside the installed package.

    Resolved through `importlib.resources` rather than from `__file__` and a
    walk up to a repository root: the verifier must work from an installed
    wheel, where no source checkout exists.
    """

    root = resources.files("docspec") / "schemas" / "document_release" / format_version
    return Path(str(root))


def _require_json_safe_integers(value: Any, path: str = "$") -> None:
    """Refuse an integer no conforming JSON reader can carry back unchanged."""

    if isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValueError(f"{path} integer is outside the JSON safe range")
        return
    if isinstance(value, list):
        for index, member in enumerate(value):
            _require_json_safe_integers(member, f"{path}/{index}")
        return
    if isinstance(value, dict):
        for key, member in value.items():
            _require_json_safe_integers(member, f"{path}/{key}")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode one identity-bearing value as platform canonical JSON."""

    _require_json_safe_integers(value)
    try:
        return _identity_canonical_json_bytes(value)
    except IntegrityError as exc:  # pragma: no cover - defensive translation
        raise ValueError(str(exc)) from exc


def canonical_sha256(value: Any) -> str:
    """Return an unqualified digest over canonical JSON bytes."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def load_strict_canonical_json(path: Path) -> Any:
    """Load a manifest and reject noncanonical source bytes."""

    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("a UTF-8 byte order mark is forbidden")
    try:
        value = thaw_json(parse_canonical_json(raw, label=path.name, file_form=False))
    except IntegrityError as exc:
        raise ValueError(str(exc)) from exc
    _require_json_safe_integers(value)
    return value


def write_canonical_json(path: Path, value: Any) -> None:
    """Write canonical JSON without a trailing newline."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def load_strict_canonical_jsonl(path: Path) -> list[Any]:
    """Load one JSONL member: one canonical-JSON record per newline-terminated line.

    The docspec minting generation carries every tabular member as JSONL under
    DocSpec's own `docspec-record-layer/1.1` framing, so a consumer streams rows
    instead of parsing a whole file to reach the first one. The strictness is
    the same strictness `load_strict_canonical_json` applies, per line: exact
    canonical bytes in non-file form, no byte order mark, no blank line, and a
    final newline after the last record rather than a bare last line.
    """

    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("a UTF-8 byte order mark is forbidden")
    if raw and not raw.endswith(b"\n"):
        raise ValueError("every JSONL record must be terminated by a newline")
    rows: list[Any] = []
    for number, line in enumerate(raw.split(b"\n")[:-1], start=1):
        if not line:
            raise ValueError(f"{path.name} line {number} is empty")
        try:
            value = thaw_json(
                parse_canonical_json(line, label=f"{path.name}:{number}", file_form=False)
            )
        except IntegrityError as exc:
            raise ValueError(str(exc)) from exc
        _require_json_safe_integers(value)
        rows.append(value)
    return rows


def write_canonical_jsonl(path: Path, rows: Sequence[Any]) -> None:
    """Write one canonical-JSON record per line, each terminated by a newline."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(canonical_json_bytes(row) + b"\n" for row in rows))


def file_sha256(path: Path) -> str:
    """Digest a file's exact bytes without holding them in memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(bundle: Path) -> str:
    """Digest a materialized bundle's complete file inventory.

    Names a sealed fixture without naming a filesystem: the inventory carries
    relative object keys, sizes, and content digests only.
    """

    inventory: list[dict[str, Any]] = []
    for path in sorted(bundle.rglob("*")):
        relative = path.relative_to(bundle).as_posix()
        if path.is_symlink():
            inventory.append({"objectKey": relative, "symlinkTarget": str(path.readlink())})
        elif path.is_file():
            inventory.append(
                {
                    "objectKey": relative,
                    "byteSize": path.stat().st_size,
                    "sha256": file_sha256(path),
                }
            )
    return canonical_sha256(inventory)


def source_set_digest(source_item_ids: Sequence[str]) -> str:
    """Canonical set digest over a deduplicated, sorted identifier list.

    A SET digest, so a repeated identifier does not change it. Duplicates are a
    separate defect with a separate diagnostic (``invalid.duplicate-identity``);
    folding them into the digest would let one defect mask the other.
    """

    return "sha256:" + canonical_sha256(sorted(set(source_item_ids)))


# The physical and packing facts a docspec-generation identity preimage leaves
# out (`docs/decisions/0001-document-release-2-0.md`, "Identity -- two minted
# names, one derived form"). `coverage`'s representation and segment byte totals
# stay IN: they are facts about the corpus text, not about how it was packed.
# A physical-only repack therefore preserves `documentStateDigest`, which
# INCREMENTAL-EQUIVALENCE requires and a flat hash over the whole root breaks.
PHYSICAL_CONTENT_KEYS = frozenset({"globalManifest", "processingPolicies"})
PHYSICAL_COUNT_KEYS = frozenset({"memberCount", "totalMemberByteSize"})


def logical_content(content: Any) -> Any:
    """Project one release ``content`` object onto the half identity covers.

    A value that is not an object comes back unchanged, so a malformed root
    reaches the identity comparison as the mismatch it is rather than as a
    crash inside this projection.
    """

    if not isinstance(content, Mapping):
        return content
    projected: dict[str, Any] = {
        key: value for key, value in content.items() if key not in PHYSICAL_CONTENT_KEYS
    }
    counts = projected.get("counts")
    if isinstance(counts, Mapping):
        projected["counts"] = {
            key: value for key, value in counts.items() if key not in PHYSICAL_COUNT_KEYS
        }
    return projected


def safe_object_key(value: object) -> bool:
    """Report whether one member key names a file inside its own bundle.

    `require_relative_path` already refuses absolute, empty, and traversing
    keys. The three refusals added here are portability rules a POSIX path
    parser cannot see: an embedded NUL, a Windows separator, and a drive
    letter would each resolve differently on some reader's filesystem.
    """

    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        return False
    if len(value) > 1 and value[0].isascii() and value[0].isalpha() and value[1] == ":":
        return False
    try:
        return require_relative_path(value, "objectKey") == value
    except ValueError:
        return False


def member_path(bundle: Path, object_key: str) -> Path:
    """Resolve one member key against its bundle root."""

    return bundle.joinpath(*PurePosixPath(object_key).parts)


__all__ = [
    "MANIFEST_REFERENCE_FIELDS",
    "MAX_SAFE_INTEGER",
    "MEMBER_DESCRIPTOR_FIELDS",
    "MEMBER_MANIFEST_FORMAT",
    "MEMBER_MANIFEST_VERSION",
    "PHYSICAL_CONTENT_KEYS",
    "PHYSICAL_COUNT_KEYS",
    "SUBORDINATE_MANIFEST_FIELDS",
    "canonical_json_bytes",
    "canonical_sha256",
    "file_sha256",
    "load_strict_canonical_json",
    "load_strict_canonical_jsonl",
    "logical_content",
    "member_path",
    "packaged_schema_root",
    "safe_object_key",
    "source_set_digest",
    "tree_digest",
    "write_canonical_json",
    "write_canonical_jsonl",
]
