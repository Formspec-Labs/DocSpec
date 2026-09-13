"""Disposable exact task admission derived from the sealed planned-store ledger."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

from docspec.adapters.storage import LocalDocumentStoreRepository
from docspec.domain.execution import ExecutionHandoff, StoreTask
from docspec.domain.identity import OrderedJsonSequenceDigester, canonical_json_bytes
from docspec.domain.references import StoreRef
from docspec.errors import IntegrityError, LimitExceededError

_PAGE_BYTES = 4096


class _TaskMembershipIndex:
    """Build once per open lifetime, with bounded disk and memory for exact lookups.

    This database is disposable scratch, never a retained task authority. Its
    allowance is capped by the worker's scratch limit; it does not account for
    other concurrent scratch users within that worker.
    """

    def __init__(
        self,
        stores: LocalDocumentStoreRepository,
        handoff: ExecutionHandoff,
        directory: Path,
        *,
        max_scratch_bytes: int,
    ) -> None:
        self._stores = stores
        self._handoff = handoff
        self._directory = directory
        self._max_database_bytes = min(max_scratch_bytes, 4 * stores.max_plan_ledger_bytes + 1024**2)
        self._temporary: TemporaryDirectory[str] | None = None
        self._lock = Lock()

    def require_member(self, reference: StoreRef) -> None:
        """Admit only the exact initial reference before any execution effects."""
        key = canonical_json_bytes(reference.to_dict())
        if reference.revision != 0 or len(key) > self._stores.max_plan_record_bytes:
            raise IntegrityError("task input is not an exact initial planned-store reference")
        with self._lock:
            if self._temporary is None:
                self._build()
            assert self._temporary is not None
            path = Path(self._temporary.name) / "members.sqlite3"
            try:
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
                    connection.execute("PRAGMA cache_size = -2048")
                    found = connection.execute(
                        "SELECT 1 FROM members WHERE reference = ?", (key,),
                    ).fetchone()
            except sqlite3.Error as error:
                self._discard()
                raise IntegrityError(f"task membership lookup failed: {error}") from error
            if found is None:
                raise IntegrityError("task input is outside the sealed planned-store population")

    def _build(self) -> None:
        page_count = self._max_database_bytes // _PAGE_BYTES
        if page_count < 2:
            raise LimitExceededError("task membership index scratch allowance is too small")
        if self._handoff.expected_task_count > self._stores.max_plan_store_count:
            raise LimitExceededError("task membership population exceeds the planned-store limit")
        self._stores.verify_planned_store_ledger(self._handoff.planned_store_ledger)
        if self._directory.is_symlink():
            raise IntegrityError("task membership scratch must not be a symlink")
        self._directory.mkdir(parents=True, exist_ok=True)
        if self._directory.is_symlink() or not self._directory.is_dir():
            raise IntegrityError("task membership scratch must be a regular directory")
        temporary = TemporaryDirectory(prefix="planned-membership-", dir=self._directory)
        try:
            path = Path(temporary.name) / "members.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(f"PRAGMA page_size = {_PAGE_BYTES}")
                connection.execute(f"PRAGMA max_page_count = {page_count}")
                connection.execute("PRAGMA journal_mode = OFF")
                connection.execute("PRAGMA synchronous = OFF")
                connection.execute("PRAGMA cache_size = -2048")
                connection.execute("CREATE TABLE members (reference BLOB PRIMARY KEY) WITHOUT ROWID")
                task_digest = OrderedJsonSequenceDigester()
                count = byte_count = 0
                with closing(self._stores.stream_planned_stores(self._handoff.planned_store_ledger)) as references:
                    for reference in references:
                        count += 1
                        if count > self._handoff.expected_task_count or count > self._stores.max_plan_store_count:
                            raise IntegrityError("task membership population exceeds the sealed task count")
                        key = canonical_json_bytes(reference.to_dict())
                        byte_count += len(key)
                        if len(key) > self._stores.max_plan_record_bytes or byte_count > self._stores.max_plan_ledger_bytes:
                            raise LimitExceededError("task membership references exceed the planned-store byte limit")
                        connection.execute("INSERT INTO members(reference) VALUES (?)", (key,))
                        task = StoreTask(
                            self._handoff.processing_plan.artifact_id, self._handoff.operation_id, reference,
                        )
                        task_digest.accept(task.to_dict())
                if count != self._handoff.expected_task_count or task_digest.finish() != self._handoff.task_set_digest:
                    raise IntegrityError("task membership population differs from the sealed execution handoff")
                connection.commit()
        except BaseException as error:
            temporary.cleanup()
            if isinstance(error, sqlite3.Error):
                if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                    raise LimitExceededError("task membership index exceeds its scratch allowance") from error
                raise IntegrityError(f"task membership construction failed: {error}") from error
            raise
        self._temporary = temporary

    def _discard(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def close(self) -> None:
        """Release scratch; another lookup rebuilds from the immutable ledger."""
        with self._lock:
            self._discard()
