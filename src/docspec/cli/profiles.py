"""Inspect installed storage and delivery profile descriptions."""

from __future__ import annotations

import argparse
from pathlib import Path

from docspec.cli.common import _registered_profile
from docspec.cli_io import (
    emit as _emit,
)
from docspec.cli_io import (
    existing_root as _existing_root,
)
from docspec.cli_io import (
    read_bytes as _read_bytes,
)
from docspec.domain.identity import (
    identity_digest,
    sha256_digest,
)
from docspec.profile_registry import BUILTIN_PROFILE_DIRECTORY, ProfileRegistry


def _cmd_profile_verify(args: argparse.Namespace) -> int:
    path = Path(args.profile)
    registered = _registered_profile(path)
    description = registered.description
    payload = _read_bytes(path, label="storage profile")
    _emit(
        {
            "format": "docspec-profile-verification",
            "formatVersion": "1.0",
            "profileId": description.profile_id,
            "role": description.role.value,
            "version": description.version,
            "implementationStatus": registered.implementation_status,
            "fileDigest": sha256_digest(payload),
            "configurationDigest": description.configuration_digest,
            "descriptionDigest": registered.description_digest,
            "verdict": "pass",
        }
    )
    return 0


def _cmd_profile_list(args: argparse.Namespace) -> int:
    directory = _existing_root(args.directory or BUILTIN_PROFILE_DIRECTORY, label="profile directory")
    registry = ProfileRegistry.from_directory(directory)
    profiles = [
        {
            "profileId": item.description.profile_id,
            "role": item.description.role.value,
            "version": item.description.version,
            "implementationStatus": item.implementation_status,
            "implementationModule": item.implementation_module,
            "configurationDigest": item.description.configuration_digest,
            "descriptionDigest": item.description_digest,
            "verifierTestId": item.verifier_test_id,
        }
        for item in registry.list()
    ]
    profile_members = [
        {
            "path": path.name,
            "digest": sha256_digest(_read_bytes(path, label="storage profile")),
        }
        for path in sorted(directory.glob("*.json"))
    ]
    profiles.sort(key=lambda item: (item["role"], item["profileId"], item["version"]))
    _emit(
        {
            "format": "docspec-profile-list",
            "formatVersion": "1.0",
            "profileCount": len(profiles),
            "directory": directory.as_posix(),
            "directoryDigest": identity_digest(profile_members),
            "profiles": profiles,
            "verdict": "pass",
        }
    )
    return 0
