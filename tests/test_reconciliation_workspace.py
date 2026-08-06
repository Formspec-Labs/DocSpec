from __future__ import annotations

from pathlib import Path

import pytest

from docspec.adapters.reconciliation import LocalSqliteReconciliationWorkspaceFactory
from docspec.errors import IntegrityError, LimitExceededError


def _record(record_id: str, source_item_id: str, value: str) -> dict[str, str]:
    return {"recordId": record_id, "sourceItemId": source_item_id, "value": value}


def test_sqlite_workspace_replaces_affected_rows_and_streams_one_sorted_collection(tmp_path: Path) -> None:
    factory = LocalSqliteReconciliationWorkspaceFactory(tmp_path / "workspace", read_batch_size=1)

    with factory.create() as workspace:
        workspace.mark_affected("source-a")
        workspace.add_record(
            "segments",
            identity="record-b",
            source_item_id="source-a",
            record=_record("record-b", "source-a", "replacement"),
        )
        workspace.retain_records(
            "segments",
            (
                _record("record-b", "source-a", "old affected row"),
                _record("record-a", "source-z", "retained row"),
            ),
            identity_field="recordId",
            source_item_field="sourceItemId",
        )

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
    factory = LocalSqliteReconciliationWorkspaceFactory(tmp_path / "workspace")
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
    factory = LocalSqliteReconciliationWorkspaceFactory(
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
