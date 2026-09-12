"""Local catalog: retained immutable results and guarded current selection."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from rulespec_artifacts import Producer, Supersedes

from docspec.adapters.atomic_directory import sync_directory
from docspec.adapters.platform_artifact import (
    ARTIFACT_ROOT_KEY,
    DocumentReleaseArtifactVerifier,
    LocalDerivationBuilder,
    admit_local_artifact,
    derivation_inputs,
    derivation_logical_id,
    derivation_spec,
    write_release_members,
)
from docspec.adapters.storage.files import _contained, _read_exact, _storage_root, publish_directory_exclusive
from docspec.application.commit import DocumentReleaseVerifier
from docspec.domain.identity import (
    canonical_json_bytes,
    canonical_json_file_bytes,
    ordered_json_sequence_digest,
    parse_canonical_json,
    require_sha256,
    require_text,
    thaw_json,
)
from docspec.domain.jobs import StoreState
from docspec.domain.plans import ProcessingPlan
from docspec.domain.references import ArtifactRef, BlobRef, DocumentReleaseRef, LayerRef, StoreRef
from docspec.domain.release import DocumentRelease
from docspec.errors import IntegrityError, LimitExceededError, StaleBaseError, StateTransitionError
from docspec.ports.blob_store import BlobStore
from docspec.ports.control_repository import ControlRepository
from docspec.ports.document_store_repository import DocumentStoreRepository
from docspec.ports.record_storage import RecordStorage

_DOCUMENT_RELEASE_SUCCESSION_REASON = "derive document state from previousRelease"


class RootOnlyBlobProfileStateReachability:
    """Traverse the portable profile root, whose membership lives in records."""

    def references(
        self,
        reference: ArtifactRef,
        state: Mapping[str, Any],
    ) -> Iterator[BlobRef]:
        del reference
        if set(state) != {"profileId", "profileVersion", "storageRoot"}:
            raise IntegrityError("blob profile state has an invalid closed shape")
        for field in ("profileId", "profileVersion", "storageRoot"):
            require_text(state[field], f"blob profile state {field}")
        yield from ()


def _release_layer(release: DocumentRelease, layer_kind: str) -> LayerRef:
    matches = [layer for layer in release.active_layers if layer.layer_kind == layer_kind]
    if len(matches) != 1:
        raise IntegrityError(f"release must contain exactly one {layer_kind!r} layer")
    return matches[0]


class _LocalDocumentCatalogReader:
    """Pinned local metadata with verified, partition-directed logical reads."""

    def __init__(self, release: DocumentRelease, records: RecordStorage) -> None:
        self._release = release
        self._records = records

    @property
    def release(self) -> DocumentRelease:
        return self._release

    def lookup(self, *, layer_kind: str, record_id: str) -> dict[str, Any] | None:
        partition_value = record_id if layer_kind == "source-items" else None
        return self._records.lookup(
            _release_layer(self._release, layer_kind),
            record_id,
            partition_value=partition_value,
        )

    def scan(self, *, layer_kind: str) -> Iterator[dict[str, Any]]:
        yield from self._records.stream(_release_layer(self._release, layer_kind))

    def scan_source(
        self,
        *,
        layer_kind: str,
        source_item_id: str,
    ) -> Iterator[dict[str, Any]]:
        require_text(source_item_id, "source_item_id")
        for record in self._records.scan_partition_value(
            _release_layer(self._release, layer_kind),
            source_item_id,
        ):
            if "sourceItemId" not in record:
                raise IntegrityError(f"release layer {layer_kind!r} does not carry source-item identity")
            if record["sourceItemId"] == source_item_id:
                yield record


class LocalManifestDocumentCatalog:
    """Retain shared derivations with an optional compare-and-swap current head."""

    def __init__(
        self,
        root: Path,
        *,
        records: RecordStorage,
        stores: DocumentStoreRepository,
        controls: ControlRepository,
        producer: Producer,
        blobs: BlobStore | None = None,
        max_release_bytes: int = 1024**2,
        create: bool = True,
    ) -> None:
        if max_release_bytes <= 0:
            raise ValueError("max_release_bytes must be positive")
        self.root = _storage_root(root, create=create)
        self.records = records
        self.stores = stores
        self.controls = controls
        self.producer = producer
        self.verifier = DocumentReleaseVerifier(
            controls=controls,
            records=records,
            stores=stores,
            blobs=blobs,
        )
        self.artifact_verifier = DocumentReleaseArtifactVerifier(
            verifier=self.verifier,
            producer=producer,
        )
        self.artifact_builder = LocalDerivationBuilder(producer, self.artifact_verifier)
        self.max_release_bytes = max_release_bytes

    def release_id(self, plan: ProcessingPlan, partition_policy: Mapping[str, object]) -> str:
        return derivation_logical_id(plan, partition_policy)

    @staticmethod
    def _artifact_directory(kind: str, digest: str) -> str:
        hexadecimal = require_sha256(digest, "derivation artifact digest").removeprefix("sha256:")
        return f"document-catalog/{kind}/{hexadecimal[:2]}/{hexadecimal}"

    @classmethod
    def _release_locator(cls, digest: str) -> str:
        return f"{cls._artifact_directory('releases', digest)}/{ARTIFACT_ROOT_KEY}"

    @classmethod
    def _staged_locator(cls, digest: str) -> str:
        return f"{cls._artifact_directory('staged', digest)}/{ARTIFACT_ROOT_KEY}"

    def open(self, reference: DocumentReleaseRef) -> DocumentRelease:
        """Admit pinned metadata and small controls without scanning retained data."""
        artifact, source = self._admit_artifact(reference)
        return self.artifact_verifier.read(artifact, source)

    def audit(self, reference: DocumentReleaseRef) -> DocumentRelease:
        """Verify every retained layer, blob, receipt and output-store relationship."""
        _, release = self._audit_artifact(reference)
        return release

    def _admit_artifact(self, reference: DocumentReleaseRef, *, staged: bool = False):
        allowed = {self._release_locator(reference.digest)}
        if staged:
            allowed.add(self._staged_locator(reference.digest))
        if reference.locator not in allowed:
            raise IntegrityError("document release locator differs from its artifact pin")
        root_path = _contained(self.root, reference.locator)
        if root_path.is_file() and root_path.stat().st_size > self.max_release_bytes:
            raise LimitExceededError(f"document release exceeds the {self.max_release_bytes}-byte limit")
        return admit_local_artifact(
            root_path.parent,
            logical_id=reference.release_id,
            artifact_digest=reference.digest,
            root_byte_limit=self.max_release_bytes,
        )

    def _audit_artifact(self, reference: DocumentReleaseRef, *, staged: bool = False):
        artifact, source = self._admit_artifact(reference, staged=staged)
        return artifact, self.artifact_verifier.audit(artifact, source)

    def open_reader(self, reference: DocumentReleaseRef) -> _LocalDocumentCatalogReader:
        """Admit metadata once; the reader verifies each record member it consumes."""

        return _LocalDocumentCatalogReader(self.open(reference), self.records)

    _layer = staticmethod(_release_layer)

    def lookup(
        self,
        reference: DocumentReleaseRef,
        *,
        layer_kind: str,
        record_id: str,
    ) -> dict[str, Any] | None:
        return self.open_reader(reference).lookup(layer_kind=layer_kind, record_id=record_id)

    def scan(self, reference: DocumentReleaseRef, *, layer_kind: str) -> Iterator[dict[str, Any]]:
        yield from self.open_reader(reference).scan(layer_kind=layer_kind)

    def compare(
        self,
        older: DocumentReleaseRef,
        newer: DocumentReleaseRef,
        *,
        layer_kind: str,
    ) -> Iterator[tuple[str, str]]:
        old_layer = self._layer(self.open(older), layer_kind)
        new_layer = self._layer(self.open(newer), layer_kind)
        old_identity = self.records.identity_field(old_layer)
        new_identity = self.records.identity_field(new_layer)
        if old_identity != new_identity:
            raise IntegrityError("cannot compare layers with different logical identity fields")
        old_records = iter(self.records.stream(old_layer))
        new_records = iter(self.records.stream(new_layer))
        old_record = next(old_records, None)
        new_record = next(new_records, None)
        while old_record is not None or new_record is not None:
            if old_record is None:
                yield new_record[new_identity], "added"
                new_record = next(new_records, None)
            elif new_record is None:
                yield old_record[old_identity], "deleted"
                old_record = next(old_records, None)
            elif old_record[old_identity] < new_record[new_identity]:
                yield old_record[old_identity], "deleted"
                old_record = next(old_records, None)
            elif old_record[old_identity] > new_record[new_identity]:
                yield new_record[new_identity], "added"
                new_record = next(new_records, None)
            else:
                if canonical_json_bytes(old_record) != canonical_json_bytes(new_record):
                    yield old_record[old_identity], "changed"
                old_record = next(old_records, None)
                new_record = next(new_records, None)

    def stage(self, release: DocumentRelease) -> ArtifactRef:
        plan, _, _ = self.verifier.verify_metadata(release)
        if len(release.file_bytes) > self.max_release_bytes:
            raise LimitExceededError(f"document release exceeds the {self.max_release_bytes}-byte limit")
        if release.release_id != derivation_logical_id(plan, release.partition_policy):
            raise IntegrityError("document release identity differs from its processing plan")
        staging_root = _contained(self.root, "document-catalog/.staging/placeholder", create_parents=True).parent
        working = Path(tempfile.mkdtemp(prefix="derivation-", dir=staging_root))
        try:
            members = write_release_members(working, release)
            artifact = self.artifact_builder.seal(
                working,
                spec=derivation_spec(plan, release.partition_policy),
                inputs=derivation_inputs(plan),
                members=members,
                supersedes=(
                    None
                    if release.previous_release is None
                    else Supersedes(
                        release.previous_release.release_id,
                        release.previous_release.digest,
                        _DOCUMENT_RELEASE_SUCCESSION_REASON,
                    )
                ),
            )
            if artifact.pin.logical_id != release.release_id:
                raise IntegrityError("document release identity differs from its sealed derivation")
            locator = self._staged_locator(artifact.pin.artifact_digest)
            destination = _contained(
                self.root,
                self._artifact_directory("staged", artifact.pin.artifact_digest),
            )
            reference = DocumentReleaseRef(release.release_id, locator, artifact.pin.artifact_digest)
            if destination.exists():
                if destination.is_symlink() or not destination.is_dir():
                    raise IntegrityError("staged derivation path is not a regular directory")
                self._audit_artifact(reference, staged=True)
                shutil.rmtree(working)
            else:
                publish_directory_exclusive(
                    self.root,
                    working,
                    destination.relative_to(self.root).as_posix(),
                )
            published, source = self._admit_artifact(reference, staged=True)
            self.artifact_verifier.read(published, source)
            root_size = _contained(self.root, locator).stat().st_size
            return ArtifactRef(
                release.release_id,
                locator,
                artifact.pin.artifact_digest,
                "application/vnd.spicy-artifact+json",
                root_size,
            )
        finally:
            if working.exists():
                shutil.rmtree(working)

    def _load_staged(self, reference: ArtifactRef):
        if reference.media_type != "application/vnd.spicy-artifact+json":
            raise IntegrityError("staged release has the wrong media type")
        staged_locator = self._staged_locator(reference.digest)
        published_locator = self._release_locator(reference.digest)
        if reference.locator not in {staged_locator, published_locator}:
            raise IntegrityError("staged release locator differs from its artifact pin")
        resolved_locator = reference.locator
        if reference.locator == staged_locator and not _contained(self.root, staged_locator).is_file():
            resolved_locator = published_locator
        artifact, release = self._audit_artifact(
            DocumentReleaseRef(reference.artifact_id, resolved_locator, reference.digest),
            staged=True,
        )
        if _contained(self.root, resolved_locator).stat().st_size != reference.byte_size:
            raise IntegrityError("staged release root size differs from its reference")
        return artifact, release, resolved_locator

    def current(self) -> DocumentReleaseRef | None:
        locator = "document-catalog/current.json"
        path = _contained(self.root, locator)
        if not path.exists():
            return None
        payload = _read_exact(self.root, locator)
        value = thaw_json(parse_canonical_json(payload, label="document catalog current pointer"))
        if not isinstance(value, dict) or set(value) != {"format", "formatVersion", "release"}:
            raise IntegrityError("document catalog current pointer has an invalid closed shape")
        if value["format"] != "docspec-document-catalog-current" or value["formatVersion"] != "1.0":
            raise IntegrityError("document catalog current pointer has an unknown format")
        try:
            reference = DocumentReleaseRef.from_dict(value["release"])
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"document catalog current reference is invalid: {error}") from error
        self.open(reference)
        return reference

    def _write_current(self, reference: DocumentReleaseRef) -> None:
        locator = "document-catalog/current.json"
        destination = _contained(self.root, locator, create_parents=True)
        if destination.is_symlink():
            raise IntegrityError("document catalog current pointer must not be a symlink")
        payload = canonical_json_file_bytes(
            {
                "format": "docspec-document-catalog-current",
                "formatVersion": "1.0",
                "release": reference.to_dict(),
            }
        )
        descriptor, name = tempfile.mkstemp(prefix="current-", dir=destination.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            sync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _write_lock(self, release_id: str) -> Iterator[None]:
        lock = _contained(self.root, "document-catalog/.commit.lock", create_parents=True)
        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise StateTransitionError("another document catalog write is in progress") from error
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(release_id.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            yield
        finally:
            lock.unlink(missing_ok=True)

    def retain(
        self,
        staged: ArtifactRef,
        *,
        stores: Iterable[StoreRef],
    ) -> DocumentReleaseRef:
        """Save verified result bytes without requiring or advancing the current head."""

        reference, _ = self._retain_result(staged, stores=stores)
        return reference

    def _retain_result(
        self,
        staged: ArtifactRef,
        *,
        stores: Iterable[StoreRef],
    ) -> tuple[DocumentReleaseRef, DocumentRelease]:
        with self._write_lock(staged.artifact_id):
            artifact, release, resolved_locator = self._load_staged(staged)
            if release.previous_release is not None:
                self.audit(release.previous_release)
            previous_store_id: str | None = None

            def verified_store_values() -> Iterator[dict[str, Any]]:
                nonlocal previous_store_id
                for reference in stores:
                    if previous_store_id is not None and reference.store_id <= previous_store_id:
                        raise IntegrityError("catalog retention store references must be sorted and distinct")
                    previous_store_id = reference.store_id
                    store = self.stores.load(reference)
                    if store.state != StoreState.SEALED:
                        raise IntegrityError("catalog retention contains an unsealed document store")
                    yield reference.to_dict()

            if ordered_json_sequence_digest(verified_store_values()) != release.store_receipt_set_digest:
                raise IntegrityError("catalog retention store receipt set differs from the release")
            locator = self._release_locator(artifact.pin.artifact_digest)
            new_reference = DocumentReleaseRef(
                release.release_id,
                locator,
                artifact.pin.artifact_digest,
            )
            staged_directory = _contained(self.root, resolved_locator).parent
            published_directory = _contained(
                self.root,
                self._artifact_directory("releases", artifact.pin.artifact_digest),
            )
            if staged_directory != published_directory:
                if published_directory.exists():
                    if published_directory.is_symlink() or not published_directory.is_dir():
                        raise IntegrityError("published derivation path is not a regular directory")
                    self.audit(new_reference)
                    shutil.rmtree(staged_directory)
                else:
                    publish_directory_exclusive(
                        self.root,
                        staged_directory,
                        published_directory.relative_to(self.root).as_posix(),
                    )
            # The exclusive rename publishes the directory already audited by
            # _load_staged. Confirm its pin after publication without rescanning
            # external data; any existing destination was audited above.
            self.open(new_reference)
            return new_reference, release

    def select(
        self,
        reference: DocumentReleaseRef,
        *,
        expected_current: DocumentReleaseRef | None,
    ) -> DocumentReleaseRef:
        """Select a verified result without changing its immutable input lineage.

        The expected current head guards this choice; it need not be the result's
        base. An already-selected exact reference is an idempotent success.
        """

        with self._write_lock(reference.release_id):
            release = self.audit(reference)
            if release.previous_release is not None:
                self.audit(release.previous_release)
            current = self.current()
            if current == reference:
                return reference
            if current != expected_current:
                raise StaleBaseError("document catalog current release differs from the expected current head")
            self._write_current(reference)
            return reference

    def commit(
        self,
        staged: ArtifactRef,
        *,
        expected_base: DocumentReleaseRef | None,
        stores: Iterable[StoreRef],
    ) -> DocumentReleaseRef:
        """Retain a verified result, then select it while its base remains current.

        A stale selection leaves the retained result available by its exact pin.
        """

        reference, release = self._retain_result(staged, stores=stores)
        if release.previous_release != expected_base:
            raise IntegrityError("document release lineage differs from the expected catalog base")
        return self.select(reference, expected_current=expected_base)
