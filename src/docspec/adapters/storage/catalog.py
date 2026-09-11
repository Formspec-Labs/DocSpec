"""Local catalog: verified releases and compare-and-swap publication."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterable, Iterator, Mapping
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

_DOCUMENT_RELEASE_SUCCESSION_REASON = "advance document catalog from previousRelease"


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
    """One verified local release view with partition-directed logical reads."""

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
    """Publish shared derivations with an operator-only compare-and-swap head."""

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
    ) -> None:
        if max_release_bytes <= 0:
            raise ValueError("max_release_bytes must be positive")
        self.root = _storage_root(root)
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
            controls=controls,
            records=records,
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

    def _verify_release_dependencies(self, release: DocumentRelease) -> None:
        self.verifier.verify(release)

    def open(self, reference: DocumentReleaseRef) -> DocumentRelease:
        expected_locator = self._release_locator(reference.digest)
        if reference.locator != expected_locator:
            raise IntegrityError("document release locator differs from its identity")
        _, release = self._open_artifact(reference)
        return release

    def _open_artifact(self, reference: DocumentReleaseRef, *, staged: bool = False):
        allowed = {self._release_locator(reference.digest)}
        if staged:
            allowed.add(self._staged_locator(reference.digest))
        if reference.locator not in allowed:
            raise IntegrityError("document release locator differs from its artifact pin")
        root_path = _contained(self.root, reference.locator)
        if root_path.is_file() and root_path.stat().st_size > self.max_release_bytes:
            raise LimitExceededError(f"document release exceeds the {self.max_release_bytes}-byte limit")
        artifact, source = admit_local_artifact(
            root_path.parent,
            logical_id=reference.release_id,
            artifact_digest=reference.digest,
            root_byte_limit=self.max_release_bytes,
        )
        release = self.artifact_verifier.read(artifact, source)
        return artifact, release

    def open_reader(self, reference: DocumentReleaseRef) -> _LocalDocumentCatalogReader:
        """Verify once and return a per-operation immutable release reader."""

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
        self._verify_release_dependencies(release)
        if len(release.file_bytes) > self.max_release_bytes:
            raise LimitExceededError(f"document release exceeds the {self.max_release_bytes}-byte limit")
        try:
            plan = ProcessingPlan.from_dict(self.controls.load(release.processing_plan))
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"document release processing plan is invalid: {error}") from error
        if release.release_id != derivation_logical_id(plan, release.partition_policy):
            raise IntegrityError("document release identity differs from its processing plan")
        staging_root = _contained(self.root, "document-catalog/.staging/placeholder", create_parents=True).parent
        working = Path(tempfile.mkdtemp(prefix="derivation-", dir=staging_root))
        try:
            members = write_release_members(working, release, self.records)
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
            if destination.exists():
                if destination.is_symlink() or not destination.is_dir():
                    raise IntegrityError("staged derivation path is not a regular directory")
                shutil.rmtree(working)
            else:
                publish_directory_exclusive(
                    self.root,
                    working,
                    destination.relative_to(self.root).as_posix(),
                )
            reference = DocumentReleaseRef(release.release_id, locator, artifact.pin.artifact_digest)
            self._open_artifact(reference, staged=True)
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
        artifact, release = self._open_artifact(
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

    def commit(
        self,
        staged: ArtifactRef,
        *,
        expected_base: DocumentReleaseRef | None,
        stores: Iterable[StoreRef],
    ) -> DocumentReleaseRef:
        artifact, release, resolved_locator = self._load_staged(staged)
        previous_store_id: str | None = None

        def verified_store_values() -> Iterator[dict[str, Any]]:
            nonlocal previous_store_id
            for reference in stores:
                if previous_store_id is not None and reference.store_id <= previous_store_id:
                    raise IntegrityError("catalog commit store references must be sorted and distinct")
                previous_store_id = reference.store_id
                store = self.stores.load(reference)
                if store.state != StoreState.SEALED:
                    raise IntegrityError("catalog commit contains an unsealed document store")
                yield reference.to_dict()

        if ordered_json_sequence_digest(verified_store_values()) != release.store_receipt_set_digest:
            raise IntegrityError("catalog commit store receipt set differs from the release")
        lock = _contained(self.root, "document-catalog/.commit.lock", create_parents=True)
        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise StateTransitionError("another document catalog commit is in progress") from error
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(release.release_id.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            current = self.current()
            locator = self._release_locator(artifact.pin.artifact_digest)
            new_reference = DocumentReleaseRef(
                release.release_id,
                locator,
                artifact.pin.artifact_digest,
            )
            if current == new_reference:
                return new_reference
            if current != expected_base:
                raise StaleBaseError("document catalog current release differs from the expected base")
            if release.previous_release != expected_base:
                raise IntegrityError("document release lineage differs from the expected catalog base")
            staged_directory = _contained(self.root, resolved_locator).parent
            published_directory = _contained(
                self.root,
                self._artifact_directory("releases", artifact.pin.artifact_digest),
            )
            if staged_directory != published_directory:
                if published_directory.exists():
                    if published_directory.is_symlink() or not published_directory.is_dir():
                        raise IntegrityError("published derivation path is not a regular directory")
                    self._open_artifact(new_reference)
                    shutil.rmtree(staged_directory)
                else:
                    publish_directory_exclusive(
                        self.root,
                        staged_directory,
                        published_directory.relative_to(self.root).as_posix(),
                    )
            self.open(new_reference)
            self._write_current(new_reference)
            return new_reference
        finally:
            lock.unlink(missing_ok=True)
