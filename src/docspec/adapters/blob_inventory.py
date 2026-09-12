"""Read-only inventory of local content-addressed blobs against saved roots."""

from __future__ import annotations

import os
import re
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.adapters.storage import LocalContentAddressedBlobStore
from docspec.domain.identity import stable_urn
from docspec.domain.maintenance import BlobRetentionSet
from docspec.domain.plans import ProcessingPlan
from docspec.domain.profiles import ProfileRole
from docspec.domain.references import ArtifactRef, BlobRef
from docspec.errors import IntegrityError
from docspec.ports.control_repository import ControlRepository
from docspec.ports.record_storage import RecordStorage

_MAX_GC_SAMPLE_COUNT = 1_000
_SHA256_OBJECT = re.compile(r"^[0-9a-f]{64}$")
_SHA256_PREFIX = re.compile(r"^[0-9a-f]{2}$")


def preview_blob_inventory(
    retention_reference: ArtifactRef,
    *,
    plan: ProcessingPlan,
    controls: ControlRepository,
    records: RecordStorage,
    blobs: LocalContentAddressedBlobStore,
    index_root: Path,
    max_index_bytes: int,
    minimum_age_seconds: int = 0,
    sample_limit: int = 20,
    index_cache_kib: int = 8192,
) -> dict[str, Any]:
    """Verify all retained bytes before reporting unreferenced local objects.

    This is an observation for explicit immutable roots, never deletion
    authority. Concurrent writes can change the inventory after it is read.
    """
    if type(minimum_age_seconds) is not int or minimum_age_seconds < 0:
        raise ValueError("minimum blob retention age must be a non-negative integer")
    if type(sample_limit) is not int or not 0 <= sample_limit <= _MAX_GC_SAMPLE_COUNT:
        raise ValueError(f"blob GC sample limit must be between 0 and {_MAX_GC_SAMPLE_COUNT}")
    retention = BlobRetentionSet.from_dict(controls.load(retention_reference))
    if retention.retention_set_id != retention_reference.artifact_id:
        raise IntegrityError("blob retention-set identity differs from its immutable reference")

    record_profile = plan.profiles.for_role(ProfileRole.RECORD_STORAGE)
    if retention.references.profile_id != record_profile.profile_id:
        raise IntegrityError("blob retention layer differs from the local record-storage profile")
    records.verify(retention.references)

    blob_profile = plan.profiles.for_role(ProfileRole.BLOB_STORAGE)
    profile_state = controls.load(retention.blob_profile_state)
    expected_state_fields = {"profileId", "profileVersion", "storageRoot"}
    if set(profile_state) != expected_state_fields:
        raise IntegrityError("blob profile state has an invalid closed shape")
    if (
        profile_state["profileId"] != blob_profile.profile_id
        or profile_state["profileVersion"] != blob_profile.version
    ):
        raise IntegrityError("blob retention set differs from the local blob-storage profile")
    state_root = Path(profile_state["storageRoot"])
    if (not state_root.is_absolute() or state_root.is_symlink() or not state_root.is_dir()
            or state_root.resolve(strict=True) != blobs.root):
        raise IntegrityError("blob retention set belongs to a different blob-storage root")

    reference_fields = {
        "recordId",
        "blobProfileStateId",
        "blobProfileStateDigest",
        "locator",
        "digest",
        "byteSize",
        "mediaType",
    }
    collection = "blob-gc:retained-locators"
    retained_reference_count = 0
    retained_byte_count = 0
    object_count = 0
    retained_object_count = 0
    candidate_count = 0
    candidate_byte_count = 0
    candidate_sample: list[dict[str, Any]] = []
    now = time.time()
    workspace_factory = LocalSqliteReconciliationWorkspaceFactory(
        index_root,
        max_spooled_bytes=max_index_bytes,
        max_record_bytes=4 * 1024,
        cache_kib=index_cache_kib,
        read_batch_size=1_024,
    )
    with workspace_factory.create() as retained_index, closing(records.stream(retention.references)) as rows:
        for row in rows:
            if set(row) != reference_fields:
                raise IntegrityError("blob retention reference has an invalid closed shape")
            if (
                row["blobProfileStateId"] != retention.blob_profile_state.artifact_id
                or row["blobProfileStateDigest"] != retention.blob_profile_state.digest
            ):
                raise IntegrityError("blob retention reference names a different profile state")
            reference = BlobRef(
                row["locator"],
                row["digest"],
                row["byteSize"],
                row["mediaType"],
            )
            expected_record_id = stable_urn(
                "blob-retention-reference",
                {
                    "blobProfileState": retention.blob_profile_state.to_dict(),
                    "locator": reference.locator,
                },
            )
            if row["recordId"] != expected_record_id:
                raise IntegrityError("blob retention reference identity differs")
            blobs.verify(reference)
            retained_index.add_record(
                collection,
                identity=reference.locator,
                source_item_id=reference.locator,
                record=reference.to_dict(),
            )
            retained_reference_count += 1
            retained_byte_count += reference.byte_size
        if retained_reference_count != retention.references.record_count:
            raise IntegrityError("blob retention layer stream count differs from its immutable reference")

        object_root = blobs.root / "objects" / "sha256"
        if object_root.exists():
            if object_root.is_symlink() or not object_root.is_dir():
                raise IntegrityError("blob object root must be a regular directory")
            with os.scandir(object_root) as prefixes:
                for prefix_entry in prefixes:
                    if prefix_entry.is_symlink() or not prefix_entry.is_dir(follow_symlinks=False):
                        raise IntegrityError(f"blob object tree has an invalid prefix entry: {prefix_entry.name}")
                    prefix = prefix_entry.name
                    if _SHA256_PREFIX.fullmatch(prefix) is None:
                        raise IntegrityError(f"blob object tree has an invalid digest prefix: {prefix}")
                    with os.scandir(prefix_entry.path) as objects:
                        for object_entry in objects:
                            if object_entry.is_symlink() or not object_entry.is_file(follow_symlinks=False):
                                raise IntegrityError(
                                    f"blob object tree has an invalid object entry: {prefix}/{object_entry.name}"
                                )
                            hexadecimal = object_entry.name
                            locator = f"objects/sha256/{prefix}/{hexadecimal}"
                            if (
                                _SHA256_OBJECT.fullmatch(hexadecimal) is None
                                or prefix != hexadecimal[:2]
                            ):
                                raise IntegrityError(
                                    f"blob object has an invalid content-addressed locator: {locator}"
                                )
                            object_count += 1
                            metadata = object_entry.stat(follow_symlinks=False)
                            if retained_index.lookup_record(collection, locator) is not None:
                                retained_object_count += 1
                                continue
                            age_seconds = max(0, int(now - metadata.st_mtime))
                            if age_seconds < minimum_age_seconds:
                                continue
                            candidate_count += 1
                            candidate_byte_count += metadata.st_size
                            if len(candidate_sample) < sample_limit:
                                candidate_sample.append(
                                    {
                                        "locator": locator,
                                        "byteSize": metadata.st_size,
                                        "ageSeconds": age_seconds,
                                    }
                                )
    if retained_object_count != retained_reference_count:
        raise IntegrityError("blob object inventory differs from the verified retention set")
    return {
        "format": "docspec-blob-gc-dry-run",
        "formatVersion": "1.0",
        "scope": "relative-to-supplied-retention-set",
        "retentionSet": retention_reference.to_dict(),
        "retentionReferenceLayer": retention.references.to_dict(),
        "minimumAgeSeconds": minimum_age_seconds,
        "objectCount": object_count,
        "retainedReferenceCount": retained_reference_count,
        "retainedObjectCount": retained_object_count,
        "retainedByteCount": retained_byte_count,
        "candidateCount": candidate_count,
        "candidateByteCount": candidate_byte_count,
        "candidateSampleLimit": sample_limit,
        "candidateSampleTruncated": candidate_count > len(candidate_sample),
        "candidateSample": candidate_sample,
        "boundedMembershipIndex": {
            "adapterId": "docspec.local-sqlite-record-workspace",
            "maxSpooledBytes": max_index_bytes,
            "cacheKiB": index_cache_kib,
        },
        "dryRun": True,
        "verdict": "pass",
    }
