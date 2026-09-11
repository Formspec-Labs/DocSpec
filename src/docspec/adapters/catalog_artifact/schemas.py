"""Catalog schema admission with an authoritative diagnostic fallback."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import jsonschema

from docspec.domain.source_catalog import (
    source_catalog_schemas,
)
from docspec.errors import IntegrityError

# Use the compiled engine for valid rows; the Python validator retains the
# authoritative refusal diagnostics and supports environments without the engine.
try:
    import jsonschema_rs
except ImportError:  # pragma: no cover - environment without the compiled engine
    jsonschema_rs = None  # type: ignore[assignment]


_SCHEMAS = source_catalog_schemas()


_POLICY_VALIDATOR = jsonschema.Draft202012Validator(_SCHEMAS["catalog-policy.schema.json"])


_RECEIPT_VALIDATOR = jsonschema.Draft202012Validator(_SCHEMAS["catalog-build-receipt.schema.json"])


def _schema_error(validator: jsonschema.Draft202012Validator, value: object, label: str) -> None:
    try:
        validator.validate(value)
    except jsonschema.ValidationError as error:
        path = "/".join(str(part) for part in error.absolute_path) or "$"
        raise IntegrityError(f"{label} schema failure at {path}: {error.message}") from error


class _CompiledSchemaGate:
    """Fast-accept schema checking with python-jsonschema as the sole authority.

    The compiled validator only ever short-circuits ACCEPTANCE; every rejection
    is re-decided by ``jsonschema`` so refusal semantics and error text cannot
    drift behind a faster engine. The differential test pins the two validators
    to each other on this schema's shapes.
    """

    __slots__ = ("_authority", "_fast", "implementation_id")

    def __init__(self, schema: Mapping[str, Any]) -> None:
        self._authority = jsonschema.Draft202012Validator(schema)
        #: Which engine decides acceptance here. ``jsonschema-rs`` is a declared
        #: dependency, so the pure-Python value should never be seen in a
        #: released build -- and that is the point of naming it. Falling back
        #: costs ~116x on this schema (1,993 us/row against 17.1 us/row), which
        #: used to happen silently.
        self.implementation_id = "jsonschema-rs"
        if jsonschema_rs is None:
            self._fast = None
            self.implementation_id = "jsonschema"
            return
        try:
            self._fast = jsonschema_rs.validator_for(dict(schema))
        except Exception:  # noqa: BLE001 - an uncompilable schema falls back to the authority
            self._fast = None
            self.implementation_id = "jsonschema"

    def error(self, value: object, label: str) -> None:
        if self._fast is not None and self._fast.is_valid(value):
            return
        _schema_error(self._authority, value, label)


_ITEM_VALIDATOR = _CompiledSchemaGate(_SCHEMAS["source-item.schema.json"])


def source_item_validator_implementation() -> str:
    """Name the schema engine deciding acceptance for source-item rows."""

    return _ITEM_VALIDATOR.implementation_id
