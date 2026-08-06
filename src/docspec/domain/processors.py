"""Closed descriptions for injected, provider-neutral content processors."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable

from docspec.domain.content import DerivedRecord, EvidenceCoordinate, ProcessorDisposition, Segment
from docspec.domain.identity import (
    freeze_json,
    identity_digest,
    require_sha256,
    require_text,
    sha256_digest,
    stable_urn,
    thaw_json,
)
from docspec.domain.references import ArtifactRef
from docspec.domain.policies import (
    PROCESSOR_DATA_FIELDS,
    ProcessorExecutionScope,
    ProviderInteractionEvidence,
)
from docspec.errors import IntegrityError


def _closed_shape(value: object, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{label} has an invalid closed shape")
    return value


def _sequence(value: object, label: str) -> tuple[Any, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray, memoryview)):
        raise ValueError(f"{label} must be an array")
    return tuple(value)


def _validate_sorted_text(values: tuple[str, ...], label: str, *, allow_empty: bool = True) -> None:
    if not allow_empty and not values:
        raise ValueError(f"{label} must not be empty")
    for value in values:
        require_text(value, label)
    if tuple(sorted(set(values))) != values:
        raise ValueError(f"{label} must be sorted and distinct")


@dataclass(frozen=True, slots=True)
class ProcessorInput:
    """One accepted DocSpec record kind and its schema and media surfaces."""

    record_kind: str
    schema_ids: tuple[str, ...]
    media_types: tuple[str, ...]

    def __post_init__(self) -> None:
        require_text(self.record_kind, "processor input record_kind")
        if not isinstance(self.schema_ids, tuple) or not isinstance(self.media_types, tuple):
            raise ValueError("processor input schemas and media types must be immutable tuples")
        _validate_sorted_text(self.schema_ids, "processor input schema_ids", allow_empty=False)
        _validate_sorted_text(self.media_types, "processor input media_types", allow_empty=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recordKind": self.record_kind,
            "schemaIds": list(self.schema_ids),
            "mediaTypes": list(self.media_types),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProcessorInput:
        raw = _closed_shape(value, {"recordKind", "schemaIds", "mediaTypes"}, "processor input")
        return cls(
            record_kind=raw["recordKind"],
            schema_ids=_sequence(raw["schemaIds"], "processor input schemaIds"),
            media_types=_sequence(raw["mediaTypes"], "processor input mediaTypes"),
        )


class ProcessorResourceKind(StrEnum):
    """Provider-neutral roles for resources whose exact identities affect output."""

    MODEL = "model"
    REFERENCE_DATA = "reference-data"
    SOFTWARE = "software"


@dataclass(frozen=True, slots=True)
class ProcessorResourceIdentity:
    """A digest-pinned external resource or model used by a processor."""

    resource_id: str
    resource_kind: ProcessorResourceKind
    revision: str
    identity_digest: str

    def __post_init__(self) -> None:
        require_text(self.resource_id, "processor resource_id")
        require_text(self.revision, "processor resource revision")
        require_sha256(self.identity_digest, "processor resource identity_digest")
        try:
            kind = ProcessorResourceKind(self.resource_kind)
        except (TypeError, ValueError) as error:
            raise ValueError("processor resource kind is not registered") from error
        object.__setattr__(self, "resource_kind", kind)

    def to_dict(self) -> dict[str, str]:
        return {
            "resourceId": self.resource_id,
            "resourceKind": self.resource_kind.value,
            "revision": self.revision,
            "identityDigest": self.identity_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProcessorResourceIdentity:
        raw = _closed_shape(
            value,
            {"resourceId", "resourceKind", "revision", "identityDigest"},
            "processor resource identity",
        )
        return cls(
            resource_id=raw["resourceId"],
            resource_kind=raw["resourceKind"],
            revision=raw["revision"],
            identity_digest=raw["identityDigest"],
        )


class ProcessorCacheMode(StrEnum):
    """Reuse choices that do not name or require a cache implementation."""

    DISABLED = "disabled"
    EXACT_INPUTS = "exact-inputs"


@dataclass(frozen=True, slots=True)
class ProcessorCachePolicy:
    """A backend-neutral rule for reusing an exact processor result."""

    mode: ProcessorCacheMode
    key_schema_id: str | None

    def __post_init__(self) -> None:
        try:
            mode = ProcessorCacheMode(self.mode)
        except (TypeError, ValueError) as error:
            raise ValueError("processor cache mode is not registered") from error
        object.__setattr__(self, "mode", mode)
        if mode is ProcessorCacheMode.DISABLED:
            if self.key_schema_id is not None:
                raise ValueError("a disabled processor cache must not declare a key schema")
        else:
            require_text(self.key_schema_id, "processor cache key_schema_id")

    def to_dict(self) -> dict[str, str | None]:
        return {"mode": self.mode.value, "keySchemaId": self.key_schema_id}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProcessorCachePolicy:
        raw = _closed_shape(value, {"mode", "keySchemaId"}, "processor cache policy")
        return cls(mode=raw["mode"], key_schema_id=raw["keySchemaId"])


@dataclass(frozen=True, slots=True)
class ProcessorItemLimits:
    """Hard bounds applied to one scheduled processor item."""

    max_input_records: int
    max_input_bytes: int
    max_output_records: int
    max_output_bytes: int
    max_duration_seconds: int

    def __post_init__(self) -> None:
        for label, value in self.to_dict().items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"processor item limit {label} must be a positive integer")

    def to_dict(self) -> dict[str, int]:
        return {
            "maxInputRecords": self.max_input_records,
            "maxInputBytes": self.max_input_bytes,
            "maxOutputRecords": self.max_output_records,
            "maxOutputBytes": self.max_output_bytes,
            "maxDurationSeconds": self.max_duration_seconds,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProcessorItemLimits:
        raw = _closed_shape(
            value,
            {
                "maxInputRecords",
                "maxInputBytes",
                "maxOutputRecords",
                "maxOutputBytes",
                "maxDurationSeconds",
            },
            "processor item limits",
        )
        return cls(
            max_input_records=raw["maxInputRecords"],
            max_input_bytes=raw["maxInputBytes"],
            max_output_records=raw["maxOutputRecords"],
            max_output_bytes=raw["maxOutputBytes"],
            max_duration_seconds=raw["maxDurationSeconds"],
        )


@dataclass(frozen=True, slots=True)
class ProcessorRecordRef:
    """One exact logical input record without carrying its bulk content."""

    record_kind: str
    record_id: str
    schema_id: str
    record_digest: str

    def __post_init__(self) -> None:
        require_text(self.record_kind, "processor input record kind")
        require_text(self.record_id, "processor input record identity")
        require_text(self.schema_id, "processor input schema identity")
        require_sha256(self.record_digest, "processor input record digest")

    def to_dict(self) -> dict[str, str]:
        return {
            "recordKind": self.record_kind,
            "recordId": self.record_id,
            "schemaId": self.schema_id,
            "recordDigest": self.record_digest,
        }

    @classmethod
    def for_segment(cls, segment: Segment) -> ProcessorRecordRef:
        """Name one complete logical segment without binding cache identity to its locator."""

        value = segment.to_dict()
        value["content"] = {
            "digest": segment.content.digest,
            "byteSize": segment.content.byte_size,
            "mediaType": segment.content.media_type,
        }
        return cls(
            "segment",
            segment.segment_id,
            "docspec-segment/1",
            identity_digest(value),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProcessorRecordRef:
        raw = _closed_shape(
            value,
            {"recordKind", "recordId", "schemaId", "recordDigest"},
            "processor record reference",
        )
        return cls(raw["recordKind"], raw["recordId"], raw["schemaId"], raw["recordDigest"])


@dataclass(frozen=True, slots=True)
class ProcessorPayload:
    """Worker-local segment data projected through one sealed data-use policy."""

    input_record: ProcessorRecordRef
    allowed_fields: tuple[str, ...]
    content: bytes | None
    content_media_type: str | None
    evidence: EvidenceCoordinate | None
    representation_coordinates: tuple[int, int] | None
    segment_kind: str | None
    segment_ordinal: int | None

    def __post_init__(self) -> None:
        if tuple(sorted(set(self.allowed_fields))) != self.allowed_fields or not self.allowed_fields:
            raise ValueError("processor payload allowed fields must be non-empty, sorted, and distinct")
        unknown = set(self.allowed_fields) - set(PROCESSOR_DATA_FIELDS)
        if unknown:
            raise ValueError(f"processor payload contains unknown allowed fields: {sorted(unknown)}")
        expected_presence = {
            "content": self.content,
            "contentMediaType": self.content_media_type,
            "evidence": self.evidence,
            "representationCoordinates": self.representation_coordinates,
            "segmentKind": self.segment_kind,
            "segmentOrdinal": self.segment_ordinal,
        }
        for field_name, field_value in expected_presence.items():
            if (field_name in self.allowed_fields) != (field_value is not None):
                raise ValueError(f"processor payload field {field_name} differs from its data-use policy")
        if self.content is not None and not isinstance(self.content, bytes):
            raise TypeError("processor payload content must be immutable bytes")
        if self.content_media_type is not None:
            require_text(self.content_media_type, "processor payload content media type")
        if self.representation_coordinates is not None:
            start, end = self.representation_coordinates
            if type(start) is not int or type(end) is not int or start < 0 or end < start:
                raise ValueError("processor payload representation coordinates are invalid")
        if self.segment_kind is not None:
            require_text(self.segment_kind, "processor payload segment kind")
        if self.segment_ordinal is not None and (type(self.segment_ordinal) is not int or self.segment_ordinal < 0):
            raise ValueError("processor payload segment ordinal must be a non-negative integer")

    @classmethod
    def for_segment(cls, segment: Segment, content: bytes, allowed_fields: tuple[str, ...]) -> ProcessorPayload:
        if not isinstance(content, bytes):
            raise TypeError("processor source content must be immutable bytes")
        if len(content) != segment.content.byte_size or sha256_digest(content) != segment.content.digest:
            raise IntegrityError("processor source content differs from its immutable segment reference")
        fields = frozenset(allowed_fields)
        return cls(
            input_record=ProcessorRecordRef.for_segment(segment),
            allowed_fields=allowed_fields,
            content=content if "content" in fields else None,
            content_media_type=segment.content.media_type if "contentMediaType" in fields else None,
            evidence=segment.evidence if "evidence" in fields else None,
            representation_coordinates=(segment.representation_start, segment.representation_end)
            if "representationCoordinates" in fields
            else None,
            segment_kind=segment.kind if "segmentKind" in fields else None,
            segment_ordinal=segment.ordinal if "segmentOrdinal" in fields else None,
        )

    def require(self, field_name: str) -> None:
        if field_name not in self.allowed_fields:
            raise IntegrityError(f"processor requires data-use field {field_name!r}, but the policy excludes it")

    @property
    def input_byte_size(self) -> int:
        return len(self.content) if self.content is not None else 0


@dataclass(frozen=True, slots=True)
class ProcessorResourceUse:
    """Bounded provider-neutral observations for one processor invocation."""

    input_bytes: int
    output_bytes: int
    duration_milliseconds: int
    external_request_count: int = 0

    def __post_init__(self) -> None:
        for label, value in self.to_dict().items():
            if type(value) is not int or value < 0:
                raise ValueError(f"processor resource use {label} must be a non-negative integer")

    def to_dict(self) -> dict[str, int]:
        return {
            "inputBytes": self.input_bytes,
            "outputBytes": self.output_bytes,
            "durationMilliseconds": self.duration_milliseconds,
            "externalRequestCount": self.external_request_count,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProcessorResourceUse:
        raw = _closed_shape(
            value,
            {"inputBytes", "outputBytes", "durationMilliseconds", "externalRequestCount"},
            "processor resource use",
        )
        return cls(
            raw["inputBytes"],
            raw["outputBytes"],
            raw["durationMilliseconds"],
            raw["externalRequestCount"],
        )


@dataclass(frozen=True, slots=True)
class ProcessorRequest:
    """Closed semantic work description passed to every injected processor."""

    plan: ArtifactRef
    processor_id: str
    processor_description_digest: str
    source_item_id: str
    input_records: tuple[ProcessorRecordRef, ...]
    prerequisite_results: tuple[ArtifactRef, ...]
    allowed_fields: tuple[str, ...]
    item_limits: ProcessorItemLimits
    cache_key_schema_id: str
    invocation_id: str

    def __post_init__(self) -> None:
        require_text(self.processor_id, "processor request processor identity")
        require_sha256(self.processor_description_digest, "processor description digest")
        require_text(self.source_item_id, "processor request source item")
        require_text(self.cache_key_schema_id, "processor cache-key schema identity")
        require_text(self.invocation_id, "processor invocation identity")
        if not self.input_records:
            raise ValueError("processor request requires at least one exact input record")
        if len({item.record_id for item in self.input_records}) != len(self.input_records):
            raise ValueError("processor request repeats an input record identity")
        if len({item.artifact_id for item in self.prerequisite_results}) != len(self.prerequisite_results):
            raise ValueError("processor request repeats a prerequisite result identity")
        _validate_sorted_text(self.allowed_fields, "processor request allowed fields", allow_empty=False)

    def reuse_content(self) -> dict[str, Any]:
        return {
            "cacheKeySchemaId": self.cache_key_schema_id,
            "sourceItemId": self.source_item_id,
            "processorId": self.processor_id,
            "processorDescriptionDigest": self.processor_description_digest,
            "inputRecords": [item.to_dict() for item in self.input_records],
            "prerequisiteResults": [
                {"artifactId": item.artifact_id, "digest": item.digest}
                for item in self.prerequisite_results
            ],
            "allowedFields": list(self.allowed_fields),
            "itemLimits": self.item_limits.to_dict(),
        }

    @property
    def reuse_key(self) -> str:
        return stable_urn("processor-result-reuse", self.reuse_content())

    def identity_content(self) -> dict[str, Any]:
        content = self.reuse_content()
        content["prerequisiteResults"] = [item.to_dict() for item in self.prerequisite_results]
        return {
            "plan": self.plan.to_dict(),
            **content,
            "invocationId": self.invocation_id,
        }

    @property
    def request_id(self) -> str:
        return stable_urn("processor-request", self.identity_content())

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "docspec-processor-request",
            "formatVersion": "1.0",
            "requestId": self.request_id,
            "reuseKey": self.reuse_key,
            **self.identity_content(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProcessorRequest:
        expected = {
            "format",
            "formatVersion",
            "requestId",
            "reuseKey",
            "plan",
            "cacheKeySchemaId",
            "sourceItemId",
            "processorId",
            "processorDescriptionDigest",
            "inputRecords",
            "prerequisiteResults",
            "allowedFields",
            "itemLimits",
            "invocationId",
        }
        raw = _closed_shape(value, expected, "processor request")
        if raw["format"] != "docspec-processor-request" or raw["formatVersion"] != "1.0":
            raise ValueError("processor request has an unknown format")
        result = cls(
            ArtifactRef.from_dict(raw["plan"]),
            raw["processorId"],
            raw["processorDescriptionDigest"],
            raw["sourceItemId"],
            tuple(ProcessorRecordRef.from_dict(item) for item in _sequence(raw["inputRecords"], "inputs")),
            tuple(ArtifactRef.from_dict(item) for item in _sequence(raw["prerequisiteResults"], "prerequisites")),
            _sequence(raw["allowedFields"], "allowed fields"),
            ProcessorItemLimits.from_dict(raw["itemLimits"]),
            raw["cacheKeySchemaId"],
            raw["invocationId"],
        )
        if raw["requestId"] != result.request_id or raw["reuseKey"] != result.reuse_key:
            raise ValueError("processor request identity or reuse key differs")
        return result


_PROVIDER_RECEIPT_FIELDS = {
    "executionKind",
    "requestId",
    "reuseKey",
    "processorId",
    "processorDescriptionDigest",
    "inputIds",
    "outputDigest",
    "outputSchemaId",
    "outputMediaType",
    "configurationDigest",
    "dataUsePolicyDigest",
    "retryPolicyDigest",
}


def _validate_provider_receipt(receipt: Mapping[str, Any]) -> None:
    raw = _closed_shape(receipt, _PROVIDER_RECEIPT_FIELDS, "processor provider receipt")
    for name in (
        "executionKind",
        "requestId",
        "reuseKey",
        "processorId",
        "outputSchemaId",
        "outputMediaType",
    ):
        require_text(raw[name], f"processor provider receipt {name}")
    for name in (
        "processorDescriptionDigest",
        "outputDigest",
        "configurationDigest",
        "dataUsePolicyDigest",
        "retryPolicyDigest",
    ):
        require_sha256(raw[name], f"processor provider receipt {name}")
    input_ids = _sequence(raw["inputIds"], "processor provider receipt inputIds")
    for input_id in input_ids:
        require_text(input_id, "processor provider receipt input identity")
    if len(set(input_ids)) != len(input_ids):
        raise ValueError("processor provider receipt repeats an input identity")


@dataclass(frozen=True, slots=True)
class ProcessorResult:
    """Closed result returned by a processor and stored for exact reuse."""

    request_id: str
    reuse_key: str
    disposition: ProcessorDisposition
    output_media_type: str
    resource_identities: tuple[ProcessorResourceIdentity, ...]
    derived_records: tuple[DerivedRecord, ...]
    resource_use: ProcessorResourceUse
    warnings: tuple[str, ...]
    provider_receipt: dict[str, Any]
    provider_evidence: ProviderInteractionEvidence | None = None

    def __post_init__(self) -> None:
        require_text(self.request_id, "processor result request identity")
        require_text(self.reuse_key, "processor result reuse key")
        require_text(self.output_media_type, "processor result output media type")
        try:
            disposition = ProcessorDisposition(self.disposition)
        except (TypeError, ValueError) as error:
            raise ValueError("processor result disposition is not registered") from error
        object.__setattr__(self, "disposition", disposition)
        if not isinstance(self.resource_identities, tuple) or not all(
            isinstance(item, ProcessorResourceIdentity) for item in self.resource_identities
        ):
            raise ValueError("processor result resources must use ProcessorResourceIdentity")
        resource_keys = tuple(
            (item.resource_kind.value, item.resource_id) for item in self.resource_identities
        )
        if tuple(sorted(set(resource_keys))) != resource_keys:
            raise ValueError("processor result resources must be sorted and distinct")
        if disposition is ProcessorDisposition.PRODUCED and not self.derived_records:
            raise ValueError("a produced processor result requires at least one derived record")
        if len({item.derived_id for item in self.derived_records}) != len(self.derived_records):
            raise ValueError("processor result repeats a derived record identity")
        receipt = thaw_json(freeze_json(self.provider_receipt, label="processor provider receipt"))
        if not isinstance(receipt, dict):
            raise ValueError("processor provider receipt must be a JSON object")
        from docspec.domain.security import require_secret_free

        require_secret_free(receipt, label="processor provider receipt")
        require_secret_free(self.warnings, label="processor warnings")
        _validate_provider_receipt(receipt)
        if self.provider_evidence is not None and not isinstance(
            self.provider_evidence,
            ProviderInteractionEvidence,
        ):
            raise TypeError("processor provider evidence must use ProviderInteractionEvidence")
        object.__setattr__(self, "provider_receipt", receipt)
        receipt_digest = processor_receipt_digest(receipt)
        if any(item.provider_receipt_digest != receipt_digest for item in self.derived_records):
            raise IntegrityError("processor receipt digest differs from a derived record")
        _validate_sorted_text(self.warnings, "processor result warnings")

    def identity_content(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "reuseKey": self.reuse_key,
            "disposition": self.disposition.value,
            "outputMediaType": self.output_media_type,
            "resourceIdentities": [item.to_dict() for item in self.resource_identities],
            "derivedRecords": [item.to_dict() for item in self.derived_records],
            "resourceUse": self.resource_use.to_dict(),
            "warnings": list(self.warnings),
            "providerReceipt": self.provider_receipt,
            "providerEvidence": None if self.provider_evidence is None else self.provider_evidence.to_dict(),
        }

    @property
    def result_id(self) -> str:
        return stable_urn("processor-result", self.identity_content())

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "docspec-processor-result",
            "formatVersion": "1.2",
            "resultId": self.result_id,
            **self.identity_content(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProcessorResult:
        expected = {
            "format",
            "formatVersion",
            "resultId",
            "requestId",
            "reuseKey",
            "disposition",
            "outputMediaType",
            "resourceIdentities",
            "derivedRecords",
            "resourceUse",
            "warnings",
            "providerReceipt",
            "providerEvidence",
        }
        raw = _closed_shape(value, expected, "processor result")
        if raw["format"] != "docspec-processor-result" or raw["formatVersion"] != "1.2":
            raise ValueError("processor result has an unknown format")
        result = cls(
            raw["requestId"],
            raw["reuseKey"],
            ProcessorDisposition(raw["disposition"]),
            raw["outputMediaType"],
            tuple(
                ProcessorResourceIdentity.from_dict(item)
                for item in _sequence(raw["resourceIdentities"], "resource identities")
            ),
            tuple(DerivedRecord.from_dict(item) for item in _sequence(raw["derivedRecords"], "records")),
            ProcessorResourceUse.from_dict(raw["resourceUse"]),
            _sequence(raw["warnings"], "warnings"),
            raw["providerReceipt"],
            None
            if raw["providerEvidence"] is None
            else ProviderInteractionEvidence.from_dict(raw["providerEvidence"]),
        )
        if raw["resultId"] != result.result_id:
            raise ValueError("processor result identity differs")
        return result


def _description_content(
    *,
    name: str,
    version: str,
    implementation_id: str,
    accepted_inputs: tuple[ProcessorInput, ...],
    output_schema_id: str,
    output_media_types: tuple[str, ...],
    execution_scope: ProcessorExecutionScope,
    external_resources: tuple[ProcessorResourceIdentity, ...],
    dependencies: tuple[str, ...],
    deterministic: bool,
    cache_policy: ProcessorCachePolicy,
    configuration_digest: str,
    data_use_policy_digest: str,
    item_limits: ProcessorItemLimits,
    retry_policy_digest: str,
    capabilities: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "name": name,
        "version": version,
        "implementationId": implementation_id,
        "acceptedInputs": [item.to_dict() for item in accepted_inputs],
        "outputSchemaId": output_schema_id,
        "outputMediaTypes": list(output_media_types),
        "executionScope": ProcessorExecutionScope(execution_scope).value,
        "externalResources": [item.to_dict() for item in external_resources],
        "dependencies": list(dependencies),
        "deterministic": deterministic,
        "cachePolicy": cache_policy.to_dict(),
        "configurationDigest": configuration_digest,
        "dataUsePolicyDigest": data_use_policy_digest,
        "itemLimits": item_limits.to_dict(),
        "retryPolicyDigest": retry_policy_digest,
        "capabilities": list(capabilities),
    }


@dataclass(frozen=True, slots=True)
class ProcessorDescription:
    """The pinned behavior and policy surface for one injected processor."""

    processor_id: str
    name: str
    version: str
    implementation_id: str
    accepted_inputs: tuple[ProcessorInput, ...]
    output_schema_id: str
    output_media_types: tuple[str, ...]
    execution_scope: ProcessorExecutionScope
    external_resources: tuple[ProcessorResourceIdentity, ...]
    dependencies: tuple[str, ...]
    deterministic: bool
    cache_policy: ProcessorCachePolicy
    configuration_digest: str
    data_use_policy_digest: str
    item_limits: ProcessorItemLimits
    retry_policy_digest: str
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (
            ("processor_id", self.processor_id),
            ("name", self.name),
            ("version", self.version),
            ("implementation_id", self.implementation_id),
            ("output_schema_id", self.output_schema_id),
        ):
            require_text(value, label)
        for label, value in (
            ("configuration_digest", self.configuration_digest),
            ("data_use_policy_digest", self.data_use_policy_digest),
            ("retry_policy_digest", self.retry_policy_digest),
        ):
            require_sha256(value, label)
        if type(self.deterministic) is not bool:
            raise ValueError("processor deterministic must be a boolean")
        try:
            execution_scope = ProcessorExecutionScope(self.execution_scope)
        except (TypeError, ValueError) as error:
            raise ValueError("processor execution scope is not registered") from error
        object.__setattr__(self, "execution_scope", execution_scope)
        if not isinstance(self.accepted_inputs, tuple) or not self.accepted_inputs:
            raise ValueError("a processor must declare at least one accepted input")
        if not all(isinstance(item, ProcessorInput) for item in self.accepted_inputs):
            raise ValueError("accepted processor inputs must use ProcessorInput")
        input_keys = tuple(item.record_kind for item in self.accepted_inputs)
        if tuple(sorted(set(input_keys))) != input_keys:
            raise ValueError("accepted processor inputs must be sorted by distinct record kind")
        if not isinstance(self.external_resources, tuple) or not all(
            isinstance(item, ProcessorResourceIdentity) for item in self.external_resources
        ):
            raise ValueError("external processor resources must use ProcessorResourceIdentity")
        resource_keys = tuple((item.resource_kind.value, item.resource_id) for item in self.external_resources)
        if tuple(sorted(set(resource_keys))) != resource_keys:
            raise ValueError("external processor resources must be sorted and distinct")
        if not isinstance(self.cache_policy, ProcessorCachePolicy):
            raise ValueError("processor cache policy must use ProcessorCachePolicy")
        if not isinstance(self.item_limits, ProcessorItemLimits):
            raise ValueError("processor item limits must use ProcessorItemLimits")
        if not isinstance(self.output_media_types, tuple):
            raise ValueError("processor output media types must be an immutable tuple")
        _validate_sorted_text(self.output_media_types, "processor output media_types", allow_empty=False)
        for label, values in (
            ("processor dependencies", self.dependencies),
            ("processor capabilities", self.capabilities),
        ):
            if not isinstance(values, tuple):
                raise ValueError(f"{label} must be an immutable tuple")
            _validate_sorted_text(values, label)
        if self.processor_id in self.dependencies:
            raise ValueError("a processor cannot depend on itself")
        if self.cache_policy.mode is ProcessorCacheMode.EXACT_INPUTS and not self.deterministic:
            raise ValueError("only a deterministic processor may reuse exact-input results")
        if self.processor_id != stable_urn("processor", self.definition_content()):
            raise ValueError("processor description identity differs from its definition")

    @property
    def input_kinds(self) -> tuple[str, ...]:
        """Return the accepted record kinds for simple schedulers."""

        return tuple(item.record_kind for item in self.accepted_inputs)

    def definition_content(self) -> dict[str, Any]:
        """Return every identity-bearing field except the processor identifier."""

        return _description_content(
            name=self.name,
            version=self.version,
            implementation_id=self.implementation_id,
            accepted_inputs=self.accepted_inputs,
            output_schema_id=self.output_schema_id,
            output_media_types=self.output_media_types,
            execution_scope=self.execution_scope,
            external_resources=self.external_resources,
            dependencies=self.dependencies,
            deterministic=self.deterministic,
            cache_policy=self.cache_policy,
            configuration_digest=self.configuration_digest,
            data_use_policy_digest=self.data_use_policy_digest,
            item_limits=self.item_limits,
            retry_policy_digest=self.retry_policy_digest,
            capabilities=self.capabilities,
        )

    @classmethod
    def create(
        cls,
        *,
        name: str,
        version: str,
        implementation_id: str,
        accepted_inputs: tuple[ProcessorInput, ...],
        output_schema_id: str,
        output_media_types: tuple[str, ...],
        execution_scope: ProcessorExecutionScope,
        external_resources: tuple[ProcessorResourceIdentity, ...],
        dependencies: tuple[str, ...],
        deterministic: bool,
        cache_policy: ProcessorCachePolicy,
        configuration_digest: str,
        data_use_policy_digest: str,
        item_limits: ProcessorItemLimits,
        retry_policy_digest: str,
        capabilities: tuple[str, ...] = (),
    ) -> ProcessorDescription:
        content = _description_content(
            name=name,
            version=version,
            implementation_id=implementation_id,
            accepted_inputs=accepted_inputs,
            output_schema_id=output_schema_id,
            output_media_types=output_media_types,
            execution_scope=execution_scope,
            external_resources=external_resources,
            dependencies=dependencies,
            deterministic=deterministic,
            cache_policy=cache_policy,
            configuration_digest=configuration_digest,
            data_use_policy_digest=data_use_policy_digest,
            item_limits=item_limits,
            retry_policy_digest=retry_policy_digest,
            capabilities=capabilities,
        )
        return cls(
            processor_id=stable_urn("processor", content),
            name=name,
            version=version,
            implementation_id=implementation_id,
            accepted_inputs=accepted_inputs,
            output_schema_id=output_schema_id,
            output_media_types=output_media_types,
            execution_scope=ProcessorExecutionScope(execution_scope),
            external_resources=external_resources,
            dependencies=dependencies,
            deterministic=deterministic,
            cache_policy=cache_policy,
            configuration_digest=configuration_digest,
            data_use_policy_digest=data_use_policy_digest,
            item_limits=item_limits,
            retry_policy_digest=retry_policy_digest,
            capabilities=capabilities,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"processorId": self.processor_id, **self.definition_content()}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProcessorDescription:
        expected = {
            "processorId",
            "name",
            "version",
            "implementationId",
            "acceptedInputs",
            "outputSchemaId",
            "outputMediaTypes",
            "executionScope",
            "externalResources",
            "dependencies",
            "deterministic",
            "cachePolicy",
            "configurationDigest",
            "dataUsePolicyDigest",
            "itemLimits",
            "retryPolicyDigest",
            "capabilities",
        }
        raw = _closed_shape(value, expected, "processor description")
        inputs = _sequence(raw["acceptedInputs"], "processor acceptedInputs")
        resources = _sequence(raw["externalResources"], "processor externalResources")
        return cls(
            processor_id=raw["processorId"],
            name=raw["name"],
            version=raw["version"],
            implementation_id=raw["implementationId"],
            accepted_inputs=tuple(ProcessorInput.from_dict(item) for item in inputs),
            output_schema_id=raw["outputSchemaId"],
            output_media_types=_sequence(raw["outputMediaTypes"], "processor outputMediaTypes"),
            execution_scope=raw["executionScope"],
            external_resources=tuple(ProcessorResourceIdentity.from_dict(item) for item in resources),
            dependencies=_sequence(raw["dependencies"], "processor dependencies"),
            deterministic=raw["deterministic"],
            cache_policy=ProcessorCachePolicy.from_dict(raw["cachePolicy"]),
            configuration_digest=raw["configurationDigest"],
            data_use_policy_digest=raw["dataUsePolicyDigest"],
            item_limits=ProcessorItemLimits.from_dict(raw["itemLimits"]),
            retry_policy_digest=raw["retryPolicyDigest"],
            capabilities=_sequence(raw["capabilities"], "processor capabilities"),
        )


@dataclass(frozen=True, slots=True)
class ProcessorSet:
    """A validated acyclic processor graph pinned by a stable identity."""

    processors: tuple[ProcessorDescription, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.processors, tuple) or not all(
            isinstance(item, ProcessorDescription) for item in self.processors
        ):
            raise ValueError("processor sets must contain ProcessorDescription values")
        identifiers = [item.processor_id for item in self.processors]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("processor identities must be distinct")
        names = [item.name for item in self.processors]
        if len(set(names)) != len(names):
            raise ValueError("processor names must be distinct within a processor set")
        known = set(identifiers)
        for item in self.processors:
            missing = set(item.dependencies) - known
            if missing:
                raise ValueError(f"processor {item.processor_id} has unknown dependencies: {sorted(missing)}")
        self._topological_order()

    @property
    def processor_set_id(self) -> str:
        return stable_urn("processor-set", [item.to_dict() for item in self.processors])

    def _topological_order(self) -> tuple[str, ...]:
        remaining = {item.processor_id: set(item.dependencies) for item in self.processors}
        order: list[str] = []
        while remaining:
            ready = sorted(identifier for identifier, dependencies in remaining.items() if not dependencies)
            if not ready:
                raise ValueError("processor dependencies must form an acyclic graph")
            order.extend(ready)
            for identifier in ready:
                del remaining[identifier]
            for dependencies in remaining.values():
                dependencies.difference_update(ready)
        return tuple(order)

    @property
    def execution_order(self) -> tuple[ProcessorDescription, ...]:
        by_id = {item.processor_id: item for item in self.processors}
        return tuple(by_id[identifier] for identifier in self._topological_order())

    def invalidated_by(self, changed_processor_ids: Iterable[str]) -> tuple[str, ...]:
        """Return the changed processors and only their transitive dependents."""

        invalid = set(changed_processor_ids)
        unknown = invalid - {item.processor_id for item in self.processors}
        if unknown:
            raise ValueError(f"unknown changed processors: {sorted(unknown)}")
        changed = True
        while changed:
            changed = False
            for item in self.processors:
                if item.processor_id not in invalid and invalid.intersection(item.dependencies):
                    invalid.add(item.processor_id)
                    changed = True
        return tuple(identifier for identifier in self._topological_order() if identifier in invalid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "processorSetId": self.processor_set_id,
            "processors": [item.to_dict() for item in self.processors],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProcessorSet:
        raw = _closed_shape(value, {"processorSetId", "processors"}, "processor set")
        processors = _sequence(raw["processors"], "processor set processors")
        result = cls(tuple(ProcessorDescription.from_dict(item) for item in processors))
        if raw["processorSetId"] != result.processor_set_id:
            raise ValueError("processor set identity differs")
        return result


def processor_receipt_digest(value: Mapping[str, Any]) -> str:
    """Normalize and digest a provider receipt without exposing provider types."""

    normalized = freeze_json(value, label="processor receipt")
    if not isinstance(normalized, Mapping):
        raise ValueError("processor receipt must be a JSON object")
    return identity_digest(normalized)
