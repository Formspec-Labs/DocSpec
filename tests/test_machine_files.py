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
    return set(_load(ROOT / "conformance" / "test-matrix.json"))


def test_profile_descriptions_are_closed_and_cover_every_role() -> None:
    profile_paths = sorted((ROOT / "src" / "docspec" / "storage_profiles").glob("*.json"))
    registry = ProfileRegistry.from_directory(ROOT / "src" / "docspec" / "storage_profiles")
    registered = registry.list()
    required_test_ids = _required_test_ids()
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
        assert isinstance(description.limits, dict)
        assert set(description.requires).issubset(profile_ids)
        assert item.profile_set_id
        assert item.verifier_test_id in required_test_ids
