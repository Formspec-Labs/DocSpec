"""Locate and verify text bodies within shared member files."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from docspec.adapters.document_release.diagnostics import VerificationIssue, _issue, _validator_issues
from docspec.adapters.document_release.members import _read_slice
from docspec.adapters.document_release.rules import DOCSPEC_GENERATION, TEXT_BODY_INDEX_ROLE, TEXT_BODY_INDEX_ROW_DEF
from docspec.adapters.document_release.schemas import _row_subschema
from docspec.document_release_support import (
    load_strict_canonical_jsonl,
)


@dataclass(frozen=True, slots=True)
class TextBodyIndex:
    """The ``text-body-index`` member, read two ways over the same rows.

    ``by_body`` is amendment A4's own key -- ``(family, textBodyId)`` -- and is
    what a document body, an attachment's captured rendition, or a comment
    resolves through. ``by_digest`` is ``(family, member, sha256)`` and exists
    for the one case A4 did not have to answer: a capture that names bytes
    belonging to ANOTHER body's slice of a shared bucket, which is exactly what
    an attachment rendition that IS its owner's body rendition does (amendment
    B4). The index already records which byte range of which member digests to
    what, so a capture naming one of those digests is verified against that
    range rather than against the whole bucket. ``by_body`` keeps priority, so
    nothing about a document body's verification changes.
    """

    by_body: Mapping[tuple[Any, Any], Mapping[str, Any]]
    by_digest: Mapping[tuple[Any, Any, Any], Mapping[str, Any]]

    @classmethod
    def empty(cls) -> TextBodyIndex:
        return cls({}, {})


def _read_text_body_index(
    members: Sequence[Mapping[str, Any]],
    member_paths: Mapping[str, Path],
    generation: str,
    schemas: Mapping[str, Mapping[str, Any]],
    issues: list[VerificationIssue],
) -> TextBodyIndex:
    """Load the ``text-body-index`` member, keyed by ``(family, textBodyId)``.

    Amendment A4. Digest-bucketed ``text/`` and ``blobs/`` members hold whatever
    bodies hash into them, and no other field in this format carries an offset
    into a member, so without this index one body's bytes cannot be recovered
    from a bucket it shares. With it they can, and the refusal to mint a
    multi-body bucket lifts wherever the index covers that bucket -- which needs
    no separate rule here: an indexed body is checked against its slice, an
    unindexed body against the whole member, and a body sharing an unindexed
    bucket therefore fails its own capture or representation digest.

    An indexed slice whose bytes do not digest to the row's ``sha256`` is
    ``invalid.member-digest``, exactly as a whole member's would be.
    """

    if generation != DOCSPEC_GENERATION:
        return TextBodyIndex.empty()
    matching = [member for member in members if member.get("role") == TEXT_BODY_INDEX_ROLE]
    if not matching:
        return TextBodyIndex.empty()
    if len(matching) > 1:
        _issue(
            issues,
            "invalid.schema",
            "manifests/global.json/members",
            f"at most one {TEXT_BODY_INDEX_ROLE} member is allowed",
        )
        return TextBodyIndex.empty()
    member = matching[0]
    object_key = str(member.get("objectKey"))
    path = member_paths.get(object_key)
    if path is None or path.is_symlink() or not path.is_file():
        return TextBodyIndex.empty()
    try:
        rows = load_strict_canonical_jsonl(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        _issue(issues, "invalid.schema", object_key, str(exc))
        return TextBodyIndex.empty()
    if len(rows) != member.get("recordCount"):
        _issue(
            issues,
            "invalid.schema",
            f"member:{object_key}/recordCount",
            f"expected {len(rows)}",
        )
    row_schema = _row_subschema(schemas.get("member-manifest"), TEXT_BODY_INDEX_ROW_DEF)
    row_validator = (
        None if row_schema is None else jsonschema.Draft202012Validator(row_schema)
    )
    sizes: dict[Path, int] = {}
    index: dict[tuple[Any, Any], Mapping[str, Any]] = {}
    by_digest: dict[tuple[Any, Any, Any], Mapping[str, Any]] = {}
    for position, row in enumerate(rows):
        row_path = f"{object_key}/{position}"
        if row_validator is not None:
            issues.extend(_validator_issues(row_validator, row, path=row_path))
        if not isinstance(row, dict):
            continue
        identity = (row.get("family"), row.get("textBodyId"))
        if identity in index:
            _issue(
                issues,
                "invalid.duplicate-identity",
                f"{row_path}/textBodyId",
                f"duplicate {identity[0]!r} slice for text body {identity[1]!r}",
            )
            continue
        index[identity] = row
        by_digest.setdefault(
            (row.get("family"), row.get("member"), row.get("sha256")), row
        )
        target = member_paths.get(str(row.get("member")))
        if target is None or target.is_symlink() or not target.is_file():
            _issue(
                issues,
                "invalid.membership-missing",
                f"{row_path}/member",
                "indexed member is not a declared member of this bundle",
            )
            continue
        start, length = row.get("startByte"), row.get("byteLength")
        if not isinstance(start, int) or not isinstance(length, int):
            continue
        if isinstance(start, bool) or isinstance(length, bool):
            continue
        # Amendment B3: both ends of the range, not just the far one. A negative
        # offset used to reach `_read_slice` and raise an uncaught `OSError`.
        if start < 0 or length < 0:
            _issue(
                issues,
                "invalid.member-digest",
                f"{row_path}/startByte",
                f"slice [{start}, {start + length}) is not a non-negative byte range",
            )
            continue
        if target not in sizes:
            sizes[target] = target.stat().st_size
        member_size = sizes[target]
        if start + length > member_size:
            _issue(
                issues,
                "invalid.member-digest",
                f"{row_path}/byteLength",
                f"slice exceeds the {member_size}-byte member",
            )
            continue
        actual = hashlib.sha256(_read_slice(target, start, length)).hexdigest()
        if actual != row.get("sha256"):
            _issue(
                issues,
                "invalid.member-digest",
                f"{row_path}/sha256",
                f"indexed slice digests to {actual}",
            )
    return TextBodyIndex(index, by_digest)


def _indexed_bytes(
    path: Path,
    index: TextBodyIndex,
    family: str,
    body_id: Any,
    *,
    member: Any = None,
    digest: Any = None,
) -> bytes:
    """The bytes one capture or representation owns inside a member.

    A bucket holding one body indexes it at offset zero for its whole length, so
    the body-keyed answer and the whole file coincide and nothing changes for an
    unpartitioned reader. Where the body is not indexed and the caller supplied
    the member and digest it declared, the same index is read the other way
    (amendment B4): a capture naming an indexed slice's digest is checked
    against that slice rather than against the whole bucket.
    """

    row = index.by_body.get((family, body_id))
    if not isinstance(row, Mapping) and digest is not None:
        row = index.by_digest.get((family, member, digest))
    if not isinstance(row, Mapping):
        return path.read_bytes()
    start, length = row.get("startByte"), row.get("byteLength")
    if (
        isinstance(start, int)
        and isinstance(length, int)
        and not isinstance(start, bool)
        and not isinstance(length, bool)
        and 0 <= start <= start + length <= path.stat().st_size
    ):
        return _read_slice(path, start, length)
    return path.read_bytes()
