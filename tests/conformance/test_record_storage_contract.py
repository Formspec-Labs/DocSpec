from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from docspec.domain.profiles import ProfileRole
from docspec.domain.storage import PartitionPolicy, RecordSchema
from docspec.errors import IntegrityError
from docspec.ports.record_storage import RecordStorage
from docspec.profile_registry import ProfileRegistry, RegisteredProfile

ROOT = Path(__file__).resolve().parents[2]

# One shared logical layer fixture: every registered record profile must
# expose exactly these records through the same port surface.
SCHEMA = RecordSchema(
    "docspec-conformance-record/1.0",
    ("recordId", "sourceItemId", "value"),
    "recordId",
    "sourceItemId",
)
POLICY = PartitionPolicy("source-item-sha256-v1", 8)
BASE_RECORDS = (
    {"recordId": "alpha", "sourceItemId": "source-alpha", "value": 1},
    {"recordId": "bravo", "sourceItemId": "source-bravo", "value": 2},
    {"recordId": "charlie", "sourceItemId": "source-charlie", "value": 3},
    {"recordId": "prune-me", "sourceItemId": "source-prune", "value": 4},
)


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


def _bucket(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big") % POLICY.bucket_count


def _registered_record_profiles() -> tuple[RegisteredProfile, ...]:
    profiles = ProfileRegistry.from_directory(ROOT / "profiles").list(ProfileRole.RECORD_STORAGE)
    assert profiles
    assert {item.description.implementation_id for item in profiles} == set(_FACTORIES), (
        "a registered record profile has no conformance factory"
    )
    return profiles


def test_every_registered_record_profile_passes_the_shared_layer_contract(tmp_path: Path) -> None:
    pruned_partition = _bucket("source-prune")
    replacement = tuple(
        {**record, "value": record["value"] * 10}
        for record in BASE_RECORDS
        if _bucket(record["sourceItemId"]) == pruned_partition and record["recordId"] != "prune-me"
    )
    survivors = tuple(
        record
        for record in BASE_RECORDS
        if _bucket(record["sourceItemId"]) != pruned_partition
    ) + replacement

    for registered in _registered_record_profiles():
        storage = _FACTORIES[registered.description.implementation_id](
            registered,
            tmp_path / registered.description.implementation_id,
        )
        base = storage.write_layer(
            BASE_RECORDS,
            layer_kind="conformance-records",
            schema=SCHEMA,
            partition_policy=POLICY,
        )
        storage.verify(base)
        assert base.profile_id == registered.description.profile_id
        assert base.record_count == len(BASE_RECORDS)
        assert storage.schema(base) == SCHEMA
        assert storage.identity_field(base) == "recordId"
        assert storage.partition_policy(base) == POLICY

        assert list(storage.stream(base)) == sorted(BASE_RECORDS, key=lambda record: record["recordId"])
        assert list(storage.stream(base, partitions=frozenset({pruned_partition}))) == sorted(
            (record for record in BASE_RECORDS if _bucket(record["sourceItemId"]) == pruned_partition),
            key=lambda record: record["recordId"],
        )
        assert list(storage.scan_partition_value(base, "source-alpha")) == [BASE_RECORDS[0]]
        assert storage.lookup(base, "bravo") == BASE_RECORDS[1]
        assert storage.lookup(base, "bravo", partition_value="source-bravo") == BASE_RECORDS[1]
        assert storage.lookup(base, "absent") is None

        updated = storage.write_layer(
            replacement,
            layer_kind="conformance-records",
            schema=SCHEMA,
            partition_policy=POLICY,
            base=base,
            replace_partitions=frozenset({pruned_partition}),
        )
        storage.verify(updated)
        assert list(storage.stream(updated)) == sorted(survivors, key=lambda record: record["recordId"])
        assert storage.lookup(updated, "prune-me") is None, "a replaced partition must prune its removed records"
        assert storage.lookup(base, "prune-me") == BASE_RECORDS[3], "the base layer must stay immutable"

        base_members = {
            item["partition"]: item["path"]
            for item in json.loads((storage.root / base.state_ref).read_text(encoding="utf-8"))["members"]
        }
        updated_members = {
            item["partition"]: item["path"]
            for item in json.loads((storage.root / updated.state_ref).read_text(encoding="utf-8"))["members"]
        }
        for partition in base_members.keys() - {pruned_partition}:
            assert updated_members[partition] == base_members[partition], (
                "an untouched partition must be reused, not rewritten"
            )


def test_every_registered_record_profile_rejects_unordered_and_duplicate_identities(tmp_path: Path) -> None:
    for registered in _registered_record_profiles():
        storage = _FACTORIES[registered.description.implementation_id](
            registered,
            tmp_path / registered.description.implementation_id,
        )
        with pytest.raises(IntegrityError, match="strictly ordered"):
            storage.write_layer(
                (BASE_RECORDS[1], BASE_RECORDS[0]),
                layer_kind="conformance-records",
                schema=SCHEMA,
                partition_policy=POLICY,
            )
        with pytest.raises(IntegrityError, match="strictly ordered"):
            storage.write_layer(
                (BASE_RECORDS[0], BASE_RECORDS[0]),
                layer_kind="conformance-records",
                schema=SCHEMA,
                partition_policy=POLICY,
            )


def test_every_registered_record_profile_fails_closed_on_tampered_member_bytes(tmp_path: Path) -> None:
    for registered in _registered_record_profiles():
        root = tmp_path / registered.description.implementation_id
        storage = _FACTORIES[registered.description.implementation_id](registered, root)
        layer = storage.write_layer(
            BASE_RECORDS,
            layer_kind="conformance-records",
            schema=SCHEMA,
            partition_policy=POLICY,
        )
        storage.verify(layer)
        tampered = [
            path
            for path in sorted(root.rglob("*"))
            if path.is_file() and b'"recordId":"prune-me"' in path.read_bytes()
        ]
        assert tampered, "the fixture record must be findable in exactly the persisted member bytes"
        for path in tampered:
            path.write_bytes(path.read_bytes().replace(b'"value":4', b'"value":5'))
        with pytest.raises(IntegrityError):
            storage.verify(layer)
