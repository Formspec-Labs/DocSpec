from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

from docspec.domain.profiles import ProfileRole
from docspec.profile_registry import ProfileRegistry
from tools.generate_ownership_manifest import manifest_bytes as ownership_manifest_bytes
from tools.generate_scale_profile_schema import schema_bytes


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
        planned_test_module = row["plannedTestModule"]
        if planned_test_module is not None:
            assert isinstance(planned_test_module, str)
            assert (ROOT / planned_test_module).is_file(), (
                f"{row['testId']} names a plannedTestModule that does not exist: {planned_test_module}"
            )
        if row["status"] == "implemented":
            assert row["selectors"]
            assert planned_test_module is None
        elif row["status"] == "planned":
            assert row["selectors"] == []
            assert planned_test_module is not None
        else:
            assert row["selectors"]

        for selector in row["selectors"]:
            file_name, separator, function_name = selector.partition("::")
            assert separator
            test_file = ROOT / file_name
            assert test_file.is_file()
            assert f"def {function_name}(" in test_file.read_text(encoding="utf-8")


def test_module_inventory_matches_the_installed_source_tree() -> None:
    # ownership/modules.json used to hand-list all 67 module paths, each carrying a
    # redundant "owner": "DocSpec" (a single-owner project has one owner, not 67
    # copies of the same string), with no generator: a new module was a two-place
    # edit and nothing but this test's declared-vs-actual path check ever verified it.
    #
    # tools/generate_ownership_manifest.py now derives the module *path list*
    # directly from the source tree and merges in the one thing a generator cannot
    # invent -- capability, status, and conformanceTests per module -- from a small
    # hand-maintained table, raising if a module is missing from that table or the
    # table names a module no longer on disk. This test confirms the checked-in
    # file is exactly what the generator currently produces, and separately checks
    # every declared conformanceTests id is real -- that cross-file link is outside
    # what the generator alone can verify.
    inventory = _load(ROOT / "ownership" / "modules.json")
    assert (ROOT / "ownership" / "modules.json").read_bytes() == ownership_manifest_bytes()

    required_test_ids = _required_test_ids()
    for row in inventory["modules"]:
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


def test_scale_profile_schema_is_generated_from_the_domain_model_and_up_to_date() -> None:
    # conformance/scale-profile.schema.json used to be a hand-typed 13.7 KB JSON Schema
    # checked only by ~20 hand-picked property assertions here -- nothing verified the
    # other ~90% of it, and nothing could: pyproject.toml declares `dependencies = []`,
    # so no JSON Schema validator exists in this tree, and the real constraints live
    # independently in docspec/domain/scale.py. The two had already drifted (several
    # fields the domain model enforces as strict integers were declared "number" in the
    # schema, silently accepting floats the real parser rejects).
    #
    # tools/generate_scale_profile_schema.py now derives the schema from the same
    # dataclasses, so this test only has to confirm the checked-in file is exactly what
    # the generator currently produces -- any drift between the schema and the domain
    # model fails here instead of accumulating unnoticed.
    schema_path = ROOT / "conformance" / "scale-profile.schema.json"
    assert schema_path.read_bytes() == schema_bytes()
