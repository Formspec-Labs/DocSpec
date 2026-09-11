"""Open immutable snapshots and memoize full verification by pinned digest."""

from __future__ import annotations

from rulespec_artifacts import (
    ArtifactPin,
    ArtifactVerificationError,
    Producer,
    admit_artifact,
)

from docspec.adapters.catalog_artifact import rows
from docspec.adapters.catalog_artifact.verification import SourceCatalogArtifactVerifier, SourceCatalogBuildGateVerifier
from docspec.domain.references import SourceCatalogRef
from docspec.errors import IntegrityError
from docspec.ports.source_catalog import (
    ImmutableSourceCatalogReader,
    SourceCatalogSnapshot,
    SourceCatalogSnapshotSummary,
    SourceCatalogStore,
)


class SourceCatalogArtifactReader(ImmutableSourceCatalogReader):
    """Open complete snapshots through an injected immutable member resolver."""

    def __init__(self, store: SourceCatalogStore, *, producer: Producer) -> None:
        self._store = store
        self._producer = producer
        self._verified: dict[tuple[str, str], SourceCatalogSnapshotSummary] = {}

    def open_snapshot(self, reference: SourceCatalogRef) -> SourceCatalogSnapshot:
        try:
            source = self._store.source_for(reference)
            blob_source = self._store.blob_source()
            verifier = SourceCatalogArtifactVerifier(self._producer, blob_source)
            admit_artifact(
                source,
                blob_source=blob_source,
                expected_pin=ArtifactPin(reference.catalog_id, reference.digest),
                semantic_verifier=verifier,
            )
        except ArtifactVerificationError as error:
            raise IntegrityError(f"source catalog artifact is invalid: {error}") from error
        if verifier.summary is None:
            raise RuntimeError("source catalog verifier produced no summary")
        # A reader that already ran the full gate on this exact digest
        # (verify_snapshot memoizes it) streams items without repeating the
        # per-row schema and canonicality proofs; structural checks -- row
        # ordering, partition placement, counts -- still run on every row.
        already_verified = (reference.catalog_id, reference.digest) in self._verified
        return SourceCatalogSnapshot(
            verifier.summary,
            rows._iter_located_catalog_rows(
                blob_source,
                verifier.partitions,
                verifier.summary.item_count,
                validate=not already_verified,
            ),
        )

    def verify_snapshot(self, reference: SourceCatalogRef) -> SourceCatalogSnapshotSummary:
        """Fully verify one snapshot, remembering the verdict for this reader.

        The consumer runs the same gate the producer ran: artifact admission
        hashes every member against the manifest, and the build-gate verifier
        re-derives every digest and diagnostic from the rows and refuses any
        mismatch with the sealed spec and receipt. (The item iteration this
        method used to run instead re-validated row shapes but never compared
        a single digest -- this is stronger and cheaper.) The verdict is
        memoized per (catalogId, digest) for the life of this reader: the
        store is content-addressed and immutable, so a repeated verify of the
        same digest would re-prove the same bytes.
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
