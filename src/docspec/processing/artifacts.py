"""Small byte-bearing values used inside one DocSpec worker.

Scheduler messages carry only immutable references. These values deliberately
stay inside a worker while extraction and segmentation run, then a storage
adapter persists their bytes and records.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from docspec.domain.content import (
    IDENTITY_BYTE_SLICE_TRANSFORMATION,
    EvidenceCoordinate,
    EvidenceMapping,
    Representation,
    Segment,
)
from docspec.domain.identity import sha256_digest
from docspec.domain.references import BlobRef
from docspec.errors import IntegrityError

IDENTITY_TRANSFORM = IDENTITY_BYTE_SLICE_TRANSFORMATION
PDF_PAGE_TEXT_TRANSFORM = "pypdf-page-text"


def content_blob_ref(content: bytes, media_type: str) -> BlobRef:
    """Return a location-neutral content-addressed reference for worker output."""

    if not isinstance(content, bytes):
        raise TypeError("worker content must be immutable bytes")
    digest = sha256_digest(content)
    return BlobRef(
        locator=f"cas+sha256://{digest.removeprefix('sha256:')}",
        digest=digest,
        byte_size=len(content),
        media_type=media_type,
    )


def verify_blob_bytes(reference: BlobRef, content: bytes, *, label: str) -> None:
    """Fail closed unless bytes match their immutable reference."""

    if not isinstance(content, bytes):
        raise TypeError(f"{label} must be immutable bytes")
    if reference.byte_size != len(content):
        raise IntegrityError(f"{label} byte size differs from its reference")
    if reference.digest != sha256_digest(content):
        raise IntegrityError(f"{label} digest differs from its reference")


def decode_utf8(content: bytes, *, label: str) -> str:
    """Decode exact UTF-8 and attach a useful source label to failures."""

    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise IntegrityError(f"{label} is not valid UTF-8: {error}") from error


def utf8_byte_offsets(text: str) -> tuple[int, ...]:
    """Map every Python codepoint boundary to its exact UTF-8 byte offset."""

    offsets = [0]
    total = 0
    for character in text:
        total += len(character.encode("utf-8"))
        offsets.append(total)
    return tuple(offsets)


@dataclass(frozen=True, slots=True)
class RepresentationPayload:
    """One immutable representation record together with its worker-local bytes."""

    representation: Representation
    content: bytes

    def __post_init__(self) -> None:
        verify_blob_bytes(self.representation.blob, self.content, label="representation")
        recreated = Representation.create(
            source_item_id=self.representation.source_item_id,
            file_id=self.representation.file_id,
            file_digest=self.representation.file_digest,
            kind=self.representation.kind,
            blob=self.representation.blob,
            extractor_id=self.representation.extractor_id,
            configuration_digest=self.representation.configuration_digest,
            evidence_mappings=self.representation.evidence_mappings,
            warnings=self.representation.warnings,
        )
        if recreated.representation_id != self.representation.representation_id:
            raise IntegrityError("representation identity does not match its semantic inputs")

    def evidence_for_range(self, start: int, end: int) -> EvidenceCoordinate:
        """Resolve one exact representation slice to one source coordinate."""

        if start < 0 or end < start or end > len(self.content):
            raise IntegrityError("requested representation range is outside its bytes")
        try:
            return self.representation.evidence_for_range(start, end)
        except ValueError as error:
            raise IntegrityError(str(error)) from error


@dataclass(frozen=True, slots=True)
class SegmentPayload:
    """One segment record together with its worker-local exact content bytes."""

    segment: Segment
    content: bytes

    def __post_init__(self) -> None:
        verify_blob_bytes(self.segment.content, self.content, label="segment")
        recreated = Segment.create(
            source_item_id=self.segment.source_item_id,
            file_id=self.segment.file_id,
            representation_id=self.segment.representation_id,
            representation_start=self.segment.representation_start,
            representation_end=self.segment.representation_end,
            ordinal=self.segment.ordinal,
            kind=self.segment.kind,
            content=self.segment.content,
            evidence=self.segment.evidence,
            segmenter_id=self.segment.segmenter_id,
            policy_digest=self.segment.policy_digest,
            derivation=self.segment.derivation,
        )
        if recreated.segment_id != self.segment.segment_id:
            raise IntegrityError("segment identity does not match its semantic inputs")

    @property
    def representation_start(self) -> int:
        return self.segment.representation_start

    @property
    def representation_end(self) -> int:
        return self.segment.representation_end


def build_segment(
    representation: RepresentationPayload,
    *,
    ordinal: int,
    kind: str,
    start: int,
    end: int,
    segmenter_id: str,
    policy_digest: str,
    derivation: tuple[str, ...],
    media_type: str | None = None,
) -> SegmentPayload:
    """Build one identified segment from an exact representation byte range.

    Public because every segmenter in this package mints a segment the same
    way: exact slice, content-addressed reference, evidence resolved through
    the representation's own mapping, identity recomputed from those inputs.
    """

    content = representation.content[start:end]
    reference = content_blob_ref(content, media_type or representation.representation.blob.media_type)
    evidence = representation.evidence_for_range(start, end)
    source = representation.representation
    segment = Segment.create(
        source_item_id=source.source_item_id,
        file_id=source.file_id,
        representation_id=source.representation_id,
        representation_start=start,
        representation_end=end,
        ordinal=ordinal,
        kind=kind,
        content=reference,
        evidence=evidence,
        segmenter_id=segmenter_id,
        policy_digest=policy_digest,
        derivation=(f"representation:{source.representation_id}", *derivation),
    )
    return SegmentPayload(segment, content)


DerivedEvidenceResolver = Callable[[EvidenceMapping, bytes], bytes]


def verify_representation_evidence(
    payload: RepresentationPayload,
    source_bytes: bytes,
    *,
    derived_resolver: DerivedEvidenceResolver | None = None,
) -> None:
    """Prove every representation range from its exact captured source bytes."""

    if sha256_digest(source_bytes) != payload.representation.file_digest:
        raise IntegrityError("captured source bytes differ from the representation's file digest")
    for mapping in payload.representation.evidence_mappings:
        evidence = mapping.evidence
        if evidence.source_digest != payload.representation.file_digest:
            raise IntegrityError("evidence source digest differs from the representation's file digest")
        output = payload.content[mapping.representation_start : mapping.representation_end]
        if mapping.transformation == IDENTITY_TRANSFORM:
            if evidence.start is None or evidence.end is None:
                raise IntegrityError("identity evidence requires source byte coordinates")
            if evidence.end > len(source_bytes):
                raise IntegrityError("evidence byte range exceeds the captured source")
            resolved = source_bytes[evidence.start : evidence.end]
        elif derived_resolver is not None:
            resolved = derived_resolver(mapping, source_bytes)
        else:
            raise IntegrityError(f"evidence transformation {mapping.transformation!r} requires its named resolver")
        if resolved != output:
            raise IntegrityError("representation bytes do not round-trip through their source evidence")


def verify_segment_representation(
    payload: SegmentPayload,
    representation: RepresentationPayload,
) -> None:
    """Prove a reloaded segment is the exact persisted representation slice it names."""

    if payload.segment.representation_id != representation.representation.representation_id:
        raise IntegrityError("segment names a different representation")
    if payload.segment.file_id != representation.representation.file_id:
        raise IntegrityError("segment names a different captured file")
    if payload.representation_end > len(representation.content):
        raise IntegrityError("segment range exceeds its representation")
    expected_content = representation.content[payload.representation_start : payload.representation_end]
    if payload.content != expected_content:
        raise IntegrityError("segment bytes differ from their exact representation slice")
    expected_evidence = representation.evidence_for_range(
        payload.representation_start,
        payload.representation_end,
    )
    if payload.segment.evidence != expected_evidence:
        raise IntegrityError("segment evidence differs from its representation mapping")


def verify_segment_evidence(
    payload: SegmentPayload,
    representation: RepresentationPayload,
    source_bytes: bytes,
    *,
    derived_resolver: DerivedEvidenceResolver | None = None,
) -> None:
    """Prove a segment resolves to its representation and captured source."""

    verify_segment_representation(payload, representation)
    verify_representation_evidence(representation, source_bytes, derived_resolver=derived_resolver)
