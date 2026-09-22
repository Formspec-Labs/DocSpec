"""Bounded SQLite workspace for stateful source-catalog policies."""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Self

from docspec.domain.identity import (
    canonical_json_bytes,
    parse_canonical_json,
    require_text,
    thaw_json,
)
from docspec.errors import IntegrityError, LimitExceededError

_PAGE_BYTES = 65536


def _scratch_page_limit(max_scratch_bytes: int | None) -> int | None:
    if max_scratch_bytes is None:
        return None
    if type(max_scratch_bytes) is not int or max_scratch_bytes < 1:
        raise ValueError("catalog scratch allowance must be a positive integer")
    # Reserve a rollback copy of every page, its per-page metadata, and a
    # generous journal header. Staged artifact bytes are a separate allowance.
    pages = (max_scratch_bytes - _PAGE_BYTES) // (2 * _PAGE_BYTES + 8)
    if pages < 2:
        raise LimitExceededError("catalog scratch allowance cannot hold the minimum SQLite workspace and journal")
    return pages


#: Each byte of a UTF-16-BE key becomes that byte plus one, big-endian in two
#: bytes, so that no key byte is NUL and the NUL pair can terminate a part. The
#: mapping only has 256 inputs, so precomputing it turns a per-byte Python loop
#: into one table lookup and join -- measured 3.76 us -> 1.08 us per key, and
#: byte-identical output.
_KEY_BYTE_PAIRS = tuple(bytes((((value + 1) >> 8) & 0xFF, (value + 1) & 0xFF)) for value in range(256))


def _ordered_key(parts: tuple[str, ...]) -> bytes:
    if not parts:
        raise ValueError("catalog workspace keys must contain at least one part")
    result = bytearray()
    for part in parts:
        text = require_text(part, "catalog workspace key part")
        try:
            encoded = text.encode("utf-16-be")
        except UnicodeEncodeError as error:
            raise ValueError("catalog workspace key contains a lone Unicode surrogate") from error
        result += b"".join(map(_KEY_BYTE_PAIRS.__getitem__, encoded))
        result += b"\x00\x00"
    return bytes(result)


def _mapping(payload: bytes, *, label: str) -> Mapping[str, Any]:
    value = thaw_json(parse_canonical_json(payload, label=label, file_form=False))
    if not isinstance(value, dict):
        raise IntegrityError(f"{label} must be an object")
    return value


class SqliteCatalogPolicyWorkspace:
    """Store policy indexes and ordered frames without corpus-sized memory."""

    def __init__(self, *, directory: Path | None = None, path: Path | None = None,
                 max_scratch_bytes: int | None = None) -> None:
        """Open a disposable workspace, or a durable one at ``path``.

        Without ``path`` the workspace is a temporary directory removed on
        close, as before. With ``path`` the file outlives the process: a build
        that dies leaves everything it committed, and the next build with the
        same identity resumes from it (see ``_ResumeLedger``). Nothing here is
        digested either way; the artifact is built from what the table yields.
        ``max_scratch_bytes`` reserves space for the database and rollback
        journal. It does not include staged catalog payloads or other workers.
        """

        page_limit = _scratch_page_limit(max_scratch_bytes)
        self._connection = None
        self._temporary = None
        if path is not None:
            database = Path(path)
            database.parent.mkdir(parents=True, exist_ok=True)
        else:
            parent = str(Path(directory)) if directory is not None else None
            self._temporary = tempfile.TemporaryDirectory(
                prefix="docspec-catalog-policy-",
                dir=parent,
            )
            database = Path(self._temporary.name) / "workspace.sqlite3"
        try:
            self._open(database, page_limit)
        except BaseException:
            self.close()
            raise

    def _open(self, database: Path, page_limit: int | None) -> None:
        self._connection = sqlite3.connect(database)
        # Set before CREATE TABLE, which is the only point it can be set: SQLite
        # fixes the page size when the first table is written.
        #
        # Catalog rows average ~10.5 KB, which rounds badly against 4 KB pages --
        # a raw b-tree sample of two real 40+ GB workspaces found 88% of pages
        # were overflow, and the partition read-back is then latency-bound random
        # I/O one page at a time. Measured here at that row size: 1.05 s to write
        # and commit 20,000 rows at 4 KB against 0.25 s at 64 KB, 1.23x payload on
        # disk against 1.12x.
        #
        # Safe because the workspace is disposable and its bytes are never
        # digested -- the artifact is built from what this table yields, not from
        # the file. No identity moves.
        self._connection.execute("PRAGMA page_size=65536")
        if page_limit is not None:
            if self._connection.execute("PRAGMA page_size").fetchone()[0] != _PAGE_BYTES:
                raise IntegrityError("catalog resume workspace uses a different SQLite page size")
            actual_limit = self._connection.execute(f"PRAGMA max_page_count={page_limit}").fetchone()[0]
            if actual_limit > page_limit:
                raise LimitExceededError("existing catalog workspace exceeds its scratch allowance")
        self._connection.execute("PRAGMA journal_mode=DELETE")
        self._connection.execute("PRAGMA synchronous=OFF")
        self._write(
            """
            CREATE TABLE IF NOT EXISTS entries (
                namespace TEXT NOT NULL,
                ordered_key BLOB NOT NULL,
                payload BLOB NOT NULL,
                PRIMARY KEY (namespace, ordered_key)
            ) WITHOUT ROWID
            """
        )

    def _write(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        try:
            return self._connection.execute(sql, parameters)
        except sqlite3.Error as error:
            if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                raise LimitExceededError("catalog workspace exceeds its scratch allowance") from error
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        self._connection = None
        try:
            if connection is not None:
                connection.close()
        finally:
            if self._temporary is not None:
                self._temporary.cleanup()

    def commit(self) -> None:
        """Make everything written so far survive a kill.

        Cheap under ``synchronous=OFF``: no fsync, only the journal reset. The
        builder calls this at each resume point; what a resumed build sees is
        exactly the last commit, because SQLite rolls back the rest on open.
        """

        self._connection.commit()

    def put(
        self,
        namespace: str,
        key: tuple[str, ...],
        value: Mapping[str, Any],
    ) -> None:
        """Store one canonical JSON value, refusing a key that already exists."""

        self.put_payload(namespace, key, canonical_json_bytes(value))

    def put_payload(self, namespace: str, key: tuple[str, ...], payload: bytes) -> None:
        """Store one already-canonical payload without re-serializing it.

        The caller vouches that ``payload`` is exactly ``canonical_json_bytes``
        of the value it stands for; ``put`` remains the checked general path.
        """

        selected_namespace = require_text(namespace, "catalog workspace namespace")
        try:
            self._write(
                "INSERT INTO entries(namespace, ordered_key, payload) VALUES (?, ?, ?)",
                (selected_namespace, _ordered_key(key), payload),
            )
        except sqlite3.IntegrityError as error:
            raise IntegrityError(
                f"catalog workspace key already exists in namespace {selected_namespace!r}"
            ) from error

    def replace(
        self,
        namespace: str,
        key: tuple[str, ...],
        value: Mapping[str, Any],
    ) -> None:
        """Supersede one existing row, refusing when the key is absent.

        An UPDATE that matches nothing would otherwise be a silent no-op, and a
        caller replacing a row it believes it wrote has a defect worth raising
        rather than absorbing.
        """

        selected_namespace = require_text(namespace, "catalog workspace namespace")
        payload = canonical_json_bytes(value)
        cursor = self._write(
            "UPDATE entries SET payload = ? WHERE namespace = ? AND ordered_key = ?",
            (payload, selected_namespace, _ordered_key(key)),
        )
        if cursor.rowcount != 1:
            raise IntegrityError(
                f"catalog workspace key is absent in namespace {selected_namespace!r}"
            )

    def get(self, namespace: str, key: tuple[str, ...]) -> Mapping[str, Any] | None:
        """Return one stored value, or None when the key is absent."""
        selected_namespace = require_text(namespace, "catalog workspace namespace")
        row = self._connection.execute(
            "SELECT payload FROM entries WHERE namespace = ? AND ordered_key = ?",
            (selected_namespace, _ordered_key(key)),
        ).fetchone()
        if row is None:
            return None
        return _mapping(row[0], label=f"catalog workspace {selected_namespace} value")

    def iter_ordered(
        self, namespace: str, *, after: tuple[str, ...] | None = None
    ) -> Iterator[Mapping[str, Any]]:
        """Stream stored values in key order, optionally resuming after one key."""
        selected_namespace = require_text(namespace, "catalog workspace namespace")
        if after is None:
            cursor = self._connection.execute(
                "SELECT payload FROM entries WHERE namespace = ? ORDER BY ordered_key",
                (selected_namespace,),
            )
        else:
            cursor = self._connection.execute(
                "SELECT payload FROM entries WHERE namespace = ? AND ordered_key > ?"
                " ORDER BY ordered_key",
                (selected_namespace, _ordered_key(after)),
            )
        for (payload,) in cursor:
            yield _mapping(payload, label=f"catalog workspace {selected_namespace} value")

    def iter_payloads(self, namespace: str) -> Iterator[bytes]:
        """Stream stored canonical payloads verbatim, without parse or thaw."""

        selected_namespace = require_text(namespace, "catalog workspace namespace")
        cursor = self._connection.execute(
            "SELECT payload FROM entries WHERE namespace = ? ORDER BY ordered_key",
            (selected_namespace,),
        )
        for (payload,) in cursor:
            yield bytes(payload)


__all__ = ["SqliteCatalogPolicyWorkspace"]
