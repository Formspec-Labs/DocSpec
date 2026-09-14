"""Production boundary probes verify supported behavior and honest receipts."""

from tests.support.core_boundary_experiment import run_stage
import pytest

from docspec.application.core_edits import VALUE_EDIT_BATCH_ROWS
from docspec.ports.record_storage import BATCH_BYTES


@pytest.mark.parametrize("count", [16, VALUE_EDIT_BATCH_ROWS + 1])
def test_nested_edit_recipe_publishes_and_checks_typed_fixture_values(tmp_path, count):
    result = run_stage(tmp_path, "nested", count=count)
    assert result["members"] == result["transformed"] == result["reopened_verified"] == result["unchanged_originals"] == count
    assert result["activities"] == (count + VALUE_EDIT_BATCH_ROWS - 1) // VALUE_EDIT_BATCH_ROWS
    assert result["operation_seconds"] > 0
    assert result["metrics"]["sqlite_transactions"] > 0


def test_supplied_schema_recipe_uses_one_native_validator_and_refuses_before_publication(tmp_path):
    result = run_stage(tmp_path, "schema", count=16)
    assert result["compiled_validators"] == 1
    assert "jsonschema_rs" in result["validator"]
    assert result["reopened_verified"] == 16
    assert result["admission"]["payload_schema_calls"] == result["admission"]["fixed_record_admissions"] == 17
    assert result["admission"]["payload_schema_seconds"] > 0
    assert result["invalid_refused_before_publication"]["instance_path"] == ("position",)
    assert result["invalid_refused_before_publication"]["schema_path"] == ("properties", "position", "type")


def test_actual_record_publication_and_content_value_boundaries(tmp_path):
    result = run_stage(tmp_path, "boundary")
    native, published = result["native_record_ceiling"], result["inline_publication_ceiling"]
    assert native["encoded_record_bytes"] == BATCH_BYTES
    assert native["native_encoded_record_admitted"] and not native["published_and_reopened"]
    assert published["encoded_record_bytes"] == BATCH_BYTES - native["commit_receipt_bytes"]
    assert published["encoded_record_bytes"] + published["commit_receipt_bytes"] == BATCH_BYTES
    assert published["published_and_reopened"]
    assert result["inline_publication_plus_one"]["encoded_record_bytes"] == published["encoded_record_bytes"] + 1
    assert not result["inline_publication_plus_one"]["published_and_reopened"]
    assert result["native_record_plus_one"]["encoded_record_bytes"] == BATCH_BYTES + 1
    assert not result["native_record_plus_one"].get("native_encoded_record_admitted", False)
    content = result["content_reference_value"]
    assert content["encoded_json_value_bytes"] == BATCH_BYTES and content["encoded_record_bytes"] < 1024
    assert content["published_and_reopened"] and content["plus_one_refusal"]
