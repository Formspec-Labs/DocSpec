"""Persist stage receipts and classify execution failures consistently."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from docspec.domain.identity import stable_urn
from docspec.domain.jobs import FailureClass, FailureRecord
from docspec.domain.references import ArtifactRef
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.control_repository import ControlRepository


def put_receipt(controls: ControlRepository, kind: str, identity_kind: str, value: Mapping[str, Any]) -> ArtifactRef:
    artifact_id = stable_urn(identity_kind, value)
    return controls.put(kind=kind, artifact_id=artifact_id, value=value)


def failure_record(stage: str, error: Exception, attempt: int) -> FailureRecord:
    if isinstance(error, LimitExceededError):
        failure_class = FailureClass.DETERMINISTIC_INPUT
    elif isinstance(error, IntegrityError):
        failure_class = FailureClass.ARTIFACT_INTEGRITY
    elif isinstance(error, MemoryError):
        failure_class = FailureClass.TRANSIENT_RESOURCE
    elif isinstance(error, (TimeoutError, ConnectionError, OSError)):
        failure_class = FailureClass.TRANSIENT_EXTERNAL
    elif isinstance(error, (ValueError, TypeError)):
        failure_class = FailureClass.DETERMINISTIC_INPUT
    else:
        failure_class = FailureClass.IMPLEMENTATION_DEFECT
    retryable = failure_class in {FailureClass.TRANSIENT_EXTERNAL, FailureClass.TRANSIENT_RESOURCE}
    diagnostic = f"docspec.{stage}.{type(error).__name__.lower()}"
    detail = f"{stage} failed with {type(error).__name__}"
    return FailureRecord(failure_class, diagnostic, detail, attempt, retryable)
