"""Qualification probe: same-content concurrent writes must both succeed.

This uses only the installed public writer and synchronizes its native IO.
It exposes the known pending-hardlink cleanup race without sleeps or byte edits.
"""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import os
from threading import Event, local

from rulespec_artifacts import LocalBlobWriter


def test_same_blob_reuse_survives_the_winners_pending_link_cleanup(tmp_path, monkeypatch):
    payload = b"same immutable bytes"
    digest = "sha256:" + sha256(payload).hexdigest()
    first_writer = LocalBlobWriter(tmp_path, object_prefix="objects/sha256", shard_digits=2)
    second_writer = LocalBlobWriter(tmp_path, object_prefix="objects/sha256", shard_digits=2)
    winner_at_cleanup, allow_cleanup, winner_finished = Event(), Event(), Event()
    current = local()
    original_unlink, original_read = os.unlink, os.read

    def unlink(name, *, dir_fd=None):
        if getattr(current, "role", None) == "winner" and dir_fd is not None:
            winner_at_cleanup.set()
            assert allow_cleanup.wait(10), "second writer never reached verification"
        return original_unlink(name, dir_fd=dir_fd)

    def read(descriptor, size):
        if getattr(current, "role", None) == "reuse" and not current.released:
            # The shared verifier has already captured its before fstat here.
            current.released = True
            allow_cleanup.set()
            assert winner_finished.wait(10), "winner cleanup did not finish"
        return original_read(descriptor, size)

    def winner():
        current.role = "winner"
        try:
            return first_writer.put((payload,), max_bytes=len(payload))
        finally:
            winner_finished.set()

    def reuse():
        current.role, current.released = "reuse", False
        return second_writer.put((), max_bytes=len(payload), expected_digest=digest, expected_size=len(payload))

    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "read", read)
    with ThreadPoolExecutor(max_workers=2) as workers:
        written = workers.submit(winner)
        try:
            assert winner_at_cleanup.wait(10), "winner never published the blob"
            reused = workers.submit(reuse)
            first = written.result(timeout=15)
            assert (tmp_path / first.object_key).read_bytes() == payload
            assert list((tmp_path / ".pending").iterdir()) == []
            second = reused.result(timeout=15)
            assert second.reused and second.digest == first.digest
        finally:
            allow_cleanup.set()
