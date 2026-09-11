"""DocSpec command requests: closed local-run requests and storage setup."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rulespec_artifacts import Producer

from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
    LocalJsonlRecordStorage,
    LocalManifestDocumentCatalog,
)
from docspec.cli.common import _producer_record, _read_canonical_object
from docspec.cli_io import (
    CliError,
)
from docspec.cli_io import (
    read_object as _read_json_object,
)
from docspec.domain.jobs import FailureClass
from docspec.domain.plans import ProcessingPlan
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.profiles import ProfileRole
from docspec.processing.extraction import DefaultExtractorRegistry
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.processing.segmentation import DefaultSegmenterRegistry
from docspec.profile_registry import BUILTIN_PROFILE_DIRECTORY, ProfileRegistry, RegisteredProfile
from docspec.workspace import LocalWorkspace

_LOCAL_PROFILE_SET_ID = "urn:docspec:profile-set:portable-local:1"


_LOCAL_PROFILE_MODULES = {
    ProfileRole.RELEASE_MANIFEST: "docspec.domain.release:DocumentRelease",
    ProfileRole.DOCUMENT_CATALOG: "docspec.adapters.storage:LocalManifestDocumentCatalog",
    ProfileRole.RECORD_STORAGE: "docspec.adapters.storage:LocalJsonlRecordStorage",
    ProfileRole.BLOB_STORAGE: "docspec.adapters.storage:LocalContentAddressedBlobStore",
    ProfileRole.DOCUMENT_STORE: "docspec.adapters.storage:LocalDocumentStoreRepository",
    ProfileRole.RESULT_DELIVERY: "docspec.adapters.sinks:DurableDatasetSink",
}


_LOCAL_RUN_FIELDS = {
    "format",
    "formatVersion",
    "plan",
    "workspace",
    "retryPolicy",
    "acceptedFailurePolicy",
    "execution",
    "completedAt",
    "documentReleaseProducer",
    "sourceCatalogProducer",
}

_LOCAL_RUN_OPTIONAL_FIELDS = {"profileDirectory", "roots", "resultSinkId", "partitionPolicyId"}


_LOCAL_EXECUTION_REQUIRED_FIELDS = {"deadlineEpochSeconds"}


_LOCAL_EXECUTION_OPTIONAL_DEFAULTS = {
    "maxWorkers": 1,
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
    if not _LOCAL_RUN_FIELDS <= set(value) or not set(value) <= _LOCAL_RUN_FIELDS | _LOCAL_RUN_OPTIONAL_FIELDS:
        raise CliError("local run request has an invalid closed shape")
    if value["format"] != "docspec-local-run-request" or value["formatVersion"] != "2.0":
        raise CliError("local run request has an unknown format")
    try:
        workspace = LocalWorkspace(
            value["workspace"],
            value.get("roots", {}),
            value.get("profileDirectory", BUILTIN_PROFILE_DIRECTORY),
        )
    except ValueError as error:
        raise CliError(str(error)) from error
    execution = value["execution"]
    allowed_execution_fields = _LOCAL_EXECUTION_REQUIRED_FIELDS | set(_LOCAL_EXECUTION_OPTIONAL_DEFAULTS) | {"maxInFlight"}
    if (
        not isinstance(execution, dict)
        or not _LOCAL_EXECUTION_REQUIRED_FIELDS <= set(execution)
        or not set(execution) <= allowed_execution_fields
    ):
        raise CliError("local run execution settings have an invalid closed shape")
    execution = {**_LOCAL_EXECUTION_OPTIONAL_DEFAULTS, **execution}
    execution.setdefault("maxInFlight", execution["maxWorkers"])
    zero_allowed = {"retryInitialDelayMilliseconds", "retryMaxDelayMilliseconds"}
    for name, setting in execution.items():
        minimum = 0 if name in zero_allowed else 1
        if type(setting) is not int or setting < minimum:
            raise CliError(f"local run execution setting {name} must be an integer of at least {minimum}")
    if execution["retryMaxDelayMilliseconds"] < execution["retryInitialDelayMilliseconds"]:
        raise CliError("local run execution retry maximum must not be less than its initial delay")
    value = {"resultSinkId": "urn:docspec:local:sink", "partitionPolicyId": "source-item-sha256-v1", **value}
    for name in ("resultSinkId", "partitionPolicyId"):
        if not isinstance(value[name], str) or not value[name]:
            raise CliError(f"local run {name} must be a non-empty string")
    return {
        **value,
        "plan": _absolute_request_path(value["plan"], label="local run plan"),
        "workspace": workspace.root,
        "profileDirectory": workspace.profile_directory,
        "roots": workspace.roots,
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
