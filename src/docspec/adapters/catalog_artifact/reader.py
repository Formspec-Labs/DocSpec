"""Open immutable snapshots and memoize full verification by pinned digest."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any

from rulespec_artifacts import (
    ROOT_OBJECT_KEY,
    ArtifactPin,
    ArtifactVerificationError,
    MemberSource,
    Producer,
    VerifiedArtifact,
    admit_artifact,
    expected_artifact_digest,
    expected_logical_id,
    parse_canonical_json,
)

from docspec.adapters.catalog_artifact import rows
from docspec.adapters.catalog_artifact.payloads import _CatalogPayloadSource
from docspec.adapters.catalog_artifact.rules import _CatalogPartition
from docspec.adapters.catalog_artifact.verification import SourceCatalogArtifactVerifier, SourceCatalogBuildGateVerifier
from docspec.domain.references import SourceCatalogRef
from docspec.errors import IntegrityError
from docspec.ports.source_catalog import (
    ImmutableSourceCatalogReader,
    LocatedSourceCatalogMapping,
    SourceCatalogBlobSource,
    SourceCatalogSnapshot,
    SourceCatalogSnapshotSummary,
    SourceCatalogStore,
)


@dataclass(frozen=True, slots=True)
class AdmittedSourceCatalog:
    """One pinned catalog's checked summary and bounded, repeatable row access.

    Obtain this through ``open_admitted_source_catalog`` or a reader's
    ``admit_snapshot``. A row stream checks the exact supplying payload before
    returning its rows. Exhaust the stream to check all ordering and counts;
    close it when stopping early to release the open partition resources.
    """

    summary: SourceCatalogSnapshotSummary
    _blob_source: SourceCatalogBlobSource
    _partitions: tuple[_CatalogPartition, ...]
    _rows_verified: bool = False

    def open_snapshot(self) -> SourceCatalogSnapshot:
        """Read domain objects without repeating artifact admission."""

        return SourceCatalogSnapshot(
            self.summary,
            rows._iter_located_catalog_rows(
                self._blob_source, self._partitions, self.summary.item_count,
                validate=not self._rows_verified,
            ),
        )

    def iter_located_mappings(self) -> Iterator[LocatedSourceCatalogMapping]:
        """Read validated JSON dictionaries with their supplying blob digests."""

        return rows._iter_located_catalog_rows(
            self._blob_source, self._partitions, self.summary.item_count,
            validate=not self._rows_verified, as_dict=True,
        )

    def iter_mappings(self) -> Iterator[dict[str, Any]]:
        """Read dictionaries without constructing or freezing domain objects."""

        located = self.iter_located_mappings()
        try:
            for value in located:
                yield value.item
        finally:
            located.close()


def open_admitted_source_catalog(
    artifact: VerifiedArtifact,
    source: MemberSource,
    *,
    blob_source: SourceCatalogBlobSource,
    producer: Producer,
    expected_pin: ArtifactPin,
) -> AdmittedSourceCatalog:
    """Open a Rulespec-admitted artifact without admitting or deriving it again.

    Supply the artifact returned by ``rulespec_artifacts.admit_artifact`` and
    its member source. The root, manifest digests, and small product members
    are checked against that admission; arbitrary replacement bytes do not
    inherit its verdict. Payloads are bound when each row stream opens them.
    Full producer re-derivation remains ``verify_snapshot``'s separate job.
    """

    try:
        root = parse_canonical_json(rows._read_small(source, ROOT_OBJECT_KEY), path=ROOT_OBJECT_KEY)
        if (
            artifact.pin != expected_pin
            or root != artifact.root
            or root.get("logicalId") != expected_pin.logical_id
            or root.get("artifactDigest") != expected_pin.artifact_digest
            or expected_logical_id(root) != expected_pin.logical_id
            or expected_artifact_digest(root) != expected_pin.artifact_digest
            or root.get("inputs") != [value.as_dict() for value in artifact.inputs]
            or root.get("memberManifests") != [value.as_dict() for value in artifact.manifests]
        ):
            raise IntegrityError("source catalog differs from the admitted artifact and expected pin")
        verifier = SourceCatalogArtifactVerifier(producer, blob_source)
        verifier(artifact, source)
    except ArtifactVerificationError as error:
        raise IntegrityError(f"source catalog artifact is invalid: {error}") from error
    if verifier.summary is None:
        raise RuntimeError("source catalog verifier produced no summary")
    return AdmittedSourceCatalog(
        verifier.summary,
        _CatalogPayloadSource(blob_source, verifier.partitions),
        verifier.partitions,
    )


class SourceCatalogArtifactReader(ImmutableSourceCatalogReader):
    """Open complete snapshots through an injected immutable member resolver."""

    def __init__(self, store: SourceCatalogStore, *, producer: Producer) -> None:
        self._store = store
        self._producer = producer
        self._verified: dict[tuple[str, str], SourceCatalogSnapshotSummary] = {}

    def open_snapshot(self, reference: SourceCatalogRef) -> SourceCatalogSnapshot:
        return self.admit_snapshot(reference).open_snapshot()

    def admit_snapshot(self, reference: SourceCatalogRef) -> AdmittedSourceCatalog:
        """Admit once, then choose object or mapping access on the same catalog."""

        try:
            source = self._store.source_for(reference)
            blob_source = self._store.blob_source()
            pin = ArtifactPin(reference.catalog_id, reference.digest)
            artifact = admit_artifact(
                source,
                blob_source=blob_source,
                expected_pin=pin,
            )
        except ArtifactVerificationError as error:
            raise IntegrityError(f"source catalog artifact is invalid: {error}") from error
        catalog = open_admitted_source_catalog(
            artifact, source, blob_source=blob_source,
            producer=self._producer, expected_pin=pin,
        )
        return replace(
            catalog,
            _rows_verified=(reference.catalog_id, reference.digest) in self._verified,
        )

    def verify_snapshot(self, reference: SourceCatalogRef) -> SourceCatalogSnapshotSummary:
        """Fully verify one snapshot, remembering the verdict for this reader.

        Runs the same gate the producer ran: artifact admission hashes every
        member against the manifest, and the build-gate verifier re-derives
        every digest and diagnostic from the rows and refuses any mismatch with
        the sealed spec and receipt. The verdict is memoized per
        (catalogId, digest) for the life of this reader, because the store is
        content-addressed and immutable.
        """

        key = (reference.catalog_id, reference.digest)
        cached = self._verified.get(key)
        if cached is not None:
            return cached
        try:
            source = self._store.source_for(reference)
            blob_source = self._store.blob_source()
            gate = SourceCatalogBuildGateVerifier(self._producer, blob_source)
            admit_artifact(
                source,
                blob_source=blob_source,
                expected_pin=ArtifactPin(reference.catalog_id, reference.digest),
                semantic_verifier=gate,
            )
        except ArtifactVerificationError as error:
            raise IntegrityError(f"source catalog artifact is invalid: {error}") from error
        if gate.summary is None:
            raise RuntimeError("source catalog gate verifier produced no summary")
        self._verified[key] = gate.summary
        return gate.summary
