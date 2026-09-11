"""Read and verify the closed inventory of release member files."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import jsonschema

from docspec.adapters.document_release.diagnostics import VerificationIssue, _issue, _schema_issues, _validator_issues
from docspec.adapters.document_release.rules import (
    ALLOWED_MEMBER_ROLES,
    FORMAT,
    FORMAT_VERSION,
    MEMBER_MANIFEST_SCHEMA,
    OPAQUE_ROLES,
    REPRESENTATION_MEDIA_TYPE,
    SCHEMA_IDS,
    TABULAR_MEDIA_TYPE,
    TABULAR_ROLES,
    TEXT_BODY_INDEX_ROLE,
    expected_document_state_digest,
    expected_release_id,
)
from docspec.adapters.document_release.schemas import _load_schema
from docspec.document_release_support import (
    MANIFEST_REFERENCE_FIELDS,
    MEMBER_DESCRIPTOR_FIELDS,
    MEMBER_MANIFEST_FORMAT,
    MEMBER_MANIFEST_VERSION,
    SUBORDINATE_MANIFEST_FIELDS,
    file_sha256,
    load_strict_canonical_json,
    load_strict_canonical_jsonl,
    member_path,
    safe_object_key,
)


def _read_slice(path: Path, start: int, length: int) -> bytes:
    """Read exactly one member's byte slice without holding the member.

    The bounds are refused before the seek, not after it (amendment B3). A
    negative offset reaches `seek` as an `OSError` that no caller catches, so an
    untrusted bundle could take the gate down instead of being refused by it;
    the caller's own guard checked only the overflow half.
    """

    if isinstance(start, bool) or isinstance(length, bool):
        raise ValueError("a member slice offset must be an integer")
    if not isinstance(start, int) or not isinstance(length, int):
        raise ValueError("a member slice offset must be an integer")
    if start < 0 or length < 0:
        raise ValueError(f"a member slice offset must be non-negative, not [{start}, {length})")
    with path.open("rb") as handle:
        handle.seek(start)
        return handle.read(length)


def _read_root(bundle: Path, issues: list[VerificationIssue]) -> dict[str, Any] | None:
    root_path = bundle / "release.json"
    if root_path.is_symlink():
        _issue(issues, "invalid.path", "release.json", "root manifest is a symlink")
        return None
    if not root_path.is_file():
        _issue(issues, "invalid.membership-missing", "release.json", "root manifest is absent")
        return None
    try:
        root = load_strict_canonical_json(root_path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        _issue(issues, "invalid.root-syntax", "release.json", str(exc))
        return None
    if not isinstance(root, dict):
        _issue(issues, "invalid.root-syntax", "release.json", "root must be an object")
        return None
    if root.get("format") != FORMAT or root.get("formatVersion") != FORMAT_VERSION:
        _issue(
            issues,
            "invalid.format",
            "release.json",
            f"expected {FORMAT!r} version {FORMAT_VERSION!r}",
        )
    # Two names over one content: the state digest is the minted one, the
    # release id is derived from its hex. Both are checked, because a root
    # that carries a correct id beside a wrong state digest is a root whose
    # two names disagree about the same corpus.
    try:
        expected_state = expected_document_state_digest(root)
    except (TypeError, ValueError) as exc:
        _issue(issues, "invalid.identity", "release.json", str(exc))
    else:
        if root.get("documentStateDigest") != expected_state:
            _issue(
                issues,
                "invalid.identity",
                "release.json/documentStateDigest",
                f"expected {expected_state}",
            )
    try:
        expected = expected_release_id(root)
    except (TypeError, ValueError) as exc:
        _issue(issues, "invalid.identity", "release.json", str(exc))
    else:
        if root.get("releaseId") != expected:
            _issue(issues, "invalid.identity", "release.json/releaseId", f"expected {expected}")
    return root


def _materialized_files(bundle: Path, issues: list[VerificationIssue]) -> set[str]:
    result: set[str] = set()
    for path in bundle.rglob("*"):
        relative = path.relative_to(bundle).as_posix()
        if path.is_symlink():
            _issue(issues, "invalid.path", relative, "symlinks are forbidden")
            result.add(relative)
            continue
        if path.is_file():
            result.add(relative)
    return result


# Schema members are single documents and declare null; row streams and
# partitioned text/blob members declare their record count (restamp item 16).
COUNTED_MEMBER_ROLES = frozenset({*TABULAR_ROLES, *OPAQUE_ROLES, TEXT_BODY_INDEX_ROLE})


def _validate_member_descriptor(
    member: Any, *, path: str, issues: list[VerificationIssue]
) -> dict[str, Any] | None:
    if not isinstance(member, dict):
        _issue(issues, "invalid.schema", path, "member descriptor must be an object")
        return None
    if set(member) != MEMBER_DESCRIPTOR_FIELDS:
        _issue(issues, "invalid.schema", path, "member descriptor has an unknown or missing field")
    if not safe_object_key(member.get("objectKey")):
        _issue(issues, "invalid.path", f"{path}/objectKey", "unsafe member path")
    role = member.get("role")
    if role not in ALLOWED_MEMBER_ROLES:
        _issue(
            issues,
            "invalid.schema",
            f"{path}/role",
            f"unknown role {role!r}",
        )
    if role == "schema" and member.get("mediaType") != "application/schema+json":
        _issue(issues, "invalid.schema", f"{path}/mediaType", "expected application/schema+json")
    if role in TABULAR_ROLES and member.get("mediaType") != TABULAR_MEDIA_TYPE:
        _issue(issues, "invalid.schema", f"{path}/mediaType", f"expected {TABULAR_MEDIA_TYPE}")
    if role == TEXT_BODY_INDEX_ROLE:
        if member.get("mediaType") != TABULAR_MEDIA_TYPE:
            _issue(issues, "invalid.schema", f"{path}/mediaType", f"expected {TABULAR_MEDIA_TYPE}")
        if member.get("schemaId") != SCHEMA_IDS["member-manifest"]:
            _issue(
                issues,
                "invalid.schema",
                f"{path}/schemaId",
                f"expected {SCHEMA_IDS['member-manifest']}",
            )
    if role == "representation" and member.get("mediaType") != REPRESENTATION_MEDIA_TYPE:
        _issue(
            issues,
            "invalid.schema",
            f"{path}/mediaType",
            f"expected {REPRESENTATION_MEDIA_TYPE}",
        )
    if role in TABULAR_ROLES and member.get("schemaId") != SCHEMA_IDS[
        TABULAR_ROLES[role]
    ]:
        _issue(
            issues,
            "invalid.schema",
            f"{path}/schemaId",
            f"expected {SCHEMA_IDS[TABULAR_ROLES[role]]}",
        )
    record_count = member.get("recordCount")
    if role in COUNTED_MEMBER_ROLES:
        if not isinstance(record_count, int) or isinstance(record_count, bool):
            _issue(issues, "invalid.schema", f"{path}/recordCount", "invalid record count")
    elif record_count is not None:
        _issue(
            issues,
            "invalid.schema",
            f"{path}/recordCount",
            f"a {role!r} member declares no row count in this generation and must declare null",
        )
    return member


def _read_member_manifest(
    bundle: Path,
    root: Mapping[str, Any],
    issues: list[VerificationIssue],
) -> tuple[list[dict[str, Any]], dict[str, Path], set[str]]:
    declared = {"release.json"}
    content = root.get("content")
    if not isinstance(content, dict):
        return [], {}, declared
    reference = content.get("globalManifest")
    if not isinstance(reference, dict):
        _issue(
            issues,
            "invalid.schema",
            "release.json/content/globalManifest",
            "manifest reference must be an object",
        )
        return [], {}, declared
    if set(reference) != MANIFEST_REFERENCE_FIELDS:
        _issue(
            issues,
            "invalid.schema",
            "release.json/content/globalManifest",
            "manifest reference has an unknown or missing field",
        )
    object_key = reference.get("objectKey")
    if not safe_object_key(object_key):
        _issue(
            issues,
            "invalid.path",
            "release.json/content/globalManifest/objectKey",
            "unsafe member path",
        )
        return [], {}, declared
    declared.add(object_key)
    path = member_path(bundle, object_key)
    if path.is_symlink():
        _issue(issues, "invalid.path", object_key, "manifest is a symlink")
        return [], {}, declared
    if not path.is_file():
        _issue(issues, "invalid.membership-missing", object_key, "manifest is absent")
        return [], {}, declared
    if path.stat().st_size != reference.get("byteSize") or file_sha256(path) != reference.get(
        "sha256"
    ):
        _issue(
            issues,
            "invalid.member-digest",
            object_key,
            "manifest size or digest differs from the root reference",
        )
    try:
        manifest = load_strict_canonical_json(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        _issue(issues, "invalid.schema", object_key, str(exc))
        return [], {}, declared
    issues.extend(_schema_issues(manifest, _load_schema(MEMBER_MANIFEST_SCHEMA), path=object_key))
    if not isinstance(manifest, dict) or set(manifest) != SUBORDINATE_MANIFEST_FIELDS:
        _issue(issues, "invalid.schema", object_key, "invalid member manifest fields")
        return [], {}, declared
    if (
        manifest.get("format") != MEMBER_MANIFEST_FORMAT
        or manifest.get("formatVersion") != MEMBER_MANIFEST_VERSION
    ):
        _issue(issues, "invalid.schema", object_key, "unsupported member manifest format")
    expected_scope = {"kind": reference.get("scopeKind"), "id": reference.get("scopeId")}
    if manifest.get("scope") != expected_scope or manifest.get("manifestId") != reference.get(
        "manifestId"
    ):
        _issue(
            issues,
            "invalid.schema",
            object_key,
            "manifest scope differs from the root reference",
        )
    raw_members = manifest.get("members")
    if not isinstance(raw_members, list):
        _issue(issues, "invalid.schema", f"{object_key}/members", "members must be an array")
        return [], {}, declared
    object_keys = [m.get("objectKey") for m in raw_members if isinstance(m, dict)]
    if object_keys != sorted(object_keys, key=lambda key: str(key)):
        _issue(
            issues,
            "invalid.schema",
            f"{object_key}/members",
            "members must be sorted by objectKey",
        )
    members: list[dict[str, Any]] = []
    member_paths: dict[str, Path] = {}
    for index, raw_member in enumerate(raw_members):
        member = _validate_member_descriptor(
            raw_member,
            path=f"{object_key}/members/{index}",
            issues=issues,
        )
        if member is None:
            continue
        member_key = member.get("objectKey")
        if safe_object_key(member_key):
            if member_key in member_paths:
                _issue(
                    issues,
                    "invalid.duplicate-identity",
                    f"{object_key}/members/{index}/objectKey",
                    f"duplicate member {member_key}",
                )
            else:
                member_paths[member_key] = member_path(bundle, member_key)
            declared.add(member_key)
        elif isinstance(member_key, str) and member_key:
            declared.add(member_key)
        members.append(member)
    expected_counts = {
        "memberCount": len(raw_members),
        "totalByteSize": sum(
            m.get("byteSize", 0)
            for m in raw_members
            if isinstance(m, dict)
            and isinstance(m.get("byteSize"), int)
            and not isinstance(m.get("byteSize"), bool)
        ),
        "totalRecordCount": sum(
            m.get("recordCount") or 0
            for m in raw_members
            if isinstance(m, dict)
            and (
                m.get("recordCount") is None
                or (isinstance(m.get("recordCount"), int) and not isinstance(m.get("recordCount"), bool))
            )
        ),
    }
    if manifest.get("counts") != expected_counts:
        _issue(issues, "invalid.schema", f"{object_key}/counts", f"expected {expected_counts}")
    return members, member_paths, declared


def _verify_member_files(
    bundle: Path,
    members: Sequence[Mapping[str, Any]],
    member_paths: Mapping[str, Path],
    declared: set[str],
    issues: list[VerificationIssue],
) -> None:
    materialized = _materialized_files(bundle, issues)
    for object_key in sorted(declared - materialized):
        _issue(issues, "invalid.membership-missing", object_key, "declared member is absent")
    for object_key in sorted(materialized - declared):
        _issue(issues, "invalid.membership-extra", object_key, "file is not declared")
    for member in members:
        object_key = member.get("objectKey")
        if not isinstance(object_key, str) or object_key not in member_paths:
            continue
        path = member_paths[object_key]
        if path.is_symlink() or not path.is_file():
            continue
        try:
            size = path.stat().st_size
            digest = file_sha256(path)
        except OSError as exc:
            _issue(issues, "invalid.membership-missing", object_key, str(exc))
            continue
        if size != member.get("byteSize") or digest != member.get("sha256"):
            _issue(
                issues,
                "invalid.member-digest",
                object_key,
                "member size or digest differs from its descriptor",
            )


def _read_rows(
    role: str,
    members: Sequence[Mapping[str, Any]],
    member_paths: Mapping[str, Path],
    schemas: Mapping[str, Mapping[str, Any]],
    issues: list[VerificationIssue],
) -> tuple[list[dict[str, Any]] | None, str]:
    """Load one tabular member's rows, or ``None`` when they cannot be trusted.

    Tabular members carry one canonical JSON record per newline-terminated
    line. The declared row schema and count must match the parsed stream.
    """

    matching = [member for member in members if member.get("role") == role]
    if len(matching) != 1:
        _issue(
            issues,
            "invalid.schema",
            "manifests/global.json/members",
            f"exactly one {role} member is required",
        )
        return None, role
    member = matching[0]
    object_key = str(member.get("objectKey"))
    path = member_paths.get(object_key)
    if path is None or path.is_symlink() or not path.is_file():
        return None, object_key
    try:
        rows = load_strict_canonical_jsonl(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        _issue(issues, "invalid.schema", object_key, str(exc))
        return None, object_key
    if not isinstance(rows, list):
        _issue(issues, "invalid.schema", object_key, f"{role} must be an array")
        return None, object_key
    if len(rows) != member.get("recordCount"):
        _issue(
            issues,
            "invalid.schema",
            f"member:{object_key}/recordCount",
            f"expected {len(rows)}",
        )
    schema = schemas.get(TABULAR_ROLES[role])
    if schema is not None:
        validator = jsonschema.Draft202012Validator(schema)
        for index, row in enumerate(rows):
            issues.extend(_validator_issues(validator, row, path=f"{object_key}/{index}"))
    return [row for row in rows if isinstance(row, dict)], object_key
