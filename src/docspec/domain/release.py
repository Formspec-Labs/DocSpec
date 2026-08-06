"""The canonical, format-neutral DocSpec corpus release root."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from docspec.domain.identity import (
    canonical_json_file_bytes,
    freeze_json,
    identity_digest,
    require_sha256,
    require_text,
    sha256_digest,
    stable_urn,
    thaw_json,
)
from docspec.domain.profiles import ProfileSet
from docspec.domain.policies import RetentionPolicy
from docspec.domain.references import ArtifactRef, DocumentReleaseRef, LayerRef, SourceCatalogRef


@dataclass(frozen=True, slots=True)
class DocumentRelease:
    """One complete immutable logical snapshot in a DocumentCatalog."""

    release_id: str
    previous_release: DocumentReleaseRef | None
    source_catalog: SourceCatalogRef
    processing_plan: ArtifactRef
    profiles: ProfileSet
    active_layers: tuple[LayerRef, ...]
    blob_roots: tuple[ArtifactRef, ...]
    retention_dispositions: RetentionPolicy
    store_receipt_set_digest: str
    run_receipt: ArtifactRef
    catalog_commit_receipt: ArtifactRef
    counts: dict[str, int]
    failures: dict[str, Any]
    coverage: dict[str, Any]
    partition_policy: dict[str, Any]

    def __post_init__(self) -> None:
        require_text(self.release_id, "release_id")
        require_sha256(self.store_receipt_set_digest, "store receipt-set digest")
        layers = [item.layer_kind for item in self.active_layers]
        if layers != sorted(layers) or len(set(layers)) != len(layers):
            raise ValueError("active release layer kinds must be sorted and distinct")
        roots = [item.artifact_id for item in self.blob_roots]
        if roots != sorted(roots) or len(set(roots)) != len(roots):
            raise ValueError("release blob roots must be sorted and distinct")
        if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in self.counts.values()):
            raise ValueError("release counts must be non-negative integers")
        object.__setattr__(self, "counts", thaw_json(freeze_json(self.counts, label="release counts")))
        if not isinstance(self.retention_dispositions, RetentionPolicy):
            raise TypeError("document release retention dispositions must use RetentionPolicy")
        object.__setattr__(self, "coverage", thaw_json(freeze_json(self.coverage, label="release coverage")))
        object.__setattr__(self, "failures", thaw_json(freeze_json(self.failures, label="release failures")))
        object.__setattr__(
            self,
            "partition_policy",
            thaw_json(freeze_json(self.partition_policy, label="partition policy")),
        )
        if self.release_id != stable_urn("document-release", self.identity_content()):
            raise ValueError("document release identity differs")

    @classmethod
    def create(
        cls,
        *,
        previous_release: DocumentReleaseRef | None,
        source_catalog: SourceCatalogRef,
        processing_plan: ArtifactRef,
        profiles: ProfileSet,
        active_layers: tuple[LayerRef, ...],
        blob_roots: tuple[ArtifactRef, ...],
        retention_dispositions: RetentionPolicy,
        store_receipt_set_digest: str,
        run_receipt: ArtifactRef,
        catalog_commit_receipt: ArtifactRef,
        counts: dict[str, int],
        failures: dict[str, Any],
        coverage: dict[str, Any],
        partition_policy: dict[str, Any],
    ) -> DocumentRelease:
        layers = tuple(sorted(active_layers, key=lambda item: (item.layer_kind, item.layer_id)))
        roots = tuple(sorted(blob_roots, key=lambda item: item.artifact_id))
        content = cls._content(
            previous_release,
            source_catalog,
            processing_plan,
            profiles,
            layers,
            roots,
            retention_dispositions,
            store_receipt_set_digest,
            run_receipt,
            catalog_commit_receipt,
            counts,
            failures,
            coverage,
            partition_policy,
        )
        return cls(
            stable_urn("document-release", content),
            previous_release,
            source_catalog,
            processing_plan,
            profiles,
            layers,
            roots,
            retention_dispositions,
            store_receipt_set_digest,
            run_receipt,
            catalog_commit_receipt,
            counts,
            failures,
            coverage,
            partition_policy,
        )

    @staticmethod
    def _content(
        previous_release: DocumentReleaseRef | None,
        source_catalog: SourceCatalogRef,
        processing_plan: ArtifactRef,
        profiles: ProfileSet,
        active_layers: tuple[LayerRef, ...],
        blob_roots: tuple[ArtifactRef, ...],
        retention_dispositions: RetentionPolicy,
        store_receipt_set_digest: str,
        run_receipt: ArtifactRef,
        catalog_commit_receipt: ArtifactRef,
        counts: dict[str, int],
        failures: dict[str, Any],
        coverage: dict[str, Any],
        partition_policy: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "previousRelease": None if previous_release is None else previous_release.to_dict(),
            "sourceCatalog": source_catalog.to_dict(),
            "processingPlan": processing_plan.to_dict(),
            "profiles": profiles.to_dict(),
            "activeLayers": [item.to_dict() for item in active_layers],
            "blobRoots": [item.to_dict() for item in blob_roots],
            "retentionDispositions": retention_dispositions.to_dict(),
            "storeReceiptSetDigest": store_receipt_set_digest,
            "runReceipt": run_receipt.to_dict(),
            "catalogCommitReceipt": catalog_commit_receipt.to_dict(),
            "counts": counts,
            "failures": failures,
            "coverage": coverage,
            "partitionPolicy": partition_policy,
        }

    def identity_content(self) -> dict[str, Any]:
        return self._content(
            self.previous_release,
            self.source_catalog,
            self.processing_plan,
            self.profiles,
            self.active_layers,
            self.blob_roots,
            self.retention_dispositions,
            self.store_receipt_set_digest,
            self.run_receipt,
            self.catalog_commit_receipt,
            self.counts,
            self.failures,
            self.coverage,
            self.partition_policy,
        )

    @property
    def logical_state_digest(self) -> str:
        return identity_digest(
            {
                "activeLayers": [item.to_dict() for item in self.active_layers],
                "blobRoots": [item.to_dict() for item in self.blob_roots],
                "retentionDispositions": self.retention_dispositions.to_dict(),
                "counts": self.counts,
                "coverage": self.coverage,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "docspec-document-release",
            "formatVersion": "1.1",
            "releaseId": self.release_id,
            **self.identity_content(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DocumentRelease:
        expected = {
            "format",
            "formatVersion",
            "releaseId",
            "previousRelease",
            "sourceCatalog",
            "processingPlan",
            "profiles",
            "activeLayers",
            "blobRoots",
            "retentionDispositions",
            "storeReceiptSetDigest",
            "runReceipt",
            "catalogCommitReceipt",
            "counts",
            "failures",
            "coverage",
            "partitionPolicy",
        }
        if set(value) != expected or value["format"] != "docspec-document-release" or value["formatVersion"] != "1.1":
            raise ValueError("document release has an unknown format or invalid closed shape")
        return cls(
            release_id=value["releaseId"],
            previous_release=None
            if value["previousRelease"] is None
            else DocumentReleaseRef.from_dict(value["previousRelease"]),
            source_catalog=SourceCatalogRef.from_dict(value["sourceCatalog"]),
            processing_plan=ArtifactRef.from_dict(value["processingPlan"]),
            profiles=ProfileSet.from_dict(value["profiles"]),
            active_layers=tuple(LayerRef.from_dict(item) for item in value["activeLayers"]),
            blob_roots=tuple(ArtifactRef.from_dict(item) for item in value["blobRoots"]),
            retention_dispositions=RetentionPolicy.from_dict(value["retentionDispositions"]),
            store_receipt_set_digest=value["storeReceiptSetDigest"],
            run_receipt=ArtifactRef.from_dict(value["runReceipt"]),
            catalog_commit_receipt=ArtifactRef.from_dict(value["catalogCommitReceipt"]),
            counts=value["counts"],
            failures=value["failures"],
            coverage=value["coverage"],
            partition_policy=value["partitionPolicy"],
        )

    @property
    def file_bytes(self) -> bytes:
        return canonical_json_file_bytes(self.to_dict())

    def reference(self, locator: str) -> DocumentReleaseRef:
        return DocumentReleaseRef(self.release_id, locator, sha256_digest(self.file_bytes))

    def artifact_ref(self, locator: str) -> ArtifactRef:
        payload = self.file_bytes
        return ArtifactRef(self.release_id, locator, sha256_digest(payload), "application/json", len(payload))
