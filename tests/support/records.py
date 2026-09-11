"""Shared records fixtures, extracted from tests.conformance.test_record_storage_contract."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Callable

from docspec.domain.profiles import ProfileRole
from docspec.ports.record_storage import RecordStorage
from docspec.profile_registry import ProfileRegistry, RegisteredProfile

ROOT = Path(__file__).resolve().parents[2]


def _implementation(registered: RegisteredProfile) -> type:
    module_name, _, attribute = registered.implementation_module.partition(":")
    return getattr(importlib.import_module(module_name), attribute)


def _local_jsonl_storage(registered: RegisteredProfile, root: Path) -> RecordStorage:
    limits = registered.description.limits
    return _implementation(registered)(
        root / "records",
        max_member_bytes=limits["maxMemberBytes"],
        max_record_bytes=limits["maxRecordBytes"],
        max_root_bytes=limits["maxRootBytes"],
        max_open_members=limits["maxOpenMembers"],
        max_merge_scratch_bytes=limits["maxMergeScratchBytes"],
    )


# Constructing an adapter from its machine description is the one thing the
# description cannot carry itself, so each registered record profile names a
# factory here. A newly registered profile fails the coverage check below
# until it joins this table and passes the same fixture.
_FACTORIES: dict[str, Callable[[RegisteredProfile, Path], RecordStorage]] = {
    "docspec.record-storage.local-jsonl.v1": _local_jsonl_storage,
}


def _registered_record_profiles() -> tuple[RegisteredProfile, ...]:
    profiles = ProfileRegistry.from_directory(ROOT / "profiles").list(ProfileRole.RECORD_STORAGE)
    assert profiles
    assert {item.description.implementation_id for item in profiles} == set(_FACTORIES), (
        "a registered record profile has no conformance factory"
    )
    return profiles
