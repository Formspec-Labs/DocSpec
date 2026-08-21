from __future__ import annotations

import importlib
import itertools
import json
import shutil
import sys
from pathlib import Path

import pytest

from docspec.cli import main
from docspec.domain.profiles import ProfileRole, ProfileSet
from docspec.errors import ProfileError
from docspec.profile_registry import ProfileRegistry

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_description_helpers = importlib.import_module("tests.conformance.test_profile_descriptions")
_seeded_local_run = _description_helpers._seeded_local_run

PROFILE_ROOT = ROOT / "profiles"


def _registry() -> ProfileRegistry:
    return ProfileRegistry.from_directory(PROFILE_ROOT)


def test_every_one_per_role_combination_composes_or_registers_its_incompatibility() -> None:
    """Walk the complete one-per-role composition space of the registered
    machine descriptions: each combination either composes into a verified
    ProfileSet or refuses with the registered missing-requirement
    incompatibility, and the split is decided by the declared `requires`
    edges alone."""

    registry = _registry()
    by_role = {role: registry.list(role) for role in ProfileRole}
    assert all(by_role.values())
    combinations = list(itertools.product(*(by_role[role] for role in sorted(by_role, key=lambda role: role.value))))
    assert len(combinations) == 9

    composed = 0
    refused = 0
    for combination in combinations:
        selected_ids = tuple(item.description.profile_id for item in combination)
        expected_missing = {
            requirement
            for item in combination
            for requirement in item.description.requires
            if requirement not in set(selected_ids)
        }
        if expected_missing:
            with pytest.raises(ProfileError, match="missing required profiles") as refusal:
                registry.select(selected_ids)
            assert any(requirement in str(refusal.value) for requirement in sorted(expected_missing))
            refused += 1
            continue
        profile_set = registry.select(selected_ids)
        assert profile_set.pins == tuple(
            sorted(
                (item.description.pin(description_digest=item.description_digest) for item in combination),
                key=lambda pin: pin.role.value,
            )
        )
        composed += 1

    # The portable-local set supports all three delivery profiles; both S3
    # blob profiles are acquisition-side and refuse to satisfy the local
    # document store's declared blob requirement.
    assert (composed, refused) == (3, 6)


def test_a_specified_but_unimplemented_profile_refuses_selection(tmp_path: Path) -> None:
    directory = tmp_path / "profiles"
    shutil.copytree(PROFILE_ROOT, directory)
    target = directory / "local-jsonl-records-v1.json"
    value = json.loads(target.read_text(encoding="utf-8"))
    value["implementationStatus"] = "specified"
    value["implementationModule"] = None
    target.write_text(json.dumps(value), encoding="utf-8")

    registry = ProfileRegistry.from_directory(directory)
    with pytest.raises(ProfileError, match="specified but not implemented"):
        registry.select(
            (
                "urn:docspec:profile:release-manifest:canonical-json:1",
                "urn:docspec:profile:document-catalog:local-manifest:1",
                "urn:docspec:profile:record-storage:local-jsonl:1",
                "urn:docspec:profile:blob-storage:local-content-addressed:1",
                "urn:docspec:profile:document-store-persistence:local-json:1",
                "urn:docspec:profile:result-delivery:durable-dataset:1",
            )
        )


def test_an_incompatible_selection_fails_run_start_before_any_planning_state(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    registry = _registry()
    registered = {item.description.profile_id: item for item in registry.list()}
    selected_ids = (
        "urn:docspec:profile:release-manifest:canonical-json:1",
        "urn:docspec:profile:document-catalog:local-manifest:1",
        "urn:docspec:profile:record-storage:local-jsonl:1",
        "urn:docspec:profile:blob-storage:amazon-s3-content-addressed:1",
        "urn:docspec:profile:document-store-persistence:local-json:1",
        "urn:docspec:profile:result-delivery:durable-dataset:1",
    )
    incompatible = ProfileSet(
        tuple(
            sorted(
                (
                    registered[identifier].description.pin(
                        description_digest=registered[identifier].description_digest
                    )
                    for identifier in selected_ids
                ),
                key=lambda pin: pin.role.value,
            )
        )
    )
    request, roots = _seeded_local_run(tmp_path, incompatible)

    destination = tmp_path / "run-reference.json"
    receipt = tmp_path / "run-operation.json"
    assert (
        main(
            [
                "run",
                "start",
                "--request",
                str(request),
                "--destination",
                str(destination),
                "--receipt",
                str(receipt),
            ]
        )
        == 2
    )
    error = json.loads(capfd.readouterr().err)
    assert error["verdict"] == "fail"
    assert "missing required profiles" in error["message"]
    failure = json.loads(receipt.read_text(encoding="utf-8"))
    assert failure["verdict"] == "failed"
    assert not destination.exists()
    assert not Path(roots["controlRepository"]).exists(), "the incompatibility must be found before work begins"
