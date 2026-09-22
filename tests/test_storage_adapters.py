"""Local content-addressed blob store contract: publication and deletion survive a directory-flush failure as
retryable complete bytes, reads stream, deduplicate and take half-open ranges, and every limit, tampering,
symlink or noncanonical-locator case fails closed.
"""

from __future__ import annotations


from dataclasses import replace
from pathlib import Path

import pytest

from docspec.adapters.storage import (
    LocalContentAddressedBlobStore,
)
from docspec.domain.references import BlobRef
from docspec.errors import IntegrityError, LimitExceededError


def test_blob_publication_flush_failure_leaves_retryable_complete_bytes(tmp_path, monkeypatch):
    from docspec.adapters.storage import files

    store = LocalContentAddressedBlobStore(tmp_path / "blobs")
    sync = files.sync_directory
    flushed = []

    def fail_after_link(directory):
        flushed.append(directory)
        raise OSError("directory flush interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(files, "sync_directory", fail_after_link)
        with pytest.raises(OSError, match="interrupted"):
            store.put_if_absent([b"durable"], media_type="text/plain")
    assert flushed
    reference = store.put_if_absent([b"durable"], media_type="text/plain")
    assert b"".join(store.read(reference)) == b"durable"
    assert not list((store.root / ".staging").iterdir())

    flushed.clear()
    def observe(directory):
        flushed.append(directory)
        sync(directory)

    with monkeypatch.context() as patch:
        patch.setattr(files, "sync_directory", observe)
        store.ensure_ready(reference)
    leaf = (store.root / reference.locator).parent
    assert flushed == [leaf, leaf.parent, leaf.parent.parent, store.root, store.root.parent]


def test_blob_delete_directory_flush_failure_is_retryable(tmp_path, monkeypatch):
    from docspec.adapters.storage import files

    store = LocalContentAddressedBlobStore(tmp_path)
    reference = store.put_if_absent([b"remove"], media_type="text/plain")
    with monkeypatch.context() as patch:
        def interrupted(directory):
            raise OSError("unlink flush interrupted")
        patch.setattr(files, "sync_directory", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            store.delete(reference)
    assert not (store.root / reference.locator).exists()
    assert not store.delete(reference)


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
    with pytest.raises(IntegrityError):
        b"".join(store.read(reference))

    target = tmp_path / "outside"
    target.write_bytes(b"safe")
    link = store.root / "objects" / "sha256" / "aa" / ("a" * 64)
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    linked = BlobRef(link.relative_to(store.root).as_posix(), f"sha256:{'a' * 64}", 4, "text/plain")
    with pytest.raises(IntegrityError):
        store.verify(linked)
    with pytest.raises(IntegrityError):
        b"".join(store.read(linked))


def test_blob_read_refuses_correct_bytes_at_a_different_locator_before_open(tmp_path, monkeypatch):
    store = LocalContentAddressedBlobStore(tmp_path / "objects")
    reference = store.put_if_absent([b"safe"], media_type="text/plain")
    misplaced = store.root / "copied-object"
    misplaced.write_bytes(b"safe")
    reference = replace(reference, locator="copied-object")
    original_open = Path.open

    def guarded_open(self, *args, **kwargs):
        if self == misplaced:
            pytest.fail("a blob with a noncanonical locator was opened")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    with pytest.raises(IntegrityError, match="locator does not match its digest"):
        b"".join(store.read(reference))


@pytest.mark.parametrize("allowance, error", [(8, LimitExceededError), (16, IntegrityError)])
def test_blob_read_never_yields_growth_past_its_allowance_or_reference(tmp_path, allowance, error):
    store = LocalContentAddressedBlobStore(tmp_path / "objects")
    reference = store.put_if_absent([b"12345678"], media_type="text/plain")
    chunks = store.read(reference, chunk_size=4, max_bytes=allowance)
    received = next(chunks)
    with (store.root / reference.locator).open("ab") as stream:
        stream.write(b"unexpected growth")
    received += next(chunks)
    with pytest.raises(error):
        next(chunks)
    assert received == b"12345678"
