"""Local bounded reconciliation workspace backed by an ephemeral SQLite file."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from docspec.domain.identity import canonical_json_bytes, parse_canonical_json, require_text, thaw_json
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.reconciliation_workspace import ReconciliationWorkspace


class LocalSqliteReconciliationWorkspace:
    """Use bounded process memory while assembling a complete logical run.

    The database is scratch space only. It is removed when reconciliation
    finishes; immutable ``RecordStorage`` output remains the authority.
    """

    def __init__(
        self,
        directory: Path,
        *,
        max_spooled_bytes: int,
        max_record_bytes: int,
        cache_kib: int,
        read_batch_size: int,
    ) -> None:
        self._directory = directory
        self._max_spooled_bytes = max_spooled_bytes
        self._max_record_bytes = max_record_bytes
        self._cache_kib = cache_kib
        self._read_batch_size = read_batch_size
        self._path: Path | None = None
        self._connection: sqlite3.Connection | None = None
        self._spooled_bytes = 0

    def __enter__(self) -> Self:
        descriptor, name = tempfile.mkstemp(prefix="reconcile-", suffix=".sqlite3", dir=self._directory)
        os.close(descriptor)
        self._path = Path(name)
        try:
            connection = sqlite3.connect(self._path)
            self._connection = connection
            connection.execute("PRAGMA journal_mode = OFF")
            connection.execute("PRAGMA synchronous = OFF")
            connection.execute("PRAGMA temp_store = FILE")
            connection.execute(f"PRAGMA cache_size = -{self._cache_kib}")
            connection.execute(
                "CREATE TABLE records ("
                "collection TEXT NOT NULL, identity TEXT NOT NULL, "
                "source_item_id TEXT NOT NULL, payload BLOB NOT NULL, "
                "PRIMARY KEY (collection, identity)) WITHOUT ROWID"
            )
            connection.execute(
                "CREATE TABLE affected_source_items (source_item_id TEXT PRIMARY KEY) WITHOUT ROWID"
            )
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._path is not None:
            self._path.unlink(missing_ok=True)
            self._path = None

    def _open_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("reconciliation workspace is not open")
        return self._connection

    def mark_affected(self, source_item_id: str) -> None:
        require_text(source_item_id, "source_item_id")
        self._open_connection().execute(
            "INSERT OR IGNORE INTO affected_source_items(source_item_id) VALUES (?)",
            (source_item_id,),
        )

    def is_affected(self, source_item_id: str) -> bool:
        require_text(source_item_id, "source_item_id")
        row = self._open_connection().execute(
            "SELECT 1 FROM affected_source_items WHERE source_item_id = ?",
            (source_item_id,),
        ).fetchone()
        return row is not None

    def _insert(
        self,
        collection: str,
        *,
        identity: str,
        source_item_id: str,
        record: Mapping[str, Any],
    ) -> None:
        collection = require_text(collection, "collection")
        identity = require_text(identity, "identity")
        source_item_id = require_text(source_item_id, "source_item_id")
        payload = canonical_json_bytes(record)
        if len(payload) > self._max_record_bytes:
            raise LimitExceededError(
                f"reconciliation record exceeds the {self._max_record_bytes}-byte limit"
            )
        connection = self._open_connection()
        existing = connection.execute(
            "SELECT source_item_id, payload FROM records WHERE collection = ? AND identity = ?",
            (collection, identity),
        ).fetchone()
        if existing is not None:
            outcome = "repeats" if existing == (source_item_id, payload) else "conflicts for"
            raise IntegrityError(
                f"reconciliation collection {collection!r} {outcome} identity {identity!r}"
            )
        if self._spooled_bytes + len(payload) > self._max_spooled_bytes:
            raise LimitExceededError(
                f"reconciliation records exceed the {self._max_spooled_bytes}-byte spool limit"
            )
        try:
            connection.execute(
                "INSERT INTO records(collection, identity, source_item_id, payload) VALUES (?, ?, ?, ?)",
                (collection, identity, source_item_id, payload),
            )
        except sqlite3.Error as error:
            raise IntegrityError(f"reconciliation workspace could not spool a record: {error}") from error
        self._spooled_bytes += len(payload)

    def add_record(
        self,
        collection: str,
        *,
        identity: str,
        source_item_id: str,
        record: Mapping[str, Any],
    ) -> None:
        self._insert(
            collection,
            identity=identity,
            source_item_id=source_item_id,
            record=record,
        )

    def retain_records(
        self,
        collection: str,
        records: Iterable[Mapping[str, Any]],
        *,
        identity_field: str,
        source_item_field: str,
    ) -> None:
        identity_field = require_text(identity_field, "identity_field")
        source_item_field = require_text(source_item_field, "source_item_field")
        for record in records:
            try:
                identity = record[identity_field]
                source_item_id = record[source_item_field]
            except KeyError as error:
                raise IntegrityError("retained reconciliation record is missing an identity field") from error
            if not isinstance(identity, str) or not isinstance(source_item_id, str):
                raise IntegrityError("retained reconciliation record identities must be strings")
            if not self.is_affected(source_item_id):
                self._insert(
                    collection,
                    identity=identity,
                    source_item_id=source_item_id,
                    record=record,
                )

    def stream_records(self, collection: str) -> Iterator[dict[str, Any]]:
        collection = require_text(collection, "collection")
        cursor = self._open_connection().execute(
            "SELECT payload FROM records WHERE collection = ? ORDER BY identity",
            (collection,),
        )
        while rows := cursor.fetchmany(self._read_batch_size):
            for (payload,) in rows:
                value = thaw_json(
                    parse_canonical_json(
                        payload,
                        label=f"reconciliation collection {collection!r}",
                        file_form=False,
                    )
                )
                if not isinstance(value, dict):
                    raise IntegrityError("reconciliation record must be a JSON object")
                yield value

    def lookup_record(self, collection: str, identity: str) -> dict[str, Any] | None:
        collection = require_text(collection, "collection")
        identity = require_text(identity, "identity")
        row = self._open_connection().execute(
            "SELECT payload FROM records WHERE collection = ? AND identity = ?",
            (collection, identity),
        ).fetchone()
        if row is None:
            return None
        value = thaw_json(
            parse_canonical_json(
                row[0],
                label=f"reconciliation collection {collection!r}",
                file_form=False,
            )
        )
        if not isinstance(value, dict):
            raise IntegrityError("reconciliation record must be a JSON object")
        return value


class LocalSqliteReconciliationWorkspaceFactory:
    """Create disposable SQLite workspaces below one operator-selected root."""

    def __init__(
        self,
        root: Path,
        *,
        max_spooled_bytes: int = 1024**4,
        max_record_bytes: int = 8 * 1024**2,
        cache_kib: int = 8192,
        read_batch_size: int = 1024,
    ) -> None:
        if min(max_spooled_bytes, max_record_bytes, cache_kib, read_batch_size) <= 0:
            raise ValueError("reconciliation workspace limits must be positive")
        root = Path(root)
        if root.is_symlink():
            raise IntegrityError("reconciliation workspace root must not be a symlink")
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise IntegrityError("reconciliation workspace root must be a regular directory")
        self.root = root.resolve(strict=True)
        self.max_spooled_bytes = max_spooled_bytes
        self.max_record_bytes = max_record_bytes
        self.cache_kib = cache_kib
        self.read_batch_size = read_batch_size

    def create(self) -> ReconciliationWorkspace:
        return LocalSqliteReconciliationWorkspace(
            self.root,
            max_spooled_bytes=self.max_spooled_bytes,
            max_record_bytes=self.max_record_bytes,
            cache_kib=self.cache_kib,
            read_batch_size=self.read_batch_size,
        )


__all__ = ["LocalSqliteReconciliationWorkspace", "LocalSqliteReconciliationWorkspaceFactory"]
