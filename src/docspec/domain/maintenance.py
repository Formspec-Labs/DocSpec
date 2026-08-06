"""Small immutable roots emitted by corpus maintenance operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

from docspec.domain.identity import freeze_json, require_sha256, require_text, stable_urn, thaw_json
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, LayerRef, StoreRef


@dataclass(frozen=True, slots=True)
class BlobRetentionSet:
    """Verified reachability roots plus a scalable layer of retained blobs."""

    retention_set_id: str
    blob_profile_state: ArtifactRef
    retained_releases: tuple[DocumentReleaseRef, ...]
    retained_stores: tuple[StoreRef, ...]
    references: LayerRef
    verification_evidence: dict[str, Any]

    def __post_init__(self) -> None:
        require_text(self.retention_set_id, "retention_set_id")
        if self.references.layer_kind != "blob-retention-references":
            raise ValueError("blob retention set names an unexpected reference layer")
        if self.references.schema_id != "docspec-blob-retention-reference/1.0":
            raise ValueError("blob retention set names an unexpected reference schema")
        if not self.retained_releases and not self.retained_stores:
            raise ValueError("blob retention set must name at least one immutable root")
        self._require_ordered_distinct(
            self.retained_releases,
            key=lambda item: (item.release_id, item.locator, item.digest),
            label="retained release roots",
        )
        self._require_ordered_distinct(
            self.retained_stores,
            key=lambda item: (item.store_id, item.revision, item.locator, item.digest),
            label="retained store roots",
        )
        expected_evidence = {
            "profileStateVerificationCount",
            "catalogVerifiedReleaseCount",
            "visitedStoreRevisionCount",
            "activeBlobLayerScanCount",
            "activeBlobRecordReadCount",
            "blobReferenceOccurrenceCount",
            "directBlobVerificationCount",
            "retainedReferenceCount",
            "boundedStreaming",
        }
        if set(self.verification_evidence) != expected_evidence:
            raise ValueError("blob retention verification evidence has an invalid closed shape")
        for name in expected_evidence - {"boundedStreaming"}:
            value = self.verification_evidence[name]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("blob retention verification counts must be non-negative integers")
        if self.verification_evidence["boundedStreaming"] is not True:
            raise ValueError("blob retention verification must declare bounded streaming")
        if self.verification_evidence["profileStateVerificationCount"] != 1:
            raise ValueError("blob retention set must verify exactly one blob profile state")
        if self.verification_evidence["retainedReferenceCount"] != self.references.record_count:
            raise ValueError("blob retention reference count differs from its verified layer")
        object.__setattr__(
            self,
            "verification_evidence",
            thaw_json(freeze_json(self.verification_evidence, label="blob retention verification evidence")),
        )
        if self.retention_set_id != stable_urn("blob-retention-set", self.identity_content()):
            raise ValueError("blob retention-set identity differs")

    @staticmethod
    def _require_ordered_distinct[T](
        values: tuple[T, ...],
        *,
        key: Any,
        label: str,
    ) -> None:
        keys = [key(item) for item in values]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError(f"{label} must be sorted and distinct")

    @classmethod
    def create(
        cls,
        *,
        blob_profile_state: ArtifactRef,
        retained_releases: tuple[DocumentReleaseRef, ...],
        retained_stores: tuple[StoreRef, ...],
        references: LayerRef,
        verification_evidence: dict[str, Any],
    ) -> Self:
        content = cls._content(
            blob_profile_state,
            retained_releases,
            retained_stores,
            references,
            verification_evidence,
        )
        return cls(
            stable_urn("blob-retention-set", content),
            blob_profile_state,
            retained_releases,
            retained_stores,
            references,
            verification_evidence,
        )

    @staticmethod
    def _content(
        blob_profile_state: ArtifactRef,
        retained_releases: tuple[DocumentReleaseRef, ...],
        retained_stores: tuple[StoreRef, ...],
        references: LayerRef,
        verification_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "blobProfileState": blob_profile_state.to_dict(),
            "retainedReleases": [item.to_dict() for item in retained_releases],
            "retainedStores": [item.to_dict() for item in retained_stores],
            "references": references.to_dict(),
            "verificationEvidence": verification_evidence,
        }

    def identity_content(self) -> dict[str, Any]:
        return self._content(
            self.blob_profile_state,
            self.retained_releases,
            self.retained_stores,
            self.references,
            self.verification_evidence,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "docspec-blob-retention-set",
            "formatVersion": "1.0",
            "retentionSetId": self.retention_set_id,
            **self.identity_content(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        expected = {
            "format",
            "formatVersion",
            "retentionSetId",
            "blobProfileState",
            "retainedReleases",
            "retainedStores",
            "references",
            "verificationEvidence",
        }
        if (
            set(value) != expected
            or value["format"] != "docspec-blob-retention-set"
            or value["formatVersion"] != "1.0"
        ):
            raise ValueError("blob retention set has an unknown format or invalid closed shape")
        return cls(
            value["retentionSetId"],
            ArtifactRef.from_dict(value["blobProfileState"]),
            tuple(DocumentReleaseRef.from_dict(item) for item in value["retainedReleases"]),
            tuple(StoreRef.from_dict(item) for item in value["retainedStores"]),
            LayerRef.from_dict(value["references"]),
            value["verificationEvidence"],
        )


@dataclass(frozen=True, slots=True)
class ReleaseCompactionReceipt:
    """Evidence that one successor preserves exact active logical records."""

    receipt_id: str
    source_release: DocumentReleaseRef
    successor_release: DocumentReleaseRef
    source_logical_state_digest: str
    successor_logical_state_digest: str
    rewritten_layer_kinds: tuple[str, ...]
    reused_layer_kinds: tuple[str, ...]
    completed_at: str
    verification_evidence: dict[str, Any]

    def __post_init__(self) -> None:
        require_text(self.receipt_id, "compaction receipt_id")
        require_text(self.completed_at, "compaction completed_at")
        require_sha256(self.source_logical_state_digest, "source logical-state digest")
        require_sha256(self.successor_logical_state_digest, "successor logical-state digest")
        if self.source_release == self.successor_release:
            raise ValueError("compaction must publish a distinct successor release")
        if self.source_logical_state_digest != self.successor_logical_state_digest:
            raise ValueError("compaction successor changes active logical state")
        if not self.rewritten_layer_kinds:
            raise ValueError("compaction must rewrite at least one physical layer")
        for label, values in (
            ("rewritten layer kinds", self.rewritten_layer_kinds),
            ("reused layer kinds", self.reused_layer_kinds),
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{label} must be sorted and distinct")
        if set(self.rewritten_layer_kinds) & set(self.reused_layer_kinds):
            raise ValueError("compaction layer classifications overlap")
        expected_evidence = {
            "logicalRecordCount",
            "logicalRecordReadCount",
            "logicalScanPassCount",
            "explicitCatalogOpenCount",
            "boundedStreaming",
        }
        if set(self.verification_evidence) != expected_evidence:
            raise ValueError("compaction verification evidence has an invalid closed shape")
        for name in expected_evidence - {"boundedStreaming"}:
            value = self.verification_evidence[name]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("compaction verification counts must be non-negative integers")
        if self.verification_evidence["boundedStreaming"] is not True:
            raise ValueError("compaction verification must declare bounded streaming")
        if self.verification_evidence["logicalScanPassCount"] != 3:
            raise ValueError("compaction must evidence source digest, rewrite, and successor digest scans")
        if self.verification_evidence["logicalRecordReadCount"] != (
            self.verification_evidence["logicalRecordCount"]
            * self.verification_evidence["logicalScanPassCount"]
        ):
            raise ValueError("compaction logical record-read evidence does not reconcile")
        object.__setattr__(
            self,
            "verification_evidence",
            thaw_json(freeze_json(self.verification_evidence, label="compaction verification evidence")),
        )
        if self.receipt_id != stable_urn("release-compaction-receipt", self.identity_content()):
            raise ValueError("release compaction-receipt identity differs")

    @classmethod
    def create(
        cls,
        *,
        source_release: DocumentReleaseRef,
        successor_release: DocumentReleaseRef,
        source_logical_state_digest: str,
        successor_logical_state_digest: str,
        rewritten_layer_kinds: tuple[str, ...],
        reused_layer_kinds: tuple[str, ...],
        completed_at: str,
        verification_evidence: dict[str, Any],
    ) -> Self:
        content = cls._content(
            source_release,
            successor_release,
            source_logical_state_digest,
            successor_logical_state_digest,
            rewritten_layer_kinds,
            reused_layer_kinds,
            completed_at,
            verification_evidence,
        )
        return cls(
            stable_urn("release-compaction-receipt", content),
            source_release,
            successor_release,
            source_logical_state_digest,
            successor_logical_state_digest,
            rewritten_layer_kinds,
            reused_layer_kinds,
            completed_at,
            verification_evidence,
        )

    @staticmethod
    def _content(
        source_release: DocumentReleaseRef,
        successor_release: DocumentReleaseRef,
        source_logical_state_digest: str,
        successor_logical_state_digest: str,
        rewritten_layer_kinds: tuple[str, ...],
        reused_layer_kinds: tuple[str, ...],
        completed_at: str,
        verification_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "sourceRelease": source_release.to_dict(),
            "successorRelease": successor_release.to_dict(),
            "sourceLogicalStateDigest": source_logical_state_digest,
            "successorLogicalStateDigest": successor_logical_state_digest,
            "rewrittenLayerKinds": list(rewritten_layer_kinds),
            "reusedLayerKinds": list(reused_layer_kinds),
            "completedAt": completed_at,
            "verificationEvidence": verification_evidence,
        }

    def identity_content(self) -> dict[str, Any]:
        return self._content(
            self.source_release,
            self.successor_release,
            self.source_logical_state_digest,
            self.successor_logical_state_digest,
            self.rewritten_layer_kinds,
            self.reused_layer_kinds,
            self.completed_at,
            self.verification_evidence,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "docspec-release-compaction-receipt",
            "formatVersion": "1.0",
            "receiptId": self.receipt_id,
            **self.identity_content(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Self:
        expected = {
            "format",
            "formatVersion",
            "receiptId",
            "sourceRelease",
            "successorRelease",
            "sourceLogicalStateDigest",
            "successorLogicalStateDigest",
            "rewrittenLayerKinds",
            "reusedLayerKinds",
            "completedAt",
            "verificationEvidence",
        }
        if (
            set(value) != expected
            or value["format"] != "docspec-release-compaction-receipt"
            or value["formatVersion"] != "1.0"
        ):
            raise ValueError("release compaction receipt has an unknown format or invalid closed shape")
        return cls(
            value["receiptId"],
            DocumentReleaseRef.from_dict(value["sourceRelease"]),
            DocumentReleaseRef.from_dict(value["successorRelease"]),
            value["sourceLogicalStateDigest"],
            value["successorLogicalStateDigest"],
            tuple(value["rewrittenLayerKinds"]),
            tuple(value["reusedLayerKinds"]),
            value["completedAt"],
            value["verificationEvidence"],
        )


__all__ = ["BlobRetentionSet", "ReleaseCompactionReceipt"]
