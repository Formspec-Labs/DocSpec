"""SQLite record-workspace contract: one collection streams in sorted identity order, repeated or conflicting
logical identities refuse with IntegrityError, and the spooled-byte limit refuses with LimitExceededError while
the workspace root is removed on close.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docspec.adapters.record_workspace import LocalSqliteRecordWorkspaceFactory
from docspec.errors import IntegrityError, LimitExceededError


def _record(record_id: str, source_item_id: str, value: str) -> dict[str, str]:
    return {"recordId": record_id, "sourceItemId": source_item_id, "value": value}


def test_sqlite_workspace_streams_one_sorted_collection(tmp_path: Path) -> None:
    factory = LocalSqliteRecordWorkspaceFactory(tmp_path / "workspace", read_batch_size=1)

    with factory.create() as workspace:
        workspace.add_record(
            "segments",
            identity="record-b",
            source_item_id="source-a",
            record=_record("record-b", "source-a", "replacement"),
        )
        workspace.add_record("segments", identity="record-a", source_item_id="source-z",
            record=_record("record-a", "source-z", "retained row"))

        assert list(workspace.stream_records("segments")) == [
            _record("record-a", "source-z", "retained row"),
            _record("record-b", "source-a", "replacement"),
        ]
        assert workspace.lookup_record("segments", "record-a") == _record(
            "record-a", "source-z", "retained row"
        )
        assert workspace.lookup_record("segments", "missing") is None

    assert list(factory.root.iterdir()) == []


def test_sqlite_workspace_rejects_repeated_and_conflicting_logical_identities(tmp_path: Path) -> None:
    factory = LocalSqliteRecordWorkspaceFactory(tmp_path / "workspace")
    original = _record("record-a", "source-a", "first")

    with factory.create() as workspace:
        workspace.add_record("segments", identity="record-a", source_item_id="source-a", record=original)
        with pytest.raises(IntegrityError, match="repeats identity 'record-a'"):
            workspace.add_record("segments", identity="record-a", source_item_id="source-a", record=original)
        with pytest.raises(IntegrityError, match="conflicts for identity 'record-a'"):
            workspace.add_record(
                "segments",
                identity="record-a",
                source_item_id="source-a",
                record=_record("record-a", "source-a", "different"),
            )


def test_sqlite_workspace_enforces_its_spooled_byte_limit(tmp_path: Path) -> None:
    factory = LocalSqliteRecordWorkspaceFactory(
        tmp_path / "workspace",
        max_spooled_bytes=20,
        max_record_bytes=1024,
    )

    with factory.create() as workspace:
        with pytest.raises(LimitExceededError, match="spool limit"):
            workspace.add_record(
                "segments",
                identity="record-a",
                source_item_id="source-a",
                record=_record("record-a", "source-a", "larger than twenty bytes"),
            )
