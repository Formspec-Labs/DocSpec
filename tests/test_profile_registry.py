import json
from pathlib import Path

import pytest

from docspec.domain.profiles import ProfileRole
from docspec.domain.release import RELEASE_LOGICAL_SCHEMA
from docspec.errors import ProfileError
from docspec.profile_registry import DEFAULT_GOVERNANCE_POLICY_IDS, ProfileRegistry


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


def test_profile_governance_is_closed_declared_and_identity_bearing(tmp_path: Path) -> None:
    original_path = ROOT / "profiles" / "local-jsonl-records-v1.json"
    original = ProfileRegistry.from_file(original_path)
    governance = original.description.governance

    assert governance.access_policy_id.startswith("urn:docspec:policy:access:")
    assert governance.encryption_policy_id.startswith("urn:docspec:policy:encryption:")
    assert governance.region_policy_id.startswith("urn:docspec:policy:region:")
    assert governance.retention_policy_id.startswith("urn:docspec:policy:retention:")
    assert governance.redistribution_policy_id.startswith("urn:docspec:policy:redistribution:")

    changed_value = json.loads(original_path.read_text(encoding="utf-8"))
    test_region_policy = "urn:docspec:policy:region:test-deployment:1"
    changed_value["governancePolicies"]["regionPolicyId"] = test_region_policy
    changed_path = tmp_path / "changed-governance.json"
    changed_path.write_text(json.dumps(changed_value), encoding="utf-8")
    with pytest.raises(ProfileError, match="unknown governance policies"):
        ProfileRegistry.from_file(changed_path)
    changed = ProfileRegistry.from_file(
        changed_path,
        governance_policy_ids=DEFAULT_GOVERNANCE_POLICY_IDS | {test_region_policy},
    )
    assert changed.description_digest != original.description_digest

    changed_value["governancePolicies"]["unknownPolicyId"] = "urn:docspec:policy:unknown:1"
    changed_path.write_text(json.dumps(changed_value), encoding="utf-8")
    with pytest.raises(ProfileError, match="closed shape"):
        ProfileRegistry.from_file(changed_path)


@pytest.mark.parametrize("field", ("logicalSchemas", "physicalMediaTypes"))
def test_profile_registry_rejects_string_values_where_arrays_are_required(
    tmp_path: Path,
    field: str,
) -> None:
    value = json.loads((ROOT / "profiles" / "local-jsonl-records-v1.json").read_text(encoding="utf-8"))
    value[field] = "docspec-not-an-array/1"
    path = tmp_path / f"invalid-{field}.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ProfileError, match="schemas, media types, or limits"):
        ProfileRegistry.from_file(path)


def test_release_bearing_profiles_declare_the_schema_the_code_emits() -> None:
    """The two profiles that persist DocumentRelease roots must name the exact
    logical schema `DocumentRelease.to_dict` produces, or a deployment could
    select storage that silently disagrees with the written surface (the
    declarations were stale at 1.0 once before)."""

    for name in ("canonical-release-manifest-v1.json", "local-document-catalog-v1.json"):
        value = json.loads((ROOT / "profiles" / name).read_text(encoding="utf-8"))
        assert value["logicalSchemas"] == [RELEASE_LOGICAL_SCHEMA], name
