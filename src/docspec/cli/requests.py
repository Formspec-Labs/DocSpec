"""DocSpec command requests: closed local-run request parsing and typed argument adaptation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
from docspec.profile_registry import BUILTIN_PROFILE_DIRECTORY
from docspec.runtime.composition import _utc_instant, _verify_plan_policies
from docspec.runtime.defaults import local_execution_limits
from docspec.runtime.storage import _local_profiles, _local_storage
from docspec.workspace import LocalWorkspace

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


_LOCAL_DEFAULT_LIMITS = local_execution_limits()
_LOCAL_EXECUTION_OPTIONAL_DEFAULTS = {
    "maxWorkers": _LOCAL_DEFAULT_LIMITS.worker_count,
    **{key: value for key, value in _LOCAL_DEFAULT_LIMITS.to_dict().items()
       if key not in {"workerCount", "maxInFlight"}},
}


def _absolute_request_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CliError(f"{label} must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise CliError(f"{label} must be an absolute path")
    return path


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
    if value["format"] != "docspec-local-run-request" or value["formatVersion"] != "3.0":
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
    for name, setting in execution.items():
        if type(setting) is not int or setting < 1:
            raise CliError(f"local run execution setting {name} must be a positive integer")
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


def _local_run_arguments(request: dict[str, Any]) -> dict[str, Any]:
    """Adapt parsed command input once into the supported typed runtime API."""
    execution = request["execution"]
    return {
        "plan": ProcessingPlan.from_dict(_read_canonical_object(request["plan"], label="processing plan")),
        "workspace": LocalWorkspace(request["workspace"], request["roots"], request["profileDirectory"]),
        "retry_policy": request["retryPolicy"],
        "accepted_failure_policy": request["acceptedFailurePolicy"],
        "source_catalog_producer": request["sourceCatalogProducer"],
        "document_release_producer": request["documentReleaseProducer"],
        "execution_limits": local_execution_limits(
            worker_count=execution["maxWorkers"], max_in_flight=execution["maxInFlight"],
            max_task_index_bytes=execution["maxTaskIndexBytes"],
        ),
        "deadline_epoch_seconds": execution["deadlineEpochSeconds"],
        "completed_at": request["completedAt"],
        "partition_policy_id": request["partitionPolicyId"],
        "result_sink_id": request["resultSinkId"],
    }


def _local_storage_for_run_request(path: Path):
    """Parse one request and open its verified storage without preparing tasks."""
    request = _local_run_request(path)
    arguments = _local_run_arguments(request)
    plan, workspace = arguments["plan"], arguments["workspace"]
    _verify_plan_policies(plan, arguments["retry_policy"], arguments["accepted_failure_policy"])
    profiles = _local_profiles(plan, workspace)
    return request, plan, *_local_storage(workspace.roots, profiles, arguments["document_release_producer"])
