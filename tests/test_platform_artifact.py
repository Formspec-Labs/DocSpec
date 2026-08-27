from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from rulespec_artifacts import (
    ROOT_OBJECT_KEY,
    ArtifactInput,
    ArtifactPin,
    MemberSource,
    Supersedes,
    VerifiedArtifact,
    admit_artifact,
)

from docspec.adapters.platform_artifact import (
    BlobMemberSource,
    DerivationMember,
    DerivationSpec,
    LocalDerivationBuilder,
)
from docspec.adapters.storage import LocalContentAddressedBlobStore
from docspec.domain.references import BlobRef
from docspec.errors import IntegrityError
from tests.helpers import document_release_producer


def spec() -> DerivationSpec:
    return DerivationSpec(
        processor_id="urn:docspec:processor:fixture",
        processor_version="1",
        processor_digest="sha256:" + "1" * 64,
        policy_id="urn:docspec:policy:fixture",
        policy_version="1",
        policy_digest="sha256:" + "2" * 64,
        parameters_digest="sha256:" + "3" * 64,
        partitioning_id="urn:docspec:partitioning:source-item",
        partitioning_digest="sha256:" + "6" * 64,
        expected_output_roles=("documents", "task-ledger"),
    )


def input_pin() -> ArtifactInput:
    return ArtifactInput("source", "urn:spicy:artifact:source-catalog:" + "a" * 64, "sha256:" + "4" * 64)


def working(root: Path, name: str) -> tuple[Path, tuple[DerivationMember, ...]]:
    selected = root / name
    (selected / "records").mkdir(parents=True)
    (selected / "evidence").mkdir()
    (selected / "records/documents.jsonl").write_bytes(b'{"id":"one"}\n')
    (selected / "evidence/tasks.jsonl").write_bytes(b'{"task":"one"}\n')
    return selected, (
        DerivationMember(
            "records/documents.jsonl",
            "documents",
            "application/x-ndjson",
            record_count=1,
        ),
        DerivationMember(
            "evidence/tasks.jsonl",
            "task-ledger",
            "application/x-ndjson",
            record_count=1,
        ),
    )


def accept_semantics(artifact: VerifiedArtifact, source: MemberSource) -> None:
    assert artifact.member_count == 2
    assert ROOT_OBJECT_KEY in set(source.keys())


def builder() -> LocalDerivationBuilder:
    return LocalDerivationBuilder(document_release_producer(), accept_semantics)


def test_byte_identical_results_have_one_identity(tmp_path: Path) -> None:
    pins: list[ArtifactPin] = []
    for name in ("first", "second", "third"):
        root, members = working(tmp_path, f"working-{name}")
        artifact = builder().seal(
            root,
            spec=spec(),
            inputs=(input_pin(),),
            members=members,
        )
        pins.append(artifact.pin)
    assert len({pin.logical_id for pin in pins}) == 1
    assert len({pin.artifact_digest for pin in pins}) == 1


def test_supersedes_changes_physical_identity_without_changing_logical_identity(
    tmp_path: Path,
) -> None:
    plain_root, plain_members = working(tmp_path, "plain")
    plain = builder().seal(
        plain_root,
        spec=spec(),
        inputs=(input_pin(),),
        members=plain_members,
    )
    successor_root, successor_members = working(tmp_path, "successor")
    successor = builder().seal(
        successor_root,
        spec=spec(),
        inputs=(input_pin(),),
        members=successor_members,
        supersedes=Supersedes(
            "urn:spicy:artifact:derivation:" + "9" * 64,
            "sha256:" + "8" * 64,
            "advance the current derivation",
        ),
    )

    assert successor.pin.logical_id == plain.pin.logical_id
    assert successor.pin.artifact_digest != plain.pin.artifact_digest
    assert successor.root["supersedes"] == {
        "artifactDigest": "sha256:" + "8" * 64,
        "logicalId": "urn:spicy:artifact:derivation:" + "9" * 64,
        "reason": "advance the current derivation",
    }


def test_blob_member_source_verifies_and_closes_the_same_distribution(tmp_path: Path) -> None:
    root, members = working(tmp_path, "working")
    artifact = builder().seal(
        root,
        spec=spec(),
        inputs=(input_pin(),),
        members=members,
    )
    blobs = LocalContentAddressedBlobStore(tmp_path / "blobs")
    references = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            key = path.relative_to(root).as_posix()
            payload = path.read_bytes()
            references[key] = blobs.put_if_absent(
                (payload,),
                media_type="application/octet-stream",
                expected_size=len(payload),
            )
    verified = admit_artifact(
        BlobMemberSource(blobs, references),
        expected_pin=artifact.pin,
    )
    assert verified.pin == artifact.pin

    class ClosingStore:
        closed = False

        def read(self, reference: BlobRef):  # type: ignore[no-untyped-def]
            try:
                yield b"abcdef"
            finally:
                self.closed = True

    store = ClosingStore()
    reference = BlobRef("fixture", "sha256:" + "0" * 64, 6, "application/octet-stream")
    with BlobMemberSource(store, {"one": reference}).open("one") as stream:  # type: ignore[arg-type]
        assert stream.read(1) == b"a"
    assert store.closed


def test_structural_or_semantic_failure_cannot_publish(tmp_path: Path) -> None:
    root, members = working(tmp_path, "structural")
    with pytest.raises(IntegrityError, match="output roles"):
        builder().seal(
            root,
            spec=replace(spec(), expected_output_roles=("documents", "missing")),
            inputs=(input_pin(),),
            members=members,
        )
    assert (root / "records/documents.jsonl").exists()
    assert not (root / ROOT_OBJECT_KEY).exists()
    assert not (root / "manifests").exists()

    def refuse_semantics(artifact: VerifiedArtifact, source: MemberSource) -> None:
        raise IntegrityError("semantic refusal")

    root, members = working(tmp_path, "semantic")
    with pytest.raises(IntegrityError, match="semantic refusal"):
        LocalDerivationBuilder(document_release_producer(), refuse_semantics).seal(
            root,
            spec=spec(),
            inputs=(input_pin(),),
            members=members,
        )
    assert not (root / ROOT_OBJECT_KEY).exists()
    assert not (root / "manifests").exists()


def test_protocol_symlink_is_refused_before_external_write(tmp_path: Path) -> None:
    root, members = working(tmp_path, "working")
    outside = tmp_path / "outside.json"
    (root / ROOT_OBJECT_KEY).symlink_to(outside)
    with pytest.raises(IntegrityError, match="protocol"):
        builder().seal(
            root,
            spec=spec(),
            inputs=(input_pin(),),
            members=members,
        )
    assert not outside.exists()
    assert (root / ROOT_OBJECT_KEY).is_symlink()
