"""Thin DocSpec mappings for the shared Rulespec artifact protocol."""

from __future__ import annotations

import hashlib
import io
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, ClassVar

from rulespec_artifacts import (
    FORMAT,
    FORMAT_VERSION,
    ROOT_OBJECT_KEY,
    ArtifactInput,
    ArtifactPin,
    ArtifactVerificationError,
    LocalMemberSource,
    MemberDescriptor,
    MemberNotFoundError,
    MemberSource,
    MemberSourceError,
    Producer,
    SemanticVerifier,
    VerifiedArtifact,
    admit_artifact,
    build_artifact_root,
    canonical_json_bytes as artifact_json_bytes,
    describe_member,
    expected_logical_id,
    iter_member_descriptors,
    write_member_manifest,
)

from docspec.application.commit import DocumentReleaseVerifier
from docspec.domain.identity import (
    canonical_json_bytes,
    identity_digest,
    parse_canonical_json,
    require_sha256,
    require_text,
    stable_urn,
    thaw_json,
)
from docspec.domain.plans import ProcessingPlan
from docspec.domain.references import BlobRef, LayerRef
from docspec.domain.release import DocumentRelease
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.blob_store import BlobStore
from docspec.ports.control_repository import ControlRepository
from docspec.ports.record_storage import RecordStorage

RELEASE_STATE_KEY = "release.json"
ARTIFACT_ROOT_KEY = ROOT_OBJECT_KEY
RELEASE_STATE_ROLE = "release-state"
RECORDS_ROLE = "records"
RELEASE_STATE_MEDIA_TYPE = "application/vnd.docspec.document-release+json"
RECORDS_MEDIA_TYPE = "application/x-ndjson"
_RELEASE_BYTE_LIMIT = 1024 * 1024
_RECORD_BYTE_LIMIT = 8 * 1024 * 1024


def admit_local_artifact(
    root: Path,
    *,
    logical_id: str,
    artifact_digest: str,
    root_byte_limit: int,
) -> tuple[VerifiedArtifact, LocalMemberSource]:
    """Use the one local-files adapter and normalize structural refusal."""

    try:
        source = LocalMemberSource(root)
        artifact = admit_artifact(
            source,
            expected_pin=ArtifactPin(logical_id, artifact_digest),
            root_byte_limit=root_byte_limit,
        )
    except ArtifactVerificationError as error:
        raise IntegrityError(f"platform artifact is invalid: {error}") from error
    return artifact, source


class _ChunkReader(io.RawIOBase):
    """Expose an existing blob iterator as one binary stream."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        super().__init__()
        self._chunks = iter(chunks)
        self._pending = memoryview(b"")

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:
        target = memoryview(buffer).cast("B")
        written = 0
        while written < len(target):
            if not self._pending:
                try:
                    self._pending = memoryview(next(self._chunks))
                except StopIteration:
                    break
                except Exception as error:
                    raise MemberSourceError("blob read failed") from error
            take = min(len(target) - written, len(self._pending))
            target[written : written + take] = self._pending[:take]
            self._pending = self._pending[take:]
            written += take
        return written

    def close(self) -> None:
        chunks = self._chunks
        self._chunks = iter(())
        self._pending = memoryview(b"")
        try:
            close = getattr(chunks, "close", None)
            if close is not None:
                close()
        finally:
            super().close()


class BlobMemberSource:
    """Bridge an injected immutable blob map to Rulespec's storage seam."""

    def __init__(self, store: BlobStore, members: Mapping[str, BlobRef]) -> None:
        self._store = store
        self._members = dict(members)

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._members))

    @contextmanager
    def open(self, object_key: str) -> Iterator[BinaryIO]:
        reference = self._members.get(object_key)
        if reference is None:
            raise MemberNotFoundError(object_key)
        try:
            chunks = self._store.read(reference)
        except Exception as error:
            raise MemberSourceError("blob open failed") from error
        stream = io.BufferedReader(_ChunkReader(chunks))
        try:
            yield stream
        finally:
            stream.close()


@dataclass(frozen=True, slots=True)
class DerivationMember:
    """DocSpec meaning for one already-written output member."""

    object_key: str
    role: str
    media_type: str
    record_count: int | None = None


@dataclass(frozen=True, slots=True)
class DerivationSpec:
    """Current DocSpec release identity fields carried by the shared container."""

    kind: ClassVar[str] = "derivation"

    processor_id: str
    processor_version: str
    processor_digest: str
    policy_id: str
    policy_version: str
    policy_digest: str
    parameters_digest: str
    partitioning_id: str
    partitioning_digest: str
    expected_output_roles: tuple[str, ...]

    def __post_init__(self) -> None:
        for name, value in (
            ("processor_id", self.processor_id),
            ("processor_version", self.processor_version),
            ("policy_id", self.policy_id),
            ("policy_version", self.policy_version),
            ("partitioning_id", self.partitioning_id),
        ):
            require_text(value, name)
        for name, value in (
            ("processor_digest", self.processor_digest),
            ("policy_digest", self.policy_digest),
            ("parameters_digest", self.parameters_digest),
            ("partitioning_digest", self.partitioning_digest),
        ):
            require_sha256(value, name)
        if not self.expected_output_roles or len(self.expected_output_roles) != len(
            set(self.expected_output_roles)
        ):
            raise ValueError("derivation expected output roles must be nonempty and distinct")
        for role in self.expected_output_roles:
            require_text(role, "derivation expected output role")

    def as_dict(self) -> dict[str, object]:
        return {
            "expectedOutputRoles": sorted(self.expected_output_roles),
            "parametersDigest": self.parameters_digest,
            "partitioningDigest": self.partitioning_digest,
            "partitioningId": self.partitioning_id,
            "policyDigest": self.policy_digest,
            "policyId": self.policy_id,
            "policyVersion": self.policy_version,
            "processorDigest": self.processor_digest,
            "processorId": self.processor_id,
            "processorVersion": self.processor_version,
        }


def record_member_key(layer: LayerRef) -> str:
    return f"records/{hashlib.sha256(layer.layer_id.encode('utf-8')).hexdigest()}.jsonl"


def derivation_spec(plan: ProcessingPlan, partition_policy: Mapping[str, object]) -> DerivationSpec:
    """Map one processing plan to the logical fields owned by the shared spec."""

    policy = {
        "acceptedFailurePolicyDigest": plan.accepted_failure_policy_digest,
        "dataUsePolicy": plan.data_use_policy.to_dict(),
        "retentionPolicy": plan.retention_policy.to_dict(),
        "retryPolicyDigest": plan.retry_policy_digest,
    }
    parameters = {
        "limits": plan.limits.to_dict(),
        "profiles": plan.profiles.to_dict(),
        "selection": plan.selection,
        "stages": plan.stages.to_dict(),
    }
    partitioning = dict(partition_policy)
    return DerivationSpec(
        processor_id=plan.processors.processor_set_id,
        processor_version="1",
        processor_digest=identity_digest(plan.processors.to_dict()),
        policy_id=stable_urn("derivation-policy", policy),
        policy_version="1",
        policy_digest=identity_digest(policy),
        parameters_digest=identity_digest(parameters),
        partitioning_id=stable_urn("partitioning", partitioning),
        partitioning_digest=identity_digest(partitioning),
        expected_output_roles=(RECORDS_ROLE, RELEASE_STATE_ROLE),
    )


def derivation_inputs(plan: ProcessingPlan) -> tuple[ArtifactInput, ...]:
    """Keep physical source and base pins at the artifact boundary."""

    inputs = [ArtifactInput("source", plan.source_catalog.catalog_id, plan.source_catalog.digest)]
    if plan.base_release is not None:
        inputs.append(ArtifactInput("base", plan.base_release.release_id, plan.base_release.digest))
    return tuple(sorted(inputs, key=lambda item: (item.role, item.logical_id, item.artifact_digest)))


def derivation_logical_id(plan: ProcessingPlan, partition_policy: Mapping[str, object]) -> str:
    spec = derivation_spec(plan, partition_policy)
    return expected_logical_id(
        {
            "format": FORMAT,
            "formatVersion": FORMAT_VERSION,
            "inputs": [item.as_dict() for item in derivation_inputs(plan)],
            "kind": spec.kind,
            "spec": spec.as_dict(),
        }
    )


def write_release_members(
    root: Path,
    release: DocumentRelease,
    records: RecordStorage,
) -> tuple[DerivationMember, ...]:
    """Materialize final logical layers once, in bounded record streams."""

    root = Path(root)
    (root / "records").mkdir()
    with (root / RELEASE_STATE_KEY).open("xb") as handle:
        handle.write(release.file_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    members = [DerivationMember(RELEASE_STATE_KEY, RELEASE_STATE_ROLE, RELEASE_STATE_MEDIA_TYPE)]
    for layer in release.active_layers:
        object_key = record_member_key(layer)
        count = 0
        with (root / object_key).open("xb") as handle:
            for row in records.stream(layer):
                handle.write(canonical_json_bytes(row) + b"\n")
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        if count != layer.record_count:
            raise IntegrityError(f"release layer {layer.layer_kind!r} changed while it was materialized")
        members.append(DerivationMember(object_key, RECORDS_ROLE, RECORDS_MEDIA_TYPE, count))
    return tuple(sorted(members, key=lambda item: item.object_key))


@contextmanager
def _exclusive_writer(directory_fd: int, name: str) -> Iterator[BinaryIO]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, 0o644, dir_fd=directory_fd)
    except FileExistsError as error:
        raise IntegrityError(f"derivation protocol path already exists: {name}") from error
    stream = os.fdopen(descriptor, "wb", closefd=True)
    try:
        yield stream
        stream.flush()
        os.fsync(stream.fileno())
    finally:
        stream.close()


class LocalDerivationBuilder:
    """Add the shared root and manifest to producer-written local outputs."""

    def __init__(self, producer: Producer, semantic_verifier: SemanticVerifier) -> None:
        self._producer = producer
        self._semantic_verifier = semantic_verifier

    def seal(
        self,
        working: Path,
        *,
        spec: DerivationSpec,
        inputs: Sequence[ArtifactInput],
        members: Sequence[DerivationMember],
    ) -> VerifiedArtifact:
        working = Path(working)
        if working.is_symlink() or not working.is_dir():
            raise IntegrityError("derivation working root must be a real directory")
        output_roles = {member.role for member in members}
        if output_roles != set(spec.expected_output_roles):
            raise IntegrityError("derivation output roles differ from the declared output roles")
        manifest_key = "manifest.json"
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(working, flags)
        manifest_created = False
        root_created = False
        try:
            if (working / ROOT_OBJECT_KEY).exists() or (working / manifest_key).exists():
                raise IntegrityError("derivation working root already contains protocol files")
            source = LocalMemberSource(working)
            descriptors = (
                describe_member(
                    source,
                    object_key=member.object_key,
                    role=member.role,
                    media_type=member.media_type,
                    record_count=member.record_count,
                )
                for member in sorted(members, key=lambda item: item.object_key)
            )
            with _exclusive_writer(directory_fd, manifest_key) as stream:
                manifest_created = True
                manifest = write_member_manifest(
                    stream,
                    scope_kind="global",
                    scope_id="all",
                    object_key=manifest_key,
                    members=descriptors,
                )
            root = build_artifact_root(
                kind=spec.kind,
                spec=spec.as_dict(),
                producer=self._producer,
                inputs=inputs,
                manifests=(manifest,),
            )
            with _exclusive_writer(directory_fd, ROOT_OBJECT_KEY) as stream:
                root_created = True
                stream.write(artifact_json_bytes(root))
            return admit_artifact(
                LocalMemberSource(working),
                semantic_verifier=self._semantic_verifier,
            )
        except BaseException:
            for name, created in (
                (ROOT_OBJECT_KEY, root_created),
                (manifest_key, manifest_created),
            ):
                if not created:
                    continue
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            raise
        finally:
            os.close(directory_fd)


class DocumentReleaseArtifactVerifier:
    """Check only DocSpec meaning after Rulespec verifies common structure."""

    def __init__(
        self,
        *,
        verifier: DocumentReleaseVerifier,
        controls: ControlRepository,
        records: RecordStorage,
        producer: Producer,
    ) -> None:
        self._verifier = verifier
        self._controls = controls
        self._records = records
        self._producer = producer

    @staticmethod
    def _descriptors(artifact: VerifiedArtifact, source: MemberSource) -> dict[str, MemberDescriptor]:
        return {member.object_key: member for member in iter_member_descriptors(artifact, source)}

    @staticmethod
    def _release(source: MemberSource, descriptor: MemberDescriptor) -> DocumentRelease:
        if descriptor.role != RELEASE_STATE_ROLE or descriptor.media_type != RELEASE_STATE_MEDIA_TYPE:
            raise IntegrityError("the derivation release member has the wrong role or media type")
        if descriptor.byte_size > _RELEASE_BYTE_LIMIT:
            raise LimitExceededError(f"document release exceeds the {_RELEASE_BYTE_LIMIT}-byte limit")
        with source.open(descriptor.object_key) as stream:
            payload = stream.read(descriptor.byte_size + 1)
        if len(payload) != descriptor.byte_size:
            raise IntegrityError("the document release member size differs")
        try:
            value = thaw_json(parse_canonical_json(payload, label="document release"))
            if not isinstance(value, dict):
                raise ValueError("root must be an object")
            release = DocumentRelease.from_dict(value)
        except (TypeError, ValueError, IntegrityError) as error:
            raise IntegrityError(f"the document release member is invalid: {error}") from error
        return release

    def read(self, artifact: VerifiedArtifact, source: MemberSource) -> DocumentRelease:
        if artifact.root["kind"] != DerivationSpec.kind:
            raise IntegrityError("document release reference names a different artifact kind")
        if artifact.root["producer"] != self._producer.as_dict():
            raise IntegrityError("document release producer differs from the installed implementation")
        descriptors = self._descriptors(artifact, source)
        release_member = descriptors.get(RELEASE_STATE_KEY)
        if release_member is None:
            raise IntegrityError("a DocSpec derivation must contain its release state")
        release = self._release(source, release_member)
        if release.release_id != artifact.pin.logical_id:
            raise IntegrityError("document release identity differs from the shared derivation")

        self._controls.verify(release.processing_plan)
        try:
            plan = ProcessingPlan.from_dict(self._controls.load(release.processing_plan))
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"document release processing plan is invalid: {error}") from error
        expected_spec = derivation_spec(plan, release.partition_policy)
        expected_inputs = derivation_inputs(plan)
        if artifact.root["spec"] != expected_spec.as_dict() or artifact.inputs != expected_inputs:
            raise IntegrityError("derivation identity fields differ from the document processing plan")

        expected_keys = {RELEASE_STATE_KEY, *(record_member_key(layer) for layer in release.active_layers)}
        if set(descriptors) != expected_keys:
            raise IntegrityError("derivation members differ from the complete document release")
        for layer in release.active_layers:
            member = descriptors[record_member_key(layer)]
            if (
                member.role != RECORDS_ROLE
                or member.media_type != RECORDS_MEDIA_TYPE
                or member.record_count != layer.record_count
            ):
                raise IntegrityError(f"release layer {layer.layer_kind!r} has the wrong member description")
            self._compare_layer(layer, member, source)
        self._verifier.verify(release)
        return release

    def _compare_layer(self, layer: LayerRef, member: MemberDescriptor, source: MemberSource) -> None:
        with source.open(member.object_key) as stream:
            for row in self._records.stream(layer):
                actual = stream.readline(_RECORD_BYTE_LIMIT + 2)
                if len(actual) > _RECORD_BYTE_LIMIT + 1:
                    raise LimitExceededError(f"release layer {layer.layer_kind!r} contains an oversized record")
                if actual != canonical_json_bytes(row) + b"\n":
                    raise IntegrityError(f"release layer {layer.layer_kind!r} differs from its published member")
            if stream.read(1):
                raise IntegrityError(f"release layer {layer.layer_kind!r} contains an extra record")

    def __call__(self, artifact: VerifiedArtifact, source: MemberSource) -> None:
        self.read(artifact, source)


__all__ = [
    "ARTIFACT_ROOT_KEY",
    "BlobMemberSource",
    "DerivationMember",
    "DocumentReleaseArtifactVerifier",
    "DerivationSpec",
    "LocalDerivationBuilder",
    "admit_local_artifact",
    "derivation_inputs",
    "derivation_logical_id",
    "derivation_spec",
    "record_member_key",
    "write_release_members",
]
