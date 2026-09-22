"""Local bounded record workspace backed by an ephemeral SQLite file."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from docspec.domain.identity import canonical_json_bytes, parse_canonical_json, require_text, thaw_json
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.record_workspace import RecordWorkspace


class LocalSqliteRecordWorkspace:
    """Use bounded process memory while assembling bounded record collections.

    The database is scratch space only. It is removed when the workspace
    closes; immutable ``RecordStorage`` output remains the authority.
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
        descriptor, name = tempfile.mkstemp(prefix="records-", suffix=".sqlite3", dir=self._directory)
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
            raise RuntimeError("record workspace is not open")
        return self._connection

    def add_record(
        self,
        collection: str,
        *,
        identity: str,
        source_item_id: str,
        record: Mapping[str, Any],
    ) -> None:
        """Spool one canonical record under its collection identity; repeats, conflicts and byte limits are refused."""

        collection = require_text(collection, "collection")
        identity = require_text(identity, "identity")
        source_item_id = require_text(source_item_id, "source_item_id")
        payload = canonical_json_bytes(record)
        if len(payload) > self._max_record_bytes:
            raise LimitExceededError(
                f"record exceeds the {self._max_record_bytes}-byte limit"
            )
        connection = self._open_connection()
        existing = connection.execute(
            "SELECT source_item_id, payload FROM records WHERE collection = ? AND identity = ?",
            (collection, identity),
        ).fetchone()
        if existing is not None:
            outcome = "repeats" if existing == (source_item_id, payload) else "conflicts for"
            raise IntegrityError(
                f"record collection {collection!r} {outcome} identity {identity!r}"
            )
        if self._spooled_bytes + len(payload) > self._max_spooled_bytes:
            raise LimitExceededError(
                f"records exceed the {self._max_spooled_bytes}-byte spool limit"
            )
        try:
            connection.execute(
                "INSERT INTO records(collection, identity, source_item_id, payload) VALUES (?, ?, ?, ?)",
                (collection, identity, source_item_id, payload),
            )
        except sqlite3.Error as error:
            raise IntegrityError(f"record workspace could not spool a record: {error}") from error
        self._spooled_bytes += len(payload)

    def stream_records(self, collection: str) -> Iterator[dict[str, Any]]:
        """Stream one collection's records in identity order."""

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
                        label=f"record collection {collection!r}",
                        file_form=False,
                    )
                )
                if not isinstance(value, dict):
                    raise IntegrityError("record must be a JSON object")
                yield value

    def lookup_record(self, collection: str, identity: str) -> dict[str, Any] | None:
        """Return one record by identity, or None when it is absent."""

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
                label=f"record collection {collection!r}",
                file_form=False,
            )
        )
        if not isinstance(value, dict):
            raise IntegrityError("record must be a JSON object")
        return value


class LocalSqliteRecordWorkspaceFactory:
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
            raise ValueError("record workspace limits must be positive")
        root = Path(root)
        if root.is_symlink():
            raise IntegrityError("record workspace root must not be a symlink")
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise IntegrityError("record workspace root must be a regular directory")
        self.root = root.resolve(strict=True)
        self.max_spooled_bytes = max_spooled_bytes
        self.max_record_bytes = max_record_bytes
        self.cache_kib = cache_kib
        self.read_batch_size = read_batch_size

    def create(self) -> RecordWorkspace:
        return LocalSqliteRecordWorkspace(
            self.root,
            max_spooled_bytes=self.max_spooled_bytes,
            max_record_bytes=self.max_record_bytes,
            cache_kib=self.cache_kib,
            read_batch_size=self.read_batch_size,
        )


__all__ = ["LocalSqliteRecordWorkspace", "LocalSqliteRecordWorkspaceFactory"]
