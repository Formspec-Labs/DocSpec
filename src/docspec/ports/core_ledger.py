"""Bounded metadata ledger for records, links, progress, and removals; publication belongs to application services."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from contextlib import AbstractContextManager
from typing import Any, Protocol

from docspec.domain.core import CoreRecord
from docspec.domain.core_admission import AdmittedRecord
from docspec.domain.references import BlobRef, LayerRef


RecordKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class MetadataLink:
    owner: RecordKey
    relation: str
    label: str
    target: RecordKey


@dataclass(frozen=True, slots=True)
class MetadataBatch:
    """One atomic metadata unit, prepared after application-level checks.

    Merely recording a value does not assert successful retention. Only the
    publisher supplies retained keys, candidate indexes and checked links.
    """

    unit_id: str
    records: tuple[CoreRecord | dict[str, Any] | AdmittedRecord, ...] = ()
    retained: tuple[RecordKey, ...] = ()
    candidates: tuple[tuple[str, str], ...] = ()  # correspondence digest, result ID
    links: tuple[MetadataLink, ...] = ()
    expected_versions: tuple[tuple[RecordKey, int], ...] = ()
    # Internal bulk admission: the supplied entity records already exist as
    # exact canonical rows in this protected layer. SQLite stores their pins.
    record_layer: LayerRef | None = None


@dataclass(frozen=True, slots=True)
class StoredRecord:
    key: RecordKey
    value: CoreRecord | None
    retained: bool
    available: bool
    evidence_version: int
    row_digest: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateMatch:
    request_id: str
    result_id: str
    evidence_version: int


@dataclass(frozen=True, slots=True)
class RemovalContent:
    store: str
    reference: BlobRef


@dataclass(frozen=True, slots=True)
class RemovalOutcome:
    content: RemovalContent
    status: str
    error: str | None = None


class CoreLedger(Protocol):
    """Protected read and commit ledger of Core record metadata, links, progress, selections, and removals."""

    def content_guard(self, *, exclusive: bool = False) -> AbstractContextManager[None]: ...

    def request_guard(self, request_id: str) -> AbstractContextManager[None]: ...

    def commit(self, batch: MetadataBatch) -> bool: ...

    def is_committed(self, unit_id: str) -> bool: ...

    def read_records(self, keys: Iterable[RecordKey], *, include_values: bool = True,
                     include_unavailable_values: bool = False) -> Iterator[tuple[StoredRecord | None, ...]]: ...

    def find_candidates(self, requests: Iterable[tuple[str, str]]) -> Iterator[tuple[CandidateMatch, ...]]: ...

    def read_links(self, keys: Iterable[RecordKey]) -> Iterator[tuple[MetadataLink, ...]]: ...

    def read_dependencies(self, result_ids: Iterable[str]) -> Iterator[tuple[MetadataLink, ...]]: ...

    def affected_results(self, changed: Iterable[RecordKey]) -> Iterator[tuple[str, ...]]: ...

    def producers(self, key: RecordKey) -> tuple[str, ...]: ...

    def executions(self, request_id: str) -> Iterator[tuple[str, ...]]: ...

    def record_progress(self, update_id: str, execution_id: str, status: str, description: dict[str, Any], *, expected_update_id: str | None = None) -> bool: ...

    def read_progress(self, execution_id: str) -> Iterator[tuple[bytes, ...]]: ...

    def recovery_progress(self, *, exclude: Iterable[RecordKey] = ()) -> Iterator[tuple[tuple[str, bytes], ...]]: ...

    def select_current(self, update_id: str, dataset: str, target: RecordKey, expected_current: RecordKey | None) -> bool: ...

    def current(self, dataset: str) -> RecordKey | None: ...

    def retained_records(self, *, kind: str | None = None) -> Iterator[tuple[StoredRecord, ...]]: ...

    def source_layers(self, *, exclude: Iterable[RecordKey] = (), include: Iterable[RecordKey] | None = None,
                      include_unretained: bool = False) -> Iterator[tuple[LayerRef, ...]]: ...

    def removal_blockers(self, keys: Iterable[RecordKey]) -> Iterator[tuple[RecordKey, ...]]: ...

    def begin_removal(self, update_id: str, policy_id: str, keys: Iterable[RecordKey], *, content: Iterable[RemovalContent] = ()) -> bool: ...

    def removal_outcomes(self, update_id: str, *, pending_only: bool = False) -> Iterator[tuple[RemovalOutcome, ...]]: ...

    def removal(self, update_id: str) -> tuple[str, tuple[RecordKey, ...], bool] | None: ...

    def record_removal_outcome(self, update_id: str, outcome: RemovalOutcome) -> None: ...

    def finish_removal(self, update_id: str) -> None: ...

    def pending_removals(self) -> Iterator[tuple[tuple[str, str, RecordKey | None], ...]]: ...

    def close(self) -> None: ...
