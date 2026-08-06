"""Load, verify, and select versioned physical profiles without hard-wiring formats."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docspec.domain.identity import identity_digest, parse_closed_json, thaw_json
from docspec.domain.profiles import ProfileDescription, ProfileRole, ProfileSet
from docspec.errors import ProfileError

_FIELDS = {
    "format",
    "formatVersion",
    "profileId",
    "role",
    "version",
    "implementationStatus",
    "implementationId",
    "implementationModule",
    "configuration",
    "configurationDigest",
    "logicalSchemas",
    "physicalMediaTypes",
    "capabilities",
    "limits",
    "compatibility",
    "verifier",
}


def _description_identity(value: dict[str, Any]) -> dict[str, Any]:
    """Return the executable profile fields; mutable evidence status is not identity-bearing."""

    return {
        key: value[key]
        for key in (
            "format",
            "formatVersion",
            "profileId",
            "role",
            "version",
            "implementationId",
            "implementationModule",
            "configuration",
            "configurationDigest",
            "logicalSchemas",
            "physicalMediaTypes",
            "capabilities",
            "limits",
            "compatibility",
        )
    } | {"verifierTestId": value["verifier"]["testId"]}


@dataclass(frozen=True, slots=True)
class RegisteredProfile:
    description: ProfileDescription
    description_digest: str
    implementation_status: str
    implementation_module: str | None
    profile_set_id: str
    verifier_status: str
    verifier_test_id: str


class ProfileRegistry:
    def __init__(self, profiles: tuple[RegisteredProfile, ...]) -> None:
        by_id = {item.description.profile_id: item for item in profiles}
        if len(by_id) != len(profiles):
            raise ProfileError("profile registry contains duplicate profile identities")
        self._profiles = tuple(sorted(profiles, key=lambda item: item.description.profile_id))
        self._by_id = by_id

    @classmethod
    def from_directory(cls, root: Path) -> ProfileRegistry:
        root = Path(root)
        if not root.is_dir() or root.is_symlink():
            raise ProfileError(f"profile directory is missing or unsafe: {root}")
        files = sorted(root.glob("*.json"))
        if not files:
            raise ProfileError("profile directory contains no JSON descriptions")
        return cls(tuple(cls.from_file(path) for path in files))

    @staticmethod
    def from_file(path: Path) -> RegisteredProfile:
        """Load and verify one closed machine-readable profile description."""

        path = Path(path)
        if not path.is_file() or path.is_symlink():
            raise ProfileError(f"profile file is missing or unsafe: {path}")
        value = thaw_json(parse_closed_json(path.read_bytes(), label=path.name))
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise ProfileError(f"{path.name} has an invalid closed profile shape")
        if value["format"] != "docspec-storage-profile" or value["formatVersion"] != "1.0":
            raise ProfileError(f"{path.name} has an unknown profile format")
        if value["implementationStatus"] not in {"specified", "implemented"}:
            raise ProfileError(f"{path.name} has an unknown implementation status")
        module = value["implementationModule"]
        if module is not None and (not isinstance(module, str) or not module):
            raise ProfileError(f"{path.name} has an invalid implementation module")
        configuration = value["configuration"]
        if not isinstance(configuration, dict) or identity_digest(configuration) != value["configurationDigest"]:
            raise ProfileError(f"{path.name} configuration digest differs")
        capabilities = value["capabilities"]
        if (
            not isinstance(capabilities, dict)
            or not capabilities
            or any(not isinstance(name, str) or not isinstance(enabled, bool) for name, enabled in capabilities.items())
        ):
            raise ProfileError(f"{path.name} has invalid capabilities")
        compatibility = value["compatibility"]
        if not isinstance(compatibility, dict) or set(compatibility) != {"profileSetId", "requires"}:
            raise ProfileError(f"{path.name} has invalid compatibility")
        verifier = value["verifier"]
        if not isinstance(verifier, dict) or set(verifier) != {"status", "testId"}:
            raise ProfileError(f"{path.name} has an invalid verifier")
        description = ProfileDescription(
            role=ProfileRole(value["role"]),
            profile_id=value["profileId"],
            version=value["version"],
            implementation_id=value["implementationId"],
            configuration=configuration,
            schemas=tuple(value["logicalSchemas"]),
            media_types=tuple(value["physicalMediaTypes"]),
            capabilities=tuple(sorted(name for name, enabled in capabilities.items() if enabled)),
            limits=value["limits"],
            requires=tuple(value["compatibility"]["requires"]),
        )
        if description.configuration_digest != value["configurationDigest"]:
            raise ProfileError(f"{path.name} configuration pin differs")
        return RegisteredProfile(
            description,
            identity_digest(_description_identity(value)),
            value["implementationStatus"],
            module,
            compatibility["profileSetId"],
            verifier["status"],
            verifier["testId"],
        )

    def list(self, role: ProfileRole | None = None) -> tuple[RegisteredProfile, ...]:
        return tuple(item for item in self._profiles if role is None or item.description.role == role)

    def select(self, profile_ids: tuple[str, ...]) -> ProfileSet:
        try:
            selected = tuple(self._by_id[identifier] for identifier in profile_ids)
        except KeyError as error:
            raise ProfileError(f"unknown selected profile {error.args[0]}") from error
        if any(item.implementation_status != "implemented" for item in selected):
            raise ProfileError("a selected profile is specified but not implemented")
        selected_ids = set(profile_ids)
        for item in selected:
            missing = set(item.description.requires) - selected_ids
            if missing:
                raise ProfileError(
                    f"profile {item.description.profile_id} is missing required profiles: {sorted(missing)}"
                )
        return ProfileSet(
            tuple(
                sorted(
                    (
                        item.description.pin(description_digest=item.description_digest)
                        for item in selected
                    ),
                    key=lambda pin: pin.role.value,
                )
            )
        )

    def to_inventory(self) -> list[dict[str, Any]]:
        return [
            {
                "profile": item.description.to_dict(),
                "descriptionDigest": item.description_digest,
                "implementationStatus": item.implementation_status,
                "implementationModule": item.implementation_module,
                "profileSetId": item.profile_set_id,
                "verifier": {"status": item.verifier_status, "testId": item.verifier_test_id},
            }
            for item in self._profiles
        ]
