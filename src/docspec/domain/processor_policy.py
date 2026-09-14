"""Data-use authorization and retained provider evidence for document processors."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from docspec.domain.identity import closed_mapping, freeze_json, identity_digest, require_sha256, require_text, stable_urn, thaw_json
from docspec.domain.security import require_secret_free


PROCESSOR_DATA_FIELDS = (
    "content",
    "contentMediaType",
    "evidence",
    "prerequisiteResults",
    "representationCoordinates",
    "segmentKind",
    "segmentOrdinal",
)


def _array(value: object, label: str) -> tuple[Any, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray, memoryview)):
        raise ValueError(f"{label} must be an array")
    return tuple(value)


class ProcessorExecutionScope(StrEnum):
    """Whether a processor may cross the local execution boundary."""

    LOCAL_ONLY = "local-only"
    DECLARED_EXTERNAL = "declared-external"


class ProviderEvidenceMode(StrEnum):
    """How a processor adapter preserves its provider interaction evidence."""

    DIGEST_ONLY = "digest-only"
    REDACTED_RECORD = "redacted-record"


@dataclass(frozen=True, slots=True)
class ProviderEvidence:
    """One request or response preserved exactly as its data-use policy permits."""

    mode: ProviderEvidenceMode
    digest: str
    redacted_record: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        try:
            mode = ProviderEvidenceMode(self.mode)
        except (TypeError, ValueError) as error:
            raise ValueError("provider evidence mode is not registered") from error
        object.__setattr__(self, "mode", mode)
        require_sha256(self.digest, "provider evidence digest")
        if mode is ProviderEvidenceMode.DIGEST_ONLY:
            if self.redacted_record is not None:
                raise ValueError("digest-only provider evidence must not persist a record")
            return
        record = thaw_json(freeze_json(self.redacted_record, label="redacted provider evidence"))
        if not isinstance(record, dict):
            raise ValueError("redacted provider evidence must be a JSON object")
        require_secret_free(record, label="redacted provider evidence")
        if identity_digest(record) != self.digest:
            raise ValueError("redacted provider evidence digest differs from its record")
        object.__setattr__(self, "redacted_record", record)

    @classmethod
    def digest_only(cls, digest: str) -> ProviderEvidence:
        return cls(ProviderEvidenceMode.DIGEST_ONLY, digest)

    @classmethod
    def redacted(cls, record: Mapping[str, Any]) -> ProviderEvidence:
        frozen = thaw_json(freeze_json(record, label="redacted provider evidence"))
        if not isinstance(frozen, dict):
            raise ValueError("redacted provider evidence must be a JSON object")
        return cls(ProviderEvidenceMode.REDACTED_RECORD, identity_digest(frozen), frozen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "digest": self.digest,
            "redactedRecord": self.redacted_record,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProviderEvidence:
        raw = closed_mapping(value, {"mode", "digest", "redactedRecord"}, "provider evidence", error=ValueError)
        return cls(raw["mode"], raw["digest"], raw["redactedRecord"])


@dataclass(frozen=True, slots=True)
class ProviderInteractionEvidence:
    """Provider-neutral request and response evidence for one external invocation."""

    provider_id: str
    request: ProviderEvidence
    response: ProviderEvidence

    def __post_init__(self) -> None:
        require_text(self.provider_id, "provider identity")
        if not isinstance(self.request, ProviderEvidence) or not isinstance(self.response, ProviderEvidence):
            raise TypeError("provider interaction evidence requires typed request and response evidence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "providerId": self.provider_id,
            "request": self.request.to_dict(),
            "response": self.response.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProviderInteractionEvidence:
        raw = closed_mapping(
            value,
            {"providerId", "request", "response"},
            "provider interaction evidence",
            error=ValueError,
        )
        return cls(
            raw["providerId"],
            ProviderEvidence.from_dict(raw["request"]),
            ProviderEvidence.from_dict(raw["response"]),
        )


@dataclass(frozen=True, slots=True)
class DataUsePolicy:
    """Closed field and provider rules enforced before processor invocation."""

    policy_id: str
    execution_scope: ProcessorExecutionScope
    allowed_fields: tuple[str, ...]
    request_evidence: ProviderEvidenceMode
    response_evidence: ProviderEvidenceMode

    def __post_init__(self) -> None:
        require_text(self.policy_id, "data-use policy identity")
        try:
            scope = ProcessorExecutionScope(self.execution_scope)
            request_mode = ProviderEvidenceMode(self.request_evidence)
            response_mode = ProviderEvidenceMode(self.response_evidence)
        except (TypeError, ValueError) as error:
            raise ValueError("data-use policy contains an unregistered mode") from error
        object.__setattr__(self, "execution_scope", scope)
        object.__setattr__(self, "request_evidence", request_mode)
        object.__setattr__(self, "response_evidence", response_mode)
        if not isinstance(self.allowed_fields, tuple):
            raise ValueError("data-use allowed fields must be an immutable tuple")
        for field_name in self.allowed_fields:
            require_text(field_name, "data-use allowed field")
        if tuple(sorted(set(self.allowed_fields))) != self.allowed_fields or not self.allowed_fields:
            raise ValueError("data-use allowed fields must be non-empty, sorted, and distinct")
        unknown = set(self.allowed_fields) - set(PROCESSOR_DATA_FIELDS)
        if unknown:
            raise ValueError(f"data-use policy contains unknown allowed fields: {sorted(unknown)}")
        if self.policy_id != stable_urn("data-use-policy", self.identity_content()):
            raise ValueError("data-use policy identity differs")

    @classmethod
    def create(
        cls,
        *,
        execution_scope: ProcessorExecutionScope,
        allowed_fields: tuple[str, ...],
        request_evidence: ProviderEvidenceMode = ProviderEvidenceMode.DIGEST_ONLY,
        response_evidence: ProviderEvidenceMode = ProviderEvidenceMode.DIGEST_ONLY,
    ) -> DataUsePolicy:
        fields = tuple(sorted(set(allowed_fields)))
        content = {
            "executionScope": ProcessorExecutionScope(execution_scope).value,
            "allowedFields": list(fields),
            "requestEvidence": ProviderEvidenceMode(request_evidence).value,
            "responseEvidence": ProviderEvidenceMode(response_evidence).value,
        }
        return cls(
            stable_urn("data-use-policy", content),
            ProcessorExecutionScope(execution_scope),
            fields,
            ProviderEvidenceMode(request_evidence),
            ProviderEvidenceMode(response_evidence),
        )

    @classmethod
    def local_content(cls) -> DataUsePolicy:
        return cls.create(
            execution_scope=ProcessorExecutionScope.LOCAL_ONLY,
            allowed_fields=PROCESSOR_DATA_FIELDS,
        )

    def identity_content(self) -> dict[str, Any]:
        return {
            "executionScope": self.execution_scope.value,
            "allowedFields": list(self.allowed_fields),
            "requestEvidence": self.request_evidence.value,
            "responseEvidence": self.response_evidence.value,
        }

    @property
    def digest(self) -> str:
        return identity_digest(self.to_dict())

    @property
    def allows_external_processing(self) -> bool:
        return self.execution_scope is ProcessorExecutionScope.DECLARED_EXTERNAL

    def require_provider_evidence(
        self,
        evidence: ProviderInteractionEvidence | None,
        *,
        external: bool,
    ) -> None:
        if not external:
            if evidence is not None:
                raise ValueError("a local processor must not attach external provider evidence")
            return
        if not self.allows_external_processing:
            raise ValueError("the data-use policy does not allow external processing")
        if evidence is None:
            raise ValueError("external processing requires provider request and response evidence")
        if evidence.request.mode is not self.request_evidence:
            raise ValueError("provider request evidence differs from the data-use policy")
        if evidence.response.mode is not self.response_evidence:
            raise ValueError("provider response evidence differs from the data-use policy")

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "docspec-data-use-policy",
            "formatVersion": "1.0",
            "policyId": self.policy_id,
            **self.identity_content(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DataUsePolicy:
        raw = closed_mapping(
            value,
            {
                "format",
                "formatVersion",
                "policyId",
                "executionScope",
                "allowedFields",
                "requestEvidence",
                "responseEvidence",
            },
            "data-use policy",
            error=ValueError,
        )
        if raw["format"] != "docspec-data-use-policy" or raw["formatVersion"] != "1.0":
            raise ValueError("data-use policy has an unknown format")
        return cls(
            policy_id=raw["policyId"],
            execution_scope=raw["executionScope"],
            allowed_fields=_array(raw["allowedFields"], "data-use allowed fields"),
            request_evidence=raw["requestEvidence"],
            response_evidence=raw["responseEvidence"],
        )



@dataclass(frozen=True, slots=True)
class ProcessorLimits:
    max_input_bytes: int = 64 * 1024**2
    max_output_bytes: int = 8 * 1024**2
    max_output_records: int = 2048
    max_duration_seconds: int = 60

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in self.to_dict().values()):
            raise ValueError("processor limits must be positive integers")

    def to_dict(self):
        return {"maxInputBytes": self.max_input_bytes, "maxOutputBytes": self.max_output_bytes,
                "maxOutputRecords": self.max_output_records, "maxDurationSeconds": self.max_duration_seconds}


@dataclass(frozen=True, slots=True)
class ProcessorResponse:
    """Provider values and observations; Core owns request and result identity."""
    values: tuple[Any, ...]
    media_type: str
    resources: tuple[dict[str, Any], ...] = ()
    provider_evidence: ProviderInteractionEvidence | None = None
    external_request_count: int = 0

    def __post_init__(self):
        require_text(self.media_type, "processor output media type")
        if type(self.external_request_count) is not int or self.external_request_count < 0:
            raise ValueError("external request count must be a non-negative integer")
        if self.provider_evidence is not None and not isinstance(self.provider_evidence, ProviderInteractionEvidence):
            raise TypeError("processor provider evidence must use ProviderInteractionEvidence")
