"""Embedded schema admission and registered row schemas."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import jsonschema

from docspec.adapters.document_release.diagnostics import VerificationIssue, _issue, _schema_issues
from docspec.adapters.document_release.rules import (
    SCHEMA_FILES,
    SCHEMA_IDS,
)
from docspec.document_release_support import (
    canonical_sha256,
)


def _load_schema(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_root_shape(
    root: Mapping[str, Any],
    schemas: Mapping[str, Mapping[str, Any]],
    issues: list[VerificationIssue],
) -> None:
    """Check the root after its registered embedded schema has been admitted.

    Missing or invalid schema members are reported during schema-set admission.
    Avoid reporting their absence a second time here.
    """

    schema = schemas.get("release-root")
    if schema is not None:
        issues.extend(_schema_issues(root, schema, path="release.json"))


def _row_subschema(schema: Any, name: str) -> dict[str, Any] | None:
    """Address one ``$defs`` entry of a carried schema as a schema in its own right.

    The wrapper keeps the whole ``$defs`` block so the entry's internal ``$ref``s
    still resolve, and carries nothing else, so none of the enclosing document's
    own keywords leak onto the row being checked.
    """

    if not isinstance(schema, Mapping):
        return None
    definitions = schema.get("$defs")
    if not isinstance(definitions, Mapping) or name not in definitions:
        return None
    return {"$defs": dict(definitions), "$ref": f"#/$defs/{name}"}


def _validate_schema_set(
    root: Mapping[str, Any],
    members: Sequence[Mapping[str, Any]],
    member_paths: Mapping[str, Path],
    issues: list[VerificationIssue],
) -> dict[str, dict[str, Any]]:
    """Check the carried schema set, and hand back the bodies it resolved.

    The returned role-to-schema map serves every later row check. Embedded
    schemas must match the packaged definitions registered under their IDs.
    """

    bodies: dict[str, dict[str, Any]] = {}
    content = root.get("content")
    schema_set = content.get("schemaSet") if isinstance(content, dict) else None
    if not isinstance(schema_set, dict):
        _issue(
            issues,
            "invalid.schema",
            "release.json/content/schemaSet",
            "schemaSet must be an object",
        )
        return bodies
    descriptors = schema_set.get("schemas")
    if not isinstance(descriptors, list):
        _issue(
            issues,
            "invalid.schema",
            "release.json/content/schemaSet/schemas",
            "schemas must be an array",
        )
        return bodies
    base = "release.json/content/schemaSet"
    ids = [item.get("schemaId") for item in descriptors if isinstance(item, dict)]
    if ids != sorted(ids, key=lambda value: str(value)):
        _issue(issues, "invalid.schema", f"{base}/schemas", "schemas must be sorted by schemaId")
    try:
        expected_set_id = f"urn:spicy:schema-set:v1:{canonical_sha256(descriptors)}"
    except (TypeError, ValueError) as exc:
        _issue(issues, "invalid.schema", base, str(exc))
    else:
        if schema_set.get("schemaSetId") != expected_set_id:
            _issue(issues, "invalid.schema", f"{base}/schemaSetId", f"expected {expected_set_id}")
    schema_members = {
        member["schemaId"]: member
        for member in members
        if member.get("role") == "schema" and isinstance(member.get("schemaId"), str)
    }
    seen_roles: dict[str, int] = {}
    for index, descriptor in enumerate(descriptors):
        path = f"{base}/schemas/{index}"
        if not isinstance(descriptor, dict):
            continue
        schema_id = descriptor.get("schemaId")
        roles = descriptor.get("roles")
        role = roles[0] if isinstance(roles, list) and len(roles) == 1 else None
        if role is None or SCHEMA_IDS.get(role) != schema_id:
            _issue(
                issues,
                "invalid.schema",
                f"{path}/roles",
                f"role {role!r} must resolve to the registered schema for {schema_id!r}",
            )
            continue
        seen_roles[role] = seen_roles.get(role, 0) + 1
        member = schema_members.get(schema_id)
        if member is None:
            _issue(
                issues,
                "invalid.membership-missing",
                f"{path}/schemaId",
                "schema descriptor has no schema member",
            )
            continue
        if member.get("sha256") != descriptor.get("schemaSha256"):
            _issue(
                issues,
                "invalid.member-digest",
                f"{path}/schemaSha256",
                "schema descriptor digest differs from the member",
            )
        resolved = member_paths.get(str(member.get("objectKey")))
        if resolved is None or not resolved.is_file():
            continue
        try:
            schema = json.loads(resolved.read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator.check_schema(schema)
        except (OSError, ValueError, jsonschema.SchemaError) as exc:
            _issue(issues, "invalid.schema", str(member.get("objectKey")), str(exc))
            continue
        if schema.get("$id") != schema_id:
            _issue(
                issues,
                "invalid.schema",
                str(member.get("objectKey")),
                "$id differs from the descriptor",
            )
            continue
        if schema != _load_schema(SCHEMA_FILES[role]):
            # The packaged schema is authoritative. A bundle may carry
            # its own copy -- that is what makes it portable -- but a copy that
            # says something else is a bundle checked against a contract nobody
            # registered.
            _issue(
                issues,
                "invalid.schema",
                str(member.get("objectKey")),
                f"embedded schema differs from the registered schema for role {role!r}",
            )
            continue
        bodies[role] = schema
    for role in sorted(SCHEMA_FILES):
        if seen_roles.get(role) != 1:
            _issue(issues, "invalid.schema", f"{base}/schemas", f"role {role!r} must resolve exactly once")
    return bodies
