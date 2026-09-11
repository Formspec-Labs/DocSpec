"""DocSpec command blobs: blob verification and bounded collection inventory."""

from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path
from typing import Any

from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
)
from docspec.cli.common import _read_canonical_object
from docspec.cli.requests import _absolute_request_path, _local_storage_for_run_request
from docspec.cli_io import (
    CliError,
)
from docspec.cli_io import (
    emit as _emit,
)
from docspec.cli_io import (
    existing_root as _existing_root,
)
from docspec.cli_io import (
    read_object as _read_json_object,
)
from docspec.domain.identity import (
    stable_urn,
)
from docspec.domain.maintenance import BlobRetentionSet
from docspec.domain.profiles import ProfileRole
from docspec.domain.references import ArtifactRef, BlobRef

_MAX_GC_SAMPLE_COUNT = 1_000


_SHA256_OBJECT = re.compile(r"^[0-9a-f]{64}$")


_SHA256_PREFIX = re.compile(r"^[0-9a-f]{2}$")


def _blob_reader(
    root: Path,
    max_blob_bytes: int,
    stream_chunk_bytes: int,
) -> LocalContentAddressedBlobStore:
    reader = object.__new__(LocalContentAddressedBlobStore)
    reader.root = _existing_root(root, label="blob store root")
    reader.max_blob_bytes = max_blob_bytes
    reader.stream_chunk_bytes = stream_chunk_bytes
    reader._staging = reader.root / ".staging"
    return reader


def _cmd_blob_store_verify(args: argparse.Namespace) -> int:
    reference = BlobRef.from_dict(_read_json_object(args.reference, label="blob reference"))
    _blob_reader(args.root, args.max_blob_bytes, args.stream_chunk_bytes).verify(reference)
    _emit(
        {
            "format": "docspec-blob-verification",
            "formatVersion": "1.0",
            "reference": reference.to_dict(),
            "verdict": "pass",
        }
    )
    return 0


def _cmd_blob_store_gc(args: argparse.Namespace) -> int:
    if not args.dry_run:
        raise CliError("blob-store gc currently requires --dry-run")
    if args.minimum_age_seconds < 0:
        raise CliError("minimum blob retention age must be non-negative")
    if args.sample_limit < 0 or args.sample_limit > _MAX_GC_SAMPLE_COUNT:
        raise CliError(f"blob GC sample limit must be between 0 and {_MAX_GC_SAMPLE_COUNT}")

    run_request_path = _absolute_request_path(args.run_request.as_posix(), label="local run request")
    request, plan, controls, _, records, blobs, _ = _local_storage_for_run_request(
        run_request_path
    )
    retention_reference = ArtifactRef.from_dict(
        _read_canonical_object(args.retention_set, label="blob retention-set reference")
    )
    retention = BlobRetentionSet.from_dict(controls.load(retention_reference))
    if retention.retention_set_id != retention_reference.artifact_id:
        raise CliError("blob retention-set identity differs from its immutable reference")

    record_profile = plan.profiles.for_role(ProfileRole.RECORD_STORAGE)
    if retention.references.profile_id != record_profile.profile_id:
        raise CliError("blob retention layer differs from the local record-storage profile")
    records.verify(retention.references)

    blob_profile = plan.profiles.for_role(ProfileRole.BLOB_STORAGE)
    profile_state = controls.load(retention.blob_profile_state)
    expected_state_fields = {"profileId", "profileVersion", "storageRoot"}
    if set(profile_state) != expected_state_fields:
        raise CliError("blob profile state has an invalid closed shape")
    if (
        profile_state["profileId"] != blob_profile.profile_id
        or profile_state["profileVersion"] != blob_profile.version
    ):
        raise CliError("blob retention set differs from the local blob-storage profile")
    state_root = _absolute_request_path(profile_state["storageRoot"], label="blob profile storage root")
    if _existing_root(state_root, label="blob profile storage root") != blobs.root:
        raise CliError("blob retention set belongs to a different blob-storage root")

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
        request["roots"]["reconciliation"] / "blob-gc",
        max_spooled_bytes=args.max_index_bytes,
        max_record_bytes=4 * 1024,
        cache_kib=args.index_cache_kib,
        read_batch_size=1_024,
    )
    with workspace_factory.create() as retained_index:
        for row in records.stream(retention.references):
            if set(row) != reference_fields:
                raise CliError("blob retention reference has an invalid closed shape")
            if (
                row["blobProfileStateId"] != retention.blob_profile_state.artifact_id
                or row["blobProfileStateDigest"] != retention.blob_profile_state.digest
            ):
                raise CliError("blob retention reference names a different profile state")
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
                raise CliError("blob retention reference identity differs")
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
            raise CliError("blob retention layer stream count differs from its immutable reference")

        object_root = blobs.root / "objects" / "sha256"
        if object_root.exists():
            if object_root.is_symlink() or not object_root.is_dir():
                raise CliError("blob object root must be a regular directory")
            with os.scandir(object_root) as prefixes:
                for prefix_entry in prefixes:
                    if prefix_entry.is_symlink() or not prefix_entry.is_dir(follow_symlinks=False):
                        raise CliError(f"blob object tree has an invalid prefix entry: {prefix_entry.name}")
                    prefix = prefix_entry.name
                    if _SHA256_PREFIX.fullmatch(prefix) is None:
                        raise CliError(f"blob object tree has an invalid digest prefix: {prefix}")
                    with os.scandir(prefix_entry.path) as objects:
                        for object_entry in objects:
                            if object_entry.is_symlink() or not object_entry.is_file(follow_symlinks=False):
                                raise CliError(
                                    f"blob object tree has an invalid object entry: {prefix}/{object_entry.name}"
                                )
                            hexadecimal = object_entry.name
                            locator = f"objects/sha256/{prefix}/{hexadecimal}"
                            if (
                                _SHA256_OBJECT.fullmatch(hexadecimal) is None
                                or prefix != hexadecimal[:2]
                            ):
                                raise CliError(
                                    f"blob object has an invalid content-addressed locator: {locator}"
                                )
                            object_count += 1
                            metadata = object_entry.stat(follow_symlinks=False)
                            if retained_index.lookup_record(collection, locator) is not None:
                                retained_object_count += 1
                                continue
                            age_seconds = max(0, int(now - metadata.st_mtime))
                            if age_seconds < args.minimum_age_seconds:
                                continue
                            candidate_count += 1
                            candidate_byte_count += metadata.st_size
                            if len(candidate_sample) < args.sample_limit:
                                candidate_sample.append(
                                    {
                                        "locator": locator,
                                        "byteSize": metadata.st_size,
                                        "ageSeconds": age_seconds,
                                    }
                                )
    if retained_object_count != retained_reference_count:
        raise CliError("blob object inventory differs from the verified retention set")
    _emit(
        {
            "format": "docspec-blob-gc-dry-run",
            "formatVersion": "1.0",
            "retentionSet": retention_reference.to_dict(),
            "retentionReferenceLayer": retention.references.to_dict(),
            "minimumAgeSeconds": args.minimum_age_seconds,
            "objectCount": object_count,
            "retainedReferenceCount": retained_reference_count,
            "retainedObjectCount": retained_object_count,
            "retainedByteCount": retained_byte_count,
            "candidateCount": candidate_count,
            "candidateByteCount": candidate_byte_count,
            "candidateSampleLimit": args.sample_limit,
            "candidateSampleTruncated": candidate_count > len(candidate_sample),
            "candidateSample": candidate_sample,
            "boundedMembershipIndex": {
                "adapterId": "docspec.local-sqlite-record-workspace",
                "maxSpooledBytes": args.max_index_bytes,
                "cacheKiB": args.index_cache_kib,
            },
            "dryRun": True,
            "verdict": "pass",
        }
    )
    return 0
