"""DocSpec command profiles: profile inspection and scale-profile sealing."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from docspec.cli.common import _registered_profile, _write_artifact_and_receipt
from docspec.cli_io import (
    emit as _emit,
)
from docspec.cli_io import (
    existing_root as _existing_root,
)
from docspec.cli_io import (
    read_bytes as _read_bytes,
)
from docspec.cli_io import (
    read_object as _read_json_object,
)
from docspec.domain.identity import (
    identity_digest,
    sha256_digest,
)
from docspec.domain.scale import ScaleProfile
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
            "verifier": {"status": item.verifier_status, "testId": item.verifier_test_id},
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


def _cmd_scale_profile_seal(args: argparse.Namespace) -> int:
    profile = ScaleProfile.from_content_dict(
        _read_json_object(args.request, label="scale profile content")
    )
    receipt = _write_artifact_and_receipt(
        operation="scale-profile.seal",
        request_path=args.request,
        destination=args.destination,
        receipt_path=args.receipt,
        artifact_id=profile.profile_id,
        payload=profile.to_bytes(),
    )
    _emit(receipt)
    return 0


def _cmd_scale_profile_verify(args: argparse.Namespace) -> int:
    profile = ScaleProfile.from_bytes(_read_bytes(args.profile, label="scale profile"))
    content: dict[str, Any] = {
        "format": "docspec-scale-profile-verification",
        "formatVersion": "1.0",
        "profileId": profile.profile_id,
        "profileDigest": profile.digest,
        "workloadKind": profile.workload_kind.value,
    }
    document = profile.document_processing_workload
    catalog = profile.catalog_workload
    if document is not None:
        content.update(
            {
                "unitCount": document.targets.unit_count,
                "processorTargetCount": len(document.targets.processor_targets),
            }
        )
    elif catalog is not None:
        content.update(
            {
                "sourceNativeInputCount": len(catalog.source_native_inputs),
                "maxSourceRecordCount": catalog.ceilings.max_source_record_count,
            }
        )
    content["verdict"] = "pass"
    _emit(content)
    return 0
