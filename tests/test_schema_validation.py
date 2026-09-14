"""Supplied schemas retain their draft, resource snapshot and error meaning."""

import pytest

from docspec.adapters.schema_validation import compile_payload_schema, validate_payload
from docspec.errors import IntegrityError, SchemaValidationError


def test_references_are_explicit_and_frozen_when_compiled():
    schema = {"$ref": "https://example.invalid/value"}
    reference = {"type": "integer", "minimum": 2}
    validator = compile_payload_schema(schema, references={"https://example.invalid/value": reference})
    schema["$ref"] = "https://example.invalid/changed"
    reference["minimum"] = 100
    validate_payload(validator, 2, "payload")
    with pytest.raises(SchemaValidationError):
        validate_payload(validator, 1, "payload")
    with pytest.raises(IntegrityError, match="not supplied"):
        compile_payload_schema({"$ref": "https://example.invalid/missing"})


def test_relative_references_use_the_pinned_base_uri():
    validator = compile_payload_schema(
        {"$ref": "value"}, base_uri="https://example.invalid/schemas/root",
        references={"https://example.invalid/schemas/value": {"type": "string"}},
    )
    validate_payload(validator, "kept", "payload")
    with pytest.raises(SchemaValidationError):
        validate_payload(validator, 1, "payload")


def test_formats_are_explicit_assertions_and_unknown_assertions_refuse():
    schema = {"type": "string", "format": "date-time"}
    validate_payload(compile_payload_schema(schema), "not a date", "payload")
    with pytest.raises(SchemaValidationError):
        validate_payload(compile_payload_schema(schema, validate_formats=True), "not a date", "payload")
    with pytest.raises(IntegrityError):
        compile_payload_schema({"format": "not-a-known-format"}, validate_formats=True)


@pytest.mark.parametrize("schema", [
    {"$schema": "https://example.invalid/unknown"},
    {"$schema": "http://json-schema.org/draft-07/schema#"},
    {"type": "not-a-type"},
])
def test_unsupported_drafts_and_malformed_schemas_refuse(schema):
    with pytest.raises(IntegrityError):
        compile_payload_schema(schema)


def test_error_paths_are_structured_and_escape_json_pointer_tokens():
    validator = compile_payload_schema({"properties": {"a/b": {"type": "array", "items": {"type": "integer"}}}})
    with pytest.raises(SchemaValidationError) as refused:
        validate_payload(validator, {"a/b": ["wrong"]}, "catalog row")
    assert refused.value.instance_path == ("a/b", 0)
    assert refused.value.schema_path == ("properties", "a/b", "items", "type")
    assert "/a~1b/0" in str(refused.value)
    assert "integer" in refused.value.message


def test_boolean_schemas_and_local_definitions_are_authoritative():
    validate_payload(compile_payload_schema(True), None, "payload")
    with pytest.raises(SchemaValidationError):
        validate_payload(compile_payload_schema(False), None, "payload")
    validator = compile_payload_schema({"$defs": {"v": {"const": "allowed"}}, "$ref": "#/$defs/v"})
    validate_payload(validator, "allowed", "payload")
    with pytest.raises(SchemaValidationError):
        validate_payload(validator, "refused", "payload")
