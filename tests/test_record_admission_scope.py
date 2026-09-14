"""Physical admission reuse follows the existing content protection lifetime."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from docspec.adapters.storage.ledger import LocalSqliteCoreLedger
from docspec.adapters.storage.records import IcebergRecordStorage
from docspec.errors import IntegrityError
from tests.test_record_batches import batches, write


def test_admission_scope_is_bounded_nested_and_thread_local(tmp_path):
    with closing(IcebergRecordStorage(tmp_path)) as storage:
        references = [write(storage, batches(index + 1)) for index in range(10)]
        with storage.admission_scope():
            first = storage.admitted(references[0])
            with storage.admission_scope():
                assert storage.admitted(references[0]) is first
            def other_thread():
                with storage.admission_scope():
                    other = storage.admitted(references[0])
                    assert storage.admitted(references[0]) is other
                    return other
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(other_thread).result() is not first
            assert storage.admitted(references[0]) is first
            for reference in references[1:]:
                storage.admitted(reference)
            assert len(storage._admissions.layers) == 8
            assert storage.admitted(references[0]) is not first
        assert getattr(storage._admissions, "layers", None) is None
        with pytest.raises(RuntimeError), storage.admission_scope():
            storage.admitted(references[0])
            raise RuntimeError("abandon reader")
        assert getattr(storage._admissions, "layers", None) is None


@pytest.mark.parametrize("audit", ["available", "admit", "verify_members"])
def test_explicit_fresh_audit_detects_corruption_and_invalidates_cached_handle(tmp_path, audit):
    with closing(IcebergRecordStorage(tmp_path)) as storage:
        reference = write(storage, batches(2))
        with storage.admission_scope():
            storage.admitted(reference)
            (storage.root / reference.state_ref).write_bytes(b"{}")
            with pytest.raises(IntegrityError, match="differs from its reference"):
                getattr(storage, audit)(reference)
            with pytest.raises(IntegrityError, match="differs from its reference"):
                storage.admitted(reference)


def test_ledger_reuses_admission_only_under_shared_writable_protection(tmp_path):
    with closing(IcebergRecordStorage(tmp_path / "records")) as storage:
        reference = write(storage, batches(2))
        path = tmp_path / "ledger.sqlite"
        with closing(LocalSqliteCoreLedger(path, record_storage=storage)) as ledger:
            with ledger.content_guard():
                first = storage.admitted(reference)
                with ledger.content_guard():
                    assert storage.admitted(reference) is first
            assert getattr(storage._admissions, "layers", None) is None
            with ledger.content_guard():
                assert storage.admitted(reference) is not first
            with ledger.content_guard(exclusive=True):
                assert storage.admitted(reference) is not storage.admitted(reference)
                with ledger.content_guard():
                    assert getattr(storage._admissions, "layers", None) is None
        with closing(LocalSqliteCoreLedger(path, record_storage=storage, read_only=True)) as ledger:
            with ledger.content_guard():
                assert storage.admitted(reference) is not storage.admitted(reference)


def test_bulk_file_admission_reuses_bounded_parents_and_checks_every_leaf(tmp_path, monkeypatch):
    from docspec.adapters.storage import files

    calls = []
    original = files._contained_parent
    def checked(root, parts, **kwargs):
        calls.append(parts)
        return original(root, parts, **kwargs)
    monkeypatch.setattr(files, "_contained_parent", checked)
    members = []
    for index in range(257):
        parent = tmp_path / str(index)
        parent.mkdir()
        (parent / "first").write_bytes(b"one")
        (parent / "second").write_bytes(b"two")
        members.append((f"{index}/first", 3))
    members.extend((f"{index}/second", 3) for index in range(257))
    assert len(files._available_paths(tmp_path, members)) == 514
    assert len(calls) == 258  # The 257th parent is checked again, beyond the bound.
    (tmp_path / "0/second").unlink()
    (tmp_path / "0/second").symlink_to(tmp_path / "0/first")
    with pytest.raises(IntegrityError, match="unavailable"):
        files._available_paths(tmp_path, [("0/first", 3), ("0/second", 3)])


@pytest.mark.parametrize("problem", ["parent_symlink", "parent_missing", "parent_file", "leaf_missing", "leaf_directory", "wrong_size", "relative_escape"])
def test_bulk_file_admission_rejects_invalid_paths_and_fresh_changes(tmp_path, problem):
    from docspec.adapters.storage.files import _available_paths

    parent = tmp_path / "members"
    parent.mkdir()
    leaf = parent / "value"
    leaf.write_bytes(b"abc")
    assert _available_paths(tmp_path, [("members/value", 3)]) == {"members/value": str(leaf)}
    locator, size = "members/value", 3
    if problem == "parent_symlink":
        parent.rename(tmp_path / "elsewhere")
        parent.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    elif problem == "parent_missing":
        leaf.unlink()
        parent.rmdir()
    elif problem == "parent_file":
        leaf.unlink()
        parent.rmdir()
        parent.write_bytes(b"abc")
    elif problem == "leaf_missing":
        leaf.unlink()
    elif problem == "leaf_directory":
        leaf.unlink()
        leaf.mkdir()
    elif problem == "wrong_size":
        size = 4
    else:
        locator = "../value"
    with pytest.raises((IntegrityError, ValueError)):
        _available_paths(tmp_path, [(locator, size)])
