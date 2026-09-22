"""Catalog schemas use the same compiled payload validator as Core.

Compiles the policy, receipt, and source-item schemas once at import.
"""

from __future__ import annotations

from docspec.adapters.schema_validation import compile_payload_schema, validate_payload
from docspec.domain.source_catalog import (
    source_catalog_schemas,
)

_SCHEMAS = source_catalog_schemas()


_POLICY_VALIDATOR = compile_payload_schema(_SCHEMAS["catalog-policy.schema.json"])
_RECEIPT_VALIDATOR = compile_payload_schema(_SCHEMAS["catalog-build-receipt.schema.json"])
_ITEM_VALIDATOR = compile_payload_schema(_SCHEMAS["source-item.schema.json"])


def _verify_item_schema(value: object, label: str) -> None:
    """Validate one catalog item payload against the compiled item schema."""

    validate_payload(_ITEM_VALIDATOR, value, label)
