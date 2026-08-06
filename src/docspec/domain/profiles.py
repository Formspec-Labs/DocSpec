"""Versioned descriptions and pins for replaceable physical profiles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from docspec.domain.identity import freeze_json, identity_digest, require_sha256, require_text, thaw_json
from docspec.errors import ProfileError


class ProfileRole(StrEnum):
    RELEASE_MANIFEST = "ReleaseManifestProfile"
    DOCUMENT_CATALOG = "DocumentCatalogProfile"
    RECORD_STORAGE = "RecordStorageProfile"
    BLOB_STORAGE = "BlobStorageProfile"
    DOCUMENT_STORE = "DocumentStorePersistenceProfile"
    RESULT_DELIVERY = "ResultDeliveryProfile"


@dataclass(frozen=True, slots=True)
class ProfileDescription:
    role: ProfileRole
    profile_id: str
    version: str
    implementation_id: str
    configuration: dict[str, Any]
    schemas: tuple[str, ...]
    media_types: tuple[str, ...]
    capabilities: tuple[str, ...]
    limits: dict[str, Any]
    requires: tuple[str, ...] = ()
    verifier_id: str = "docspec-profile-verifier/v1"

    def __post_init__(self) -> None:
        for label, value in (
            ("profile_id", self.profile_id),
            ("version", self.version),
            ("implementation_id", self.implementation_id),
            ("verifier_id", self.verifier_id),
        ):
            require_text(value, label)
        if not self.schemas or not self.media_types or not self.capabilities:
            raise ProfileError("a profile must declare schemas, media types, and capabilities")
        if len(set(self.capabilities)) != len(self.capabilities) or tuple(sorted(self.capabilities)) != self.capabilities:
            raise ProfileError("profile capabilities must be sorted and distinct")
        object.__setattr__(self, "configuration", thaw_json(freeze_json(self.configuration, label="profile configuration")))
        object.__setattr__(self, "limits", thaw_json(freeze_json(self.limits, label="profile limits")))

    @property
    def configuration_digest(self) -> str:
        return identity_digest(self.configuration)

    @property
    def description_digest(self) -> str:
        """Identify the complete logical profile description, not only its configuration."""

        return identity_digest(self.to_dict())

    def pin(self, *, description_digest: str | None = None) -> ProfilePin:
        return ProfilePin(
            role=self.role,
            profile_id=self.profile_id,
            version=self.version,
            implementation_id=self.implementation_id,
            configuration_digest=self.configuration_digest,
            description_digest=description_digest or self.description_digest,
            capabilities=self.capabilities,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "profileId": self.profile_id,
            "version": self.version,
            "implementationId": self.implementation_id,
            "configuration": self.configuration,
            "configurationDigest": self.configuration_digest,
            "schemas": list(self.schemas),
            "mediaTypes": list(self.media_types),
            "capabilities": list(self.capabilities),
            "limits": self.limits,
            "requires": list(self.requires),
            "verifierId": self.verifier_id,
        }


@dataclass(frozen=True, slots=True)
class ProfilePin:
    role: ProfileRole
    profile_id: str
    version: str
    implementation_id: str
    configuration_digest: str
    description_digest: str
    capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        for label, value in (
            ("profile_id", self.profile_id),
            ("version", self.version),
            ("implementation_id", self.implementation_id),
        ):
            require_text(value, label)
        require_sha256(self.configuration_digest, "profile configuration digest")
        require_sha256(self.description_digest, "complete profile-description digest")
        if tuple(sorted(set(self.capabilities))) != self.capabilities:
            raise ProfileError("profile pin capabilities must be sorted and distinct")

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "profileId": self.profile_id,
            "version": self.version,
            "implementationId": self.implementation_id,
            "configurationDigest": self.configuration_digest,
            "descriptionDigest": self.description_digest,
            "capabilities": list(self.capabilities),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ProfilePin:
        expected = {
            "role",
            "profileId",
            "version",
            "implementationId",
            "configurationDigest",
            "descriptionDigest",
            "capabilities",
        }
        if set(value) != expected:
            raise ProfileError("profile pin has an invalid closed shape")
        return cls(
            role=ProfileRole(value["role"]),
            profile_id=value["profileId"],
            version=value["version"],
            implementation_id=value["implementationId"],
            configuration_digest=value["configurationDigest"],
            description_digest=value["descriptionDigest"],
            capabilities=tuple(value["capabilities"]),
        )


@dataclass(frozen=True, slots=True)
class ProfileSet:
    pins: tuple[ProfilePin, ...]

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.pins, key=lambda pin: pin.role.value))
        if ordered != self.pins:
            raise ProfileError("profile pins must be sorted by role")
        roles = [pin.role for pin in self.pins]
        if len(set(roles)) != len(roles):
            raise ProfileError("a profile set may contain only one pin per role")
        missing = set(ProfileRole) - set(roles)
        if missing:
            names = ", ".join(sorted(role.value for role in missing))
            raise ProfileError(f"profile set is missing roles: {names}")

    @property
    def profile_set_id(self) -> str:
        from docspec.domain.identity import stable_urn

        return stable_urn("profile-set", [pin.to_dict() for pin in self.pins])

    def for_role(self, role: ProfileRole) -> ProfilePin:
        return next(pin for pin in self.pins if pin.role == role)

    def to_dict(self) -> dict[str, Any]:
        return {"profileSetId": self.profile_set_id, "pins": [pin.to_dict() for pin in self.pins]}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ProfileSet:
        if set(value) != {"profileSetId", "pins"}:
            raise ProfileError("profile set has an invalid closed shape")
        result = cls(tuple(ProfilePin.from_dict(item) for item in value["pins"]))
        if value["profileSetId"] != result.profile_set_id:
            raise ProfileError("profile set identity differs")
        return result
