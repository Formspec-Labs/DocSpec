"""Source-catalog, exact-file, representation, segment, and processor records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from docspec.domain.identity import (
    freeze_json,
    identity_digest,
    require_sha256,
    require_text,
    stable_urn,
    thaw_json,
)
from docspec.domain.references import BlobRef


IDENTITY_BYTE_SLICE_TRANSFORMATION = "identity-byte-slice"


class SourceItemState(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"
    EXCLUDED = "excluded"


class AcquisitionDisposition(StrEnum):
    CAPTURED = "captured"
    UNCHANGED = "unchanged"
    DELETED = "deleted"
    EXCLUDED = "excluded"
    ACCEPTED_FAILURE = "accepted-failure"
    REJECTED_RUN = "rejected-run"


class ProcessorDisposition(StrEnum):
    PRODUCED = "produced"
    ABSTAINED = "abstained"
    EXCLUDED = "excluded"
    ACCEPTED_FAILURE = "accepted-failure"
    REJECTED_RUN = "rejected-run"


def _captured_file_identity(
    *,
    source_item_id: str,
    source_version: str,
    candidate_id: str,
    blob_digest: str,
    media_type: str,
    transport_version: str | None,
) -> dict[str, Any]:
    return {
        "sourceItemId": source_item_id,
        "sourceVersion": source_version,
        "candidateId": candidate_id,
        "blobDigest": blob_digest,
        "mediaType": media_type,
        "transportVersion": transport_version,
    }


def _representation_identity(
    *,
    file_id: str,
    file_digest: str,
    kind: str,
    blob_digest: str,
    extractor_id: str,
    configuration_digest: str,
    evidence_mappings: tuple[EvidenceMapping, ...],
) -> dict[str, Any]:
    return {
        "fileId": file_id,
        "fileDigest": file_digest,
        "kind": kind,
        "blobDigest": blob_digest,
        "extractorId": extractor_id,
        "configurationDigest": configuration_digest,
        "evidenceMappings": [mapping.to_dict() for mapping in evidence_mappings],
    }


def _segment_identity(
    *,
    representation_id: str,
    representation_start: int,
    representation_end: int,
    ordinal: int,
    kind: str,
    content_digest: str,
    evidence: EvidenceCoordinate,
    segmenter_id: str,
    policy_digest: str,
) -> dict[str, Any]:
    return {
        "representationId": representation_id,
        "representationStart": representation_start,
        "representationEnd": representation_end,
        "ordinal": ordinal,
        "kind": kind,
        "contentDigest": content_digest,
        "evidence": evidence.to_dict(),
        "segmenterId": segmenter_id,
        "policyDigest": policy_digest,
    }


def _derived_record_identity(
    *,
    source_item_id: str,
    processor_id: str,
    input_ids: tuple[str, ...],
    schema_id: str,
    output_digest: str,
    provider_receipt_digest: str,
    disposition: ProcessorDisposition,
) -> dict[str, Any]:
    return {
        "sourceItemId": source_item_id,
        "processorId": processor_id,
        "inputIds": list(input_ids),
        "schemaId": schema_id,
        "outputDigest": output_digest,
        "providerReceiptDigest": provider_receipt_digest,
        "disposition": disposition.value,
    }


@dataclass(frozen=True, slots=True)
class CandidateFile:
    candidate_id: str
    locator: str
    media_type: str
    expected_digest: str | None = None
    expected_size: int | None = None
    transport_version: str | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        require_text(self.candidate_id, "candidate_id")
        require_text(self.locator, "candidate locator")
        require_text(self.media_type, "candidate media_type")
        if self.expected_digest is not None:
            require_sha256(self.expected_digest, "candidate expected_digest")
        if self.expected_size is not None and self.expected_size < 0:
            raise ValueError("candidate expected_size must be non-negative")
        if self.transport_version is not None:
            require_text(self.transport_version, "candidate transport_version")
        metadata = {} if self.metadata is None else self.metadata
        object.__setattr__(self, "metadata", thaw_json(freeze_json(metadata, label="candidate metadata")))

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidateId": self.candidate_id,
            "locator": self.locator,
            "mediaType": self.media_type,
            "expectedDigest": self.expected_digest,
            "expectedSize": self.expected_size,
            "transportVersion": self.transport_version,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CandidateFile:
        expected = {
            "candidateId",
            "locator",
            "mediaType",
            "expectedDigest",
            "expectedSize",
            "transportVersion",
            "metadata",
        }
        if set(value) != expected:
            raise ValueError("candidate file has an invalid closed shape")
        return cls(
            candidate_id=value["candidateId"],
            locator=value["locator"],
            media_type=value["mediaType"],
            expected_digest=value["expectedDigest"],
            expected_size=value["expectedSize"],
            transport_version=value["transportVersion"],
            metadata=value["metadata"],
        )


@dataclass(frozen=True, slots=True)
class SourceItem:
    item_id: str
    version: str
    candidates: tuple[CandidateFile, ...]
    state: SourceItemState = SourceItemState.ACTIVE
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        require_text(self.item_id, "source item_id")
        require_text(self.version, "source item version")
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if self.state == SourceItemState.ACTIVE and not self.candidates:
            raise ValueError("an active source item must contain at least one candidate")
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("source item candidate identities must be distinct")
        metadata = {} if self.metadata is None else self.metadata
        object.__setattr__(self, "metadata", thaw_json(freeze_json(metadata, label="source item metadata")))

    @property
    def identity(self) -> str:
        return stable_urn("source-item-version", {"itemId": self.item_id, "version": self.version})

    def to_dict(self) -> dict[str, Any]:
        return {
            "itemId": self.item_id,
            "version": self.version,
            "state": self.state.value,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SourceItem:
        if set(value) != {"itemId", "version", "state", "candidates", "metadata"}:
            raise ValueError("source item has an invalid closed shape")
        return cls(
            item_id=value["itemId"],
            version=value["version"],
            state=SourceItemState(value["state"]),
            candidates=tuple(CandidateFile.from_dict(item) for item in value["candidates"]),
            metadata=value["metadata"],
        )


@dataclass(frozen=True, slots=True)
class CapturedFile:
    file_id: str
    source_item_id: str
    source_version: str
    candidate_id: str
    blob: BlobRef
    media_type: str
    acquired_at: str
    downloader_id: str
    transport_version: str | None
    disposition: AcquisitionDisposition = AcquisitionDisposition.CAPTURED
    acquisition_started_at: str | None = None
    downloader_configuration_digest: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("file_id", self.file_id),
            ("source_item_id", self.source_item_id),
            ("source_version", self.source_version),
            ("candidate_id", self.candidate_id),
            ("media_type", self.media_type),
            ("acquired_at", self.acquired_at),
            ("downloader_id", self.downloader_id),
        ):
            require_text(value, label)
        for label, value in (
            ("acquisition_started_at", self.acquisition_started_at),
            ("task_id", self.task_id),
            ("attempt_id", self.attempt_id),
        ):
            if value is not None:
                require_text(value, label)
        if self.downloader_configuration_digest is not None:
            require_sha256(self.downloader_configuration_digest, "downloader configuration digest")
        expected = stable_urn(
            "captured-file",
            _captured_file_identity(
                source_item_id=self.source_item_id,
                source_version=self.source_version,
                candidate_id=self.candidate_id,
                blob_digest=self.blob.digest,
                media_type=self.media_type,
                transport_version=self.transport_version,
            ),
        )
        if self.file_id != expected:
            raise ValueError("captured file identity differs")

    @classmethod
    def create(
        cls,
        *,
        source_item_id: str,
        source_version: str,
        candidate_id: str,
        blob: BlobRef,
        media_type: str,
        acquired_at: str,
        downloader_id: str,
        transport_version: str | None,
        acquisition_started_at: str | None = None,
        downloader_configuration_digest: str | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> CapturedFile:
        identity = _captured_file_identity(
            source_item_id=source_item_id,
            source_version=source_version,
            candidate_id=candidate_id,
            blob_digest=blob.digest,
            media_type=media_type,
            transport_version=transport_version,
        )
        return cls(
            file_id=stable_urn("captured-file", identity),
            source_item_id=source_item_id,
            source_version=source_version,
            candidate_id=candidate_id,
            blob=blob,
            media_type=media_type,
            acquired_at=acquired_at,
            downloader_id=downloader_id,
            transport_version=transport_version,
            acquisition_started_at=acquisition_started_at,
            downloader_configuration_digest=downloader_configuration_digest,
            task_id=task_id,
            attempt_id=attempt_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fileId": self.file_id,
            "sourceItemId": self.source_item_id,
            "sourceVersion": self.source_version,
            "candidateId": self.candidate_id,
            "blob": self.blob.to_dict(),
            "mediaType": self.media_type,
            "acquiredAt": self.acquired_at,
            "downloaderId": self.downloader_id,
            "transportVersion": self.transport_version,
            "disposition": self.disposition.value,
            "acquisitionStartedAt": self.acquisition_started_at,
            "downloaderConfigurationDigest": self.downloader_configuration_digest,
            "taskId": self.task_id,
            "attemptId": self.attempt_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CapturedFile:
        expected = {
            "fileId",
            "sourceItemId",
            "sourceVersion",
            "candidateId",
            "blob",
            "mediaType",
            "acquiredAt",
            "downloaderId",
            "transportVersion",
            "disposition",
            "acquisitionStartedAt",
            "downloaderConfigurationDigest",
            "taskId",
            "attemptId",
        }
        if set(value) != expected:
            raise ValueError("captured file has an invalid closed shape")
        return cls(
            file_id=value["fileId"],
            source_item_id=value["sourceItemId"],
            source_version=value["sourceVersion"],
            candidate_id=value["candidateId"],
            blob=BlobRef.from_dict(value["blob"]),
            media_type=value["mediaType"],
            acquired_at=value["acquiredAt"],
            downloader_id=value["downloaderId"],
            transport_version=value["transportVersion"],
            disposition=AcquisitionDisposition(value["disposition"]),
            acquisition_started_at=value["acquisitionStartedAt"],
            downloader_configuration_digest=value["downloaderConfigurationDigest"],
            task_id=value["taskId"],
            attempt_id=value["attemptId"],
        )


@dataclass(frozen=True, slots=True)
class EvidenceCoordinate:
    coordinate_system: str
    source_digest: str
    start: int | None = None
    end: int | None = None
    page: int | None = None
    region: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        require_text(self.coordinate_system, "coordinate_system")
        require_sha256(self.source_digest, "evidence source_digest")
        if (self.start is None) != (self.end is None):
            raise ValueError("evidence start and end must be supplied together")
        if self.start is not None and (self.start < 0 or self.end is None or self.end < self.start):
            raise ValueError("evidence byte coordinates must form a non-negative half-open interval")
        if self.page is not None and self.page <= 0:
            raise ValueError("evidence page must be positive")
        region = None if self.region is None else thaw_json(freeze_json(self.region, label="evidence region"))
        object.__setattr__(self, "region", region)

    def to_dict(self) -> dict[str, Any]:
        return {
            "coordinateSystem": self.coordinate_system,
            "sourceDigest": self.source_digest,
            "start": self.start,
            "end": self.end,
            "page": self.page,
            "region": self.region,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EvidenceCoordinate:
        if set(value) != {"coordinateSystem", "sourceDigest", "start", "end", "page", "region"}:
            raise ValueError("evidence coordinate has an invalid closed shape")
        return cls(
            value["coordinateSystem"],
            value["sourceDigest"],
            value["start"],
            value["end"],
            value["page"],
            value["region"],
        )


@dataclass(frozen=True, slots=True)
class EvidenceMapping:
    """Map one half-open representation byte interval to captured-file evidence."""

    representation_start: int
    representation_end: int
    evidence: EvidenceCoordinate
    transformation: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.representation_start, bool)
            or isinstance(self.representation_end, bool)
            or not isinstance(self.representation_start, int)
            or not isinstance(self.representation_end, int)
            or self.representation_start < 0
            or self.representation_end < self.representation_start
        ):
            raise ValueError("representation mapping coordinates must form a non-negative half-open interval")
        require_text(self.transformation, "evidence mapping transformation")

    def to_dict(self) -> dict[str, Any]:
        return {
            "representationStart": self.representation_start,
            "representationEnd": self.representation_end,
            "evidence": self.evidence.to_dict(),
            "transformation": self.transformation,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EvidenceMapping:
        if set(value) != {"representationStart", "representationEnd", "evidence", "transformation"}:
            raise ValueError("evidence mapping has an invalid closed shape")
        return cls(
            value["representationStart"],
            value["representationEnd"],
            EvidenceCoordinate.from_dict(value["evidence"]),
            value["transformation"],
        )


def resolve_evidence_mapping(mapping: EvidenceMapping, start: int, end: int) -> EvidenceCoordinate:
    """Resolve a contained representation interval through one persisted mapping."""

    if not mapping.representation_start <= start <= end <= mapping.representation_end:
        raise ValueError("requested range leaves its representation evidence mapping")
    if mapping.transformation == IDENTITY_BYTE_SLICE_TRANSFORMATION:
        evidence = mapping.evidence
        if evidence.start is None or evidence.end is None:
            raise ValueError("identity evidence mapping requires captured-file byte coordinates")
        return EvidenceCoordinate(
            coordinate_system=evidence.coordinate_system,
            source_digest=evidence.source_digest,
            start=evidence.start + start - mapping.representation_start,
            end=evidence.start + end - mapping.representation_start,
            page=evidence.page,
            region=evidence.region,
        )
    if start == mapping.representation_start and end == mapping.representation_end:
        return mapping.evidence
    raise ValueError("a derived evidence mapping may only be used at its declared boundary")


@dataclass(frozen=True, slots=True)
class Representation:
    representation_id: str
    source_item_id: str
    file_id: str
    file_digest: str
    kind: str
    blob: BlobRef
    extractor_id: str
    configuration_digest: str
    evidence_mappings: tuple[EvidenceMapping, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (
            ("representation_id", self.representation_id),
            ("source_item_id", self.source_item_id),
            ("file_id", self.file_id),
            ("kind", self.kind),
            ("extractor_id", self.extractor_id),
        ):
            require_text(value, label)
        require_sha256(self.file_digest, "representation file_digest")
        require_sha256(self.configuration_digest, "representation configuration_digest")
        previous_end = 0
        for mapping in self.evidence_mappings:
            if mapping.representation_start < previous_end:
                raise ValueError("representation evidence mappings overlap or are out of order")
            if mapping.representation_end > self.blob.byte_size:
                raise ValueError("representation evidence mapping exceeds its bytes")
            if mapping.evidence.source_digest != self.file_digest:
                raise ValueError("representation evidence mapping names a different captured file")
            if mapping.transformation == IDENTITY_BYTE_SLICE_TRANSFORMATION:
                evidence = mapping.evidence
                if evidence.start is None or evidence.end is None:
                    raise ValueError("identity evidence mapping requires captured-file byte coordinates")
                if evidence.end - evidence.start != mapping.representation_end - mapping.representation_start:
                    raise ValueError("identity evidence mapping must preserve byte length")
            previous_end = mapping.representation_end
        for warning in self.warnings:
            require_text(warning, "representation warning")
        expected = stable_urn(
            "representation",
            _representation_identity(
                file_id=self.file_id,
                file_digest=self.file_digest,
                kind=self.kind,
                blob_digest=self.blob.digest,
                extractor_id=self.extractor_id,
                configuration_digest=self.configuration_digest,
                evidence_mappings=self.evidence_mappings,
            ),
        )
        if self.representation_id != expected:
            raise ValueError("representation identity differs")

    @classmethod
    def create(
        cls,
        *,
        source_item_id: str,
        file_id: str,
        file_digest: str,
        kind: str,
        blob: BlobRef,
        extractor_id: str,
        configuration_digest: str,
        evidence_mappings: tuple[EvidenceMapping, ...] = (),
        warnings: tuple[str, ...] = (),
    ) -> Representation:
        identity = _representation_identity(
            file_id=file_id,
            file_digest=file_digest,
            kind=kind,
            blob_digest=blob.digest,
            extractor_id=extractor_id,
            configuration_digest=configuration_digest,
            evidence_mappings=evidence_mappings,
        )
        return cls(
            stable_urn("representation", identity),
            source_item_id,
            file_id,
            file_digest,
            kind,
            blob,
            extractor_id,
            configuration_digest,
            evidence_mappings,
            warnings,
        )

    @property
    def boundaries(self) -> tuple[EvidenceCoordinate, ...]:
        """Evidence boundaries retained as a derived view for planning and reporting."""

        return tuple(mapping.evidence for mapping in self.evidence_mappings)

    def evidence_for_range(self, start: int, end: int) -> EvidenceCoordinate:
        """Resolve one representation byte interval through its persisted evidence mapping."""

        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end < start
            or end > self.blob.byte_size
        ):
            raise ValueError("requested representation range is outside its bytes")
        for mapping in self.evidence_mappings:
            if mapping.representation_start <= start and end <= mapping.representation_end:
                return resolve_evidence_mapping(mapping, start, end)
        raise ValueError("representation range has no single reversible evidence mapping")

    def to_dict(self) -> dict[str, Any]:
        return {
            "representationId": self.representation_id,
            "sourceItemId": self.source_item_id,
            "fileId": self.file_id,
            "fileDigest": self.file_digest,
            "kind": self.kind,
            "blob": self.blob.to_dict(),
            "extractorId": self.extractor_id,
            "configurationDigest": self.configuration_digest,
            "evidenceMappings": [item.to_dict() for item in self.evidence_mappings],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Representation:
        expected = {
            "representationId",
            "sourceItemId",
            "fileId",
            "fileDigest",
            "kind",
            "blob",
            "extractorId",
            "configurationDigest",
            "evidenceMappings",
            "warnings",
        }
        if set(value) != expected:
            raise ValueError("representation has an invalid closed shape")
        return cls(
            value["representationId"],
            value["sourceItemId"],
            value["fileId"],
            value["fileDigest"],
            value["kind"],
            BlobRef.from_dict(value["blob"]),
            value["extractorId"],
            value["configurationDigest"],
            tuple(EvidenceMapping.from_dict(item) for item in value["evidenceMappings"]),
            tuple(value["warnings"]),
        )


@dataclass(frozen=True, slots=True)
class Segment:
    segment_id: str
    source_item_id: str
    file_id: str
    representation_id: str
    representation_start: int
    representation_end: int
    ordinal: int
    kind: str
    content: BlobRef
    evidence: EvidenceCoordinate
    segmenter_id: str
    policy_digest: str
    derivation: tuple[str, ...]

    def __post_init__(self) -> None:
        for label, value in (
            ("segment_id", self.segment_id),
            ("source_item_id", self.source_item_id),
            ("file_id", self.file_id),
            ("representation_id", self.representation_id),
            ("kind", self.kind),
            ("segmenter_id", self.segmenter_id),
        ):
            require_text(value, label)
        require_sha256(self.policy_digest, "segment policy_digest")
        if self.ordinal < 0:
            raise ValueError("segment ordinal must be non-negative")
        if (
            isinstance(self.representation_start, bool)
            or isinstance(self.representation_end, bool)
            or not isinstance(self.representation_start, int)
            or not isinstance(self.representation_end, int)
            or self.representation_start < 0
            or self.representation_end < self.representation_start
        ):
            raise ValueError("segment representation coordinates must form a non-negative half-open interval")
        if self.representation_end - self.representation_start != self.content.byte_size:
            raise ValueError("segment representation interval byte size differs from its content")
        expected = stable_urn(
            "segment",
            _segment_identity(
                representation_id=self.representation_id,
                representation_start=self.representation_start,
                representation_end=self.representation_end,
                ordinal=self.ordinal,
                kind=self.kind,
                content_digest=self.content.digest,
                evidence=self.evidence,
                segmenter_id=self.segmenter_id,
                policy_digest=self.policy_digest,
            ),
        )
        if self.segment_id != expected:
            raise ValueError("segment identity differs")

    @classmethod
    def create(
        cls,
        *,
        source_item_id: str,
        file_id: str,
        representation_id: str,
        representation_start: int,
        representation_end: int,
        ordinal: int,
        kind: str,
        content: BlobRef,
        evidence: EvidenceCoordinate,
        segmenter_id: str,
        policy_digest: str,
        derivation: tuple[str, ...],
    ) -> Segment:
        identity = _segment_identity(
            representation_id=representation_id,
            representation_start=representation_start,
            representation_end=representation_end,
            ordinal=ordinal,
            kind=kind,
            content_digest=content.digest,
            evidence=evidence,
            segmenter_id=segmenter_id,
            policy_digest=policy_digest,
        )
        return cls(
            stable_urn("segment", identity),
            source_item_id,
            file_id,
            representation_id,
            representation_start,
            representation_end,
            ordinal,
            kind,
            content,
            evidence,
            segmenter_id,
            policy_digest,
            derivation,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "segmentId": self.segment_id,
            "sourceItemId": self.source_item_id,
            "fileId": self.file_id,
            "representationId": self.representation_id,
            "representationStart": self.representation_start,
            "representationEnd": self.representation_end,
            "ordinal": self.ordinal,
            "kind": self.kind,
            "content": self.content.to_dict(),
            "evidence": self.evidence.to_dict(),
            "segmenterId": self.segmenter_id,
            "policyDigest": self.policy_digest,
            "derivation": list(self.derivation),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Segment:
        expected = {
            "segmentId",
            "sourceItemId",
            "fileId",
            "representationId",
            "representationStart",
            "representationEnd",
            "ordinal",
            "kind",
            "content",
            "evidence",
            "segmenterId",
            "policyDigest",
            "derivation",
        }
        if set(value) != expected:
            raise ValueError("segment has an invalid closed shape")
        return cls(
            value["segmentId"],
            value["sourceItemId"],
            value["fileId"],
            value["representationId"],
            value["representationStart"],
            value["representationEnd"],
            value["ordinal"],
            value["kind"],
            BlobRef.from_dict(value["content"]),
            EvidenceCoordinate.from_dict(value["evidence"]),
            value["segmenterId"],
            value["policyDigest"],
            tuple(value["derivation"]),
        )


@dataclass(frozen=True, slots=True)
class DerivedRecord:
    derived_id: str
    source_item_id: str
    processor_id: str
    input_ids: tuple[str, ...]
    schema_id: str
    value: dict[str, Any]
    output_digest: str
    provider_receipt_digest: str
    disposition: ProcessorDisposition
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            disposition = ProcessorDisposition(self.disposition)
        except ValueError as error:
            raise ValueError("derived record disposition is not registered") from error
        object.__setattr__(self, "disposition", disposition)
        for label, value in (
            ("derived_id", self.derived_id),
            ("source_item_id", self.source_item_id),
            ("processor_id", self.processor_id),
            ("schema_id", self.schema_id),
        ):
            require_text(value, label)
        for input_id in self.input_ids:
            require_text(input_id, "derived input_id")
        if len(set(self.input_ids)) != len(self.input_ids):
            raise ValueError("derived input identities must be distinct")
        require_sha256(self.output_digest, "derived output_digest")
        require_sha256(self.provider_receipt_digest, "provider receipt digest")
        frozen_value = freeze_json(self.value, label="derived value")
        object.__setattr__(self, "value", thaw_json(frozen_value))
        if identity_digest(frozen_value) != self.output_digest:
            raise ValueError("derived output digest differs from its value")
        expected = stable_urn(
            "derived-record",
            _derived_record_identity(
                source_item_id=self.source_item_id,
                processor_id=self.processor_id,
                input_ids=self.input_ids,
                schema_id=self.schema_id,
                output_digest=self.output_digest,
                provider_receipt_digest=self.provider_receipt_digest,
                disposition=disposition,
            ),
        )
        if self.derived_id != expected:
            raise ValueError("derived record identity differs")

    @classmethod
    def create(
        cls,
        *,
        source_item_id: str,
        processor_id: str,
        input_ids: tuple[str, ...],
        schema_id: str,
        value: dict[str, Any],
        provider_receipt_digest: str,
        disposition: ProcessorDisposition,
        warnings: tuple[str, ...] = (),
    ) -> DerivedRecord:
        output_digest = identity_digest(value)
        identity = _derived_record_identity(
            source_item_id=source_item_id,
            processor_id=processor_id,
            input_ids=input_ids,
            schema_id=schema_id,
            output_digest=output_digest,
            provider_receipt_digest=provider_receipt_digest,
            disposition=disposition,
        )
        return cls(
            derived_id=stable_urn("derived-record", identity),
            source_item_id=source_item_id,
            processor_id=processor_id,
            input_ids=input_ids,
            schema_id=schema_id,
            value=value,
            output_digest=output_digest,
            provider_receipt_digest=provider_receipt_digest,
            disposition=disposition,
            warnings=warnings,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "derivedId": self.derived_id,
            "sourceItemId": self.source_item_id,
            "processorId": self.processor_id,
            "inputIds": list(self.input_ids),
            "schemaId": self.schema_id,
            "value": self.value,
            "outputDigest": self.output_digest,
            "providerReceiptDigest": self.provider_receipt_digest,
            "disposition": self.disposition.value,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DerivedRecord:
        expected = {
            "derivedId",
            "sourceItemId",
            "processorId",
            "inputIds",
            "schemaId",
            "value",
            "outputDigest",
            "providerReceiptDigest",
            "disposition",
            "warnings",
        }
        if set(value) != expected:
            raise ValueError("derived record has an invalid closed shape")
        return cls(
            value["derivedId"],
            value["sourceItemId"],
            value["processorId"],
            tuple(value["inputIds"]),
            value["schemaId"],
            value["value"],
            value["outputDigest"],
            value["providerReceiptDigest"],
            ProcessorDisposition(value["disposition"]),
            tuple(value["warnings"]),
        )
