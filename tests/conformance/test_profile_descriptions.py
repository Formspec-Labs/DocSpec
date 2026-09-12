
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from docspec.cli import main
from docspec.domain.identity import identity_digest
from docspec.domain.profiles import ProfileSet
from docspec.errors import ProfileError
from docspec.profile_registry import ProfileRegistry
from tests.support.profiles import (
    _cli_helpers,
    _seeded_local_run,
)

ROOT = Path(__file__).resolve().parents[2]


_portable_local_profiles = _cli_helpers._portable_local_profiles

PROFILE_ROOT = ROOT / "src" / "docspec" / "storage_profiles"
# Identity-bearing description fields: changing any one must change the
# description digest a plan pins, or a deployment could swap executable
# behavior under an existing pin.
_IDENTITY_FIELDS = (
    "capabilities",
    "compatibility",
    "configuration",
    "implementationId",
    "implementationModule",
    "limits",
    "logicalSchemas",
    "physicalMediaTypes",
    "version",
)


def _storage_profile_paths() -> list[Path]:
    paths = sorted(PROFILE_ROOT.glob("*.json"))
    assert paths
    return paths


def test_every_description_on_disk_is_closed_versioned_digest_pinned_and_capable(tmp_path: Path) -> None:
    governed = set(_storage_profile_paths())
    assert set(PROFILE_ROOT.rglob("*.json")) == governed, (
        "a profile description exists that no registered loader governs"
    )

    for path in _storage_profile_paths():
        registered = ProfileRegistry.from_file(path)
        description = registered.description
        major, minor, patch = description.version.split(".")
        assert all(component.isdigit() for component in (major, minor, patch))
        assert description.configuration_digest == identity_digest(description.configuration)
        assert description.capabilities == tuple(sorted(description.capabilities))
        assert description.capabilities
        assert isinstance(description.limits, dict)
        assert registered.description_digest.startswith("sha256:")

        value = json.loads(path.read_text(encoding="utf-8"))
        widened = tmp_path / f"widened-{path.name}"
        widened.write_text(json.dumps({**value, "undeclaredField": True}), encoding="utf-8")
        with pytest.raises(ProfileError, match="closed profile shape"):
            ProfileRegistry.from_file(widened)

        for field in _IDENTITY_FIELDS:
            assert field in value, f"{path.name} lost identity-bearing field {field}"

        drifted_value = json.loads(path.read_text(encoding="utf-8"))
        drifted_value["version"] = "999.0.0"
        drifted = tmp_path / f"drifted-{path.name}"
        drifted.write_text(json.dumps(drifted_value), encoding="utf-8")
        assert ProfileRegistry.from_file(drifted).description_digest != registered.description_digest

def test_unpinned_descriptions_are_rejected_at_load(tmp_path: Path) -> None:
    for path in _storage_profile_paths():
        value = json.loads(path.read_text(encoding="utf-8"))
        value["configuration"] = {**value["configuration"], "driftedSetting": True}
        unpinned = tmp_path / f"unpinned-{path.name}"
        unpinned.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(ProfileError, match="configuration digest differs"):
            ProfileRegistry.from_file(unpinned)


@pytest.mark.parametrize(
    ("tamper", "expected_message"),
    [
        ("unknown-profile", "unknown selected profile"),
        ("description-digest-drift", "differ from their machine descriptions"),
        ("capability-drift", "differ from their machine descriptions"),
        ("configuration-digest-drift", "differ from their machine descriptions"),
    ],
)
def test_unknown_unpinned_capability_and_digest_mismatched_pins_fail_before_work(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    tamper: str,
    expected_message: str,
) -> None:
    """A plan pin the machine descriptions do not corroborate must stop
    `run start` before any planning state exists, per the negative-fixture
    families of specification section 15.3."""

    genuine = _portable_local_profiles()
    pins = list(genuine.pins)
    if tamper == "unknown-profile":
        pins[0] = replace(pins[0], profile_id=f"{pins[0].profile_id}-unknown")
    elif tamper == "description-digest-drift":
        pins[0] = replace(pins[0], description_digest=identity_digest({"drift": True}))
    elif tamper == "capability-drift":
        pins[0] = replace(pins[0], capabilities=("undeclared-capability",))
    else:
        pins[0] = replace(pins[0], configuration_digest=identity_digest({"drift": True}))
    request, roots = _seeded_local_run(tmp_path, ProfileSet(tuple(pins)))

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
    assert expected_message in error["message"]
    failure = json.loads(receipt.read_text(encoding="utf-8"))
    assert failure["verdict"] == "failed"
    assert failure["operation"] == "run.start"
    assert not destination.exists()
    assert not Path(roots["controlRepository"]).exists(), "profile rejection must precede planning state"
