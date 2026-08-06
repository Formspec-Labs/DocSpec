"""Small local processor-result cache; immutable control artifacts remain authority."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from docspec.domain.identity import require_text
from docspec.domain.references import ArtifactRef
from docspec.errors import IntegrityError


class NullProcessorResultCache:
    """Reasonable no-reuse default for compositions that do not configure a cache."""

    def lookup(self, reuse_key: str) -> ArtifactRef | None:
        require_text(reuse_key, "processor result reuse key")
        return None

    def put_if_absent(self, reuse_key: str, result: ArtifactRef) -> ArtifactRef:
        require_text(reuse_key, "processor result reuse key")
        return result

    def discard(self, reuse_key: str, expected: ArtifactRef) -> bool:
        require_text(reuse_key, "processor result reuse key")
        return False


class LocalSqliteProcessorResultCache:
    """Persist only reuse-key to immutable-result-reference mappings."""

    def __init__(self, path: Path, *, busy_timeout_milliseconds: int = 5_000) -> None:
        if type(busy_timeout_milliseconds) is not int or busy_timeout_milliseconds <= 0:
            raise ValueError("processor cache busy timeout must be a positive integer")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or path.parent.is_symlink():
            raise IntegrityError("processor cache path must not traverse a symlink")
        self.path = path
        self.busy_timeout_milliseconds = busy_timeout_milliseconds
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS processor_results ("
                "reuse_key TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, locator TEXT NOT NULL, "
                "digest TEXT NOT NULL, media_type TEXT NOT NULL, byte_size INTEGER NOT NULL"
                ") WITHOUT ROWID"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.busy_timeout_milliseconds / 1000)
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_milliseconds}")
        return connection

    def lookup(self, reuse_key: str) -> ArtifactRef | None:
        require_text(reuse_key, "processor result reuse key")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT artifact_id, locator, digest, media_type, byte_size "
                "FROM processor_results WHERE reuse_key = ?",
                (reuse_key,),
            ).fetchone()
        if row is None:
            return None
        return self._reference(row)

    @staticmethod
    def _reference(row: tuple[object, ...]) -> ArtifactRef:
        try:
            return ArtifactRef(row[0], row[1], row[2], row[3], row[4])
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"processor cache contains an invalid result reference: {error}") from error

    def put_if_absent(self, reuse_key: str, result: ArtifactRef) -> ArtifactRef:
        require_text(reuse_key, "processor result reuse key")
        values = (
            reuse_key,
            result.artifact_id,
            result.locator,
            result.digest,
            result.media_type,
            result.byte_size,
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO processor_results "
                "(reuse_key, artifact_id, locator, digest, media_type, byte_size) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                values,
            )
            winner = connection.execute(
                "SELECT artifact_id, locator, digest, media_type, byte_size "
                "FROM processor_results WHERE reuse_key = ?",
                (reuse_key,),
            ).fetchone()
            if winner is None:
                raise IntegrityError("processor cache failed to publish or resolve an immutable winner")
            return self._reference(winner)

    def discard(self, reuse_key: str, expected: ArtifactRef) -> bool:
        require_text(reuse_key, "processor result reuse key")
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM processor_results WHERE reuse_key = ? AND artifact_id = ? "
                "AND locator = ? AND digest = ? AND media_type = ? AND byte_size = ?",
                (
                    reuse_key,
                    expected.artifact_id,
                    expected.locator,
                    expected.digest,
                    expected.media_type,
                    expected.byte_size,
                ),
            )
            return cursor.rowcount == 1
