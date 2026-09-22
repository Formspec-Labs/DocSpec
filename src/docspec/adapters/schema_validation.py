"""Compiled payload schemas with explicit draft, references and diagnostics."""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

import jsonschema_rs

from docspec.errors import IntegrityError, SchemaValidationError


def compile_payload_schema(
    schema: dict[str, Any] | bool, *, references: Mapping[str, dict[str, Any] | bool] | None = None,
    base_uri: str | None = None, validate_formats: bool = False,
) -> jsonschema_rs.Draft202012Validator:
    """Compile once and reuse; the initial payload binding is Draft 2020-12.

    Resolve only the supplied reference snapshot. No network lookup or change to
    a caller's schema dictionary may alter an already compiled validator.
    Formats are assertions only when explicitly enabled; unknown asserted formats
    refuse. Core's JSON value-domain admission precedes this schema check.
    """
    schemas = deepcopy(dict(references or {}))
    definition = deepcopy(schema)
    for value in (definition, *schemas.values()):
        if isinstance(value, dict):
            declared = value.get("$schema", "https://json-schema.org/draft/2020-12/schema")
            if declared not in {
                "https://json-schema.org/draft/2020-12/schema",
                "https://json-schema.org/draft/2020-12/schema#",
            }:
                raise IntegrityError("payload schema requires the supported Draft 2020-12 binding")

    def retrieve(uri: str) -> dict[str, Any] | bool:
        if uri not in schemas:
            raise ValueError(f"schema reference was not supplied: {uri}")
        return schemas[uri]

    try:
        return jsonschema_rs.Draft202012Validator(
            definition, retriever=retrieve, base_uri=base_uri,
            validate_formats=validate_formats, ignore_unknown_formats=not validate_formats,
        )
    except (ValueError, TypeError) as error:
        raise IntegrityError(f"invalid payload schema: {error}") from error


def validate_payload(validator: jsonschema_rs.Draft202012Validator, value: Any, label: str) -> None:
    """Raise SchemaValidationError with the label and failing paths when a payload does not satisfy the validator."""

    try:
        validator.validate(value)
    except jsonschema_rs.ValidationError as error:
        raise SchemaValidationError(
            label, error.message, instance_path=tuple(error.instance_path), schema_path=tuple(error.schema_path),
        ) from error
