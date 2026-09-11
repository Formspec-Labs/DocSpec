"""DocSpec command plans: processing plans and document jobs."""

from __future__ import annotations

import argparse
from pathlib import Path

from docspec.adapters.storage import (
    LocalDocumentStoreRepository,
)
from docspec.cli.common import _write_artifact_and_receipt
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
    read_bytes as _read_bytes,
)
from docspec.cli_io import (
    read_object as _read_json_object,
)
from docspec.domain.identity import (
    canonical_json_file_bytes,
    parse_canonical_json,
    sha256_digest,
    thaw_json,
)
from docspec.domain.jobs import DocumentEntry, DocumentStore
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import DataUsePolicy, RetentionPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.profiles import ProfileSet
from docspec.domain.references import DocumentReleaseRef, SourceCatalogRef, StoreRef


def _cmd_plan_create(args: argparse.Namespace) -> int:
    request = _read_json_object(args.request, label="plan creation request")
    expected = {
        "sourceCatalog",
        "baseRelease",
        "profiles",
        "limits",
        "stages",
        "processors",
        "partitionCount",
        "selection",
        "retentionPolicy",
        "dataUsePolicy",
        "retryPolicyDigest",
        "acceptedFailurePolicyDigest",
    }
    if set(request) != expected:
        raise CliError("plan creation request has an invalid closed shape")
    plan = ProcessingPlan.create(
        source_catalog=SourceCatalogRef.from_dict(request["sourceCatalog"]),
        base_release=None
        if request["baseRelease"] is None
        else DocumentReleaseRef.from_dict(request["baseRelease"]),
        profiles=ProfileSet.from_dict(request["profiles"]),
        limits=WorkLimits.from_dict(request["limits"]),
        stages=StagePolicy.from_dict(request["stages"]),
        processors=ProcessorSet.from_dict(request["processors"]),
        partition_count=request["partitionCount"],
        selection=request["selection"],
        retention_policy=RetentionPolicy.from_dict(request["retentionPolicy"]),
        data_use_policy=DataUsePolicy.from_dict(request["dataUsePolicy"]),
        retry_policy_digest=request["retryPolicyDigest"],
        accepted_failure_policy_digest=request["acceptedFailurePolicyDigest"],
    )
    payload = canonical_json_file_bytes(plan.to_dict())
    receipt = _write_artifact_and_receipt(
        operation="plan.create",
        request_path=args.request,
        destination=args.destination,
        receipt_path=args.receipt,
        artifact_id=plan.plan_id,
        payload=payload,
    )
    _emit(receipt)
    return 0


def _cmd_document_store_create(args: argparse.Namespace) -> int:
    request = _read_json_object(args.request, label="document store creation request")
    if set(request) != {"planId", "logicalPartition", "entries", "limits"}:
        raise CliError("document store creation request has an invalid closed shape")
    store = DocumentStore.planned(
        plan_id=request["planId"],
        logical_partition=request["logicalPartition"],
        entries=tuple(DocumentEntry.from_dict(item) for item in request["entries"]),
        limits=WorkLimits.from_dict(request["limits"]),
    )
    payload = canonical_json_file_bytes(store.to_dict())
    receipt = _write_artifact_and_receipt(
        operation="document-store.create",
        request_path=args.request,
        destination=args.destination,
        receipt_path=args.receipt,
        artifact_id=store.store_id,
        payload=payload,
    )
    _emit(receipt)
    return 0


def _cmd_document_store_verify(args: argparse.Namespace) -> int:
    path = Path(args.store)
    payload = _read_bytes(path, label="document store")
    value = thaw_json(parse_canonical_json(payload, label="document store"))
    if not isinstance(value, dict):
        raise CliError("document store must be a JSON object")
    if value.get("format") == "docspec-saved-document-store":
        if args.root is None:
            raise CliError("a saved document store root requires --root for member verification")
        if min(args.max_revision_bytes, args.max_inline_bytes) <= 0:
            raise CliError("document store verification byte limits must be positive")
        if args.max_inline_bytes > args.max_revision_bytes:
            raise CliError("document store inline limit must not exceed its revision limit")
        root = _existing_root(args.root, label="document store repository root")
        try:
            locator = path.resolve(strict=True).relative_to(root).as_posix()
        except ValueError as error:
            raise CliError("saved document store is outside its repository root") from error
        repository = object.__new__(LocalDocumentStoreRepository)
        repository.root = root
        repository.max_revision_bytes = args.max_revision_bytes
        repository.max_inline_bytes = args.max_inline_bytes
        reference = StoreRef(
            value.get("storeId"),
            value.get("revision"),
            locator,
            sha256_digest(payload),
        )
        store = repository.load(reference)
        verification_scope = "saved-store-root-and-ledger-members"
    else:
        store = DocumentStore.from_dict(value)
        verification_scope = "canonical-store-root"
    _emit(
        {
            "format": "docspec-document-store-verification",
            "formatVersion": "1.0",
            "storeId": store.store_id,
            "revision": store.revision,
            "state": store.state.value,
            "verdict": None if store.verdict is None else store.verdict.value,
            "entryCount": len(store.entries),
            "terminalEntryCount": sum(entry.terminal for entry in store.entries),
            "receiptDigest": None if store.state.value != "sealed" else store.receipt_digest,
            "verificationScope": verification_scope,
            "verificationVerdict": "structurally-valid",
        }
    )
    return 0
