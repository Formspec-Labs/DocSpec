"""DocSpec commands for blob verification and read-only retention inventory."""

from __future__ import annotations

import argparse
from pathlib import Path

from docspec.adapters.blob_inventory import preview_blob_inventory
from docspec.adapters.storage import LocalContentAddressedBlobStore
from docspec.cli.common import _read_canonical_object
from docspec.cli.requests import _absolute_request_path, _local_storage_for_run_request
from docspec.cli_io import CliError, emit as _emit, existing_root as _existing_root, read_object as _read_json_object
from docspec.domain.references import ArtifactRef, BlobRef


def _blob_reader(
    root: Path,
    max_blob_bytes: int,
    stream_chunk_bytes: int,
) -> LocalContentAddressedBlobStore:
    return LocalContentAddressedBlobStore(
        _existing_root(root, label="blob store root"), max_blob_bytes=max_blob_bytes,
        stream_chunk_bytes=stream_chunk_bytes, create=False,
    )


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
    run_request_path = _absolute_request_path(args.run_request.as_posix(), label="local run request")
    request, plan, controls, _, records, blobs, _ = _local_storage_for_run_request(run_request_path)
    reference = ArtifactRef.from_dict(
        _read_canonical_object(args.retention_set, label="blob retention-set reference")
    )
    _emit(preview_blob_inventory(
        reference, plan=plan, controls=controls, records=records, blobs=blobs,
        index_root=request["roots"]["reconciliation"] / "blob-gc",
        max_index_bytes=args.max_index_bytes, minimum_age_seconds=args.minimum_age_seconds,
        sample_limit=args.sample_limit, index_cache_kib=args.index_cache_kib,
    ))
    return 0
