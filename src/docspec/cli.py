"""One operator entry point for the standalone DocSpec lifecycle."""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rulespec_artifacts import ArtifactVerificationError, Producer

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.adapters.execution import LocalExecutionBackend
from docspec.adapters.processor_cache import LocalSqliteProcessorResultCache
from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.adapters.sinks import DurableDatasetSink
from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.adapters.source_catalog_artifact import SourceCatalogArtifactReader
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.application.commit import ReleaseCommitService
from docspec.application.delivery import StoreDeliveryService
from docspec.application.execution import StoreExecutionService
from docspec.application.maintenance import ReleaseCompactionService
from docspec.application.planner import RunPlanner
from docspec.application.reconcile import RunReconciler
from docspec.application.store_state import load_latest_store
from docspec.conformance import run_conformance, summarize_report
from docspec.domain.identity import (
    canonical_json_file_bytes,
    identity_digest,
    parse_canonical_json,
    parse_closed_json,
    sha256_digest,
    stable_urn,
    thaw_json,
)
from docspec.domain.execution import (
    EXECUTE_AND_DELIVER_OPERATION_ID,
    MAX_RESULT_BYTES,
    ExecutionHandoff,
    ExecutionLimits,
    ExecutionProfile,
    StoreTask,
    StoreTaskResult,
    iter_store_tasks,
    summarize_store_tasks,
)
from docspec.domain.jobs import DocumentEntry, DocumentStore, FailureClass, StoreState
from docspec.domain.maintenance import BlobRetentionSet, ReleaseCompactionReceipt
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, DataUsePolicy, RetentionPolicy, RetryPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.profiles import ProfileRole, ProfileSet
from docspec.domain.receipts import DeliveryReceipt, RunReceipt
from docspec.domain.references import ArtifactRef, BlobRef, DocumentReleaseRef, SourceCatalogRef, StoreRef
from docspec.domain.release import DocumentRelease
from docspec.domain.scale import ScaleProfile
from docspec.domain.security import redact, redact_text, require_secret_free
from docspec.domain.storage import PartitionPolicy
from docspec.errors import DocSpecError
from docspec.processing.extraction import DefaultExtractorRegistry
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.processing.segmentation import DefaultSegmenterRegistry
from docspec.ports.content_fetcher import ContentFetcher
from docspec.ports.source_catalog import ImmutableSourceCatalogReader
from docspec.profile_registry import ProfileRegistry, RegisteredProfile
from docspec.source_catalog_cli import add_source_catalog_command

_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_GC_SAMPLE_COUNT = 1_000
_SHA256_OBJECT = re.compile(r"^[0-9a-f]{64}$")
_SHA256_PREFIX = re.compile(r"^[0-9a-f]{2}$")


class CliError(DocSpecError):
    """The requested operator action failed preflight or verification."""


def _producer_record(value: object, *, label: str) -> Producer:
    try:
        return Producer.from_dict(value, path=f"$/{label}")
    except ArtifactVerificationError as error:
        raise CliError(f"{label} is invalid: {error}") from error


def _document_release_producer(
    implementation_id: str,
    verifier_implementation_id: str,
) -> Producer:
    return _producer_record(
        {
            "product": "docspec",
            "implementationId": implementation_id,
            "verifierId": "urn:docspec:verifier:document-release",
            "verifierVersion": "1.0.0",
            "verifierImplementationId": verifier_implementation_id,
        },
        label="document-release producer",
    )


def _read_bytes(path: Path, *, label: str, max_bytes: int = _MAX_JSON_BYTES) -> bytes:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise CliError(f"{label} must be a regular, non-symlink file: {path}")
    if path.stat().st_size > max_bytes:
        raise CliError(f"{label} exceeds the {max_bytes}-byte limit")
    return path.read_bytes()


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    payload = _read_bytes(path, label=label)
    value = thaw_json(parse_closed_json(payload, label=label))
    if not isinstance(value, dict):
        raise CliError(f"{label} must be a JSON object")
    return value


def _read_canonical_object(path: Path, *, label: str) -> dict[str, Any]:
    payload = _read_bytes(path, label=label)
    value = thaw_json(parse_canonical_json(payload, label=label))
    if not isinstance(value, dict):
        raise CliError(f"{label} must be a JSON object")
    return value


def _existing_root(path: Path, *, label: str) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise CliError(f"{label} must be an existing, non-symlink directory: {path}")
    return path.resolve(strict=True)


def _emit(value: object, *, error: bool = False) -> None:
    if error:
        value = redact(value)
    else:
        require_secret_free(value, label="CLI output")
    stream = sys.stderr.buffer if error else sys.stdout.buffer
    stream.write(canonical_json_file_bytes(value))
    stream.flush()


def _write_new(path: Path, payload: bytes, *, label: str) -> None:
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise CliError(f"refusing to replace existing {label}: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise CliError(f"refusing to replace existing {label}: {path}") from error
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _write_failure_receipt(args: argparse.Namespace, error: Exception) -> None:
    """Best-effort write-once failure evidence for mutating operator commands."""

    receipt_value = getattr(args, "receipt", None)
    if receipt_value is None:
        return
    receipt_path = Path(receipt_value)
    if receipt_path.exists() or receipt_path.is_symlink():
        return
    request_digest: str | None = None
    request_value = getattr(args, "request", None)
    if request_value is not None:
        request_path = Path(request_value)
        if request_path.is_file() and not request_path.is_symlink() and request_path.stat().st_size <= _MAX_JSON_BYTES:
            request_digest = sha256_digest(request_path.read_bytes())
    content = {
        "operation": getattr(args, "operation", getattr(args, "command", "unknown")),
        "requestDigest": request_digest,
        "errorType": type(error).__name__,
        "diagnosticCode": f"DOCSPEC-CLI-{type(error).__name__.upper()}",
        "verdict": "failed",
    }
    receipt = {
        "format": "docspec-operation-failure-receipt",
        "formatVersion": "1.0",
        "receiptId": stable_urn("operation-failure-receipt", content),
        **content,
    }
    try:
        _write_new(
            receipt_path,
            canonical_json_file_bytes(receipt),
            label="operation failure receipt",
        )
    except (DocSpecError, OSError):
        return


def _require_new_output_paths(destination: Path, receipt_path: Path) -> None:
    destination = Path(destination)
    receipt_path = Path(receipt_path)
    if destination.resolve(strict=False) == receipt_path.resolve(strict=False):
        raise CliError("artifact destination and receipt path must differ")
    for path, label in ((destination, "artifact"), (receipt_path, "operation receipt")):
        if path.exists() or path.is_symlink():
            raise CliError(f"refusing to replace existing {label}: {path}")


def _artifact_receipt(
    *,
    operation: str,
    request_digest: str,
    artifact_id: str,
    destination: Path,
    payload: bytes,
) -> dict[str, Any]:
    content = {
        "operation": operation,
        "requestDigest": request_digest,
        "artifact": {
            "artifactId": artifact_id,
            "locator": destination.resolve(strict=False).as_posix(),
            "digest": sha256_digest(payload),
            "mediaType": "application/json",
            "byteSize": len(payload),
        },
        "verdict": "completed",
    }
    return {
        "format": "docspec-operation-receipt",
        "formatVersion": "1.0",
        "receiptId": stable_urn("operation-receipt", content),
        **content,
    }


def _write_artifact_and_receipt(
    *,
    operation: str,
    request_path: Path,
    destination: Path,
    receipt_path: Path,
    artifact_id: str,
    payload: bytes,
) -> dict[str, Any]:
    destination = Path(destination)
    receipt_path = Path(receipt_path)
    _require_new_output_paths(destination, receipt_path)
    request_digest = sha256_digest(_read_bytes(request_path, label="operation request"))
    receipt = _artifact_receipt(
        operation=operation,
        request_digest=request_digest,
        artifact_id=artifact_id,
        destination=destination,
        payload=payload,
    )
    _write_new(destination, payload, label="artifact")
    _write_new(receipt_path, canonical_json_file_bytes(receipt), label="operation receipt")
    return receipt


def _registered_profile(path: Path) -> RegisteredProfile:
    path = Path(path)
    _read_bytes(path, label="storage profile")
    return ProfileRegistry.from_file(path)


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
    directory = _existing_root(args.directory, label="profile directory")
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
    _emit(
        {
            "format": "docspec-scale-profile-verification",
            "formatVersion": "1.0",
            "profileId": profile.profile_id,
            "profileDigest": profile.digest,
            "unitCount": profile.targets.unit_count,
            "processorTargetCount": len(profile.targets.processor_targets),
            "verdict": "pass",
        }
    )
    return 0


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


def _release_reference(path: Path) -> DocumentReleaseRef:
    return DocumentReleaseRef.from_dict(_read_json_object(path, label="document release reference"))


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


_LOCAL_PROFILE_SET_ID = "urn:docspec:profile-set:portable-local:1"
_LOCAL_PROFILE_MODULES = {
    ProfileRole.RELEASE_MANIFEST: "docspec.domain.release:DocumentRelease",
    ProfileRole.DOCUMENT_CATALOG: "docspec.adapters.storage:LocalManifestDocumentCatalog",
    ProfileRole.RECORD_STORAGE: "docspec.adapters.storage:LocalJsonlRecordStorage",
    ProfileRole.BLOB_STORAGE: "docspec.adapters.storage:LocalContentAddressedBlobStore",
    ProfileRole.DOCUMENT_STORE: "docspec.adapters.storage:LocalDocumentStoreRepository",
    ProfileRole.RESULT_DELIVERY: "docspec.adapters.sinks:DurableDatasetSink",
}
_LOCAL_RUN_ROOTS = {
    "blobStorage",
    "controlRepository",
    "documentCatalog",
    "documentStores",
    "reconciliation",
    "recordStorage",
    "sourceCatalog",
    "sourceContent",
}
_LOCAL_RUN_FIELDS = {
    "format",
    "formatVersion",
    "plan",
    "profileDirectory",
    "roots",
    "resultSinkId",
    "partitionPolicyId",
    "retryPolicy",
    "acceptedFailurePolicy",
    "execution",
    "completedAt",
    "documentReleaseProducer",
    "sourceCatalogProducer",
}
_LOCAL_EXECUTION_REQUIRED_FIELDS = {"maxWorkers", "maxInFlight", "deadlineEpochSeconds"}
_LOCAL_EXECUTION_OPTIONAL_DEFAULTS = {
    "maxScratchBytesPerWorker": 4 * 1024**3,
    "maxNetworkBytesPerTask": 8 * 1024**3,
    "requestRateLimitPerSecond": 100,
    "maxProviderConcurrency": 4,
    "maxTaskAttempts": 1,
    "retryInitialDelayMilliseconds": 0,
    "retryMaxDelayMilliseconds": 0,
}


def _absolute_request_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CliError(f"{label} must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise CliError(f"{label} must be an absolute path")
    return path


def _utc_instant(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CliError(f"{label} must be an RFC 3339 UTC instant ending in Z")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise CliError(f"{label} must be an RFC 3339 UTC instant") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise CliError(f"{label} must use UTC")
    return value


def _retry_policy(value: object) -> RetryPolicy:
    fields = {
        "format",
        "formatVersion",
        "maxAttempts",
        "baseDelayMilliseconds",
        "maxDelayMilliseconds",
        "jitterBasisPoints",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise CliError("local run retry policy has an invalid closed shape")
    if value["format"] != "docspec-retry-policy" or value["formatVersion"] != "1.0":
        raise CliError("local run retry policy has an unknown format")
    numeric = ("maxAttempts", "baseDelayMilliseconds", "maxDelayMilliseconds", "jitterBasisPoints")
    if any(type(value[name]) is not int for name in numeric):
        raise CliError("local run retry policy values must be integers")
    return RetryPolicy(
        max_attempts=value["maxAttempts"],
        base_delay_milliseconds=value["baseDelayMilliseconds"],
        max_delay_milliseconds=value["maxDelayMilliseconds"],
        jitter_basis_points=value["jitterBasisPoints"],
    )


def _accepted_failure_policy(value: object) -> AcceptedFailurePolicy:
    fields = {"format", "formatVersion", "acceptedClasses", "acceptedDiagnosticCodes"}
    if not isinstance(value, dict) or set(value) != fields:
        raise CliError("local run accepted-failure policy has an invalid closed shape")
    if value["format"] != "docspec-accepted-failure-policy" or value["formatVersion"] != "1.0":
        raise CliError("local run accepted-failure policy has an unknown format")
    classes = value["acceptedClasses"]
    codes = value["acceptedDiagnosticCodes"]
    if not isinstance(classes, list) or not isinstance(codes, list):
        raise CliError("local run accepted-failure policy entries must be lists")
    if any(not isinstance(item, str) for item in (*classes, *codes)):
        raise CliError("local run accepted-failure policy entries must be strings")
    return AcceptedFailurePolicy(
        tuple(FailureClass(item) for item in classes),
        tuple(codes),
    )


def _local_run_request(path: Path) -> dict[str, Any]:
    value = _read_json_object(path, label="local run request")
    if set(value) != _LOCAL_RUN_FIELDS:
        raise CliError("local run request has an invalid closed shape")
    if value["format"] != "docspec-local-run-request" or value["formatVersion"] != "1.0":
        raise CliError("local run request has an unknown format")
    roots = value["roots"]
    if not isinstance(roots, dict) or set(roots) != _LOCAL_RUN_ROOTS:
        raise CliError("local run roots have an invalid closed shape")
    execution = value["execution"]
    allowed_execution_fields = _LOCAL_EXECUTION_REQUIRED_FIELDS | set(_LOCAL_EXECUTION_OPTIONAL_DEFAULTS)
    if (
        not isinstance(execution, dict)
        or not _LOCAL_EXECUTION_REQUIRED_FIELDS <= set(execution)
        or not set(execution) <= allowed_execution_fields
    ):
        raise CliError("local run execution settings have an invalid closed shape")
    execution = {**_LOCAL_EXECUTION_OPTIONAL_DEFAULTS, **execution}
    zero_allowed = {"retryInitialDelayMilliseconds", "retryMaxDelayMilliseconds"}
    for name, setting in execution.items():
        minimum = 0 if name in zero_allowed else 1
        if type(setting) is not int or setting < minimum:
            raise CliError(f"local run execution setting {name} must be an integer of at least {minimum}")
    if execution["retryMaxDelayMilliseconds"] < execution["retryInitialDelayMilliseconds"]:
        raise CliError("local run execution retry maximum must not be less than its initial delay")
    for name in ("resultSinkId", "partitionPolicyId"):
        if not isinstance(value[name], str) or not value[name]:
            raise CliError(f"local run {name} must be a non-empty string")
    return {
        **value,
        "plan": _absolute_request_path(value["plan"], label="local run plan"),
        "profileDirectory": _absolute_request_path(
            value["profileDirectory"],
            label="local run profile directory",
        ),
        "roots": {
            name: _absolute_request_path(roots[name], label=f"local run {name} root")
            for name in sorted(_LOCAL_RUN_ROOTS)
        },
        "retryPolicy": _retry_policy(value["retryPolicy"]),
        "acceptedFailurePolicy": _accepted_failure_policy(value["acceptedFailurePolicy"]),
        "documentReleaseProducer": _producer_record(
            value["documentReleaseProducer"],
            label="local-run document-release producer",
        ),
        "sourceCatalogProducer": _producer_record(
            value["sourceCatalogProducer"],
            label="local-run source-catalog producer",
        ),
        "execution": execution,
        "completedAt": _utc_instant(value["completedAt"], label="local run completedAt"),
    }


def _profile_limit(profile: RegisteredProfile, name: str) -> int:
    value = profile.description.limits.get(name)
    if type(value) is not int or value <= 0:
        raise CliError(f"profile {profile.description.profile_id} requires a positive integer {name} limit")
    return value


def _verified_local_plan(
    request: dict[str, Any],
) -> tuple[
    ProcessingPlan,
    dict[str, ContentStatisticsProcessor],
    dict[ProfileRole, RegisteredProfile],
]:
    plan = ProcessingPlan.from_dict(_read_canonical_object(request["plan"], label="processing plan"))
    registry = ProfileRegistry.from_directory(request["profileDirectory"])
    selected_ids = tuple(pin.profile_id for pin in plan.profiles.pins)
    if registry.select(selected_ids) != plan.profiles:
        raise CliError("processing plan profile pins differ from their machine descriptions")
    registered = {item.description.profile_id: item for item in registry.list()}
    selected_profiles: dict[ProfileRole, RegisteredProfile] = {}
    for pin in plan.profiles.pins:
        item = registered[pin.profile_id]
        if item.profile_set_id != _LOCAL_PROFILE_SET_ID or item.implementation_module != _LOCAL_PROFILE_MODULES[pin.role]:
            raise CliError(f"processing plan {pin.role.value} is not supported by the local composition")
        selected_profiles[pin.role] = item

    retry_policy = request["retryPolicy"]
    accepted_failure_policy = request["acceptedFailurePolicy"]
    if retry_policy.digest != plan.retry_policy_digest:
        raise CliError("local run retry policy differs from the processing plan")
    if accepted_failure_policy.digest != plan.accepted_failure_policy_digest:
        raise CliError("local run accepted-failure policy differs from the processing plan")
    if retry_policy.max_attempts != plan.limits.max_attempts:
        raise CliError("local run retry policy differs from the plan attempt limit")
    if plan.stages.extractor_ids != (DefaultExtractorRegistry.extractor_id,):
        raise CliError("local composition requires the pinned default extractor registry")
    if plan.stages.segmenter_id != DefaultSegmenterRegistry.segmenter_id:
        raise CliError("local composition requires the pinned default segmenter registry")

    content_statistics = ContentStatisticsProcessor(retry_policy=retry_policy)
    available = {content_statistics.description.processor_id: content_statistics}
    try:
        processors = {identifier: available[identifier] for identifier in plan.stages.processor_ids}
    except KeyError as error:
        raise CliError(f"local composition has no processor {error.args[0]}") from error
    if ProcessorSet(tuple(processor.description for processor in processors.values())) != plan.processors:
        raise CliError("local processor implementations differ from the processing plan")
    return plan, processors, selected_profiles


def _local_storage(
    roots: dict[str, Path],
    profiles: dict[ProfileRole, RegisteredProfile],
    producer: Producer,
) -> tuple[
    LocalJsonControlRepository,
    LocalDocumentStoreRepository,
    LocalJsonlRecordStorage,
    LocalContentAddressedBlobStore,
    LocalManifestDocumentCatalog,
]:
    controls = LocalJsonControlRepository(roots["controlRepository"])
    store_profile = profiles[ProfileRole.DOCUMENT_STORE]
    stores = LocalDocumentStoreRepository(
        roots["documentStores"],
        max_revision_bytes=_profile_limit(store_profile, "maxLedgerMemberBytes"),
        max_inline_bytes=_profile_limit(store_profile, "maxInlineBytes"),
        max_plan_ledger_bytes=_profile_limit(store_profile, "maxPlannedStoreLedgerBytes"),
        max_plan_record_bytes=_profile_limit(store_profile, "maxPlannedStoreRecordBytes"),
        max_plan_store_count=_profile_limit(store_profile, "maxPlannedStoreCount"),
    )
    record_profile = profiles[ProfileRole.RECORD_STORAGE]
    records = LocalJsonlRecordStorage(
        roots["recordStorage"],
        max_member_bytes=_profile_limit(record_profile, "maxMemberBytes"),
        max_record_bytes=_profile_limit(record_profile, "maxRecordBytes"),
        max_root_bytes=_profile_limit(record_profile, "maxRootBytes"),
        max_open_members=_profile_limit(record_profile, "maxOpenMembers"),
        max_merge_scratch_bytes=_profile_limit(record_profile, "maxMergeScratchBytes"),
    )
    blobs = LocalContentAddressedBlobStore(
        roots["blobStorage"],
        max_blob_bytes=_profile_limit(profiles[ProfileRole.BLOB_STORAGE], "maxObjectBytes"),
        stream_chunk_bytes=_profile_limit(profiles[ProfileRole.BLOB_STORAGE], "streamChunkBytes"),
    )
    release_limit = _profile_limit(profiles[ProfileRole.RELEASE_MANIFEST], "maxRootBytes")
    catalog_limit = _profile_limit(profiles[ProfileRole.DOCUMENT_CATALOG], "maxManifestBytes")
    catalog = LocalManifestDocumentCatalog(
        roots["documentCatalog"],
        records=records,
        stores=stores,
        controls=controls,
        producer=producer,
        blobs=blobs,
        max_release_bytes=min(release_limit, catalog_limit),
    )
    return controls, stores, records, blobs, catalog


def _local_storage_for_run_request(
    path: Path,
) -> tuple[
    dict[str, Any],
    ProcessingPlan,
    LocalJsonControlRepository,
    LocalDocumentStoreRepository,
    LocalJsonlRecordStorage,
    LocalContentAddressedBlobStore,
    LocalManifestDocumentCatalog,
]:
    """Resolve one verified local profile composition without starting a run."""

    request = _local_run_request(path)
    plan, _, profiles = _verified_local_plan(request)
    controls, stores, records, blobs, catalog = _local_storage(
        request["roots"],
        profiles,
        request["documentReleaseProducer"],
    )
    return request, plan, controls, stores, records, blobs, catalog


@dataclass(slots=True)
class _LocalRunComposition:
    request: dict[str, Any]
    plan: ProcessingPlan
    controls: LocalJsonControlRepository
    stores: LocalDocumentStoreRepository
    records: LocalJsonlRecordStorage
    catalog: LocalManifestDocumentCatalog
    source_catalog: ImmutableSourceCatalogReader
    partition_policy: PartitionPolicy
    plan_ref: ArtifactRef
    sink_ref: ArtifactRef
    executor: StoreExecutionService
    delivery: StoreDeliveryService
    clock: Any
    content_fetcher_composition: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _PreparedLocalRun:
    execution_profile: ExecutionProfile
    execution_profile_ref: ArtifactRef
    handoff: ExecutionHandoff
    handoff_ref: ArtifactRef


def _local_processor_cache_path(roots: dict[str, Path]) -> Path:
    return roots["reconciliation"] / "processor-results.sqlite3"


def _compose_local_run(
    request: dict[str, Any],
    *,
    source_catalog: ImmutableSourceCatalogReader | None = None,
    content_fetcher: ContentFetcher | None = None,
    content_fetcher_composition: dict[str, Any] | None = None,
) -> _LocalRunComposition:
    plan, processors, profiles = _verified_local_plan(request)
    roots = request["roots"]
    controls, stores, records, blobs, catalog = _local_storage(
        roots,
        profiles,
        request["documentReleaseProducer"],
    )
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    if source_catalog is None:
        source_catalog = SourceCatalogArtifactReader(
            LocalSourceCatalogStore(roots["sourceCatalog"], create=False),
            producer=request["sourceCatalogProducer"],
        )
    fetcher = content_fetcher or LocalFileContentFetcher(roots["sourceContent"])
    actual_fetcher_composition = {
        "implementationId": getattr(fetcher, "downloader_id", None),
        "configurationDigest": getattr(fetcher, "configuration_digest", None),
    }
    if content_fetcher_composition is None:
        content_fetcher_composition = actual_fetcher_composition
    elif (
        not isinstance(content_fetcher_composition, dict)
        or content_fetcher_composition.get("implementationId") != actual_fetcher_composition["implementationId"]
        or content_fetcher_composition.get("configurationDigest")
        != actual_fetcher_composition["configurationDigest"]
    ):
        raise CliError("content fetcher differs from its sealed worker composition")
    partition_policy = PartitionPolicy(request["partitionPolicyId"], plan.partition_count)
    completed_at = request["completedAt"]

    def clock() -> str:
        return completed_at

    blob_profile = plan.profiles.for_role(ProfileRole.BLOB_STORAGE)
    blob_state = {
        "profileId": blob_profile.profile_id,
        "profileVersion": blob_profile.version,
        "storageRoot": blobs.root.as_posix(),
    }
    blob_root = controls.put(
        kind="profile-state",
        artifact_id=stable_urn("profile-state", blob_state),
        value=blob_state,
    )
    result_profile = plan.profiles.for_role(ProfileRole.RESULT_DELIVERY)
    sink = DurableDatasetSink(
        sink_id=request["resultSinkId"],
        profile_id=result_profile.profile_id,
        storage=records,
        partition_policy=partition_policy,
        blob_roots=(blob_root,),
        clock=clock,
    )
    sink_ref = controls.put(
        kind="sinks",
        artifact_id=sink.sink_id,
        value={"sinkId": sink.sink_id, "profileId": sink.profile_id},
    )
    executor = StoreExecutionService(
        plan_ref=plan_ref,
        controls=controls,
        stores=stores,
        document_catalog=catalog,
        blobs=blobs,
        fetcher=fetcher,
        extractor=DefaultExtractorRegistry(),
        segmenter=DefaultSegmenterRegistry(),
        processors=processors,
        retry_policy=request["retryPolicy"],
        accepted_failure_policy=request["acceptedFailurePolicy"],
        clock=clock,
        processor_cache=LocalSqliteProcessorResultCache(_local_processor_cache_path(roots)),
    )
    delivery = StoreDeliveryService(stores=stores, controls=controls, sinks={sink.sink_id: sink})
    return _LocalRunComposition(
        request,
        plan,
        controls,
        stores,
        records,
        catalog,
        source_catalog,
        partition_policy,
        plan_ref,
        sink_ref,
        executor,
        delivery,
        clock,
        content_fetcher_composition,
    )


def _prepared_tasks(
    composition: _LocalRunComposition,
    prepared: _PreparedLocalRun,
) -> Iterator[StoreTask]:
    return iter_store_tasks(
        composition.plan.plan_id,
        prepared.handoff.operation_id,
        composition.stores.stream_planned_stores(prepared.handoff.planned_store_ledger),
    )


def _prepare_local_run(
    composition: _LocalRunComposition,
    *,
    resume: bool | None,
) -> _PreparedLocalRun:
    request = composition.request
    plan = composition.plan
    roots = request["roots"]
    controls = composition.controls
    stores = composition.stores
    if resume is None:
        resume = stores.has_planned_store_ledger(plan.plan_id)
    if not resume:
        planned = RunPlanner(
            source_catalog=composition.source_catalog,
            document_catalog=composition.catalog,
            stores=stores,
            controls=controls,
            workspace_factory=LocalSqliteReconciliationWorkspaceFactory(
                roots["reconciliation"] / "planning",
                read_batch_size=1,
            ),
        ).plan_run(plan.source_catalog, plan.base_release, composition.plan_ref)
        for _ in planned:
            pass
    planned_ledger = stores.planned_store_ledger(plan.plan_id)

    def tasks():
        return iter_store_tasks(
            plan.plan_id,
            EXECUTE_AND_DELIVER_OPERATION_ID,
            stores.stream_planned_stores(planned_ledger),
        )

    task_count, task_set_digest = summarize_store_tasks(tasks())
    worker_composition_value = {
        "format": "docspec-local-worker-composition",
        "formatVersion": "1.1",
        "implementationId": "docspec.cli.local-worker/v2",
        "processingPlan": composition.plan_ref.to_dict(),
        "profileSet": plan.profiles.to_dict(),
        "roots": {name: path.as_posix() for name, path in sorted(roots.items())},
        "retryPolicy": request["retryPolicy"].to_dict(),
        "acceptedFailurePolicy": request["acceptedFailurePolicy"].to_dict(),
        "contentFetcher": composition.content_fetcher_composition,
    }
    worker_composition = controls.put(
        kind="worker-compositions",
        artifact_id=stable_urn("worker-composition", worker_composition_value),
        value=worker_composition_value,
    )
    scheduler_configuration_value = {
        "format": "docspec-local-scheduler-configuration",
        "formatVersion": "1.0",
        "adapterId": "docspec.local-threaded",
        "settings": dict(sorted(request["execution"].items())),
    }
    scheduler_configuration = controls.put(
        kind="scheduler-configurations",
        artifact_id=stable_urn("scheduler-configuration", scheduler_configuration_value),
        value=scheduler_configuration_value,
    )
    cache_profile_value = {
        "format": "docspec-processor-result-cache-profile",
        "formatVersion": "1.0",
        "adapterId": "docspec.local-sqlite-processor-result-cache",
        "adapterVersion": "1.0.0",
        "lookupSemantics": "exact-reuse-key-to-immutable-result-reference",
        "resultAuthority": "control-repository",
        "failureBehavior": "execute-processor",
    }
    cache_profile = controls.put(
        kind="processor-cache-profiles",
        artifact_id=stable_urn("processor-cache-profile", cache_profile_value),
        value=cache_profile_value,
    )
    cache_state_value = {
        "format": "docspec-processor-result-cache-state",
        "formatVersion": "1.0",
        "cacheProfile": cache_profile.to_dict(),
        "databasePath": _local_processor_cache_path(roots).as_posix(),
        "observedAt": request["completedAt"],
        "verificationScope": "configuration-only",
    }
    cache_state = controls.put(
        kind="processor-cache-states",
        artifact_id=stable_urn("processor-cache-state", cache_state_value),
        value=cache_state_value,
    )
    execution_profile = ExecutionProfile(
        "docspec.local-threaded",
        "1.0.0",
        worker_composition,
        scheduler_configuration,
        ExecutionLimits(
            request["execution"]["maxWorkers"],
            1,
            request["execution"]["maxInFlight"],
            request["execution"]["maxScratchBytesPerWorker"],
            request["execution"]["maxNetworkBytesPerTask"],
            request["execution"]["requestRateLimitPerSecond"],
            request["execution"]["maxProviderConcurrency"],
            request["execution"]["maxTaskAttempts"],
            request["execution"]["retryInitialDelayMilliseconds"],
            request["execution"]["retryMaxDelayMilliseconds"],
        ),
        request["execution"]["deadlineEpochSeconds"],
        cache_profile,
        cache_state,
    )
    if execution_profile.limits.max_network_bytes_per_task < plan.limits.max_estimated_bytes:
        raise CliError("execution network bound is lower than one planned store's logical byte bound")
    execution_profile_ref = controls.put(
        kind="execution-profiles",
        artifact_id=execution_profile.profile_id,
        value=execution_profile.to_dict(),
    )
    handoff = ExecutionHandoff(
        processing_plan=composition.plan_ref,
        execution_profile=execution_profile_ref,
        worker_composition=worker_composition,
        planned_store_ledger=planned_ledger,
        operation_id=EXECUTE_AND_DELIVER_OPERATION_ID,
        expected_task_count=task_count,
        task_set_digest=task_set_digest,
        result_sink=composition.sink_ref,
        base_release=plan.base_release,
    )
    handoff_ref = controls.put(
        kind="execution-handoffs",
        artifact_id=handoff.handoff_id,
        value=handoff.to_dict(),
    )
    return _PreparedLocalRun(execution_profile, execution_profile_ref, handoff, handoff_ref)


def _load_prepared_local_run(
    composition: _LocalRunComposition,
    handoff_ref: ArtifactRef,
) -> _PreparedLocalRun:
    try:
        handoff = ExecutionHandoff.from_dict(composition.controls.load(handoff_ref))
        profile = ExecutionProfile.from_dict(composition.controls.load(handoff.execution_profile))
    except (TypeError, ValueError) as error:
        raise CliError(f"saved local execution handoff is invalid: {error}") from error
    if handoff.handoff_id != handoff_ref.artifact_id:
        raise CliError("saved execution handoff identity differs from its reference")
    if profile.profile_id != handoff.execution_profile.artifact_id:
        raise CliError("saved execution profile identity differs from its reference")
    for reference in profile.control_artifacts:
        composition.controls.verify(reference)
    planned_ledger = composition.stores.planned_store_ledger(composition.plan.plan_id)
    if (
        handoff.processing_plan != composition.plan_ref
        or handoff.execution_profile.artifact_id != profile.profile_id
        or handoff.worker_composition != profile.worker_composition
        or handoff.planned_store_ledger != planned_ledger
        or handoff.result_sink != composition.sink_ref
        or handoff.base_release != composition.plan.base_release
    ):
        raise CliError("saved execution handoff differs from the local run composition")
    return _PreparedLocalRun(profile, handoff.execution_profile, handoff, handoff_ref)


def _execute_local_task(
    composition: _LocalRunComposition,
    prepared: _PreparedLocalRun,
    task: StoreTask,
) -> StoreTaskResult:
    handoff = prepared.handoff
    if (
        task.processing_plan_id != composition.plan.plan_id
        or task.operation_id != handoff.operation_id
        or handoff.processing_plan != composition.plan_ref
    ):
        raise CliError("local worker received a task outside its sealed execution handoff")
    current_ref, current_store = load_latest_store(composition.stores, task.input_store)
    if current_store.plan_id != composition.plan.plan_id:
        raise CliError("local worker recovered a document store from another processing plan")
    if current_store.state is StoreState.SEALED:
        sealed = composition.delivery.deliver_store(current_ref, handoff.result_sink)
    else:
        processed = composition.executor.execute_store(current_ref)
        sealed = composition.delivery.deliver_store(processed, handoff.result_sink)
    return StoreTaskResult.succeeded(
        handoff_id=handoff.handoff_id,
        task=task,
        output_store=sealed,
    )


def _reconcile_local_run(
    composition: _LocalRunComposition,
    prepared: _PreparedLocalRun,
    results: Iterable[StoreTaskResult],
) -> ArtifactRef:
    plan = composition.plan
    roots = composition.request["roots"]
    return RunReconciler(
        plan_ref=composition.plan_ref,
        execution_profile_ref=prepared.execution_profile_ref,
        execution_handoff_ref=prepared.handoff_ref,
        source_catalog_ref=plan.source_catalog,
        base_release_ref=plan.base_release,
        controls=composition.controls,
        stores=composition.stores,
        records=composition.records,
        document_catalog=composition.catalog,
        source_catalog=composition.source_catalog,
        workspace_factory=LocalSqliteReconciliationWorkspaceFactory(roots["reconciliation"]),
        partition_policy=composition.partition_policy,
        clock=composition.clock,
    ).reconcile_run(results)


def _execute_local_run(
    request: dict[str, Any],
    *,
    resume: bool | None,
    source_catalog: ImmutableSourceCatalogReader | None = None,
    content_fetcher: ContentFetcher | None = None,
    content_fetcher_composition: dict[str, Any] | None = None,
) -> ArtifactRef:
    composition = _compose_local_run(
        request,
        source_catalog=source_catalog,
        content_fetcher=content_fetcher,
        content_fetcher_composition=content_fetcher_composition,
    )
    prepared = _prepare_local_run(composition, resume=resume)

    def execute_and_deliver(_handoff: ExecutionHandoff, task: StoreTask) -> StoreTaskResult:
        return _execute_local_task(composition, prepared, task)

    results = LocalExecutionBackend(
        prepared.execution_profile,
        execute_and_deliver,
        profile_reference=prepared.execution_profile_ref,
        controls=composition.controls,
        max_workers=request["execution"]["maxWorkers"],
    ).execute(prepared.handoff, _prepared_tasks(composition, prepared))
    return _reconcile_local_run(composition, prepared, results)


def _cmd_local_run_prepare(args: argparse.Namespace) -> int:
    _require_new_output_paths(args.destination, args.receipt)
    request = _local_run_request(args.request)
    prepared = _prepare_local_run(_compose_local_run(request), resume=False)
    receipt = _write_artifact_and_receipt(
        operation="run.prepare",
        request_path=args.request,
        destination=args.destination,
        receipt_path=args.receipt,
        artifact_id=prepared.handoff_ref.artifact_id,
        payload=canonical_json_file_bytes(prepared.handoff_ref.to_dict()),
    )
    _emit(receipt)
    return 0


def _local_task_request(path: Path) -> tuple[Path, ArtifactRef, StoreTask]:
    value = _read_json_object(path, label="local task execution request")
    fields = {"format", "formatVersion", "runRequest", "handoff", "task"}
    if set(value) != fields:
        raise CliError("local task execution request has an invalid closed shape")
    if value["format"] != "docspec-local-task-execution-request" or value["formatVersion"] != "1.0":
        raise CliError("local task execution request has an unknown format")
    return (
        _absolute_request_path(value["runRequest"], label="local run request"),
        ArtifactRef.from_dict(value["handoff"]),
        StoreTask.from_dict(value["task"]),
    )


def _cmd_local_task_execute(args: argparse.Namespace) -> int:
    _require_new_output_paths(args.destination, args.receipt)
    run_request_path, handoff_ref, task = _local_task_request(args.request)
    composition = _compose_local_run(_local_run_request(run_request_path))
    prepared = _load_prepared_local_run(composition, handoff_ref)
    if time.time() >= prepared.execution_profile.deadline_epoch_seconds:
        raise CliError("execution profile deadline has expired")
    result = _execute_local_task(composition, prepared, task)
    receipt = _write_artifact_and_receipt(
        operation="task.execute",
        request_path=args.request,
        destination=args.destination,
        receipt_path=args.receipt,
        artifact_id=result.result_id,
        payload=result.to_bytes(),
    )
    _emit(receipt)
    return 0


def _local_reconcile_request(path: Path) -> tuple[Path, ArtifactRef, Path]:
    value = _read_json_object(path, label="local run reconcile request")
    fields = {"format", "formatVersion", "runRequest", "handoff", "results"}
    if set(value) != fields:
        raise CliError("local run reconcile request has an invalid closed shape")
    if value["format"] != "docspec-local-run-reconcile-request" or value["formatVersion"] != "1.0":
        raise CliError("local run reconcile request has an unknown format")
    return (
        _absolute_request_path(value["runRequest"], label="local run request"),
        ArtifactRef.from_dict(value["handoff"]),
        _absolute_request_path(value["results"], label="task result stream"),
    )


def _iter_task_result_file(path: Path) -> Iterator[StoreTaskResult]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise CliError("task result stream must be a regular, non-symlink file")
    with path.open("rb") as handle:
        while line := handle.readline(MAX_RESULT_BYTES + 2):
            if len(line) > MAX_RESULT_BYTES + 1:
                raise CliError("task result exceeds its serialized line limit")
            try:
                yield StoreTaskResult.from_bytes(line)
            except (TypeError, ValueError) as error:
                raise CliError(f"task result stream contains an invalid result: {error}") from error


def _cmd_local_run_reconcile(args: argparse.Namespace) -> int:
    _require_new_output_paths(args.destination, args.receipt)
    run_request_path, handoff_ref, results_path = _local_reconcile_request(args.request)
    composition = _compose_local_run(_local_run_request(run_request_path))
    prepared = _load_prepared_local_run(composition, handoff_ref)
    reference = _reconcile_local_run(
        composition,
        prepared,
        _iter_task_result_file(results_path),
    )
    receipt = _write_artifact_and_receipt(
        operation="run.reconcile",
        request_path=args.request,
        destination=args.destination,
        receipt_path=args.receipt,
        artifact_id=reference.artifact_id,
        payload=canonical_json_file_bytes(reference.to_dict()),
    )
    _emit(receipt)
    return 0


def _cmd_local_run(args: argparse.Namespace) -> int:
    _require_new_output_paths(args.destination, args.receipt)
    request = _local_run_request(args.request)
    reference = _execute_local_run(request, resume=args.operation == "run.resume")
    receipt = _write_artifact_and_receipt(
        operation=args.operation,
        request_path=args.request,
        destination=args.destination,
        receipt_path=args.receipt,
        artifact_id=reference.artifact_id,
        payload=canonical_json_file_bytes(reference.to_dict()),
    )
    _emit(receipt)
    return 0


def _cmd_document_release_commit(args: argparse.Namespace) -> int:
    _require_new_output_paths(args.destination, args.receipt)
    value = _read_json_object(args.request, label="document release commit request")
    fields = {"format", "formatVersion", "runRequest", "runReceipt", "baseRelease"}
    if set(value) != fields:
        raise CliError("document release commit request has an invalid closed shape")
    if value["format"] != "docspec-local-release-commit-request" or value["formatVersion"] != "1.0":
        raise CliError("document release commit request has an unknown format")
    run_request_path = _absolute_request_path(value["runRequest"], label="local run request")
    run_receipt_path = _absolute_request_path(value["runReceipt"], label="run receipt reference")
    _, plan, controls, _, records, _, catalog = _local_storage_for_run_request(run_request_path)
    base_release = None if value["baseRelease"] is None else DocumentReleaseRef.from_dict(value["baseRelease"])
    if base_release != plan.base_release:
        raise CliError("commit base release differs from the processing plan")
    plan_ref = controls.put(kind="plans", artifact_id=plan.plan_id, value=plan.to_dict())
    run_receipt = ArtifactRef.from_dict(_read_canonical_object(run_receipt_path, label="run receipt reference"))
    reference = ReleaseCommitService(
        plan_ref=plan_ref,
        controls=controls,
        records=records,
        document_catalog=catalog,
    ).commit_release(base_release, run_receipt)
    receipt = _write_artifact_and_receipt(
        operation="document-release.commit",
        request_path=args.request,
        destination=args.destination,
        receipt_path=args.receipt,
        artifact_id=reference.release_id,
        payload=canonical_json_file_bytes(reference.to_dict()),
    )
    _emit(receipt)
    return 0


def _local_release_compaction_request(path: Path) -> tuple[Path, DocumentReleaseRef]:
    value = _read_json_object(path, label="local release compaction request")
    fields = {"format", "formatVersion", "runRequest", "sourceRelease"}
    if set(value) != fields:
        raise CliError("local release compaction request has an invalid closed shape")
    if value["format"] != "docspec-local-release-compaction-request" or value["formatVersion"] != "1.0":
        raise CliError("local release compaction request has an unknown format")
    return (
        _absolute_request_path(value["runRequest"], label="local run request"),
        DocumentReleaseRef.from_dict(value["sourceRelease"]),
    )


def _cmd_document_release_compact(args: argparse.Namespace) -> int:
    _require_new_output_paths(args.destination, args.receipt)
    run_request_path, source_reference = _local_release_compaction_request(args.request)
    request, plan, controls, stores, records, _, catalog = _local_storage_for_run_request(
        run_request_path
    )
    source = catalog.open(source_reference)
    try:
        source_plan = ProcessingPlan.from_dict(controls.load(source.processing_plan))
    except (TypeError, ValueError) as error:
        raise CliError(f"source release processing plan is invalid: {error}") from error
    if source_plan != plan or source.profiles != plan.profiles:
        raise CliError("compaction run composition differs from the source release")

    reference = ReleaseCompactionService(
        controls=controls,
        records=records,
        stores=stores,
        document_catalog=catalog,
        clock=lambda: request["completedAt"],
    ).compact(source_reference)
    compaction = ReleaseCompactionReceipt.from_dict(controls.load(reference))
    if compaction.receipt_id != reference.artifact_id or compaction.source_release != source_reference:
        raise CliError("saved compaction receipt differs from its immutable reference")
    receipt = _write_artifact_and_receipt(
        operation="document-release.compact",
        request_path=args.request,
        destination=args.destination,
        receipt_path=args.receipt,
        artifact_id=reference.artifact_id,
        payload=canonical_json_file_bytes(reference.to_dict()),
    )
    _emit(receipt)
    return 0


def _cmd_run_status(args: argparse.Namespace) -> int:
    receipt = _load_receipt_value(
        args.receipt,
        control_root=args.control_root,
        label="run receipt",
        parser=RunReceipt.from_dict,
        inline_format="docspec-run-receipt",
    )
    failure_counts = receipt.failures.get("counts")
    if not isinstance(failure_counts, dict) or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in failure_counts.values()
    ):
        raise CliError("run receipt failure counts have an invalid shape")
    _emit(
        {
            "format": "docspec-run-status",
            "formatVersion": "1.0",
            "runId": receipt.run_id,
            "status": "completed",
            "stateful": receipt.stateful,
            "storeCount": receipt.store_count,
            "counts": receipt.counts,
            "failureCount": sum(failure_counts.values()),
            "completedAt": receipt.completed_at,
            "verificationScope": "run-receipt-structure",
            "verdict": "structurally-valid",
        }
    )
    return 0


def _cmd_sink_verify(args: argparse.Namespace) -> int:
    receipt = _load_receipt_value(
        args.receipt,
        control_root=args.control_root,
        label="delivery receipt",
        parser=DeliveryReceipt.from_dict,
        inline_format="docspec-delivery-receipt",
    )
    _emit(
        {
            "format": "docspec-sink-verification",
            "formatVersion": "1.0",
            "receiptId": receipt.receipt_id,
            "sinkId": receipt.sink_id,
            "profileId": receipt.profile_id,
            "storeId": receipt.store_id,
            "deliveredEntryCount": receipt.delivered_entry_count,
            "deliveredEntryPopulationDigest": receipt.delivered_entry_population_digest,
            "recordCount": receipt.record_count,
            "byteCount": receipt.byte_count,
            "acceptedRecordCount": receipt.accepted_record_count,
            "rejectedRecordCount": receipt.rejected_record_count,
            "retriedRecordCount": receipt.retried_record_count,
            "undeliveredRecordCount": receipt.undelivered_record_count,
            "finalVerdict": receipt.final_verdict.value,
            "layerCount": len(receipt.layers),
            "returnedResult": None if receipt.returned_result is None else receipt.returned_result.to_dict(),
            "verificationScope": "delivery-receipt-structure",
            "verdict": "structurally-valid",
        }
    )
    return 0


def _load_receipt_value(
    path: Path,
    *,
    control_root: Path | None,
    label: str,
    parser: Any,
    inline_format: str,
) -> Any:
    """Read a receipt directly or resolve the ArtifactRef emitted by a lifecycle command."""

    value = _read_canonical_object(path, label=label)
    if value.get("format") == inline_format:
        return parser(value)
    try:
        reference = ArtifactRef.from_dict(value)
    except (TypeError, ValueError) as error:
        raise CliError(f"{label} is neither an inline receipt nor an ArtifactRef: {error}") from error
    if control_root is None:
        raise CliError(f"{label} ArtifactRef requires --control-root")
    controls = object.__new__(LocalJsonControlRepository)
    controls.root = _existing_root(control_root, label="control repository root")
    controls.max_artifact_bytes = _MAX_JSON_BYTES
    controls.verify(reference)
    return parser(controls.load(reference))


def _load_release(path: Path) -> tuple[DocumentRelease, bytes]:
    payload = _read_bytes(path, label="document release")
    value = thaw_json(parse_canonical_json(payload, label="document release"))
    if not isinstance(value, dict):
        raise CliError("document release must be a JSON object")
    return DocumentRelease.from_dict(value), payload


def _cmd_document_release_verify(args: argparse.Namespace) -> int:
    release, payload = _load_release(args.release)
    _emit(
        {
            "format": "docspec-document-release-verification",
            "formatVersion": "1.0",
            "releaseId": release.release_id,
            "artifactDigest": sha256_digest(payload),
            "logicalStateDigest": release.logical_state_digest,
            "activeLayerCount": len(release.active_layers),
            "blobRootCount": len(release.blob_roots),
            "verificationScope": "canonical-release-root",
            "verdict": "structurally-valid",
        }
    )
    return 0


def _cmd_document_release_diff(args: argparse.Namespace) -> int:
    older, _ = _load_release(args.older)
    newer, _ = _load_release(args.newer)
    old_layers = {item.layer_kind: item for item in older.active_layers}
    new_layers = {item.layer_kind: item for item in newer.active_layers}
    layer_changes: list[dict[str, Any]] = []
    for kind in sorted(set(old_layers) | set(new_layers)):
        old = old_layers.get(kind)
        new = new_layers.get(kind)
        change = "unchanged"
        if old is None:
            change = "added"
        elif new is None:
            change = "deleted"
        elif old.to_dict() != new.to_dict():
            change = "changed"
        layer_changes.append(
            {
                "layerKind": kind,
                "change": change,
                "older": None if old is None else old.to_dict(),
                "newer": None if new is None else new.to_dict(),
            }
        )
    count_deltas = {
        name: newer.counts.get(name, 0) - older.counts.get(name, 0)
        for name in sorted(set(older.counts) | set(newer.counts))
    }
    _emit(
        {
            "format": "docspec-document-release-diff",
            "formatVersion": "1.0",
            "olderReleaseId": older.release_id,
            "newerReleaseId": newer.release_id,
            "logicalStateEqual": older.logical_state_digest == newer.logical_state_digest,
            "layerChanges": layer_changes,
            "countDeltas": count_deltas,
            "declaredPreviousRelease": None
            if newer.previous_release is None
            else newer.previous_release.to_dict(),
            "verdict": "pass",
        }
    )
    return 0


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


def _cmd_conformance_run(args: argparse.Namespace) -> int:
    report = run_conformance(
        source_root=args.root,
        specification_path=args.specification,
        matrix_path=args.matrix,
        output_path=args.output,
        conformance_class=args.conformance_class,
        timeout_seconds=args.timeout_seconds,
    )
    _emit(summarize_report(args.output))
    return 0 if report["verdict"] == "pass" else 1


def _cmd_conformance_report(args: argparse.Namespace) -> int:
    summary = summarize_report(args.report)
    _emit(summary)
    return 0 if summary["verdict"] == "pass" else 1


def _add_local_catalog_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--catalog-root", type=Path, required=True, help="Existing local document-catalog root")
    parser.add_argument("--blob-root", type=Path, required=True, help="Existing local immutable-blob root")
    parser.add_argument("--record-root", type=Path, required=True, help="Existing local record-storage root")
    parser.add_argument("--store-root", type=Path, required=True, help="Existing local document-store root")
    parser.add_argument("--control-root", type=Path, required=True, help="Existing local control-artifact root")
    parser.add_argument("--implementation-id", required=True)
    parser.add_argument("--verifier-implementation-id", required=True)


def _add_mutating_paths(
    parser: argparse.ArgumentParser,
    *,
    operation: str,
    func: Any,
) -> None:
    parser.add_argument("--request", type=Path, required=True, help="Closed JSON operation request")
    parser.add_argument("--destination", type=Path, required=True, help="New destination; replacement is refused")
    parser.add_argument("--receipt", type=Path, required=True, help="New machine receipt; replacement is refused")
    parser.set_defaults(func=func, operation=operation)


def _subcommands(parser: argparse.ArgumentParser, *, dest: str) -> argparse._SubParsersAction:
    return parser.add_subparsers(dest=dest, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="docspec", description=__doc__)
    commands = _subcommands(parser, dest="command")

    add_source_catalog_command(commands)

    profile = commands.add_parser("profile", help="Inspect storage and delivery profile descriptions")
    profile_commands = _subcommands(profile, dest="profile_command")
    profile_list = profile_commands.add_parser("list", help="List and verify every profile in an explicit directory")
    profile_list.add_argument("--directory", type=Path, required=True)
    profile_list.set_defaults(func=_cmd_profile_list)
    profile_verify = profile_commands.add_parser("verify", help="Verify one closed profile description")
    profile_verify.add_argument("profile", type=Path)
    profile_verify.set_defaults(func=_cmd_profile_verify)

    scale_profile = commands.add_parser("scale-profile", help="Seal and verify exact scale campaign inputs")
    scale_profile_commands = _subcommands(scale_profile, dest="scale_profile_command")
    _add_mutating_paths(
        scale_profile_commands.add_parser("seal", help="Seal closed scale-profile content"),
        operation="scale-profile.seal",
        func=_cmd_scale_profile_seal,
    )
    scale_profile_verify = scale_profile_commands.add_parser(
        "verify",
        help="Verify one canonical identity-bearing scale profile",
    )
    scale_profile_verify.add_argument("profile", type=Path)
    scale_profile_verify.set_defaults(func=_cmd_scale_profile_verify)

    document_catalog = commands.add_parser("document-catalog", help="Open and compare complete catalog releases")
    catalog_commands = _subcommands(document_catalog, dest="document_catalog_command")
    catalog_open = catalog_commands.add_parser("open", help="Verify and open an explicit release reference")
    _add_local_catalog_arguments(catalog_open)
    catalog_open.add_argument("--reference", type=Path, required=True)
    catalog_open.set_defaults(func=_cmd_document_catalog_open)
    catalog_compare = catalog_commands.add_parser("compare", help="Compare one logical layer across two releases")
    _add_local_catalog_arguments(catalog_compare)
    catalog_compare.add_argument("--older-reference", type=Path, required=True)
    catalog_compare.add_argument("--newer-reference", type=Path, required=True)
    catalog_compare.add_argument("--layer-kind", required=True)
    catalog_compare.add_argument("--sample-limit", type=int, default=20)
    catalog_compare.set_defaults(func=_cmd_document_catalog_compare)

    plan = commands.add_parser("plan", help="Create immutable processing plans")
    plan_commands = _subcommands(plan, dest="plan_command")
    plan_create = plan_commands.add_parser("create", help="Create a ProcessingPlan from a closed JSON request")
    plan_create.add_argument("--request", type=Path, required=True)
    plan_create.add_argument("--destination", type=Path, required=True)
    plan_create.add_argument("--receipt", type=Path, required=True)
    plan_create.set_defaults(func=_cmd_plan_create, operation="plan.create")

    document_store = commands.add_parser("document-store", help="Create and verify bounded work jobs")
    store_commands = _subcommands(document_store, dest="document_store_command")
    store_create = store_commands.add_parser("create", help="Create one planned DocumentStore")
    store_create.add_argument("--request", type=Path, required=True)
    store_create.add_argument("--destination", type=Path, required=True)
    store_create.add_argument("--receipt", type=Path, required=True)
    store_create.set_defaults(func=_cmd_document_store_create, operation="document-store.create")
    store_verify = store_commands.add_parser("verify", help="Verify one canonical DocumentStore revision")
    store_verify.add_argument("store", type=Path)
    store_verify.add_argument("--root", type=Path, help="Repository root for a saved store with entry members")
    store_verify.add_argument("--max-revision-bytes", type=int, default=64 * 1024**2)
    store_verify.add_argument("--max-inline-bytes", type=int, default=1024**2)
    store_verify.set_defaults(func=_cmd_document_store_verify)

    run = commands.add_parser("run", help="Start, resume, and inspect scheduler-neutral runs")
    run_commands = _subcommands(run, dest="run_command")
    _add_mutating_paths(
        run_commands.add_parser("prepare", help="Save bounded jobs and seal an execution handoff"),
        operation="run.prepare",
        func=_cmd_local_run_prepare,
    )
    _add_mutating_paths(
        run_commands.add_parser("start", help="Execute a new run through the portable local profile"),
        operation="run.start",
        func=_cmd_local_run,
    )
    _add_mutating_paths(
        run_commands.add_parser("resume", help="Resume saved local jobs and finish their run"),
        operation="run.resume",
        func=_cmd_local_run,
    )
    _add_mutating_paths(
        run_commands.add_parser("reconcile", help="Verify a saved terminal task-result stream"),
        operation="run.reconcile",
        func=_cmd_local_run_reconcile,
    )
    run_status = run_commands.add_parser("status", help="Verify and summarize a sealed RunReceipt")
    run_status.add_argument("--receipt", type=Path, required=True)
    run_status.add_argument("--control-root", type=Path, help="Resolve an ArtifactRef from this control repository")
    run_status.set_defaults(func=_cmd_run_status)

    task = commands.add_parser("task", help="Execute portable serialized DocumentStore tasks")
    task_commands = _subcommands(task, dest="task_command")
    _add_mutating_paths(
        task_commands.add_parser("execute", help="Execute one serialized task and emit one result"),
        operation="task.execute",
        func=_cmd_local_task_execute,
    )

    sink = commands.add_parser("sink", help="Verify result delivery evidence")
    sink_commands = _subcommands(sink, dest="sink_command")
    sink_verify = sink_commands.add_parser("verify", help="Verify and summarize a DeliveryReceipt")
    sink_verify.add_argument("--receipt", type=Path, required=True)
    sink_verify.add_argument("--control-root", type=Path, help="Resolve an ArtifactRef from this control repository")
    sink_verify.set_defaults(func=_cmd_sink_verify)

    release = commands.add_parser("document-release", help="Commit, verify, compare, and compact releases")
    release_commands = _subcommands(release, dest="document_release_command")
    _add_mutating_paths(
        release_commands.add_parser("commit", help="Commit a reconciled local run with compare-and-swap"),
        operation="document-release.commit",
        func=_cmd_document_release_commit,
    )
    release_verify = release_commands.add_parser("verify", help="Verify one canonical release root")
    release_verify.add_argument("release", type=Path)
    release_verify.set_defaults(func=_cmd_document_release_verify)
    release_diff = release_commands.add_parser("diff", help="Compare two complete release roots")
    release_diff.add_argument("--older", type=Path, required=True)
    release_diff.add_argument("--newer", type=Path, required=True)
    release_diff.set_defaults(func=_cmd_document_release_diff)
    _add_mutating_paths(
        release_commands.add_parser("compact", help="Publish an equivalent compacted successor release"),
        operation="document-release.compact",
        func=_cmd_document_release_compact,
    )

    blob_store = commands.add_parser("blob-store", help="Verify immutable blobs and inventory safe collection")
    blob_commands = _subcommands(blob_store, dest="blob_store_command")
    blob_verify = blob_commands.add_parser("verify", help="Verify one immutable blob reference")
    blob_verify.add_argument("--root", type=Path, required=True)
    blob_verify.add_argument("--reference", type=Path, required=True)
    blob_verify.add_argument("--max-blob-bytes", type=int, default=8 * 1024**3)
    blob_verify.add_argument("--stream-chunk-bytes", type=int, default=1024**2)
    blob_verify.set_defaults(func=_cmd_blob_store_verify)
    blob_gc = blob_commands.add_parser("gc", help="Inventory unreferenced content-addressed objects")
    blob_gc.add_argument("--run-request", type=Path, required=True)
    blob_gc.add_argument("--retention-set", type=Path, required=True, help="JSON ArtifactRef")
    blob_gc.add_argument("--minimum-age-seconds", type=int, required=True)
    blob_gc.add_argument("--sample-limit", type=int, default=20)
    blob_gc.add_argument("--max-index-bytes", type=int, default=64 * 1024**3)
    blob_gc.add_argument("--index-cache-kib", type=int, default=8 * 1024)
    blob_gc.add_argument("--dry-run", action="store_true", required=True)
    blob_gc.set_defaults(func=_cmd_blob_store_gc)

    conformance = commands.add_parser("conformance", help="Run and inspect executable conformance evidence")
    conformance_commands = _subcommands(conformance, dest="conformance_command")
    conformance_run = conformance_commands.add_parser("run", help="Execute every required selector and seal a report")
    conformance_run.add_argument("--root", type=Path, required=True)
    conformance_run.add_argument("--specification", type=Path, required=True)
    conformance_run.add_argument("--matrix", type=Path, required=True)
    conformance_run.add_argument("--output", type=Path, required=True)
    conformance_run.add_argument("--class", dest="conformance_class", default="core")
    conformance_run.add_argument("--timeout-seconds", type=int, default=600)
    conformance_run.set_defaults(func=_cmd_conformance_run)
    conformance_report = conformance_commands.add_parser("report", help="Verify and summarize an existing report")
    conformance_report.add_argument("report", type=Path)
    conformance_report.set_defaults(func=_cmd_conformance_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if hasattr(args, "sample_limit") and args.sample_limit < 0:
            raise CliError("sample limit must be non-negative")
        return int(args.func(args))
    except (DocSpecError, OSError, TypeError, ValueError) as error:
        _write_failure_receipt(args, error)
        _emit(
            {
                "format": "docspec-cli-error",
                "formatVersion": "1.0",
                "errorType": type(error).__name__,
                "message": redact_text(str(error)),
                "verdict": "fail",
            },
            error=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
