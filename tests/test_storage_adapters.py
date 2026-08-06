from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
    LocalDocumentStoreRepository,
    LocalJsonControlRepository,
)
from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.identity import sha256_digest
from docspec.domain.jobs import ChangeKind, DocumentEntry, DocumentStore
from docspec.domain.plans import StagePolicy, WorkLimits
from docspec.domain.references import ArtifactRef, BlobRef
from docspec.errors import IntegrityError, LimitExceededError, StateTransitionError


def _limits() -> WorkLimits:
    return WorkLimits(10, 10_000, 100, 100, 100, 10_000, 60, 3)


def _planned_store(
    item_id: str = "source-1",
    *,
    plan_id: str = "plan-1",
    logical_partition: str = "000",
) -> DocumentStore:
    item = SourceItem(item_id, "v1", (CandidateFile("primary", f"{item_id}.txt", "text/plain"),))
    entry = DocumentEntry.create(item, ChangeKind.ADDED, StagePolicy(("text-v1",), "paragraph-v1"))
    return DocumentStore.planned(
        plan_id=plan_id,
        logical_partition=logical_partition,
        entries=(entry,),
        limits=_limits(),
    )


def test_blob_store_streams_deduplicates_ranges_and_materializes(tmp_path: Path) -> None:
    store = LocalContentAddressedBlobStore(
        tmp_path / "objects",
        max_blob_bytes=32,
        stream_chunk_bytes=3,
    )
    first = store.put_if_absent([b"exact ", b"bytes"], media_type="text/plain")
    second = store.put_if_absent(
        [b"exact bytes"],
        media_type="text/plain",
        expected_digest=first.digest,
        expected_size=first.byte_size,
    )

    assert first == second
    assert first.locator == f"objects/sha256/{first.digest[7:9]}/{first.digest[7:]}"
    assert list(store.read(first)) == [b"exa", b"ct ", b"byt", b"es"]
    assert store.read_range(first, start=6, end=11) == b"bytes"
    materialized = store.materialize(first, tmp_path / "work", "nested/file.txt")
    assert materialized.read_bytes() == b"exact bytes"


def test_blob_store_fails_closed_for_limits_tampering_and_symlinks(tmp_path: Path) -> None:
    store = LocalContentAddressedBlobStore(tmp_path / "objects", max_blob_bytes=8)
    with pytest.raises(LimitExceededError):
        store.put_if_absent([b"123456789"], media_type="application/octet-stream")

    reference = store.put_if_absent([b"safe"], media_type="text/plain")
    path = store.root / reference.locator
    path.write_bytes(b"evil")
    with pytest.raises(IntegrityError):
        store.verify(reference)

    target = tmp_path / "outside"
    target.write_bytes(b"safe")
    link = store.root / "objects" / "sha256" / "aa" / ("a" * 64)
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    linked = BlobRef(link.relative_to(store.root).as_posix(), f"sha256:{'a' * 64}", 4, "text/plain")
    with pytest.raises(IntegrityError):
        store.verify(linked)


def test_control_repository_uses_canonical_immutable_json(tmp_path: Path) -> None:
    repository = LocalJsonControlRepository(tmp_path / "control")
    reference = repository.put(kind="plans", artifact_id="plan-1", value={"z": 1, "a": "two"})

    assert repository.load(reference) == {"a": "two", "z": 1}
    assert (repository.root / reference.locator).read_bytes() == (
        b'{"artifactId":"plan-1","format":"docspec-control-artifact","formatVersion":"1.0",'
        b'"kind":"plans","value":{"a":"two","z":1}}\n'
    )

    duplicate_payload = b'{"a":1,"a":2}\n'
    duplicate_path = repository.root / "control" / "plans" / "duplicate.json"
    duplicate_path.write_bytes(duplicate_payload)
    duplicate_reference = ArtifactRef(
        "duplicate",
        duplicate_path.relative_to(repository.root).as_posix(),
        sha256_digest(duplicate_payload),
        "application/json",
        len(duplicate_payload),
    )
    with pytest.raises(IntegrityError, match="duplicate key"):
        repository.load(duplicate_reference)


def test_document_store_repository_saves_immutable_revisions(tmp_path: Path) -> None:
    repository = LocalDocumentStoreRepository(tmp_path / "jobs")
    planned = _planned_store()
    planned_ref = repository.save(planned)
    running = planned.start("attempt-1")
    running_ref = repository.save(running)

    assert repository.load(planned_ref) == planned
    assert repository.load(running_ref) == running
    assert repository.revisions(planned.store_id) == (planned_ref, running_ref)
    assert repository.latest(planned.store_id) == running_ref

    conflicting = replace(running, attempts=("another-attempt",))
    with pytest.raises(StateTransitionError):
        repository.save(conflicting)


def test_document_store_latest_reads_only_the_newest_revision_while_revisions_validate_history(
    tmp_path: Path,
) -> None:
    repository = LocalDocumentStoreRepository(tmp_path / "jobs")
    planned = _planned_store()
    planned_ref = repository.save(planned)
    running_ref = repository.save(planned.start("attempt-1"))

    (repository.root / planned_ref.locator).write_bytes(b"tampered historical revision\n")

    assert repository.latest(planned.store_id) == running_ref
    with pytest.raises(IntegrityError):
        repository.revisions(planned.store_id)


def test_revision_writes_stage_crash_debris_outside_the_declared_revision_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = LocalDocumentStoreRepository(tmp_path / "jobs")
    staging_directories: list[Path] = []
    real_mkstemp = __import__("tempfile").mkstemp

    def recording_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        staging_directories.append(Path(str(kwargs["dir"])))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr("docspec.adapters.storage.tempfile.mkstemp", recording_mkstemp)
    planned = _planned_store()
    planned_ref = repository.save(planned)
    running_ref = repository.save(planned.start("attempt-1"))

    expected_staging = repository.root / ".staging/writes"
    assert staging_directories == [expected_staging, expected_staging]
    (expected_staging / ".interrupted-write.tmp").write_bytes(b"partial")
    revision_directory = (repository.root / planned_ref.locator).parent
    assert {path.name for path in revision_directory.iterdir()} == {
        Path(planned_ref.locator).name,
        Path(running_ref.locator).name,
    }
    assert repository.latest(planned.store_id) == running_ref

    (revision_directory / ".unexpected-member").write_bytes(b"not declared")
    with pytest.raises(IntegrityError, match="undeclared member"):
        repository.latest(planned.store_id)


def test_document_store_repository_seals_exact_ordered_planned_population(tmp_path: Path) -> None:
    repository = LocalDocumentStoreRepository(tmp_path / "jobs")
    first = repository.save(_planned_store("source-1", logical_partition="000"))
    second = repository.save(_planned_store("source-2", logical_partition="001"))

    ledger = repository.seal_planned_stores("plan-1", (second, first))

    assert ledger.layer_kind == "planned-document-stores"
    assert ledger.schema_id == "docspec-planned-store-reference/1.0"
    assert ledger.record_count == 2
    assert repository.planned_store_ledger("plan-1") == ledger
    assert tuple(repository.stream_planned_stores(ledger)) == (second, first)

    root = json.loads((repository.root / ledger.state_ref).read_text(encoding="utf-8"))
    assert root["orderPolicy"] == "planner-emission-order"
    assert root["recordCount"] == 2
    assert root["member"]["recordCount"] == 2

    with pytest.raises(IntegrityError, match="repeats"):
        repository.seal_planned_stores("plan-1", (first, first))
    with pytest.raises(IntegrityError, match="initial planned revisions"):
        repository.seal_planned_stores("plan-1", (repository.save(_planned_store().start("attempt")),))
    with pytest.raises(StateTransitionError, match="different immutable store population"):
        repository.seal_planned_stores("plan-1", (first, second))

    member = repository.root / root["member"]["path"]
    member.write_bytes(member.read_bytes().replace(b'"ordinal":0', b'"ordinal":9', 1))
    with pytest.raises(IntegrityError, match="member bytes"):
        repository.verify_planned_store_ledger(ledger)


def test_planned_store_ledger_presence_distinguishes_absence_from_invalid_state(tmp_path: Path) -> None:
    repository = LocalDocumentStoreRepository(tmp_path / "jobs")
    assert not repository.has_planned_store_ledger("plan-1")

    planned = repository.save(_planned_store())
    ledger = repository.seal_planned_stores("plan-1", (planned,))
    assert repository.has_planned_store_ledger("plan-1")

    (repository.root / ledger.state_ref).write_bytes(b"tampered ledger\n")
    assert repository.has_planned_store_ledger("plan-1")
    with pytest.raises(IntegrityError):
        repository.planned_store_ledger("plan-1")


def test_planned_store_ledger_enforces_declared_count_and_byte_bounds(tmp_path: Path) -> None:
    count_bounded = LocalDocumentStoreRepository(
        tmp_path / "count-bounded",
        max_plan_store_count=1,
    )
    first = count_bounded.save(_planned_store("source-1", logical_partition="000"))
    second = count_bounded.save(_planned_store("source-2", logical_partition="001"))
    with pytest.raises(LimitExceededError, match="store limit"):
        count_bounded.seal_planned_stores("plan-1", (first, second))

    byte_bounded = LocalDocumentStoreRepository(
        tmp_path / "byte-bounded",
        max_plan_ledger_bytes=32,
    )
    reference = byte_bounded.save(_planned_store())
    with pytest.raises(LimitExceededError, match="byte limit"):
        byte_bounded.seal_planned_stores("plan-1", (reference,))


def test_document_store_repository_moves_large_entry_ledgers_to_bounded_members(tmp_path: Path) -> None:
    repository = LocalDocumentStoreRepository(
        tmp_path / "jobs",
        max_revision_bytes=128 * 1024,
        max_inline_bytes=2 * 1024,
    )
    stages = StagePolicy(("text-v1",), "paragraph-v1")
    entries = tuple(
        DocumentEntry.create(
            SourceItem(
                f"source-{index}",
                "v1",
                (CandidateFile("primary", f"source-{index}.txt", "text/plain"),),
            ),
            ChangeKind.ADDED,
            stages,
        )
        for index in range(5)
    )
    planned = DocumentStore.planned(
        plan_id="plan-1",
        logical_partition="000",
        entries=entries,
        limits=_limits(),
    )
    planned_ref = repository.save(planned)
    running_ref = repository.save(planned.start("attempt-1"))

    planned_root = json.loads((repository.root / planned_ref.locator).read_text())
    running_root = json.loads((repository.root / running_ref.locator).read_text())
    assert planned_root["format"] == "docspec-saved-document-store"
    assert planned_root["entriesMember"] == running_root["entriesMember"]
    assert planned_root["entriesMember"]["recordCount"] == 5
    assert repository.load(planned_ref) == planned

    member = repository.root / planned_root["entriesMember"]["path"]
    member.write_bytes(member.read_bytes().replace(b'"itemId":"source-1"', b'"itemId":"source-x"'))
    with pytest.raises(IntegrityError, match="member bytes"):
        repository.load(planned_ref)
