"""DocSpec commands for inspecting retained results and selecting current state."""

from __future__ import annotations

import argparse
from collections import Counter

from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.cli.common import (
    _document_release_producer,
    _release_reference,
    _require_new_output_paths,
    _write_artifact_and_receipt,
)
from docspec.cli.requests import _absolute_request_path, _local_storage_for_run_request
from docspec.cli_io import CliError, read_object
from docspec.domain.identity import canonical_json_file_bytes
from docspec.domain.references import DocumentReleaseRef
from docspec.cli_io import (
    emit as _emit,
)
from docspec.cli_io import (
    existing_root as _existing_root,
)


def _local_document_catalog(args: argparse.Namespace) -> LocalManifestDocumentCatalog:
    blobs = object.__new__(LocalContentAddressedBlobStore)
    blobs.root = _existing_root(args.blob_root, label="blob storage root")
    blobs.max_blob_bytes = 8 * 1024**3
    blobs.stream_chunk_bytes = 1024**2
    records = object.__new__(LocalJsonlRecordStorage)
    records.root = _existing_root(args.record_root, label="record storage root")
    records.max_member_bytes = 256 * 1024**2
    records.max_record_bytes = 8 * 1024**2
    records.max_root_bytes = 32 * 1024**2
    records.max_open_members = 64
    records.max_merge_scratch_bytes = 128 * 1024**3
    records.merge_scratch_root = None
    records.last_write_peak_open_members = 0
    records.last_read_peak_open_members = 0
    records._staging = records.root / ".staging"
    stores = object.__new__(LocalDocumentStoreRepository)
    stores.root = _existing_root(args.store_root, label="document store root")
    stores.max_revision_bytes = 64 * 1024**2
    stores.max_inline_bytes = 1024**2
    stores.max_plan_ledger_bytes = 4 * 1024**3
    stores.max_plan_record_bytes = 64 * 1024
    stores.max_plan_store_count = 10_000_000
    stores._verification_scratch = None
    controls = object.__new__(LocalJsonControlRepository)
    controls.root = _existing_root(args.control_root, label="control repository root")
    controls.max_artifact_bytes = 8 * 1024**2
    return LocalManifestDocumentCatalog(
        _existing_root(args.catalog_root, label="document catalog root"),
        records=records,
        stores=stores,
        controls=controls,
        producer=_document_release_producer(
            args.implementation_id,
            args.verifier_implementation_id,
        ),
        blobs=blobs,
    )


def _cmd_document_catalog_open(args: argparse.Namespace) -> int:
    reference = _release_reference(args.reference)
    release = _local_document_catalog(args).open(reference)
    _emit(
        {
            "format": "docspec-document-catalog-open-result",
            "formatVersion": "1.0",
            "reference": reference.to_dict(),
            "logicalStateDigest": release.logical_state_digest,
            "release": release.to_dict(),
            "verdict": "pass",
        }
    )
    return 0


def _cmd_document_catalog_compare(args: argparse.Namespace) -> int:
    older = _release_reference(args.older_reference)
    newer = _release_reference(args.newer_reference)
    counts: Counter[str] = Counter()
    sample: list[dict[str, str]] = []
    for record_id, change in _local_document_catalog(args).compare(older, newer, layer_kind=args.layer_kind):
        counts[change] += 1
        if len(sample) < args.sample_limit:
            sample.append({"recordId": record_id, "change": change})
    change_count = sum(counts.values())
    _emit(
        {
            "format": "docspec-document-catalog-comparison",
            "formatVersion": "1.0",
            "olderRelease": older.to_dict(),
            "newerRelease": newer.to_dict(),
            "layerKind": args.layer_kind,
            "changeCount": change_count,
            "changeCounts": dict(sorted(counts.items())),
            "sample": sample,
            "sampleTruncated": change_count > len(sample),
            "verdict": "pass",
        }
    )
    return 0


def _cmd_document_catalog_select(args: argparse.Namespace) -> int:
    _require_new_output_paths(args.destination, args.receipt)
    value = read_object(args.request, label="document catalog selection request")
    fields = {"format", "formatVersion", "runRequest", "release", "expectedCurrent"}
    if set(value) != fields:
        raise CliError("document catalog selection request has an invalid closed shape")
    if value["format"] != "docspec-local-catalog-select-request" or value["formatVersion"] != "1.0":
        raise CliError("document catalog selection request has an unknown format")
    run_request = _absolute_request_path(value["runRequest"], label="local run request")
    reference = DocumentReleaseRef.from_dict(value["release"])
    expected_current = (
        None if value["expectedCurrent"] is None else DocumentReleaseRef.from_dict(value["expectedCurrent"])
    )
    _, _, _, _, _, _, catalog = _local_storage_for_run_request(run_request)
    selected = catalog.select(reference, expected_current=expected_current)
    receipt = _write_artifact_and_receipt(
        operation=args.operation,
        request_path=args.request,
        destination=args.destination,
        receipt_path=args.receipt,
        artifact_id=selected.release_id,
        payload=canonical_json_file_bytes(selected.to_dict()),
    )
    _emit(receipt)
    return 0
