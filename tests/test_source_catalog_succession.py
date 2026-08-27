from __future__ import annotations

import fcntl
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from rulespec_artifacts import Supersedes

import docspec.adapters.source_catalog_store as source_catalog_store
from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
from docspec.adapters.source_catalog_artifact import (
    SourceCatalogArtifactReader,
    SourceCatalogBuildRequest,
    SourceCatalogBuilder,
)
from docspec.adapters.source_catalog_store import (
    LocalSourceCatalogCurrentPointer,
    LocalSourceCatalogStore,
)
from docspec.application.federal_register_catalog import FederalRegisterCatalogPolicy
from docspec.domain.identity import canonical_json_file_bytes
from docspec.domain.references import SourceCatalogRef
from docspec.errors import IntegrityError, StaleBaseError
from docspec.ports.source_catalog import SourceCatalogSuccession
from tests.test_source_catalog_snapshot import (
    FakeSource,
    description,
    producer,
    record,
    renditions,
)


CATALOG_ID = "urn:docspec:catalog:federal-register"
OTHER_CATALOG_ID = "urn:docspec:catalog:other-series"
SOURCE_SYSTEM_ID = "https://www.federalregister.gov/api/v1"


def _build(
    root: Path,
    source: FakeSource,
    *,
    catalog_id: str = CATALOG_ID,
    supersedes: Supersedes | None = None,
):
    store = LocalSourceCatalogStore(root)
    result = SourceCatalogBuilder(
        store=store,
        policy=FederalRegisterCatalogPolicy(SOURCE_SYSTEM_ID),
        request=SourceCatalogBuildRequest(catalog_id, producer(), supersedes),
        workspace_factory=SqliteCatalogPolicyWorkspace,
    ).build((source,))
    return store, result


def _initial_source() -> FakeSource:
    return FakeSource(
        description(),
        (record("2026-00001"),),
        renditions("2026-00001"),
    )


def _changed_source() -> FakeSource:
    changed = record("2026-00001")
    changed["record"]["title"] = "Changed title"
    return FakeSource(
        replace(
            description(),
            logical_id="urn:spicy:artifact:spicyregs-source-native-release:" + "d" * 64,
            artifact_digest="sha256:" + "d" * 64,
            source_state_digest="sha256:" + "e" * 64,
        ),
        (changed,),
        renditions("2026-00001"),
    )


def _pointer(root: Path, store: LocalSourceCatalogStore) -> LocalSourceCatalogCurrentPointer:
    return LocalSourceCatalogCurrentPointer(
        root,
        reader=SourceCatalogArtifactReader(store, producer=producer()),
    )


def _successor(root: Path, previous: SourceCatalogRef, *, catalog_id: str = CATALOG_ID):
    return _build(
        root,
        _changed_source(),
        catalog_id=catalog_id,
        supersedes=Supersedes(
            previous.catalog_id,
            previous.digest,
            "source-native state changed",
        ),
    )[1]


def _artifact_files(root: Path, reference: SourceCatalogRef) -> dict[str, bytes]:
    artifact_root = root / reference.digest.removeprefix("sha256:")
    return {
        path.relative_to(artifact_root).as_posix(): path.read_bytes()
        for path in artifact_root.rglob("*")
        if path.is_file()
    }


def test_source_catalog_pointer_initial_advance_and_readback_never_mutate_roots(
    tmp_path: Path,
) -> None:
    store, initial = _build(tmp_path, _initial_source())
    pointer = _pointer(tmp_path, store)
    initial_files = _artifact_files(tmp_path, initial.reference)

    assert pointer.current(CATALOG_ID) is None
    assert pointer.advance(CATALOG_ID, initial.reference, expected_current=None) == initial.reference
    assert pointer.current(CATALOG_ID) == initial.reference
    assert initial.summary.succession is None

    successor = _successor(tmp_path, initial.reference)
    successor_files = _artifact_files(tmp_path, successor.reference)
    assert successor.summary.succession == SourceCatalogSuccession(
        initial.reference.catalog_id,
        initial.reference.digest,
        "source-native state changed",
    )
    assert successor.reference.catalog_id != initial.reference.catalog_id

    assert (
        pointer.advance(
            CATALOG_ID,
            successor.reference,
            expected_current=initial.reference,
        )
        == successor.reference
    )
    assert pointer.current(CATALOG_ID) == successor.reference
    assert _artifact_files(tmp_path, initial.reference) == initial_files
    assert _artifact_files(tmp_path, successor.reference) == successor_files


def test_source_catalog_supersedes_moves_only_physical_identity(tmp_path: Path) -> None:
    _, plain = _build(tmp_path / "plain", _initial_source())
    _, successor_shaped = _build(
        tmp_path / "successor-shaped",
        _initial_source(),
        supersedes=Supersedes(
            "urn:spicy:artifact:docspec-source-catalog:" + "9" * 64,
            "sha256:" + "8" * 64,
            "same logical state with succession evidence",
        ),
    )

    assert successor_shaped.reference.catalog_id == plain.reference.catalog_id
    assert successor_shaped.reference.digest != plain.reference.digest
    assert successor_shaped.summary.catalog_state_digest == plain.summary.catalog_state_digest


def test_source_catalog_pointer_rejects_stale_missing_and_cross_series_candidates(
    tmp_path: Path,
) -> None:
    store, initial = _build(tmp_path, _initial_source())
    pointer = _pointer(tmp_path, store)
    pointer.advance(CATALOG_ID, initial.reference, expected_current=None)
    successor = _successor(tmp_path, initial.reference)

    with pytest.raises(StaleBaseError, match="differs from the expected root"):
        pointer.advance(CATALOG_ID, successor.reference, expected_current=None)

    missing_supersedes = _build(tmp_path, _changed_source())[1]
    with pytest.raises(IntegrityError, match="missing supersedes"):
        pointer.advance(
            CATALOG_ID,
            missing_supersedes.reference,
            expected_current=initial.reference,
        )

    cross_series = _successor(tmp_path, initial.reference, catalog_id=OTHER_CATALOG_ID)
    with pytest.raises(IntegrityError, match="different catalog series"):
        pointer.advance(
            CATALOG_ID,
            cross_series.reference,
            expected_current=initial.reference,
        )

    missing = SourceCatalogRef(
        "urn:spicy:artifact:docspec-source-catalog:" + "f" * 64,
        f"{'f' * 64}/artifact.json",
        "sha256:" + "f" * 64,
    )
    with pytest.raises(IntegrityError, match="artifact is invalid"):
        pointer.advance(CATALOG_ID, missing, expected_current=initial.reference)
    assert pointer.current(CATALOG_ID) == initial.reference


def test_source_catalog_pointer_tamper_fails_readback(tmp_path: Path) -> None:
    store, initial = _build(tmp_path, _initial_source())
    pointer = _pointer(tmp_path, store)
    pointer.advance(CATALOG_ID, initial.reference, expected_current=None)
    pointer_path = next((tmp_path / "current").glob("*.json"))
    payload = json.loads(pointer_path.read_text(encoding="utf-8"))
    payload["catalog"]["digest"] = "sha256:" + "0" * 64
    pointer_path.write_bytes(canonical_json_file_bytes(payload))

    with pytest.raises(IntegrityError):
        pointer.current(CATALOG_ID)


def test_source_catalog_pointer_crash_before_replace_preserves_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, initial = _build(tmp_path, _initial_source())
    pointer = _pointer(tmp_path, store)
    pointer.advance(CATALOG_ID, initial.reference, expected_current=None)
    successor = _successor(tmp_path, initial.reference)

    def fail_replace(
        _source: str,
        _destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        assert src_dir_fd is not None
        assert dst_dir_fd == src_dir_fd
        raise OSError("simulated crash before pointer replacement")

    monkeypatch.setattr(source_catalog_store.os, "replace", fail_replace)
    with pytest.raises(OSError, match="before pointer replacement"):
        pointer.advance(
            CATALOG_ID,
            successor.reference,
            expected_current=initial.reference,
        )

    assert pointer.current(CATALOG_ID) == initial.reference
    assert not tuple((tmp_path / "current").glob("current-*"))


def test_source_catalog_pointer_refuses_a_hardlinked_lock_without_mutating_its_peer(
    tmp_path: Path,
) -> None:
    store, initial = _build(tmp_path, _initial_source())
    pointer = _pointer(tmp_path, store)
    current = tmp_path / "current"
    current.mkdir()
    victim = current / "unrelated.txt"
    victim.write_bytes(b"must remain unchanged\n")
    lock = current / f".{pointer._series_key(CATALOG_ID)}.lock"
    os.link(victim, lock)

    with pytest.raises(IntegrityError, match="must have one filesystem link"):
        pointer.advance(CATALOG_ID, initial.reference, expected_current=None)

    assert victim.read_bytes() == b"must remain unchanged\n"
    assert lock.read_bytes() == b"must remain unchanged\n"
    assert pointer.current(CATALOG_ID) is None


def test_source_catalog_pointer_refuses_replaced_root_on_read(tmp_path: Path) -> None:
    store, initial = _build(tmp_path / "catalogs", _initial_source())
    pointer_root = tmp_path / "pointers"
    pointer = _pointer(pointer_root, store)
    pointer.advance(CATALOG_ID, initial.reference, expected_current=None)
    retained = tmp_path / "pointers-retained"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"outside must stay unchanged")
    pointer_root.rename(retained)
    pointer_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(IntegrityError, match="pointer root.*non-symlink directory"):
        pointer.current(CATALOG_ID)

    assert (outside / "sentinel.txt").read_bytes() == b"outside must stay unchanged"
    assert not (outside / "current").exists()
    assert retained.is_dir()


def test_source_catalog_pointer_uses_pinned_parent_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, initial = _build(tmp_path / "catalogs", _initial_source())
    pointer_root = tmp_path / "pointers"
    pointer = _pointer(pointer_root, store)
    pointer.advance(CATALOG_ID, initial.reference, expected_current=None)
    pointer_name = f"{pointer._series_key(CATALOG_ID)}.json"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"outside must stay unchanged")
    retained = pointer_root / "current-retained"
    actual_read_pointer = pointer._read_pointer
    swapped = False

    def swap_current_before_pointer_open(
        parent: source_catalog_store._PinnedDirectory,
        catalog_id: str,
    ) -> tuple[SourceCatalogRef, SourceCatalogRef | None] | None:
        nonlocal swapped
        if not swapped:
            current = pointer_root / "current"
            current.rename(retained)
            current.symlink_to(outside, target_is_directory=True)
            swapped = True
        return actual_read_pointer(parent, catalog_id)

    monkeypatch.setattr(pointer, "_read_pointer", swap_current_before_pointer_open)

    with pytest.raises(IntegrityError, match="current-pointer parent changed during use"):
        pointer.current(CATALOG_ID)

    assert swapped
    assert (outside / "sentinel.txt").read_bytes() == b"outside must stay unchanged"
    assert not (outside / pointer_name).exists()
    assert (retained / pointer_name).is_file()


def test_source_catalog_pointer_uses_pinned_parent_during_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, initial = _build(tmp_path / "catalogs", _initial_source())
    pointer_root = tmp_path / "pointers"
    pointer = _pointer(pointer_root, store)
    pointer_name = f"{pointer._series_key(CATALOG_ID)}.json"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"outside must stay unchanged")
    retained = pointer_root / "current-retained"
    actual_replace = source_catalog_store.os.replace
    swapped = False

    def swap_current_before_pointer_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if not swapped and destination == pointer_name:
            current = pointer_root / "current"
            current.rename(retained)
            current.symlink_to(outside, target_is_directory=True)
            swapped = True
        actual_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(source_catalog_store.os, "replace", swap_current_before_pointer_replace)

    with pytest.raises(IntegrityError, match="current-pointer parent changed during use"):
        pointer.advance(CATALOG_ID, initial.reference, expected_current=None)

    assert swapped
    assert (outside / "sentinel.txt").read_bytes() == b"outside must stay unchanged"
    assert not (outside / pointer_name).exists()
    assert (retained / pointer_name).is_file()


def test_source_catalog_pointer_rejects_broken_pointer_symlink(tmp_path: Path) -> None:
    store, _ = _build(tmp_path / "catalogs", _initial_source())
    pointer_root = tmp_path / "pointers"
    pointer = _pointer(pointer_root, store)
    current = pointer_root / "current"
    current.mkdir()
    pointer_name = f"{pointer._series_key(CATALOG_ID)}.json"
    (current / pointer_name).symlink_to("missing-current-pointer.json")

    with pytest.raises(IntegrityError, match="cannot be opened safely"):
        pointer.current(CATALOG_ID)

    assert (current / pointer_name).is_symlink()


def test_source_catalog_pointer_rejects_broken_current_parent_symlink(
    tmp_path: Path,
) -> None:
    store, _ = _build(tmp_path / "catalogs", _initial_source())
    pointer_root = tmp_path / "pointers"
    pointer = _pointer(pointer_root, store)
    current = pointer_root / "current"
    current.symlink_to("missing-current-directory", target_is_directory=True)

    with pytest.raises(IntegrityError, match="current-pointer parent.*non-symlink"):
        pointer.current(CATALOG_ID)

    assert current.is_symlink()


def test_source_catalog_pointer_advisory_lock_recovers_with_persistent_file(
    tmp_path: Path,
) -> None:
    store, initial = _build(tmp_path / "catalogs", _initial_source())
    pointer_root = tmp_path / "pointers"
    pointer = _pointer(pointer_root, store)
    current = pointer_root / "current"
    current.mkdir()
    lock = current / f".{pointer._series_key(CATALOG_ID)}.lock"
    lock.write_text("stale process metadata", encoding="utf-8")
    descriptor = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(
            source_catalog_store.StateTransitionError,
            match="another source-catalog pointer advance",
        ):
            pointer.advance(CATALOG_ID, initial.reference, expected_current=None)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)

    assert pointer.advance(CATALOG_ID, initial.reference, expected_current=None) == initial.reference
    assert pointer.current(CATALOG_ID) == initial.reference
    assert lock.is_file()
