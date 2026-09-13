"""Catalog schema admission with an authoritative diagnostic fallback."""

from __future__ import annotations

import jsonschema
import jsonschema_rs

from docspec.domain.source_catalog import (
    source_catalog_schemas,
)
from docspec.errors import IntegrityError

_SCHEMAS = source_catalog_schemas()


_POLICY_VALIDATOR = jsonschema.Draft202012Validator(_SCHEMAS["catalog-policy.schema.json"])


_RECEIPT_VALIDATOR = jsonschema.Draft202012Validator(_SCHEMAS["catalog-build-receipt.schema.json"])


def _schema_error(validator: jsonschema.Draft202012Validator, value: object, label: str) -> None:
    try:
        validator.validate(value)
    except jsonschema.ValidationError as error:
        path = "/".join(str(part) for part in error.absolute_path) or "$"
        raise IntegrityError(f"{label} schema failure at {path}: {error.message}") from error


_ITEM_VALIDATOR = jsonschema_rs.validator_for(_SCHEMAS["source-item.schema.json"])
_ITEM_AUTHORITY = jsonschema.Draft202012Validator(_SCHEMAS["source-item.schema.json"])


def _verify_item_schema(value: object, label: str) -> None:
    """Use the required compiled engine for acceptance and retain refusal diagnostics."""

    if not _ITEM_VALIDATOR.is_valid(value):
        _schema_error(_ITEM_AUTHORITY, value, label)
