"""Optional Dagster mapping over DocSpec's sealed, reference-only tasks.

The module has no import-time Dagster dependency.  Deployments install the
``dagster`` extra and call :func:`build_dagster_definitions`; DocSpec keeps task
identity, worker reconstruction, and result verification on its side of the
boundary.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import sqlite3
import stat
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Self
from urllib.parse import quote

from docspec.domain.execution import (
    MAX_HANDOFF_BYTES,
    MAX_RESULT_BYTES,
    MAX_TASK_BYTES,
    ExecutionHandoff,
    StoreTask,
    StoreTaskResult,
)
from docspec.domain.identity import (
    OrderedJsonSequenceDigester,
    canonical_json_file_bytes,
    freeze_json,
    identity_digest,
    parse_canonical_json,
    require_sha256,
    require_text,
    sha256_digest,
    stable_urn,
    thaw_json,
)
from docspec.domain.references import ArtifactRef
from docspec.errors import DocSpecError, IntegrityError, LimitExceededError, ProfileError

DAGSTER_ADAPTER_PROFILE_FORMAT = "docspec-scheduler-adapter-profile"
DAGSTER_ADAPTER_PROFILE_VERSION = "1.0"
DAGSTER_DEPLOYMENT_FORMAT = "docspec-dagster-deployment"
DAGSTER_DEPLOYMENT_VERSION = "1.0"
DAGSTER_MEMBERSHIP_VERIFICATION_FORMAT = "docspec-dagster-task-membership-verification"
DAGSTER_MEMBERSHIP_VERIFICATION_VERSION = "1.0"
DAGSTER_WORKER_REQUEST_FORMAT = "docspec-scheduler-worker-request"
DAGSTER_WORKER_REQUEST_VERSION = "1.0"
_DAGSTER_ADAPTER_ID = "docspec.scheduler.dagster.dynamic-process"
_DAGSTER_ADAPTER_VERSION = "1.0.0"
_DAGSTER_PACKAGE_REQUIREMENT = "dagster>=1.13,<2"
_DAGSTER_IMPLEMENTATION_MODULE = "docspec.adapters.dagster:build_dagster_definitions"
_DAGSTER_CONFIGURATION = freeze_json(
    {
        "coordinationStorage": "shared POSIX filesystem",
        "dynamicMappingUnit": "DocumentStore",
        "eventEvidence": "Dagster run event log",
        "executor": {
            "default": "dagster.multiprocess_executor",
            "selection": "injected Dagster ExecutorDefinition at the deployment composition root",
        },
        "resultPersistence": "one canonical StoreTaskResult file per task",
        "retryClassification": {
            "permanent": "configuration, integrity, limit, and unregistered exit failures",
            "transient": "worker timeout or sealed retryable exit code",
        },
        "taskLedger": "streaming canonical JSON Lines",
        "taskMembership": "digest-pinned SQLite lookup built from the sealed task ledger",
        "membershipVerification": "sealed digest and POSIX file-snapshot receipt",
        "workerBoundary": "subprocess request/result files",
        "workerReconstruction": "pinned ExecutionHandoff and worker composition",
    },
    label="registered Dagster adapter configuration",
)
_DAGSTER_CAPABILITIES = (
    "deploymentConfigurationSealed",
    "dynamicTaskMapping",
    "eventLog",
    "executorDefinitionInjection",
    "lazyOptionalImport",
    "multiprocessExecutorDefault",
    "nativeRetries",
    "referenceOnlySchedulerMessages",
    "replaySafeResultPersistence",
    "sealedTaskMembership",
    "streamedTaskLedger",
    "verifiedReaderReceipt",
    "workerProcessBoundary",
)
MAX_DAGSTER_PROFILE_BYTES = 64 * 1024
MAX_DAGSTER_DEPLOYMENT_BYTES = 128 * 1024
MAX_WORKER_REQUEST_BYTES = MAX_HANDOFF_BYTES + MAX_TASK_BYTES + 1024
MAX_TASK_MEMBERSHIP_BYTES = 4 * 1024**3
MAX_QUALIFICATION_EVIDENCE_BYTES = 16 * 1024**2
MAX_MEMBERSHIP_VERIFICATION_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 1024 * 1024


class DagsterAdapterError(DocSpecError):
    """A Dagster adapter configuration or worker boundary failed closed."""


class DagsterTransientWorkerError(DagsterAdapterError):
    """A sealed transient worker failure eligible for scheduler-owned retry."""


def _closed(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ProfileError(f"{label} has an invalid closed shape")
    return value


def _positive_integer(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ProfileError(f"{label} must be a positive integer")
    return value


def _non_negative_integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ProfileError(f"{label} must be a non-negative integer")
    return value


def _parse_canonical_object(data: bytes, *, maximum: int, label: str) -> dict[str, Any]:
    if not isinstance(data, bytes):
        raise TypeError(f"{label} must be canonical JSON bytes")
    if len(data) > maximum:
        raise LimitExceededError(f"{label} exceeds its {maximum}-byte limit")
    value = thaw_json(parse_canonical_json(data, label=label))
    if not isinstance(value, dict):
        raise IntegrityError(f"{label} must be a JSON object")
    return value


@dataclass(frozen=True, slots=True)
class DagsterAdapterProfile:
    """Versioned capability description for the optional Dagster adapter."""

    adapter_id: str
    adapter_version: str
    package_requirement: str
    implementation_module: str
    configuration: Mapping[str, Any]
    capabilities: tuple[str, ...]
    verifier_status: str
    verifier_test_id: str
    qualification_status: str
    qualification_evidence: ArtifactRef | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("Dagster adapter id", self.adapter_id),
            ("Dagster adapter version", self.adapter_version),
            ("Dagster package requirement", self.package_requirement),
            ("Dagster implementation module", self.implementation_module),
            ("Dagster verifier status", self.verifier_status),
            ("Dagster verifier test id", self.verifier_test_id),
            ("Dagster qualification status", self.qualification_status),
        ):
            require_text(value, label)
        if self.verifier_status not in {"partial", "implemented"}:
            raise ProfileError("Dagster verifier status is not registered")
        if self.qualification_status not in {"unrun", "passed"}:
            raise ProfileError("Dagster qualification status is not registered")
        if self.qualification_status == "unrun" and self.verifier_status != "partial":
            raise ProfileError("an unrun Dagster qualification must remain partial")
        if self.qualification_status == "passed" and (
            self.verifier_status != "implemented" or self.qualification_evidence is None
        ):
            raise ProfileError("a passed Dagster qualification requires implemented evidence")
        if self.qualification_status == "unrun" and self.qualification_evidence is not None:
            raise ProfileError("an unrun Dagster qualification must not claim evidence")
        if self.capabilities != tuple(sorted(set(self.capabilities))) or not self.capabilities:
            raise ProfileError("Dagster capabilities must be sorted, distinct, and non-empty")
        try:
            configuration = freeze_json(self.configuration, label="Dagster adapter configuration")
            if not isinstance(configuration, Mapping):
                raise ProfileError("Dagster adapter configuration must be an object")
            canonical_json_file_bytes(configuration)
        except (TypeError, ValueError) as error:
            raise ProfileError(f"Dagster adapter configuration is not canonical JSON: {error}") from error
        object.__setattr__(self, "configuration", configuration)

    @property
    def configuration_digest(self) -> str:
        return identity_digest(self.configuration)

    def identity_content(self) -> dict[str, Any]:
        return {
            "adapterId": self.adapter_id,
            "adapterVersion": self.adapter_version,
            "packageRequirement": self.package_requirement,
            "implementationStatus": "implemented",
            "implementationModule": self.implementation_module,
            "configuration": thaw_json(self.configuration),
            "configurationDigest": self.configuration_digest,
            "capabilities": list(self.capabilities),
        }

    @property
    def profile_id(self) -> str:
        return stable_urn("scheduler-adapter-profile", self.identity_content())

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": DAGSTER_ADAPTER_PROFILE_FORMAT,
            "formatVersion": DAGSTER_ADAPTER_PROFILE_VERSION,
            "profileId": self.profile_id,
            **self.identity_content(),
            "verifier": {
                "status": self.verifier_status,
                "testId": self.verifier_test_id,
                "qualificationStatus": self.qualification_status,
                "qualificationEvidence": None
                if self.qualification_evidence is None
                else self.qualification_evidence.to_dict(),
            },
        }

    def to_bytes(self) -> bytes:
        return canonical_json_file_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(
            value,
            {
                "format",
                "formatVersion",
                "profileId",
                "adapterId",
                "adapterVersion",
                "packageRequirement",
                "implementationStatus",
                "implementationModule",
                "configuration",
                "configurationDigest",
                "capabilities",
                "verifier",
            },
            "Dagster adapter profile",
        )
        if (
            item["format"] != DAGSTER_ADAPTER_PROFILE_FORMAT
            or item["formatVersion"] != DAGSTER_ADAPTER_PROFILE_VERSION
            or item["implementationStatus"] != "implemented"
        ):
            raise ProfileError("Dagster adapter profile has an unknown format")
        verifier = _closed(
            item["verifier"],
            {"status", "testId", "qualificationStatus", "qualificationEvidence"},
            "Dagster adapter verifier",
        )
        if not isinstance(item["configuration"], dict):
            raise ProfileError("Dagster adapter configuration must be an object")
        if not isinstance(item["capabilities"], list):
            raise ProfileError("Dagster adapter capabilities must be an array")
        result = cls(
            item["adapterId"],
            item["adapterVersion"],
            item["packageRequirement"],
            item["implementationModule"],
            item["configuration"],
            tuple(item["capabilities"]),
            verifier["status"],
            verifier["testId"],
            verifier["qualificationStatus"],
            None
            if verifier["qualificationEvidence"] is None
            else ArtifactRef.from_dict(verifier["qualificationEvidence"]),
        )
        if item["configurationDigest"] != result.configuration_digest:
            raise ProfileError("Dagster adapter configuration digest differs")
        if item["profileId"] != result.profile_id:
            raise ProfileError("Dagster adapter profile identity differs from its logical content")
        return result

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        return cls.from_dict(
            _parse_canonical_object(data, maximum=MAX_DAGSTER_PROFILE_BYTES, label="Dagster adapter profile")
        )


def registered_dagster_adapter_profile() -> DagsterAdapterProfile:
    """Return the exact logical adapter description implemented by this module."""

    return DagsterAdapterProfile(
        _DAGSTER_ADAPTER_ID,
        _DAGSTER_ADAPTER_VERSION,
        _DAGSTER_PACKAGE_REQUIREMENT,
        _DAGSTER_IMPLEMENTATION_MODULE,
        _DAGSTER_CONFIGURATION,
        _DAGSTER_CAPABILITIES,
        "partial",
        "SCHEDULER-PORTABILITY",
        "unrun",
    )


@dataclass(frozen=True, slots=True)
class PosixFileSnapshot:
    """Small change-detection identity for an exact file verified earlier."""

    device: int
    inode: int
    byte_size: int
    modified_ns: int
    changed_ns: int

    def __post_init__(self) -> None:
        for label, value in (
            ("POSIX file device", self.device),
            ("POSIX file inode", self.inode),
            ("POSIX file byte size", self.byte_size),
            ("POSIX file modified time", self.modified_ns),
            ("POSIX file changed time", self.changed_ns),
        ):
            _non_negative_integer(value, label)

    def to_dict(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "byteSize": self.byte_size,
            "modifiedNs": self.modified_ns,
            "changedNs": self.changed_ns,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(
            value,
            {"device", "inode", "byteSize", "modifiedNs", "changedNs"},
            "POSIX file snapshot",
        )
        return cls(
            item["device"],
            item["inode"],
            item["byteSize"],
            item["modifiedNs"],
            item["changedNs"],
        )

    @classmethod
    def capture(cls, path: Path, *, label: str) -> Self:
        try:
            details = os.stat(path, follow_symlinks=False)
        except OSError as error:
            raise IntegrityError(f"{label} cannot be inspected: {type(error).__name__}") from error
        if not stat.S_ISREG(details.st_mode):
            raise IntegrityError(f"{label} must be a regular, non-symlink file")
        return cls(
            details.st_dev,
            details.st_ino,
            details.st_size,
            details.st_mtime_ns,
            details.st_ctime_ns,
        )


@dataclass(frozen=True, slots=True)
class DagsterTaskMembershipVerification:
    """Sealed proof that one membership index exactly matched one task ledger."""

    handoff: ArtifactRef
    task_ledger: ArtifactRef
    task_membership: ArtifactRef
    handoff_id: str
    task_set_digest: str
    expected_task_count: int
    membership_snapshot: PosixFileSnapshot

    def __post_init__(self) -> None:
        require_text(self.handoff_id, "Dagster membership-verification handoff id")
        require_sha256(self.task_set_digest, "Dagster membership-verification task-set digest")
        _non_negative_integer(
            self.expected_task_count,
            "Dagster membership-verification expected task count",
        )
        if self.membership_snapshot.byte_size != self.task_membership.byte_size:
            raise ProfileError("Dagster membership-verification snapshot byte size differs")

    def identity_content(self) -> dict[str, Any]:
        return {
            "handoff": self.handoff.to_dict(),
            "taskLedger": self.task_ledger.to_dict(),
            "taskMembership": self.task_membership.to_dict(),
            "handoffId": self.handoff_id,
            "taskSetDigest": self.task_set_digest,
            "expectedTaskCount": self.expected_task_count,
            "membershipSnapshot": self.membership_snapshot.to_dict(),
        }

    @property
    def verification_id(self) -> str:
        return stable_urn("dagster-task-membership-verification", self.identity_content())

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": DAGSTER_MEMBERSHIP_VERIFICATION_FORMAT,
            "formatVersion": DAGSTER_MEMBERSHIP_VERIFICATION_VERSION,
            "verificationId": self.verification_id,
            **self.identity_content(),
        }

    def to_bytes(self) -> bytes:
        payload = canonical_json_file_bytes(self.to_dict())
        if len(payload) > MAX_MEMBERSHIP_VERIFICATION_BYTES:
            raise ProfileError("Dagster task-membership verification exceeds its serialized limit")
        return payload

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(
            value,
            {
                "format",
                "formatVersion",
                "verificationId",
                "handoff",
                "taskLedger",
                "taskMembership",
                "handoffId",
                "taskSetDigest",
                "expectedTaskCount",
                "membershipSnapshot",
            },
            "Dagster task-membership verification",
        )
        if (
            item["format"] != DAGSTER_MEMBERSHIP_VERIFICATION_FORMAT
            or item["formatVersion"] != DAGSTER_MEMBERSHIP_VERIFICATION_VERSION
        ):
            raise ProfileError("Dagster task-membership verification has an unknown format")
        result = cls(
            ArtifactRef.from_dict(item["handoff"]),
            ArtifactRef.from_dict(item["taskLedger"]),
            ArtifactRef.from_dict(item["taskMembership"]),
            item["handoffId"],
            item["taskSetDigest"],
            item["expectedTaskCount"],
            PosixFileSnapshot.from_dict(item["membershipSnapshot"]),
        )
        if item["verificationId"] != result.verification_id:
            raise ProfileError("Dagster task-membership verification identity differs")
        return result

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        return cls.from_dict(
            _parse_canonical_object(
                data,
                maximum=MAX_MEMBERSHIP_VERIFICATION_BYTES,
                label="Dagster task-membership verification",
            )
        )


@dataclass(frozen=True, slots=True)
class DagsterDeploymentConfig:
    """One sealed, deployment-owned Dagster job configuration."""

    adapter_profile: ArtifactRef
    handoff: ArtifactRef
    task_ledger: ArtifactRef
    task_membership: ArtifactRef
    task_membership_verification: ArtifactRef
    result_root: str
    worker_command: tuple[str, ...]
    max_concurrent_tasks: int
    max_retries: int
    retry_delay_seconds: int
    worker_timeout_seconds: int
    retryable_exit_codes: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.result_root, "Dagster result root")
        if not self.worker_command or any(not isinstance(item, str) or not item for item in self.worker_command):
            raise ProfileError("Dagster worker command must contain non-empty argument strings")
        _positive_integer(self.max_concurrent_tasks, "Dagster maximum concurrent tasks")
        _non_negative_integer(self.max_retries, "Dagster maximum retries")
        _non_negative_integer(self.retry_delay_seconds, "Dagster retry delay seconds")
        _positive_integer(self.worker_timeout_seconds, "Dagster worker timeout seconds")
        if self.handoff.byte_size > MAX_HANDOFF_BYTES:
            raise ProfileError("Dagster handoff exceeds the DocSpec handoff limit")
        if self.adapter_profile.byte_size > MAX_DAGSTER_PROFILE_BYTES:
            raise ProfileError("Dagster adapter profile exceeds its serialized limit")
        if self.task_membership.byte_size > MAX_TASK_MEMBERSHIP_BYTES:
            raise ProfileError("Dagster task membership exceeds its serialized limit")
        if self.task_membership_verification.byte_size > MAX_MEMBERSHIP_VERIFICATION_BYTES:
            raise ProfileError("Dagster task-membership verification exceeds its serialized limit")
        if any(type(code) is not int or code <= 0 or code > 255 for code in self.retryable_exit_codes):
            raise ProfileError("Dagster retryable exit codes must be integers from 1 through 255")
        if self.retryable_exit_codes != tuple(sorted(set(self.retryable_exit_codes))):
            raise ProfileError("Dagster retryable exit codes must be sorted and distinct")
        payload = canonical_json_file_bytes(self.to_dict())
        if len(payload) > MAX_DAGSTER_DEPLOYMENT_BYTES:
            raise ProfileError(f"Dagster deployment exceeds its {MAX_DAGSTER_DEPLOYMENT_BYTES}-byte limit")

    def identity_content(self) -> dict[str, Any]:
        return {
            "adapterProfile": self.adapter_profile.to_dict(),
            "handoff": self.handoff.to_dict(),
            "taskLedger": self.task_ledger.to_dict(),
            "taskMembership": self.task_membership.to_dict(),
            "taskMembershipVerification": self.task_membership_verification.to_dict(),
            "resultRoot": self.result_root,
            "workerCommand": list(self.worker_command),
            "maxConcurrentTasks": self.max_concurrent_tasks,
            "maxRetries": self.max_retries,
            "retryDelaySeconds": self.retry_delay_seconds,
            "workerTimeoutSeconds": self.worker_timeout_seconds,
            "retryableExitCodes": list(self.retryable_exit_codes),
        }

    @property
    def deployment_id(self) -> str:
        return stable_urn("dagster-deployment", self.identity_content())

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": DAGSTER_DEPLOYMENT_FORMAT,
            "formatVersion": DAGSTER_DEPLOYMENT_VERSION,
            "deploymentId": self.deployment_id,
            **self.identity_content(),
        }

    def to_bytes(self) -> bytes:
        return canonical_json_file_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> Self:
        fields = {
            "adapterProfile",
            "handoff",
            "taskLedger",
            "taskMembership",
            "taskMembershipVerification",
            "resultRoot",
            "workerCommand",
            "maxConcurrentTasks",
            "maxRetries",
            "retryDelaySeconds",
            "workerTimeoutSeconds",
            "retryableExitCodes",
        }
        item = _closed(
            value,
            fields | {"format", "formatVersion", "deploymentId"},
            "Dagster deployment",
        )
        if item["format"] != DAGSTER_DEPLOYMENT_FORMAT or item["formatVersion"] != DAGSTER_DEPLOYMENT_VERSION:
            raise ProfileError("Dagster deployment has an unknown format")
        if not isinstance(item["workerCommand"], list):
            raise ProfileError("Dagster worker command must be an array")
        if not isinstance(item["retryableExitCodes"], list):
            raise ProfileError("Dagster retryable exit codes must be an array")
        result = cls(
            ArtifactRef.from_dict(item["adapterProfile"]),
            ArtifactRef.from_dict(item["handoff"]),
            ArtifactRef.from_dict(item["taskLedger"]),
            ArtifactRef.from_dict(item["taskMembership"]),
            ArtifactRef.from_dict(item["taskMembershipVerification"]),
            item["resultRoot"],
            tuple(item["workerCommand"]),
            item["maxConcurrentTasks"],
            item["maxRetries"],
            item["retryDelaySeconds"],
            item["workerTimeoutSeconds"],
            tuple(item["retryableExitCodes"]),
        )
        if item["deploymentId"] != result.deployment_id:
            raise ProfileError("Dagster deployment identity differs")
        return result

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        return cls.from_dict(
            _parse_canonical_object(data, maximum=MAX_DAGSTER_DEPLOYMENT_BYTES, label="Dagster deployment")
        )


def _read_bounded_file(path: Path, *, maximum: int, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise IntegrityError(f"{label} must be a regular, non-symlink file")
    with path.open("rb") as handle:
        payload = handle.read(maximum + 1)
    if len(payload) > maximum:
        raise LimitExceededError(f"{label} exceeds its {maximum}-byte limit")
    return payload


def _matches_bounded_file(path: Path, payload: bytes, *, maximum: int, label: str) -> bool:
    if len(payload) > maximum:
        raise LimitExceededError(f"{label} comparison payload exceeds its {maximum}-byte limit")
    if path.is_symlink() or not path.is_file():
        return False
    if path.stat().st_size != len(payload):
        return False
    offset = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK_BYTES):
            if chunk != payload[offset : offset + len(chunk)]:
                return False
            offset += len(chunk)
    return offset == len(payload)


def _verified_file(reference: ArtifactRef, *, maximum: int, label: str) -> Path:
    path = Path(reference.locator)
    if path.is_symlink() or not path.is_file():
        raise IntegrityError(f"{label} must be a regular, non-symlink file")
    size = path.stat().st_size
    if size != reference.byte_size:
        raise IntegrityError(f"{label} byte size differs from its reference")
    if size > maximum:
        raise LimitExceededError(f"{label} exceeds its {maximum}-byte limit")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK_BYTES):
            digest.update(chunk)
    if f"sha256:{digest.hexdigest()}" != reference.digest:
        raise IntegrityError(f"{label} digest differs from its reference")
    return path


def _publish_immutable_bytes(target: Path, payload: bytes, *, label: str) -> None:
    if target.exists() or target.is_symlink():
        raise DagsterAdapterError(f"refusing to replace an existing {label}")
    parent = target.parent
    if parent.is_symlink() or not parent.is_dir():
        raise DagsterAdapterError(f"{label} parent must be an existing, non-symlink directory")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError as error:
            raise DagsterAdapterError(f"refusing to replace an existing {label}") from error
    finally:
        temporary.unlink(missing_ok=True)


def load_dagster_adapter_profile(deployment: DagsterDeploymentConfig) -> DagsterAdapterProfile:
    """Verify the exact adapter-profile bytes and the implementation's logical identity."""

    path = _verified_file(
        deployment.adapter_profile,
        maximum=MAX_DAGSTER_PROFILE_BYTES,
        label="Dagster adapter profile",
    )
    profile = DagsterAdapterProfile.from_bytes(
        _read_bounded_file(path, maximum=MAX_DAGSTER_PROFILE_BYTES, label="Dagster adapter profile")
    )
    if profile.profile_id != deployment.adapter_profile.artifact_id:
        raise IntegrityError("Dagster adapter profile identity differs from its reference")
    registered = registered_dagster_adapter_profile()
    if profile.identity_content() != registered.identity_content():
        raise ProfileError("Dagster adapter profile is not the logical profile implemented by this module")
    if profile.qualification_evidence is not None:
        _verified_file(
            profile.qualification_evidence,
            maximum=MAX_QUALIFICATION_EVIDENCE_BYTES,
            label="Dagster qualification evidence",
        )
    return profile


def load_dagster_deployment(path: str | Path) -> DagsterDeploymentConfig:
    """Load a canonical deployment and verify its exact adapter profile."""

    source = Path(path)
    deployment = DagsterDeploymentConfig.from_bytes(
        _read_bounded_file(source, maximum=MAX_DAGSTER_DEPLOYMENT_BYTES, label="Dagster deployment")
    )
    load_dagster_adapter_profile(deployment)
    return deployment


def _load_execution_handoff_reference(reference: ArtifactRef) -> ExecutionHandoff:
    path = _verified_file(reference, maximum=MAX_HANDOFF_BYTES, label="Dagster execution handoff")
    handoff = ExecutionHandoff.from_bytes(
        _read_bounded_file(path, maximum=MAX_HANDOFF_BYTES, label="Dagster execution handoff")
    )
    if handoff.handoff_id != reference.artifact_id:
        raise IntegrityError("Dagster execution handoff identity differs from its reference")
    return handoff


def load_execution_handoff(deployment: DagsterDeploymentConfig) -> ExecutionHandoff:
    return _load_execution_handoff_reference(deployment.handoff)


def _iter_verified_store_task_references(
    task_ledger: ArtifactRef,
    handoff: ExecutionHandoff,
) -> Iterator[StoreTask]:
    path = _verified_file(task_ledger, maximum=task_ledger.byte_size, label="Dagster task ledger")
    count = 0
    digester = OrderedJsonSequenceDigester()
    with path.open("rb") as handle:
        while line := handle.readline(MAX_TASK_BYTES + 2):
            if len(line) > MAX_TASK_BYTES + 1:
                raise LimitExceededError("Dagster task ledger member exceeds the DocSpec task limit")
            task = StoreTask.from_bytes(line)
            if task.processing_plan_id != handoff.processing_plan.artifact_id:
                raise IntegrityError("Dagster task names a different processing plan")
            if task.operation_id != handoff.operation_id:
                raise IntegrityError("Dagster task names a different execution operation")
            if count >= handoff.expected_task_count:
                raise IntegrityError("Dagster task ledger exceeds the sealed task count")
            digester.accept(task.to_dict())
            count += 1
            yield task
    if count != handoff.expected_task_count:
        raise IntegrityError("Dagster task ledger count differs from the execution handoff")
    if digester.finish() != handoff.task_set_digest:
        raise IntegrityError("Dagster task ledger population differs from the execution handoff")


def iter_verified_store_tasks(
    deployment: DagsterDeploymentConfig,
    *,
    handoff: ExecutionHandoff | None = None,
) -> Iterator[StoreTask]:
    """Coordinator path: prove the complete population once, then stream it."""

    load_dagster_adapter_profile(deployment)
    active_handoff = load_execution_handoff(deployment) if handoff is None else handoff
    with open_task_membership(
        deployment,
        handoff=active_handoff,
        verify_exact_artifact=True,
    ) as membership:
        for task in _iter_verified_store_task_references(deployment.task_ledger, active_handoff):
            membership.require(task)
            yield task


def _task_membership_identity(handoff: ExecutionHandoff, task_ledger: ArtifactRef) -> dict[str, Any]:
    return {
        "format": "docspec-dagster-task-membership",
        "formatVersion": "1.0",
        "handoffId": handoff.handoff_id,
        "taskSetDigest": handoff.task_set_digest,
        "expectedTaskCount": handoff.expected_task_count,
        "taskLedger": task_ledger.to_dict(),
    }


def _file_digest_and_size(path: Path, *, maximum: int, label: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK_BYTES):
            size += len(chunk)
            if size > maximum:
                raise LimitExceededError(f"{label} exceeds its {maximum}-byte limit")
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}", size


def build_task_membership_index(
    *,
    handoff: ArtifactRef,
    task_ledger: ArtifactRef,
    destination: str | Path,
) -> ArtifactRef:
    """Build one bounded disk index from a fully verified sealed task ledger."""

    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise DagsterAdapterError("refusing to replace an existing Dagster task-membership index")
    parent = target.parent
    if parent.is_symlink() or not parent.is_dir():
        raise DagsterAdapterError("Dagster task-membership parent must be an existing, non-symlink directory")
    active_handoff = _load_execution_handoff_reference(handoff)
    identity = _task_membership_identity(active_handoff, task_ledger)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temporary)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, task_digest TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO metadata (name, value) VALUES (?, ?)",
            ("identity", canonical_json_file_bytes(identity).decode("utf-8")),
        )
        try:
            for task in _iter_verified_store_task_references(task_ledger, active_handoff):
                connection.execute(
                    "INSERT INTO tasks (task_id, task_digest) VALUES (?, ?)",
                    (task.task_id, sha256_digest(task.to_bytes())),
                )
        except sqlite3.IntegrityError as error:
            raise IntegrityError("Dagster task ledger repeats a task identity") from error
        connection.commit()
        connection.close()
        connection = None
        digest, size = _file_digest_and_size(
            temporary,
            maximum=MAX_TASK_MEMBERSHIP_BYTES,
            label="Dagster task-membership index",
        )
        try:
            os.link(temporary, target)
        except FileExistsError as error:
            raise DagsterAdapterError("refusing to replace an existing Dagster task-membership index") from error
        return ArtifactRef(
            stable_urn("dagster-task-membership", identity),
            str(target),
            digest,
            "application/vnd.sqlite3",
            size,
        )
    finally:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)


class DagsterTaskMembership:
    """Read-only on-disk membership for one exact sealed task population."""

    __slots__ = ("_connection", "handoff_id")

    def __init__(self, connection: sqlite3.Connection, handoff_id: str) -> None:
        self._connection = connection
        self.handoff_id = handoff_id

    def require(self, task: StoreTask) -> None:
        row = self._connection.execute(
            "SELECT task_digest FROM tasks WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()
        if row is None or row[0] != sha256_digest(task.to_bytes()):
            raise IntegrityError("Dagster task is not a member of the sealed task ledger")


def _expected_task_membership_identity(
    handoff: ExecutionHandoff,
    task_ledger: ArtifactRef,
    task_membership: ArtifactRef,
) -> dict[str, Any]:
    identity = _task_membership_identity(handoff, task_ledger)
    expected_id = stable_urn("dagster-task-membership", identity)
    if task_membership.artifact_id != expected_id:
        raise IntegrityError("Dagster task-membership identity differs from the sealed task population")
    return identity


@contextmanager
def _open_task_membership_database(
    path: Path,
    *,
    expected_identity: Mapping[str, Any],
    handoff: ExecutionHandoff,
    verify_count: bool,
) -> Iterator[DagsterTaskMembership]:
    uri = f"file:{quote(str(path.resolve(strict=True)), safe='/')}?mode=ro&immutable=1"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        table_rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
        if table_rows != [("metadata",), ("tasks",)]:
            raise IntegrityError("Dagster task-membership index has an invalid closed schema")
        metadata = connection.execute("SELECT name, value FROM metadata ORDER BY name").fetchall()
        expected_metadata = [("identity", canonical_json_file_bytes(expected_identity).decode("utf-8"))]
        if metadata != expected_metadata:
            raise IntegrityError("Dagster task-membership index differs from the sealed task population")
        if verify_count:
            row = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()
            if row is None or row[0] != handoff.expected_task_count:
                raise IntegrityError("Dagster task-membership count differs from the execution handoff")
        yield DagsterTaskMembership(connection, handoff.handoff_id)
    except sqlite3.DatabaseError as error:
        raise IntegrityError("Dagster task-membership index is not a valid SQLite artifact") from error
    finally:
        if connection is not None:
            connection.close()


def seal_task_membership_verification(
    *,
    handoff: ArtifactRef,
    task_ledger: ArtifactRef,
    task_membership: ArtifactRef,
    destination: str | Path,
) -> ArtifactRef:
    """Fully prove one membership index and seal its bounded reader receipt."""

    active_handoff = _load_execution_handoff_reference(handoff)
    identity = _expected_task_membership_identity(active_handoff, task_ledger, task_membership)
    membership_path = Path(task_membership.locator)
    before = PosixFileSnapshot.capture(membership_path, label="Dagster task-membership index")
    verified_path = _verified_file(
        task_membership,
        maximum=MAX_TASK_MEMBERSHIP_BYTES,
        label="Dagster task-membership index",
    )
    if (
        PosixFileSnapshot.capture(
            membership_path,
            label="Dagster task-membership index",
        )
        != before
    ):
        raise IntegrityError("Dagster task-membership index changed during exact verification")
    with _open_task_membership_database(
        verified_path,
        expected_identity=identity,
        handoff=active_handoff,
        verify_count=True,
    ) as membership:
        for task in _iter_verified_store_task_references(task_ledger, active_handoff):
            membership.require(task)
    after = PosixFileSnapshot.capture(membership_path, label="Dagster task-membership index")
    if after != before:
        raise IntegrityError("Dagster task-membership index changed during population verification")
    verification = DagsterTaskMembershipVerification(
        handoff,
        task_ledger,
        task_membership,
        active_handoff.handoff_id,
        active_handoff.task_set_digest,
        active_handoff.expected_task_count,
        after,
    )
    payload = verification.to_bytes()
    target = Path(destination)
    _publish_immutable_bytes(target, payload, label="Dagster task-membership verification")
    return ArtifactRef(
        verification.verification_id,
        str(target),
        sha256_digest(payload),
        "application/json",
        len(payload),
    )


def load_task_membership_verification(
    deployment: DagsterDeploymentConfig,
    *,
    handoff: ExecutionHandoff,
) -> DagsterTaskMembershipVerification:
    """Verify the small receipt and the unchanged POSIX membership snapshot."""

    path = _verified_file(
        deployment.task_membership_verification,
        maximum=MAX_MEMBERSHIP_VERIFICATION_BYTES,
        label="Dagster task-membership verification",
    )
    verification = DagsterTaskMembershipVerification.from_bytes(
        _read_bounded_file(
            path,
            maximum=MAX_MEMBERSHIP_VERIFICATION_BYTES,
            label="Dagster task-membership verification",
        )
    )
    if verification.verification_id != deployment.task_membership_verification.artifact_id:
        raise IntegrityError("Dagster task-membership verification identity differs from its reference")
    if (
        verification.handoff != deployment.handoff
        or verification.task_ledger != deployment.task_ledger
        or verification.task_membership != deployment.task_membership
        or verification.handoff_id != handoff.handoff_id
        or verification.task_set_digest != handoff.task_set_digest
        or verification.expected_task_count != handoff.expected_task_count
    ):
        raise IntegrityError("Dagster task-membership verification differs from the deployment")
    _expected_task_membership_identity(handoff, deployment.task_ledger, deployment.task_membership)
    current = PosixFileSnapshot.capture(
        Path(deployment.task_membership.locator),
        label="Dagster task-membership index",
    )
    if current != verification.membership_snapshot:
        raise IntegrityError("Dagster task-membership index differs from its verified file snapshot")
    return verification


@contextmanager
def open_task_membership(
    deployment: DagsterDeploymentConfig,
    *,
    handoff: ExecutionHandoff | None = None,
    verify_exact_artifact: bool = False,
) -> Iterator[DagsterTaskMembership]:
    """Open verified membership with indexed, bounded per-task work."""

    active_handoff = load_execution_handoff(deployment) if handoff is None else handoff
    verification = load_task_membership_verification(deployment, handoff=active_handoff)
    path = Path(deployment.task_membership.locator)
    if verify_exact_artifact:
        path = _verified_file(
            deployment.task_membership,
            maximum=MAX_TASK_MEMBERSHIP_BYTES,
            label="Dagster task-membership index",
        )
        if (
            PosixFileSnapshot.capture(
                path,
                label="Dagster task-membership index",
            )
            != verification.membership_snapshot
        ):
            raise IntegrityError("Dagster task-membership index changed during exact verification")
    expected_identity = _task_membership_identity(active_handoff, deployment.task_ledger)
    with _open_task_membership_database(
        path,
        expected_identity=expected_identity,
        handoff=active_handoff,
        verify_count=verify_exact_artifact,
    ) as membership:
        if (
            PosixFileSnapshot.capture(
                path,
                label="Dagster task-membership index",
            )
            != verification.membership_snapshot
        ):
            raise IntegrityError("Dagster task-membership index changed while it was opened")
        yield membership
        if (
            PosixFileSnapshot.capture(
                path,
                label="Dagster task-membership index",
            )
            != verification.membership_snapshot
        ):
            raise IntegrityError("Dagster task-membership index changed while it was in use")


def worker_request_bytes(handoff: ExecutionHandoff, task: StoreTask) -> bytes:
    if task.processing_plan_id != handoff.processing_plan.artifact_id or task.operation_id != handoff.operation_id:
        raise IntegrityError("Dagster worker task is outside its execution handoff")
    payload = canonical_json_file_bytes(
        {
            "format": DAGSTER_WORKER_REQUEST_FORMAT,
            "formatVersion": DAGSTER_WORKER_REQUEST_VERSION,
            "handoff": handoff.to_dict(),
            "task": task.to_dict(),
        }
    )
    if len(payload) > MAX_WORKER_REQUEST_BYTES:
        raise LimitExceededError("Dagster worker request exceeds its serialized limit")
    return payload


def parse_worker_request(data: bytes) -> tuple[ExecutionHandoff, StoreTask]:
    value = _parse_canonical_object(data, maximum=MAX_WORKER_REQUEST_BYTES, label="Dagster worker request")
    item = _closed(value, {"format", "formatVersion", "handoff", "task"}, "Dagster worker request")
    if item["format"] != DAGSTER_WORKER_REQUEST_FORMAT or item["formatVersion"] != DAGSTER_WORKER_REQUEST_VERSION:
        raise IntegrityError("Dagster worker request has an unknown format")
    handoff = ExecutionHandoff.from_dict(item["handoff"])
    task = StoreTask.from_dict(item["task"])
    if task.processing_plan_id != handoff.processing_plan.artifact_id or task.operation_id != handoff.operation_id:
        raise IntegrityError("Dagster worker request task is outside its handoff")
    return handoff, task


def invoke_worker_process(
    deployment: DagsterDeploymentConfig,
    handoff: ExecutionHandoff,
    task: StoreTask,
) -> StoreTaskResult:
    """Cross a real process boundary using bounded files, not captured Python state."""

    load_dagster_adapter_profile(deployment)
    with tempfile.TemporaryDirectory(prefix="docspec-dagster-worker-") as directory:
        root = Path(directory)
        request_path = root / "request.json"
        result_path = root / "result.json"
        request_path.write_bytes(worker_request_bytes(handoff, task))
        try:
            completed = subprocess.run(
                [*deployment.worker_command, "--request", str(request_path), "--result", str(result_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=deployment.worker_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise DagsterTransientWorkerError("Dagster worker process exceeded its sealed timeout") from error
        except OSError as error:
            raise DagsterAdapterError(f"Dagster worker process could not start: {type(error).__name__}") from error
        if completed.returncode != 0:
            if completed.returncode in deployment.retryable_exit_codes:
                raise DagsterTransientWorkerError(
                    f"Dagster worker process returned sealed transient status {completed.returncode}"
                )
            raise DagsterAdapterError(f"Dagster worker process exited with status {completed.returncode}")
        result = StoreTaskResult.from_bytes(
            _read_bounded_file(result_path, maximum=MAX_RESULT_BYTES, label="Dagster worker result")
        )
    if result.handoff_id != handoff.handoff_id or result.task != task:
        raise IntegrityError("Dagster worker returned a result for a different handoff or task")
    return result


def _result_destination(deployment: DagsterDeploymentConfig, task: StoreTask) -> Path:
    root = Path(deployment.result_root)
    if root.is_symlink() or not root.is_dir():
        raise DagsterAdapterError("Dagster result root must be an existing, non-symlink directory")
    task_key = task.task_id.rsplit(":", 1)[-1]
    return root / f"{task_key}.json"


def _verify_result_membership(
    result: StoreTaskResult,
    handoff: ExecutionHandoff,
    membership: DagsterTaskMembership,
) -> None:
    if result.handoff_id != handoff.handoff_id or membership.handoff_id != handoff.handoff_id:
        raise IntegrityError("Dagster task result is outside its execution handoff")
    membership.require(result.task)


def _load_saved_result(
    deployment: DagsterDeploymentConfig,
    handoff: ExecutionHandoff,
    task: StoreTask,
    membership: DagsterTaskMembership,
) -> tuple[StoreTaskResult, Path] | None:
    destination = _result_destination(deployment, task)
    if not destination.exists() and not destination.is_symlink():
        return None
    result = StoreTaskResult.from_bytes(
        _read_bounded_file(destination, maximum=MAX_RESULT_BYTES, label="persisted Dagster result")
    )
    expected_name = f"{result.task.task_id.rsplit(':', 1)[-1]}.json"
    if destination.name != expected_name or result.task != task:
        raise IntegrityError("persisted Dagster result filename or task identity differs")
    _verify_result_membership(result, handoff, membership)
    return result, destination


def _persist_verified_task_result(
    deployment: DagsterDeploymentConfig,
    handoff: ExecutionHandoff,
    membership: DagsterTaskMembership,
    result: StoreTaskResult,
) -> Path:
    _verify_result_membership(result, handoff, membership)
    destination = _result_destination(deployment, result.task)
    payload = result.to_bytes()
    task_key = result.task.task_id.rsplit(":", 1)[-1]
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{task_key}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            saved = _load_saved_result(deployment, handoff, result.task, membership)
            if saved is None or not _matches_bounded_file(
                destination,
                payload,
                maximum=MAX_RESULT_BYTES,
                label="persisted Dagster result",
            ):
                raise IntegrityError("Dagster result replay conflicts with the saved task result")
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def persist_task_result(
    deployment: DagsterDeploymentConfig,
    result: StoreTaskResult,
    *,
    handoff: ExecutionHandoff | None = None,
) -> Path:
    """Publish one replay-safe task result to deployment-owned durable storage."""

    load_dagster_adapter_profile(deployment)
    active_handoff = load_execution_handoff(deployment) if handoff is None else handoff
    with open_task_membership(deployment, handoff=active_handoff) as membership:
        return _persist_verified_task_result(deployment, active_handoff, membership, result)


def execute_or_reuse_task(
    deployment: DagsterDeploymentConfig,
    handoff: ExecutionHandoff,
    task: StoreTask,
) -> tuple[StoreTaskResult, Path, bool]:
    """Reuse one verified terminal result or invoke the worker exactly once."""

    load_dagster_adapter_profile(deployment)
    with open_task_membership(deployment, handoff=handoff) as membership:
        membership.require(task)
        saved = _load_saved_result(deployment, handoff, task, membership)
        if saved is not None:
            return saved[0], saved[1], True
        result = invoke_worker_process(deployment, handoff, task)
        destination = _persist_verified_task_result(deployment, handoff, membership, result)
        return result, destination, False


def iter_persisted_task_results(deployment: DagsterDeploymentConfig) -> Iterator[StoreTaskResult]:
    """Stream terminal results without a corpus-sized in-memory collection."""

    load_dagster_adapter_profile(deployment)
    root = Path(deployment.result_root)
    if root.is_symlink() or not root.is_dir():
        raise DagsterAdapterError("Dagster result root must be an existing, non-symlink directory")
    handoff = load_execution_handoff(deployment)
    with open_task_membership(deployment, handoff=handoff) as membership, os.scandir(root) as members:
        for member in members:
            if member.name.startswith(".") and member.name.endswith(".tmp"):
                continue
            if not member.name.endswith(".json") or member.is_symlink() or not member.is_file(follow_symlinks=False):
                raise IntegrityError("Dagster result root contains an unexpected member")
            result = StoreTaskResult.from_bytes(
                _read_bounded_file(
                    Path(member.path),
                    maximum=MAX_RESULT_BYTES,
                    label="persisted Dagster result",
                )
            )
            expected_name = f"{result.task.task_id.rsplit(':', 1)[-1]}.json"
            if member.name != expected_name:
                raise IntegrityError("persisted Dagster result filename differs from its task identity")
            _verify_result_membership(result, handoff, membership)
            yield result


def _load_dagster() -> ModuleType:
    try:
        return importlib.import_module("dagster")
    except ModuleNotFoundError as error:
        raise DagsterAdapterError("the optional 'dagster' package is required for Dagster definitions") from error


def dagster_run_config(
    deployment_path: str | Path,
    deployment: DagsterDeploymentConfig,
    *,
    execution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the native run configuration pinned by one deployment file."""

    return {
        "resources": {"docspec_deployment": {"config": {"path": str(Path(deployment_path))}}},
        "execution": (
            {"config": {"max_concurrent": deployment.max_concurrent_tasks}}
            if execution is None
            else deepcopy(dict(execution))
        ),
    }


def build_dagster_definitions(*, executor_def: Any | None = None) -> Any:
    """Build the dynamic job with a deployment-injected Dagster executor."""

    dagster = _load_dagster()
    selected_executor = dagster.multiprocess_executor if executor_def is None else executor_def

    @dagster.resource(config_schema={"path": str})
    def docspec_deployment(context) -> DagsterDeploymentConfig:
        return load_dagster_deployment(context.resource_config["path"])

    @dagster.op(required_resource_keys={"docspec_deployment"}, out=dagster.DynamicOut(str))
    def emit_store_tasks(context) -> Iterator[Any]:
        deployment = context.resources.docspec_deployment
        handoff = load_execution_handoff(deployment)
        for task in iter_verified_store_tasks(deployment, handoff=handoff):
            mapping_key = task.task_id.rsplit(":", 1)[-1]
            yield dagster.DynamicOutput(task.to_bytes().decode("utf-8"), mapping_key=mapping_key)

    @dagster.op(required_resource_keys={"docspec_deployment"})
    def execute_store_task(context, task_payload: str) -> str:
        deployment = context.resources.docspec_deployment
        handoff = load_execution_handoff(deployment)
        task = StoreTask.from_bytes(task_payload.encode("utf-8"))
        try:
            _result, destination, _reused = execute_or_reuse_task(deployment, handoff, task)
            return str(destination)
        except DagsterTransientWorkerError as error:
            raise dagster.RetryRequested(
                max_retries=deployment.max_retries,
                seconds_to_wait=deployment.retry_delay_seconds,
            ) from error

    @dagster.job(
        name="docspec_store_tasks",
        resource_defs={"docspec_deployment": docspec_deployment},
        executor_def=selected_executor,
    )
    def docspec_store_tasks() -> None:
        emit_store_tasks().map(execute_store_task)

    return dagster.Definitions(jobs=[docspec_store_tasks])


def adapter_profile_file_digest(data: bytes) -> str:
    """Verify one adapter profile and return the exact deployable file digest."""

    profile = DagsterAdapterProfile.from_bytes(data)
    if profile.identity_content() != registered_dagster_adapter_profile().identity_content():
        raise ProfileError("Dagster adapter profile is not the logical profile implemented by this module")
    return sha256_digest(data)
