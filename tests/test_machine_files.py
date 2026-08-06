from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

from docspec.domain.profiles import ProfileRole
from docspec.profile_registry import ProfileRegistry


ROOT = Path(__file__).resolve().parents[1]

PROFILE_ROLES = {role.value for role in ProfileRole}
def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_closed_object)
    assert isinstance(value, dict)
    return value


def _required_test_ids() -> set[str]:
    specification = _load(ROOT / "conformance" / "specification.json")
    return set(specification["requiredTestIds"])


def test_conformance_specification_and_matrix_name_every_required_test() -> None:
    specification = _load(ROOT / "conformance" / "specification.json")
    matrix = _load(ROOT / "conformance" / "test-matrix.json")
    required_test_ids = _required_test_ids()

    assert set(specification) == {
        "conformanceVerdict",
        "evidencePolicy",
        "format",
        "formatVersion",
        "implementationStatus",
        "normativeSource",
        "requiredTestIds",
        "specificationId",
        "title",
        "verdictReason",
    }
    assert specification["conformanceVerdict"] == "not-yet-conformant"
    assert set(specification["evidencePolicy"]) == {
        "missingRequiredTestVerdict",
        "proseEstablishesConformance",
        "reportFormat",
        "skippedRequiredTestVerdict",
        "xfailedRequiredTestVerdict",
    }
    assert set(matrix) == {
        "format",
        "formatVersion",
        "implementationStatus",
        "specificationId",
        "tests",
    }
    assert matrix["format"] == "docspec-conformance-test-matrix"
    assert matrix["formatVersion"] == "1.0"
    assert matrix["specificationId"] == specification["specificationId"]

    rows = matrix["tests"]
    assert len(required_test_ids) == len(specification["requiredTestIds"])
    assert len(rows) == len(required_test_ids)
    assert {row["testId"] for row in rows} == required_test_ids
    assert len({row["testId"] for row in rows}) == len(rows)

    for row in rows:
        assert set(row) == {"plannedTestModule", "selectors", "status", "testId"}
        assert row["status"] in {"implemented", "partial", "planned"}
        if row["status"] == "implemented":
            assert row["selectors"]
            assert row["plannedTestModule"] is None
        elif row["status"] == "planned":
            assert row["selectors"] == []
            assert isinstance(row["plannedTestModule"], str)
        else:
            assert row["selectors"]
            assert isinstance(row["plannedTestModule"], str)

        for selector in row["selectors"]:
            file_name, separator, function_name = selector.partition("::")
            assert separator
            test_file = ROOT / file_name
            assert test_file.is_file()
            assert f"def {function_name}(" in test_file.read_text(encoding="utf-8")


def test_module_inventory_matches_the_installed_source_tree() -> None:
    inventory = _load(ROOT / "ownership" / "modules.json")
    required_test_ids = _required_test_ids()
    assert set(inventory) == {
        "archiveRoot",
        "format",
        "formatVersion",
        "implementationStatus",
        "inventoryBasis",
        "modules",
        "sourceRoot",
    }
    assert inventory["inventoryBasis"] == "working-tree"
    assert inventory["sourceRoot"] == "src/docspec"
    assert inventory["archiveRoot"] == "archive/legacy-2026-08-05"

    declared = inventory["modules"]
    declared_path_list = [row["path"] for row in declared]
    declared_paths = set(declared_path_list)
    actual_paths = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / inventory["sourceRoot"]).rglob("*.py")
    }
    assert declared_path_list == sorted(declared_path_list)
    assert declared_paths == actual_paths
    assert len(declared_paths) == len(declared)

    for row in declared:
        assert set(row) == {"capability", "conformanceTests", "owner", "path", "status"}
        assert row["owner"] == "DocSpec"
        assert row["status"] in {"implemented", "partial"}
        assert isinstance(row["capability"], str) and row["capability"]
        assert set(row["conformanceTests"]).issubset(required_test_ids)


def test_profile_descriptions_are_closed_and_cover_every_role() -> None:
    profile_paths = sorted((ROOT / "profiles").glob("*.json"))
    registry = ProfileRegistry.from_directory(ROOT / "profiles")
    registered = registry.list()
    required_test_ids = _required_test_ids()
    test_statuses = {
        row["testId"]: row["status"]
        for row in _load(ROOT / "conformance" / "test-matrix.json")["tests"]
    }
    assert registered
    assert len(profile_paths) == len(registered)
    assert {item.description.role.value for item in registered} == PROFILE_ROLES
    assert sum(item.description.role == ProfileRole.BLOB_STORAGE for item in registered) >= 3
    assert sum(item.description.role == ProfileRole.RESULT_DELIVERY for item in registered) >= 3

    profile_ids = {item.description.profile_id for item in registered}
    assert len(profile_ids) == len(registered)
    for item in registered:
        description = item.description
        assert item.implementation_status == "implemented"
        assert item.implementation_module is not None
        module_name, separator, attribute_name = item.implementation_module.partition(":")
        assert separator and module_name.startswith("docspec.") and attribute_name
        implementation_module = importlib.import_module(module_name)
        assert hasattr(implementation_module, attribute_name)
        assert description.schemas
        assert description.media_types
        assert description.capabilities
        assert description.limits
        assert set(description.requires).issubset(profile_ids)
        assert item.profile_set_id
        assert item.verifier_test_id in required_test_ids
        assert item.verifier_status == test_statuses[item.verifier_test_id]


def test_scale_profile_schema_is_closed_and_identity_bearing() -> None:
    schema = _load(ROOT / "conformance" / "scale-profile.schema.json")
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["format"] == {"const": "docspec-scale-profile"}
    assert schema["properties"]["formatVersion"] == {"const": "1.1"}
    assert schema["$defs"]["sha256"]["pattern"] == "^sha256:[0-9a-f]{64}$"
    assert "descriptionDigest" in schema["$defs"]["profilePin"]["required"]
    assert schema["$defs"]["profilePin"]["properties"]["descriptionDigest"] == {
        "$ref": "#/$defs/sha256"
    }
    assert {
        "baseRelease",
        "documentCatalog",
        "documentStorePolicy",
        "executionProfile",
        "processingPlan",
        "profileSet",
        "resultSink",
        "taskPolicy",
    }.issubset(schema["required"])
    assert schema["properties"]["processingPlan"] == {"$ref": "#/$defs/artifactPin"}
    assert schema["properties"]["executionProfile"] == {"$ref": "#/$defs/artifactPin"}
    partition_policy = schema["properties"]["partitionPolicy"]
    assert {"targetMemberBytes", "hardMaxMemberBytes"}.issubset(partition_policy["required"])
    processor_targets = schema["properties"]["targets"]["properties"]["processorTargets"]
    assert "providerLimits" in processor_targets["items"]["required"]
    distributions = schema["properties"]["inputShape"]["properties"]["distributions"]
    assert set(distributions["required"]) == {
        "bytes",
        "files",
        "images",
        "pages",
        "representations",
        "segments",
    }
