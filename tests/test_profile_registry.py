import json
from pathlib import Path

import pytest

from docspec.domain.profiles import ProfileRole
from docspec.errors import ProfileError
from docspec.profile_registry import ProfileRegistry


ROOT = Path(__file__).resolve().parents[1]


def test_machine_profiles_load_and_select_one_implemented_local_set() -> None:
    registry = ProfileRegistry.from_directory(ROOT / "profiles")
    by_role = {role: registry.list(role) for role in ProfileRole}
    assert all(by_role.values())
    selected = (
        "urn:docspec:profile:release-manifest:canonical-json:1",
        "urn:docspec:profile:document-catalog:local-manifest:1",
        "urn:docspec:profile:record-storage:local-jsonl:1",
        "urn:docspec:profile:blob-storage:local-content-addressed:1",
        "urn:docspec:profile:document-store-persistence:local-json:1",
        "urn:docspec:profile:result-delivery:durable-dataset:1",
    )
    profile_set = registry.select(selected)
    assert profile_set.for_role(ProfileRole.RECORD_STORAGE).profile_id.endswith("local-jsonl:1")


def test_profile_selection_fails_before_work_when_a_requirement_is_missing() -> None:
    registry = ProfileRegistry.from_directory(ROOT / "profiles")
    with pytest.raises(ProfileError, match="missing required profiles"):
        registry.select(("urn:docspec:profile:result-delivery:durable-dataset:1",))


def test_profile_pin_identifies_the_complete_machine_description(tmp_path: Path) -> None:
    original_path = ROOT / "profiles" / "canonical-release-manifest-v1.json"
    original = ProfileRegistry.from_file(original_path)
    changed_value = json.loads(original_path.read_text(encoding="utf-8"))
    changed_value["limits"]["maxRootBytes"] += 1
    changed_path = tmp_path / "changed-profile.json"
    changed_path.write_text(json.dumps(changed_value), encoding="utf-8")
    changed = ProfileRegistry.from_file(changed_path)

    assert changed.description.configuration_digest == original.description.configuration_digest
    assert changed.description_digest != original.description_digest
    assert changed.description.pin(description_digest=changed.description_digest) != original.description.pin(
        description_digest=original.description_digest
    )
