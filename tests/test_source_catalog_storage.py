"""Source-catalog storage contract: destination, staging, blob and shared SHA roots are pinned by descriptor
and re-checked during use, so a path swapped for a symlink refuses with IntegrityError and never mutates or
deletes outside bytes.

Also pins atomic publication (process exit after rename keeps the catalog, before rename leaves no root and a
retry succeeds), rejection of symlink or non-directory roots without mutation, and staging sessions refusing
same-name replacement and tombstones before cleanup.
"""

from __future__ import annotations

import os
from multiprocessing import get_context
from pathlib import Path

import pytest
from rulespec_artifacts import ArtifactVerificationError

from docspec.adapters.source_catalog_store import LocalSourceCatalogPublication, LocalSourceCatalogStore
from docspec.adapters.source_catalog_store import pinned_fs as catalog_pinned_fs
from docspec.adapters.source_catalog_store import staging as catalog_staging
from docspec.domain.identity import sha256_digest
from docspec.errors import IntegrityError
from tests.support.source_catalog import FakeSource, description, record, renditions
from tests.support.source_catalog_builds import (
    assert_outside_sentinel_unchanged,
    build,
    build_with_store,
)


def test_local_source_catalog_publication_pins_the_destination_parent(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "outputs"
    parent.mkdir()
    retained = tmp_path / "outputs-retained"
    replacement = tmp_path / "replacement"
    replacement.mkdir()

    with LocalSourceCatalogPublication(parent / "catalog") as publication:
        (publication.root / 'receipt.json').write_bytes(b"complete\n")
        parent.rename(retained)
        parent.symlink_to(replacement, target_is_directory=True)

        with pytest.raises(IntegrityError, match="destination parent.*non-symlink"):
            publication.publish()

    assert not tuple(replacement.iterdir())
    assert not tuple(retained.iterdir())


def test_local_source_catalog_publication_store_refuses_a_replaced_parent(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "outputs"
    parent.mkdir()
    retained = tmp_path / "outputs-retained"
    replacement = tmp_path / "replacement"
    replacement.mkdir()

    with LocalSourceCatalogPublication(parent / "catalog") as publication:
        stage_name = publication.root.name
        parent.rename(retained)
        parent.symlink_to(replacement, target_is_directory=True)
        (replacement / stage_name).mkdir()

        with pytest.raises(ValueError, match="source-catalog root changed since admission"):
            publication.store()

    assert not tuple(replacement.joinpath(stage_name).iterdir())
    assert not (retained / stage_name).exists()


def _publish_and_exit(destination: Path, published: bool) -> None:
    """Publish or abandon a catalog, then exit via os._exit to model process loss rather than an exception."""
    publication = LocalSourceCatalogPublication(destination)
    (publication.root / 'artifact.json').write_bytes(b"artifact\n" if published else b"unpublished\n")
    if published:
        (publication.root / 'note.txt').write_bytes(b"receipt\n")
        publication.publish()
    # Deliberately bypass cleanup to test actual process loss, not an exception.
    os._exit(23 if published else 31)


def _crash_publication(destination: Path, *, published: bool) -> None:
    """Run _publish_and_exit in a spawned process and assert its crash exit code."""
    process = get_context("spawn").Process(target=_publish_and_exit, args=(destination, published))
    process.start()
    try:
        process.join(timeout=30)
        assert process.exitcode == (23 if published else 31)
    finally:
        if process.is_alive():
            process.kill()
            process.join()
        process.close()


def test_local_source_catalog_publication_survives_process_exit_after_rename(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "catalog"
    _crash_publication(destination, published=True)
    assert (destination / "artifact.json").read_bytes() == b"artifact\n"
    assert (destination / "note.txt").read_bytes() == b"receipt\n"


def test_local_source_catalog_publication_process_exit_before_rename_leaves_no_root_and_retry_succeeds(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "catalog"
    _crash_publication(destination, published=False)
    assert not destination.exists()
    unpublished = tuple(tmp_path.glob(".catalog.*"))
    assert len(unpublished) == 1

    with LocalSourceCatalogPublication(destination) as publication:
        (publication.root / 'artifact.json').write_bytes(b"published\n")
        (publication.root / 'note.txt').write_bytes(b"receipt\n")
        publication.publish()

    assert (destination / "artifact.json").read_bytes() == b"published\n"
    assert unpublished[0].is_dir()


def test_local_source_catalog_store_rejects_symlink_root_without_mutation(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"outside must stay unchanged")
    root = tmp_path / "catalog-store"
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="non-symlink directory"):
        LocalSourceCatalogStore(root)

    assert_outside_sentinel_unchanged(outside)


def test_local_source_catalog_store_rejects_non_directory_root(tmp_path: Path) -> None:
    root = tmp_path / "catalog-store"
    root.write_bytes(b"not a directory")

    with pytest.raises(ValueError, match="non-symlink directory"):
        LocalSourceCatalogStore(root)

    assert root.read_bytes() == b"not a directory"


def test_local_source_catalog_readers_reject_a_replaced_store_root(tmp_path: Path) -> None:
    root = tmp_path / "catalog-store"
    source = FakeSource(description(), (record("2026-00001"),), renditions("2026-00001"))
    store, result = build(root, source)
    retained = tmp_path / "catalog-store-retained"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"outside must stay unchanged")
    root.rename(retained)
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactVerificationError, match="parent directory must be a real directory"):
        store.source_for(result.reference)
    with pytest.raises(ArtifactVerificationError, match="parent directory must be a real directory"):
        store.blob_source()

    assert_outside_sentinel_unchanged(outside)


def test_local_source_catalog_store_rejects_symlink_staging_root_without_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog-store"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"outside must stay unchanged")
    (root / ".staging").symlink_to(outside, target_is_directory=True)
    store = LocalSourceCatalogStore(root, create=False)

    with pytest.raises(IntegrityError, match="staging root.*non-symlink directory"):
        with store.stage():
            raise AssertionError("unsafe staging root must fail before yielding")

    assert_outside_sentinel_unchanged(outside)


def test_local_source_catalog_store_uses_pinned_staging_root_during_cleanup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog-store"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"outside must stay unchanged")
    store = LocalSourceCatalogStore(root)

    with pytest.raises(IntegrityError, match="staging root changed during use"):
        with store.stage():
            staging_root = root / ".staging"
            staging_root.rename(root / ".staging-retained")
            staging_root.symlink_to(outside, target_is_directory=True)

    assert_outside_sentinel_unchanged(outside)
    assert not tuple((root / ".staging-retained").iterdir())


def test_local_source_catalog_store_creates_session_under_pinned_staging_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "catalog-store"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"outside must stay unchanged")
    store = LocalSourceCatalogStore(root)
    actual_mkdir = catalog_pinned_fs.os.mkdir
    swapped = False

    def swap_staging_before_session_mkdir(
        path: str | bytes,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if not swapped and isinstance(path, str) and path.startswith("catalog-"):
            staging_root = root / ".staging"
            staging_root.rename(root / ".staging-retained")
            staging_root.symlink_to(outside, target_is_directory=True)
            swapped = True
        actual_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(catalog_pinned_fs.os, "mkdir", swap_staging_before_session_mkdir)

    with pytest.raises(IntegrityError, match="staging root changed during use"):
        with store.stage():
            pass

    assert swapped
    assert_outside_sentinel_unchanged(outside)
    assert not tuple((root / ".staging-retained").iterdir())


def test_local_source_catalog_store_refuses_same_name_session_replacement_cleanup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog-store"
    store = LocalSourceCatalogStore(root)
    replacement: Path | None = None
    admitted: Path | None = None

    with pytest.raises(IntegrityError, match="staging session changed before cleanup"):
        with store.stage():
            staging_root = root / ".staging"
            replacement = next(staging_root.glob("catalog-*"))
            admitted = staging_root / f"{replacement.name}-admitted"
            replacement.rename(admitted)
            replacement.mkdir()
            (replacement / "sentinel.txt").write_bytes(b"replacement must stay unchanged")

    assert replacement is not None and admitted is not None
    assert (replacement / "sentinel.txt").read_bytes() == b"replacement must stay unchanged"
    assert admitted.is_dir()


def test_local_source_catalog_store_refuses_same_name_tombstone_replacement_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "catalog-store"
    store = LocalSourceCatalogStore(root)
    actual_clear = catalog_pinned_fs._clear_directory_contents_at
    replacement: Path | None = None
    retained: Path | None = None
    swapped = False

    def swap_tombstone_before_descriptor_relative_clear(
        directory: catalog_pinned_fs._PinnedDirectory,
    ) -> None:
        nonlocal replacement, retained, swapped
        if not swapped:
            staging_root = root / ".staging"
            replacement = next(staging_root.glob(".cleanup-*"))
            retained = staging_root / f"{replacement.name}-admitted"
            replacement.rename(retained)
            replacement.mkdir()
            (replacement / "sentinel.txt").write_bytes(b"replacement must stay unchanged")
            swapped = True
        actual_clear(directory)

    monkeypatch.setattr(
        catalog_pinned_fs,
        "_clear_directory_contents_at",
        swap_tombstone_before_descriptor_relative_clear,
    )

    with pytest.raises(IntegrityError, match="cleanup tombstone changed during use"):
        with store.stage():
            pass

    assert swapped and replacement is not None and retained is not None
    assert (replacement / "sentinel.txt").read_bytes() == b"replacement must stay unchanged"
    assert retained.is_dir()
    assert not tuple(retained.iterdir())


@pytest.mark.parametrize("relative", (Path(".blobs"), Path(".blobs/sha256")))
def test_local_source_catalog_store_rejects_symlink_blob_roots_without_mutation(
    tmp_path: Path,
    relative: Path,
) -> None:
    root = tmp_path / "catalog-store"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"outside must stay unchanged")
    attacked = root / relative
    attacked.parent.mkdir(parents=True, exist_ok=True)
    attacked.symlink_to(outside, target_is_directory=True)
    store = LocalSourceCatalogStore(root, create=False)

    with pytest.raises(IntegrityError, match="blob root.*non-symlink directory|SHA-256 root.*non-symlink directory"):
        build_with_store(store)

    assert_outside_sentinel_unchanged(outside)


def test_local_source_catalog_store_rejects_shared_sha_root_symlink_without_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog-store"
    shared = tmp_path / "shared-blobs"
    outside = tmp_path / "outside"
    root.mkdir()
    shared.mkdir()
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"outside must stay unchanged")
    (shared / "sha256").symlink_to(outside, target_is_directory=True)
    store = LocalSourceCatalogStore(root, create=False, shared_blob_root=shared)

    with pytest.raises(IntegrityError, match="shared source-catalog SHA-256 root.*non-symlink directory"):
        build_with_store(store)

    assert_outside_sentinel_unchanged(outside)


def test_local_source_catalog_store_creates_pending_blob_under_pinned_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "catalog-store"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"outside must stay unchanged")
    store = LocalSourceCatalogStore(root)
    actual_open = catalog_pinned_fs.os.open
    swapped = False

    def swap_pending_before_file_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if (
            not swapped
            and isinstance(path, str)
            and path.startswith("blob-")
            and flags & catalog_pinned_fs.os.O_CREAT
        ):
            pending = next((root / ".staging").glob("catalog-*/blobs/.pending"))
            pending.rename(pending.with_name(".pending-retained"))
            pending.symlink_to(outside, target_is_directory=True)
            swapped = True
        return actual_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(catalog_pinned_fs.os, "open", swap_pending_before_file_open)

    with pytest.raises(IntegrityError, match="pending blob root changed during use"):
        build_with_store(store)

    assert swapped
    assert_outside_sentinel_unchanged(outside)


@pytest.mark.parametrize("destination_kind", ("local", "shared"))
def test_local_source_catalog_store_links_published_blobs_under_pinned_sha_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination_kind: str,
) -> None:
    root = tmp_path / "catalog-store"
    shared = tmp_path / "shared-blobs"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"outside must stay unchanged")
    store = LocalSourceCatalogStore(
        root,
        shared_blob_root=shared if destination_kind == "shared" else None,
    )
    target = (root / ".blobs" if destination_kind == "local" else shared) / "sha256"
    retained = target.with_name("sha256-retained")
    actual_link = catalog_staging.os.link
    swapped = False

    def swap_sha_before_link(
        source: str | bytes | Path,
        destination: str | bytes | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal swapped
        if not swapped and dst_dir_fd is not None and target.is_dir():
            named = target.lstat()
            opened = catalog_staging.os.fstat(dst_dir_fd)
            if (named.st_dev, named.st_ino) == (opened.st_dev, opened.st_ino):
                target.rename(retained)
                target.symlink_to(outside, target_is_directory=True)
                swapped = True
        actual_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(catalog_staging.os, "link", swap_sha_before_link)

    with pytest.raises(IntegrityError, match="published SHA-256 root changed during use"):
        build_with_store(store)

    assert swapped
    assert_outside_sentinel_unchanged(outside)
    assert not [path for path in root.iterdir() if not path.name.startswith(".")]


def test_local_source_catalog_store_links_shared_reuse_under_pinned_staging_sha_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "catalog-store"
    shared = tmp_path / "shared-blobs"
    outside = tmp_path / "outside"
    payload = b"shared source-catalog payload"
    blob_ref = sha256_digest(payload)
    shared_sha = shared / "sha256"
    shared_sha.mkdir(parents=True)
    (shared_sha / blob_ref.removeprefix("sha256:")).write_bytes(payload)
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"outside must stay unchanged")
    store = LocalSourceCatalogStore(root, shared_blob_root=shared)
    actual_link = catalog_staging.os.link
    swapped = False

    def swap_staged_sha_before_link(
        source: str | bytes | Path,
        destination: str | bytes | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal swapped
        candidates = tuple((root / ".staging").glob("catalog-*/blobs/sha256"))
        if not swapped and dst_dir_fd is not None and candidates:
            target = candidates[0]
            named = target.lstat()
            opened = catalog_staging.os.fstat(dst_dir_fd)
            if (named.st_dev, named.st_ino) == (opened.st_dev, opened.st_ino):
                target.rename(target.with_name("sha256-retained"))
                target.symlink_to(outside, target_is_directory=True)
                swapped = True
        actual_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(catalog_staging.os, "link", swap_staged_sha_before_link)

    with pytest.raises(IntegrityError, match="staged SHA-256 root changed during use"):
        with store.stage() as staging:
            staging.put_blob(blob_ref, len(payload), (payload,))

    assert swapped
    assert_outside_sentinel_unchanged(outside)
