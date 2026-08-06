"""Sealed processing plans and bounded work declarations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from docspec.domain.identity import freeze_json, require_sha256, require_text, stable_urn, thaw_json
from docspec.domain.policies import DataUsePolicy, RetentionPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.profiles import ProfileSet
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, SourceCatalogRef


@dataclass(frozen=True, slots=True)
class WorkLimits:
    max_entries: int
    max_estimated_bytes: int
    max_pages_or_frames: int
    max_segments: int
    max_processor_cost: int
    max_memory_bytes: int
    max_duration_seconds: int
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in self.to_dict().values()):
            raise ValueError("all work limits must be positive integers")

    def to_dict(self) -> dict[str, int]:
        return {
            "maxEntries": self.max_entries,
            "maxEstimatedBytes": self.max_estimated_bytes,
            "maxPagesOrFrames": self.max_pages_or_frames,
            "maxSegments": self.max_segments,
            "maxProcessorCost": self.max_processor_cost,
            "maxMemoryBytes": self.max_memory_bytes,
            "maxDurationSeconds": self.max_duration_seconds,
            "maxAttempts": self.max_attempts,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WorkLimits:
        expected = {
            "maxEntries",
            "maxEstimatedBytes",
            "maxPagesOrFrames",
            "maxSegments",
            "maxProcessorCost",
            "maxMemoryBytes",
            "maxDurationSeconds",
            "maxAttempts",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("work limits have an invalid closed shape")
        return cls(
            max_entries=value["maxEntries"],
            max_estimated_bytes=value["maxEstimatedBytes"],
            max_pages_or_frames=value["maxPagesOrFrames"],
            max_segments=value["maxSegments"],
            max_processor_cost=value["maxProcessorCost"],
            max_memory_bytes=value["maxMemoryBytes"],
            max_duration_seconds=value["maxDurationSeconds"],
            max_attempts=value["maxAttempts"],
        )


@dataclass(frozen=True, slots=True)
class StagePolicy:
    extractor_ids: tuple[str, ...]
    segmenter_id: str
    processor_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.extractor_ids, tuple) or not isinstance(self.processor_ids, tuple):
            raise ValueError("stage identities must be immutable tuples")
        if not self.extractor_ids:
            raise ValueError("stage policy requires at least one extractor")
        for value in (*self.extractor_ids, self.segmenter_id, *self.processor_ids):
            require_text(value, "stage identity")
        if len(set(self.extractor_ids)) != len(self.extractor_ids):
            raise ValueError("extractor identities must be distinct")
        if len(set(self.processor_ids)) != len(self.processor_ids):
            raise ValueError("processor identities must be distinct")

    def to_dict(self) -> dict[str, Any]:
        return {
            "extractorIds": list(self.extractor_ids),
            "segmenterId": self.segmenter_id,
            "processorIds": list(self.processor_ids),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> StagePolicy:
        if not isinstance(value, dict) or set(value) != {"extractorIds", "segmenterId", "processorIds"}:
            raise ValueError("stage policy has an invalid closed shape")
        if not isinstance(value["extractorIds"], list) or not isinstance(value["processorIds"], list):
            raise ValueError("stage policy identities must be arrays")
        return cls(tuple(value["extractorIds"]), value["segmenterId"], tuple(value["processorIds"]))


@dataclass(frozen=True, slots=True)
class ProcessingPlan:
    plan_id: str
    source_catalog: SourceCatalogRef
    base_release: DocumentReleaseRef | None
    profiles: ProfileSet
    limits: WorkLimits
    stages: StagePolicy
    processors: ProcessorSet
    partition_count: int
    selection: dict[str, Any]
    retention_policy: RetentionPolicy
    data_use_policy: DataUsePolicy
    retry_policy_digest: str
    accepted_failure_policy_digest: str

    def __post_init__(self) -> None:
        require_text(self.plan_id, "plan_id")
        if type(self.partition_count) is not int or self.partition_count <= 0 or self.partition_count > 65_536:
            raise ValueError("partition_count must be between 1 and 65536")
        require_sha256(self.retry_policy_digest, "retry policy digest")
        require_sha256(self.accepted_failure_policy_digest, "accepted-failure policy digest")
        processor_order = tuple(item.processor_id for item in self.processors.execution_order)
        if processor_order != self.stages.processor_ids:
            raise ValueError("stage processor identities differ from the pinned processor graph")
        selection = thaw_json(freeze_json(self.selection, label="selection"))
        if not isinstance(selection, dict):
            raise ValueError("processing plan selection must be a JSON object")
        object.__setattr__(self, "selection", selection)
        if not isinstance(self.retention_policy, RetentionPolicy):
            raise TypeError("processing plan retention policy must be a RetentionPolicy")
        if not isinstance(self.data_use_policy, DataUsePolicy):
            raise TypeError("processing plan data-use policy must be a DataUsePolicy")
        if self.plan_id != stable_urn("processing-plan", self.identity_content()):
            raise ValueError("processing plan identity differs")

    @classmethod
    def create(
        cls,
        *,
        source_catalog: SourceCatalogRef,
        base_release: DocumentReleaseRef | None,
        profiles: ProfileSet,
        limits: WorkLimits,
        stages: StagePolicy,
        processors: ProcessorSet,
        partition_count: int,
        selection: dict[str, Any],
        retention_policy: RetentionPolicy,
        data_use_policy: DataUsePolicy,
        retry_policy_digest: str,
        accepted_failure_policy_digest: str,
    ) -> ProcessingPlan:
        content = {
            "sourceCatalog": source_catalog.to_dict(),
            "baseRelease": None if base_release is None else base_release.to_dict(),
            "profiles": profiles.to_dict(),
            "limits": limits.to_dict(),
            "stages": stages.to_dict(),
            "processors": processors.to_dict(),
            "partitionCount": partition_count,
            "selection": selection,
            "retentionPolicy": retention_policy.to_dict(),
            "dataUsePolicy": data_use_policy.to_dict(),
            "retryPolicyDigest": retry_policy_digest,
            "acceptedFailurePolicyDigest": accepted_failure_policy_digest,
        }
        return cls(
            stable_urn("processing-plan", content),
            source_catalog,
            base_release,
            profiles,
            limits,
            stages,
            processors,
            partition_count,
            selection,
            retention_policy,
            data_use_policy,
            retry_policy_digest,
            accepted_failure_policy_digest,
        )

    def identity_content(self) -> dict[str, Any]:
        return {
            "sourceCatalog": self.source_catalog.to_dict(),
            "baseRelease": None if self.base_release is None else self.base_release.to_dict(),
            "profiles": self.profiles.to_dict(),
            "limits": self.limits.to_dict(),
            "stages": self.stages.to_dict(),
            "processors": self.processors.to_dict(),
            "partitionCount": self.partition_count,
            "selection": self.selection,
            "retentionPolicy": self.retention_policy.to_dict(),
            "dataUsePolicy": self.data_use_policy.to_dict(),
            "retryPolicyDigest": self.retry_policy_digest,
            "acceptedFailurePolicyDigest": self.accepted_failure_policy_digest,
        }

    def governing_content(self) -> dict[str, Any]:
        """Return behavior that may invalidate otherwise unchanged source items."""

        return {
            "profiles": self.profiles.to_dict(),
            "limits": self.limits.to_dict(),
            "stages": self.stages.to_dict(),
            "processors": self.processors.to_dict(),
            "partitionCount": self.partition_count,
            "selection": self.selection,
            "retentionPolicy": self.retention_policy.to_dict(),
            "dataUsePolicy": self.data_use_policy.to_dict(),
            "retryPolicyDigest": self.retry_policy_digest,
            "acceptedFailurePolicyDigest": self.accepted_failure_policy_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"format": "docspec-processing-plan", "formatVersion": "1.2", "planId": self.plan_id, **self.identity_content()}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ProcessingPlan:
        expected = {
            "format",
            "formatVersion",
            "planId",
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
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or value["format"] != "docspec-processing-plan"
            or value["formatVersion"] != "1.2"
        ):
            raise ValueError("processing plan has an unknown format or invalid closed shape")
        return cls(
            plan_id=value["planId"],
            source_catalog=SourceCatalogRef.from_dict(value["sourceCatalog"]),
            base_release=None if value["baseRelease"] is None else DocumentReleaseRef.from_dict(value["baseRelease"]),
            profiles=ProfileSet.from_dict(value["profiles"]),
            limits=WorkLimits.from_dict(value["limits"]),
            stages=StagePolicy.from_dict(value["stages"]),
            processors=ProcessorSet.from_dict(value["processors"]),
            partition_count=value["partitionCount"],
            selection=value["selection"],
            retention_policy=RetentionPolicy.from_dict(value["retentionPolicy"]),
            data_use_policy=DataUsePolicy.from_dict(value["dataUsePolicy"]),
            retry_policy_digest=value["retryPolicyDigest"],
            accepted_failure_policy_digest=value["acceptedFailurePolicyDigest"],
        )

    def artifact_ref(self, *, locator: str) -> ArtifactRef:
        from docspec.domain.identity import canonical_json_file_bytes, sha256_digest

        payload = canonical_json_file_bytes(self.to_dict())
        return ArtifactRef(self.plan_id, locator, sha256_digest(payload), "application/json", len(payload))
