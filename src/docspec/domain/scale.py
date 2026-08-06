"""Typed, content-addressed descriptions for repeatable scale campaigns."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

from docspec.domain.identity import (
    canonical_json_file_bytes,
    parse_canonical_json,
    require_sha256,
    require_text,
    sha256_digest,
    stable_urn,
    thaw_json,
)
from docspec.domain.profiles import ProfileSet
from docspec.domain.references import DocumentReleaseRef
from docspec.errors import IntegrityError, ProfileError

SCALE_PROFILE_FORMAT = "docspec-scale-profile"
SCALE_PROFILE_VERSION = "1.1"
MAX_SCALE_PROFILE_BYTES = 1024 * 1024


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


def _text_tuple(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ProfileError(f"{label} must be an array")
    result = tuple(require_text(item, f"{label} item") for item in value)
    if not allow_empty and not result:
        raise ProfileError(f"{label} must not be empty")
    if len(set(result)) != len(result):
        raise ProfileError(f"{label} must contain distinct values")
    return result


@dataclass(frozen=True, slots=True)
class ScaleArtifactPin:
    """A digest-pinned scale input without transport-specific metadata."""

    artifact_id: str
    locator: str
    digest: str

    def __post_init__(self) -> None:
        require_text(self.artifact_id, "scale artifact_id")
        require_text(self.locator, "scale artifact locator")
        require_sha256(self.digest, "scale artifact digest")

    def to_dict(self) -> dict[str, str]:
        return {"artifactId": self.artifact_id, "locator": self.locator, "digest": self.digest}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(value, {"artifactId", "locator", "digest"}, "scale artifact pin")
        return cls(item["artifactId"], item["locator"], item["digest"])


@dataclass(frozen=True, slots=True)
class ScaleCorpus:
    identity: str
    digest: str
    selection_method: str

    def __post_init__(self) -> None:
        require_text(self.identity, "corpus identity")
        require_sha256(self.digest, "corpus digest")
        require_text(self.selection_method, "corpus selection method")

    def to_dict(self) -> dict[str, str]:
        return {
            "identity": self.identity,
            "digest": self.digest,
            "selectionMethod": self.selection_method,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(value, {"identity", "digest", "selectionMethod"}, "scale corpus")
        return cls(item["identity"], item["digest"], item["selectionMethod"])


@dataclass(frozen=True, slots=True)
class ScaleDistribution:
    minimum: int
    median: int
    p95: int
    maximum: int

    def __post_init__(self) -> None:
        values = (
            _non_negative_integer(self.minimum, "distribution minimum"),
            _non_negative_integer(self.median, "distribution median"),
            _non_negative_integer(self.p95, "distribution p95"),
            _non_negative_integer(self.maximum, "distribution maximum"),
        )
        if values != tuple(sorted(values)):
            raise ProfileError("distribution values must be ordered minimum, median, p95, maximum")

    def to_dict(self) -> dict[str, int]:
        return {
            "minimum": self.minimum,
            "median": self.median,
            "p95": self.p95,
            "maximum": self.maximum,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(value, {"minimum", "median", "p95", "maximum"}, "scale distribution")
        return cls(item["minimum"], item["median"], item["p95"], item["maximum"])


_DISTRIBUTION_KINDS = ("files", "images", "pages", "bytes", "representations", "segments")


@dataclass(frozen=True, slots=True)
class ScaleInputShape:
    sample_identity: str
    sample_digest: str
    distributions: tuple[tuple[str, ScaleDistribution], ...]

    def __post_init__(self) -> None:
        require_text(self.sample_identity, "input-shape sample identity")
        require_sha256(self.sample_digest, "input-shape sample digest")
        names = tuple(name for name, _ in self.distributions)
        if names != _DISTRIBUTION_KINDS:
            raise ProfileError("input-shape distributions must contain the six registered kinds in canonical order")
        if any(not isinstance(distribution, ScaleDistribution) for _, distribution in self.distributions):
            raise ProfileError("input-shape distributions must be typed scale distributions")

    def to_dict(self) -> dict[str, Any]:
        return {
            "sampleIdentity": self.sample_identity,
            "sampleDigest": self.sample_digest,
            "distributions": {name: distribution.to_dict() for name, distribution in self.distributions},
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(value, {"sampleIdentity", "sampleDigest", "distributions"}, "scale input shape")
        distributions = _closed(item["distributions"], set(_DISTRIBUTION_KINDS), "scale distributions")
        return cls(
            item["sampleIdentity"],
            item["sampleDigest"],
            tuple((name, ScaleDistribution.from_dict(distributions[name])) for name in _DISTRIBUTION_KINDS),
        )


class ScaleStageKind(StrEnum):
    EXTRACTOR = "extractor"
    SEGMENTER = "segmenter"
    PROCESSOR = "processor"


@dataclass(frozen=True, slots=True)
class ScaleProcessingStage:
    stage_id: str
    stage_kind: ScaleStageKind
    implementation_id: str
    configuration_digest: str
    input_layer_kinds: tuple[str, ...]
    output_layer_kind: str

    def __post_init__(self) -> None:
        require_text(self.stage_id, "scale stage_id")
        try:
            kind = ScaleStageKind(self.stage_kind)
        except (TypeError, ValueError) as error:
            raise ProfileError("scale stage kind is not registered") from error
        object.__setattr__(self, "stage_kind", kind)
        require_text(self.implementation_id, "scale stage implementation_id")
        require_sha256(self.configuration_digest, "scale stage configuration digest")
        layers = _text_tuple(self.input_layer_kinds, "scale stage input layers")
        if layers != tuple(sorted(layers)):
            raise ProfileError("scale stage input layers must be sorted")
        object.__setattr__(self, "input_layer_kinds", layers)
        require_text(self.output_layer_kind, "scale stage output layer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stageId": self.stage_id,
            "stageKind": self.stage_kind.value,
            "implementationId": self.implementation_id,
            "configurationDigest": self.configuration_digest,
            "inputLayerKinds": list(self.input_layer_kinds),
            "outputLayerKind": self.output_layer_kind,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(
            value,
            {
                "stageId",
                "stageKind",
                "implementationId",
                "configurationDigest",
                "inputLayerKinds",
                "outputLayerKind",
            },
            "scale processing stage",
        )
        return cls(
            item["stageId"],
            ScaleStageKind(item["stageKind"]),
            item["implementationId"],
            item["configurationDigest"],
            _text_tuple(item["inputLayerKinds"], "scale stage input layers"),
            item["outputLayerKind"],
        )


@dataclass(frozen=True, slots=True)
class ScaleResources:
    environment_id: str
    docspec_version: str
    python_version: str
    worker_count: int
    worker_cpu: int
    worker_memory_bytes: int
    coordinator_memory_bytes: int

    def __post_init__(self) -> None:
        require_text(self.environment_id, "scale environment_id")
        require_text(self.docspec_version, "scale DocSpec version")
        require_text(self.python_version, "scale Python version")
        for label, value in (
            ("worker count", self.worker_count),
            ("worker CPU", self.worker_cpu),
            ("worker memory bytes", self.worker_memory_bytes),
            ("coordinator memory bytes", self.coordinator_memory_bytes),
        ):
            _positive_integer(value, f"scale {label}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "environmentId": self.environment_id,
            "docspecVersion": self.docspec_version,
            "pythonVersion": self.python_version,
            "workerCount": self.worker_count,
            "workerCpu": self.worker_cpu,
            "workerMemoryBytes": self.worker_memory_bytes,
            "coordinatorMemoryBytes": self.coordinator_memory_bytes,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        fields = {
            "environmentId",
            "docspecVersion",
            "pythonVersion",
            "workerCount",
            "workerCpu",
            "workerMemoryBytes",
            "coordinatorMemoryBytes",
        }
        item = _closed(value, fields, "scale resources")
        return cls(
            item["environmentId"],
            item["docspecVersion"],
            item["pythonVersion"],
            item["workerCount"],
            item["workerCpu"],
            item["workerMemoryBytes"],
            item["coordinatorMemoryBytes"],
        )


@dataclass(frozen=True, slots=True)
class ScaleDocumentStorePolicy:
    max_entries: int
    max_estimated_bytes: int
    max_expected_segments: int
    max_duration_seconds: int

    def __post_init__(self) -> None:
        for label, value in (
            ("max entries", self.max_entries),
            ("max estimated bytes", self.max_estimated_bytes),
            ("max expected segments", self.max_expected_segments),
            ("max duration seconds", self.max_duration_seconds),
        ):
            _positive_integer(value, f"scale document-store {label}")

    def to_dict(self) -> dict[str, int]:
        return {
            "maxEntries": self.max_entries,
            "maxEstimatedBytes": self.max_estimated_bytes,
            "maxExpectedSegments": self.max_expected_segments,
            "maxDurationSeconds": self.max_duration_seconds,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(
            value,
            {"maxEntries", "maxEstimatedBytes", "maxExpectedSegments", "maxDurationSeconds"},
            "scale document-store policy",
        )
        return cls(
            item["maxEntries"],
            item["maxEstimatedBytes"],
            item["maxExpectedSegments"],
            item["maxDurationSeconds"],
        )


@dataclass(frozen=True, slots=True)
class ScaleImplementationPin:
    implementation_id: str
    configuration_digest: str

    def __post_init__(self) -> None:
        require_text(self.implementation_id, "scale implementation_id")
        require_sha256(self.configuration_digest, "scale implementation configuration digest")

    def to_dict(self) -> dict[str, str]:
        return {"implementationId": self.implementation_id, "configurationDigest": self.configuration_digest}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(value, {"implementationId", "configurationDigest"}, "scale implementation pin")
        return cls(item["implementationId"], item["configurationDigest"])


@dataclass(frozen=True, slots=True)
class ScaleResultSinkPin:
    sink_id: str
    configuration_digest: str

    def __post_init__(self) -> None:
        require_text(self.sink_id, "scale result sink_id")
        require_sha256(self.configuration_digest, "scale result-sink configuration digest")

    def to_dict(self) -> dict[str, str]:
        return {"sinkId": self.sink_id, "configurationDigest": self.configuration_digest}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(value, {"sinkId", "configurationDigest"}, "scale result-sink pin")
        return cls(item["sinkId"], item["configurationDigest"])


@dataclass(frozen=True, slots=True)
class ScalePlacement:
    worker_region: str
    storage_region: str
    source_colocated: bool

    def __post_init__(self) -> None:
        require_text(self.worker_region, "scale worker region")
        require_text(self.storage_region, "scale storage region")
        if type(self.source_colocated) is not bool:
            raise ProfileError("scale source_colocated must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "workerRegion": self.worker_region,
            "storageRegion": self.storage_region,
            "sourceColocated": self.source_colocated,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(value, {"workerRegion", "storageRegion", "sourceColocated"}, "scale placement")
        return cls(item["workerRegion"], item["storageRegion"], item["sourceColocated"])


class ScaleCacheState(StrEnum):
    COLD = "cold"
    WARM = "warm"
    MIXED = "mixed"


@dataclass(frozen=True, slots=True)
class ScalePartitionPolicy:
    identity: str
    bucket_count: int
    target_member_bytes: int
    hard_max_member_bytes: int

    def __post_init__(self) -> None:
        require_text(self.identity, "scale partition-policy identity")
        for label, value in (
            ("bucket count", self.bucket_count),
            ("target member bytes", self.target_member_bytes),
            ("hard maximum member bytes", self.hard_max_member_bytes),
        ):
            _positive_integer(value, f"scale partition-policy {label}")
        if self.hard_max_member_bytes < self.target_member_bytes:
            raise ProfileError("hard maximum member bytes must not be less than target member bytes")

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "bucketCount": self.bucket_count,
            "targetMemberBytes": self.target_member_bytes,
            "hardMaxMemberBytes": self.hard_max_member_bytes,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(
            value,
            {"identity", "bucketCount", "targetMemberBytes", "hardMaxMemberBytes"},
            "scale partition policy",
        )
        return cls(
            item["identity"],
            item["bucketCount"],
            item["targetMemberBytes"],
            item["hardMaxMemberBytes"],
        )


@dataclass(frozen=True, slots=True)
class ScaleTaskPolicy:
    policy_id: str
    max_in_flight_stores: int
    max_attempts: int
    checkpoint_interval_seconds: int

    def __post_init__(self) -> None:
        require_text(self.policy_id, "scale task-policy id")
        for label, value in (
            ("maximum in-flight stores", self.max_in_flight_stores),
            ("maximum attempts", self.max_attempts),
            ("checkpoint interval seconds", self.checkpoint_interval_seconds),
        ):
            _positive_integer(value, f"scale task-policy {label}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "policyId": self.policy_id,
            "maxInFlightStores": self.max_in_flight_stores,
            "maxAttempts": self.max_attempts,
            "checkpointIntervalSeconds": self.checkpoint_interval_seconds,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(
            value,
            {"policyId", "maxInFlightStores", "maxAttempts", "checkpointIntervalSeconds"},
            "scale task policy",
        )
        return cls(
            item["policyId"],
            item["maxInFlightStores"],
            item["maxAttempts"],
            item["checkpointIntervalSeconds"],
        )


@dataclass(frozen=True, slots=True)
class ScaleProviderLimit:
    name: str
    unit: str
    maximum: int

    def __post_init__(self) -> None:
        require_text(self.name, "provider-limit name")
        require_text(self.unit, "provider-limit unit")
        _positive_integer(self.maximum, "provider-limit maximum")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "unit": self.unit, "maximum": self.maximum}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(value, {"name", "unit", "maximum"}, "scale provider limit")
        return cls(item["name"], item["unit"], item["maximum"])


@dataclass(frozen=True, slots=True)
class ScaleProcessorTarget:
    processor_id: str
    deadline_seconds: int
    max_concurrency: int
    cost_estimate: int
    provider_limits: tuple[ScaleProviderLimit, ...]

    def __post_init__(self) -> None:
        require_text(self.processor_id, "scale processor target id")
        _positive_integer(self.deadline_seconds, "scale processor deadline seconds")
        _positive_integer(self.max_concurrency, "scale processor maximum concurrency")
        _non_negative_integer(self.cost_estimate, "scale processor cost estimate")
        names = tuple(item.name for item in self.provider_limits)
        if names != tuple(sorted(set(names))):
            raise ProfileError("scale processor provider limits must be sorted and distinct by name")

    def to_dict(self) -> dict[str, Any]:
        return {
            "processorId": self.processor_id,
            "deadlineSeconds": self.deadline_seconds,
            "maxConcurrency": self.max_concurrency,
            "costEstimate": self.cost_estimate,
            "providerLimits": [item.to_dict() for item in self.provider_limits],
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(
            value,
            {"processorId", "deadlineSeconds", "maxConcurrency", "costEstimate", "providerLimits"},
            "scale processor target",
        )
        if not isinstance(item["providerLimits"], list):
            raise ProfileError("scale processor provider limits must be an array")
        return cls(
            item["processorId"],
            item["deadlineSeconds"],
            item["maxConcurrency"],
            item["costEstimate"],
            tuple(ScaleProviderLimit.from_dict(limit) for limit in item["providerLimits"]),
        )


@dataclass(frozen=True, slots=True)
class ScaleTargets:
    unit_count: int
    deadline_seconds: int
    max_worker_cpu: int
    max_worker_memory_bytes: int
    max_coordinator_memory_bytes: int
    processor_targets: tuple[ScaleProcessorTarget, ...]

    def __post_init__(self) -> None:
        for label, value in (
            ("unit count", self.unit_count),
            ("deadline seconds", self.deadline_seconds),
            ("maximum worker CPU", self.max_worker_cpu),
            ("maximum worker memory bytes", self.max_worker_memory_bytes),
            ("maximum coordinator memory bytes", self.max_coordinator_memory_bytes),
        ):
            _positive_integer(value, f"scale target {label}")
        ids = tuple(item.processor_id for item in self.processor_targets)
        if ids != tuple(sorted(set(ids))):
            raise ProfileError("scale processor targets must be sorted and distinct by processor id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "unitCount": self.unit_count,
            "deadlineSeconds": self.deadline_seconds,
            "maxWorkerCpu": self.max_worker_cpu,
            "maxWorkerMemoryBytes": self.max_worker_memory_bytes,
            "maxCoordinatorMemoryBytes": self.max_coordinator_memory_bytes,
            "processorTargets": [item.to_dict() for item in self.processor_targets],
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(
            value,
            {
                "unitCount",
                "deadlineSeconds",
                "maxWorkerCpu",
                "maxWorkerMemoryBytes",
                "maxCoordinatorMemoryBytes",
                "processorTargets",
            },
            "scale targets",
        )
        if not isinstance(item["processorTargets"], list):
            raise ProfileError("scale processor targets must be an array")
        return cls(
            item["unitCount"],
            item["deadlineSeconds"],
            item["maxWorkerCpu"],
            item["maxWorkerMemoryBytes"],
            item["maxCoordinatorMemoryBytes"],
            tuple(ScaleProcessorTarget.from_dict(target) for target in item["processorTargets"]),
        )


@dataclass(frozen=True, slots=True)
class ScaleAcceptanceAuthority:
    authority_id: str
    decision_artifact: str
    decision_artifact_digest: str

    def __post_init__(self) -> None:
        require_text(self.authority_id, "scale acceptance authority_id")
        require_text(self.decision_artifact, "scale acceptance decision artifact")
        require_sha256(self.decision_artifact_digest, "scale acceptance decision digest")

    def to_dict(self) -> dict[str, str]:
        return {
            "authorityId": self.authority_id,
            "decisionArtifact": self.decision_artifact,
            "decisionArtifactDigest": self.decision_artifact_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(
            value,
            {"authorityId", "decisionArtifact", "decisionArtifactDigest"},
            "scale acceptance authority",
        )
        return cls(item["authorityId"], item["decisionArtifact"], item["decisionArtifactDigest"])


_SCALE_CONTENT_FIELDS = {
    "processingPlan",
    "executionProfile",
    "corpus",
    "inputShape",
    "processingGraph",
    "resources",
    "documentStorePolicy",
    "resultSink",
    "profileSet",
    "documentCatalog",
    "baseRelease",
    "placement",
    "cacheState",
    "partitionPolicy",
    "taskPolicy",
    "targets",
    "acceptanceAuthority",
}


@dataclass(frozen=True, slots=True)
class ScaleProfile:
    """All fixed inputs, resources, and targets behind one scale claim."""

    processing_plan: ScaleArtifactPin
    execution_profile: ScaleArtifactPin
    corpus: ScaleCorpus
    input_shape: ScaleInputShape
    processing_graph: tuple[ScaleProcessingStage, ...]
    resources: ScaleResources
    document_store_policy: ScaleDocumentStorePolicy
    result_sink: ScaleResultSinkPin
    profile_set: ProfileSet
    document_catalog: ScaleImplementationPin
    base_release: DocumentReleaseRef | None
    placement: ScalePlacement
    cache_state: ScaleCacheState
    partition_policy: ScalePartitionPolicy
    task_policy: ScaleTaskPolicy
    targets: ScaleTargets
    acceptance_authority: ScaleAcceptanceAuthority

    def __post_init__(self) -> None:
        if not self.processing_graph:
            raise ProfileError("scale processing graph must not be empty")
        stage_ids = tuple(stage.stage_id for stage in self.processing_graph)
        if len(stage_ids) != len(set(stage_ids)):
            raise ProfileError("scale processing graph stage ids must be distinct")
        processor_ids = tuple(
            stage.stage_id for stage in self.processing_graph if stage.stage_kind is ScaleStageKind.PROCESSOR
        )
        target_ids = tuple(target.processor_id for target in self.targets.processor_targets)
        if set(processor_ids) != set(target_ids):
            raise ProfileError("scale processor targets must name every processor stage exactly once")
        try:
            cache_state = ScaleCacheState(self.cache_state)
        except (TypeError, ValueError) as error:
            raise ProfileError("scale cache state is not registered") from error
        object.__setattr__(self, "cache_state", cache_state)
        payload = canonical_json_file_bytes(self.to_dict())
        if len(payload) > MAX_SCALE_PROFILE_BYTES:
            raise ProfileError(f"scale profile exceeds its {MAX_SCALE_PROFILE_BYTES}-byte serialized limit")

    def identity_content(self) -> dict[str, Any]:
        return {
            "processingPlan": self.processing_plan.to_dict(),
            "executionProfile": self.execution_profile.to_dict(),
            "corpus": self.corpus.to_dict(),
            "inputShape": self.input_shape.to_dict(),
            "processingGraph": [stage.to_dict() for stage in self.processing_graph],
            "resources": self.resources.to_dict(),
            "documentStorePolicy": self.document_store_policy.to_dict(),
            "resultSink": self.result_sink.to_dict(),
            "profileSet": self.profile_set.to_dict(),
            "documentCatalog": self.document_catalog.to_dict(),
            "baseRelease": None if self.base_release is None else self.base_release.to_dict(),
            "placement": self.placement.to_dict(),
            "cacheState": self.cache_state.value,
            "partitionPolicy": self.partition_policy.to_dict(),
            "taskPolicy": self.task_policy.to_dict(),
            "targets": self.targets.to_dict(),
            "acceptanceAuthority": self.acceptance_authority.to_dict(),
        }

    @property
    def profile_id(self) -> str:
        return stable_urn("scale-profile", self.identity_content())

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": SCALE_PROFILE_FORMAT,
            "formatVersion": SCALE_PROFILE_VERSION,
            "profileId": self.profile_id,
            **self.identity_content(),
        }

    def to_bytes(self) -> bytes:
        return canonical_json_file_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return sha256_digest(self.to_bytes())

    @classmethod
    def from_content_dict(cls, value: object) -> Self:
        item = _closed(value, _SCALE_CONTENT_FIELDS, "scale profile content")
        graph = item["processingGraph"]
        if not isinstance(graph, list):
            raise ProfileError("scale processing graph must be an array")
        try:
            cache_state = ScaleCacheState(item["cacheState"])
        except (TypeError, ValueError) as error:
            raise ProfileError("scale cache state is not registered") from error
        return cls(
            ScaleArtifactPin.from_dict(item["processingPlan"]),
            ScaleArtifactPin.from_dict(item["executionProfile"]),
            ScaleCorpus.from_dict(item["corpus"]),
            ScaleInputShape.from_dict(item["inputShape"]),
            tuple(ScaleProcessingStage.from_dict(stage) for stage in graph),
            ScaleResources.from_dict(item["resources"]),
            ScaleDocumentStorePolicy.from_dict(item["documentStorePolicy"]),
            ScaleResultSinkPin.from_dict(item["resultSink"]),
            ProfileSet.from_dict(item["profileSet"]),
            ScaleImplementationPin.from_dict(item["documentCatalog"]),
            None if item["baseRelease"] is None else DocumentReleaseRef.from_dict(item["baseRelease"]),
            ScalePlacement.from_dict(item["placement"]),
            cache_state,
            ScalePartitionPolicy.from_dict(item["partitionPolicy"]),
            ScaleTaskPolicy.from_dict(item["taskPolicy"]),
            ScaleTargets.from_dict(item["targets"]),
            ScaleAcceptanceAuthority.from_dict(item["acceptanceAuthority"]),
        )

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = _closed(
            value,
            _SCALE_CONTENT_FIELDS | {"format", "formatVersion", "profileId"},
            "scale profile",
        )
        if item["format"] != SCALE_PROFILE_FORMAT or item["formatVersion"] != SCALE_PROFILE_VERSION:
            raise ProfileError("scale profile has an unknown format")
        result = cls.from_content_dict({name: item[name] for name in _SCALE_CONTENT_FIELDS})
        if item["profileId"] != result.profile_id:
            raise ProfileError("scale profile identity differs from its content")
        return result

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        if not isinstance(data, bytes):
            raise TypeError("scale profile must be canonical JSON bytes")
        if len(data) > MAX_SCALE_PROFILE_BYTES:
            raise IntegrityError(f"scale profile exceeds its {MAX_SCALE_PROFILE_BYTES}-byte serialized limit")
        value = thaw_json(parse_canonical_json(data, label="scale profile"))
        return cls.from_dict(value)
