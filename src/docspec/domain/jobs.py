"""Bounded DocumentStore jobs, immutable revisions, and terminal receipts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from docspec.domain.content import (
    AcquisitionDisposition,
    CapturedFile,
    DerivedRecord,
    Representation,
    Segment,
    SourceItem,
)
from docspec.domain.identity import identity_digest, require_text, stable_urn
from docspec.domain.plans import StagePolicy, WorkLimits
from docspec.domain.references import ArtifactRef
from docspec.errors import StateTransitionError


class ChangeKind(StrEnum):
    ADDED = "added"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    DELETED = "deleted"
    REPAIR = "repair"
    EXCLUDED = "excluded"


class EntryExecutionMode(StrEnum):
    FULL = "full"
    PROCESSORS_ONLY = "processors-only"


class StoreState(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    SEALED = "sealed"


class StoreVerdict(StrEnum):
    COMPLETED = "completed"
    ACCEPTED_FAILURE = "accepted-failure"
    REJECTED = "rejected"


class FailureClass(StrEnum):
    TRANSIENT_EXTERNAL = "transient-external"
    TRANSIENT_RESOURCE = "transient-resource"
    DETERMINISTIC_INPUT = "deterministic-input"
    POLICY_EXCLUSION = "policy-exclusion"
    ARTIFACT_INTEGRITY = "artifact-integrity"
    IMPLEMENTATION_DEFECT = "implementation-defect"


@dataclass(frozen=True, slots=True)
class FailureRecord:
    failure_class: FailureClass
    diagnostic_code: str
    detail: str
    attempt: int
    retryable: bool

    def __post_init__(self) -> None:
        try:
            failure_class = FailureClass(self.failure_class)
        except ValueError as error:
            raise ValueError("failure_class is not registered") from error
        require_text(self.diagnostic_code, "diagnostic_code")
        require_text(self.detail, "failure detail")
        if self.attempt <= 0:
            raise ValueError("failure attempt must be positive")
        retryable_classes = {FailureClass.TRANSIENT_EXTERNAL, FailureClass.TRANSIENT_RESOURCE}
        if self.retryable != (failure_class in retryable_classes):
            raise ValueError("failure retryability differs from its registered class")
        object.__setattr__(self, "failure_class", failure_class)

    def to_dict(self) -> dict[str, Any]:
        return {
            "failureClass": self.failure_class.value,
            "diagnosticCode": self.diagnostic_code,
            "detail": self.detail,
            "attempt": self.attempt,
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FailureRecord:
        if set(value) != {"failureClass", "diagnosticCode", "detail", "attempt", "retryable"}:
            raise ValueError("failure record has an invalid closed shape")
        return cls(
            FailureClass(value["failureClass"]),
            value["diagnosticCode"],
            value["detail"],
            value["attempt"],
            value["retryable"],
        )


@dataclass(frozen=True, slots=True)
class DocumentEntry:
    entry_id: str
    source_item: SourceItem
    change: ChangeKind
    requested_stages: StagePolicy
    execution_mode: EntryExecutionMode = EntryExecutionMode.FULL
    captured_files: tuple[CapturedFile, ...] = ()
    representations: tuple[Representation, ...] = ()
    segments: tuple[Segment, ...] = ()
    derived_records: tuple[DerivedRecord, ...] = ()
    disposition: AcquisitionDisposition | None = None
    failures: tuple[FailureRecord, ...] = ()
    stage_receipts: tuple[ArtifactRef, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.entry_id, "entry_id")
        try:
            execution_mode = EntryExecutionMode(self.execution_mode)
        except ValueError as error:
            raise ValueError("document entry execution mode is not registered") from error
        object.__setattr__(self, "execution_mode", execution_mode)
        if execution_mode == EntryExecutionMode.PROCESSORS_ONLY and self.change != ChangeKind.REPAIR:
            raise ValueError("processor-only execution is valid only for a repair entry")
        expected = stable_urn(
            "document-entry",
            {
                "sourceItem": self.source_item.to_dict(),
                "change": self.change.value,
                "requestedStages": self.requested_stages.to_dict(),
                "executionMode": self.execution_mode.value,
            },
        )
        if self.entry_id != expected:
            raise ValueError("document entry identity differs")

    @classmethod
    def create(
        cls,
        source_item: SourceItem,
        change: ChangeKind,
        stages: StagePolicy,
        *,
        execution_mode: EntryExecutionMode = EntryExecutionMode.FULL,
    ) -> DocumentEntry:
        identity = {
            "sourceItem": source_item.to_dict(),
            "change": change.value,
            "requestedStages": stages.to_dict(),
            "executionMode": execution_mode.value,
        }
        disposition = None
        if change == ChangeKind.DELETED:
            disposition = AcquisitionDisposition.DELETED
        elif change == ChangeKind.EXCLUDED:
            disposition = AcquisitionDisposition.EXCLUDED
        elif change == ChangeKind.UNCHANGED:
            disposition = AcquisitionDisposition.UNCHANGED
        return cls(
            stable_urn("document-entry", identity),
            source_item,
            change,
            stages,
            execution_mode,
            disposition=disposition,
        )

    @property
    def terminal(self) -> bool:
        return self.disposition is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entryId": self.entry_id,
            "sourceItem": self.source_item.to_dict(),
            "change": self.change.value,
            "requestedStages": self.requested_stages.to_dict(),
            "executionMode": self.execution_mode.value,
            "capturedFiles": [item.to_dict() for item in self.captured_files],
            "representations": [item.to_dict() for item in self.representations],
            "segments": [item.to_dict() for item in self.segments],
            "derivedRecords": [item.to_dict() for item in self.derived_records],
            "disposition": None if self.disposition is None else self.disposition.value,
            "failures": [item.to_dict() for item in self.failures],
            "stageReceipts": [item.to_dict() for item in self.stage_receipts],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DocumentEntry:
        expected = {
            "entryId",
            "sourceItem",
            "change",
            "requestedStages",
            "executionMode",
            "capturedFiles",
            "representations",
            "segments",
            "derivedRecords",
            "disposition",
            "failures",
            "stageReceipts",
            "warnings",
        }
        if set(value) != expected:
            raise ValueError("document entry has an invalid closed shape")
        return cls(
            value["entryId"],
            SourceItem.from_dict(value["sourceItem"]),
            ChangeKind(value["change"]),
            StagePolicy.from_dict(value["requestedStages"]),
            EntryExecutionMode(value["executionMode"]),
            tuple(CapturedFile.from_dict(item) for item in value["capturedFiles"]),
            tuple(Representation.from_dict(item) for item in value["representations"]),
            tuple(Segment.from_dict(item) for item in value["segments"]),
            tuple(DerivedRecord.from_dict(item) for item in value["derivedRecords"]),
            None if value["disposition"] is None else AcquisitionDisposition(value["disposition"]),
            tuple(FailureRecord.from_dict(item) for item in value["failures"]),
            tuple(ArtifactRef.from_dict(item) for item in value["stageReceipts"]),
            tuple(value["warnings"]),
        )


@dataclass(frozen=True, slots=True)
class DocumentStore:
    store_id: str
    plan_id: str
    logical_partition: str
    entries: tuple[DocumentEntry, ...]
    limits: WorkLimits
    state: StoreState = StoreState.PLANNED
    revision: int = 0
    attempts: tuple[str, ...] = ()
    delivery_receipt: ArtifactRef | None = None
    verdict: StoreVerdict | None = None

    def __post_init__(self) -> None:
        require_text(self.store_id, "store_id")
        require_text(self.plan_id, "store plan_id")
        require_text(self.logical_partition, "logical_partition")
        if not self.entries or len(self.entries) > self.limits.max_entries:
            raise ValueError("document store entry population must be non-empty and within its limit")
        entry_ids = [entry.entry_id for entry in self.entries]
        if len(set(entry_ids)) != len(entry_ids):
            raise ValueError("document store entry identities must be distinct")
        expected = stable_urn(
            "document-store",
            {"planId": self.plan_id, "logicalPartition": self.logical_partition, "entryIds": entry_ids},
        )
        if self.store_id != expected:
            raise ValueError("document store identity differs")
        if self.revision < 0:
            raise ValueError("document store revision must be non-negative")
        if self.state == StoreState.SEALED:
            if self.verdict is None or any(not entry.terminal for entry in self.entries):
                raise ValueError("a sealed document store requires a verdict and terminal entries")
        elif self.verdict is not None:
            raise ValueError("only a sealed document store may have a verdict")

    @classmethod
    def planned(
        cls,
        *,
        plan_id: str,
        logical_partition: str,
        entries: tuple[DocumentEntry, ...],
        limits: WorkLimits,
    ) -> DocumentStore:
        identity = {"planId": plan_id, "logicalPartition": logical_partition, "entryIds": [entry.entry_id for entry in entries]}
        return cls(stable_urn("document-store", identity), plan_id, logical_partition, entries, limits)

    def start(self, attempt_id: str) -> DocumentStore:
        if self.state == StoreState.SEALED:
            raise StateTransitionError("a sealed document store cannot start")
        require_text(attempt_id, "attempt_id")
        attempts = self.attempts if attempt_id in self.attempts else (*self.attempts, attempt_id)
        return replace(self, state=StoreState.RUNNING, revision=self.revision + 1, attempts=attempts)

    def checkpoint(self, entries: tuple[DocumentEntry, ...]) -> DocumentStore:
        if self.state != StoreState.RUNNING:
            raise StateTransitionError("only a running document store can checkpoint")
        if tuple(entry.entry_id for entry in entries) != tuple(entry.entry_id for entry in self.entries):
            raise StateTransitionError("a checkpoint cannot change the planned entry population or order")
        return replace(self, entries=entries, revision=self.revision + 1)

    def seal(self, verdict: StoreVerdict, delivery_receipt: ArtifactRef | None) -> DocumentStore:
        if self.state != StoreState.RUNNING:
            raise StateTransitionError("only a running document store can seal")
        if any(not entry.terminal for entry in self.entries):
            raise StateTransitionError("every document entry needs a disposition before sealing")
        if verdict == StoreVerdict.COMPLETED and delivery_receipt is None:
            raise StateTransitionError("a completed store requires a delivery receipt")
        return replace(
            self,
            state=StoreState.SEALED,
            revision=self.revision + 1,
            delivery_receipt=delivery_receipt,
            verdict=verdict,
        )

    @property
    def receipt_digest(self) -> str:
        if self.state != StoreState.SEALED:
            raise StateTransitionError("only a sealed store has a receipt digest")
        return identity_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "docspec-document-store",
            "formatVersion": "1.0",
            "storeId": self.store_id,
            "planId": self.plan_id,
            "logicalPartition": self.logical_partition,
            "entries": [entry.to_dict() for entry in self.entries],
            "limits": self.limits.to_dict(),
            "state": self.state.value,
            "revision": self.revision,
            "attempts": list(self.attempts),
            "deliveryReceipt": None if self.delivery_receipt is None else self.delivery_receipt.to_dict(),
            "verdict": None if self.verdict is None else self.verdict.value,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DocumentStore:
        expected = {
            "format",
            "formatVersion",
            "storeId",
            "planId",
            "logicalPartition",
            "entries",
            "limits",
            "state",
            "revision",
            "attempts",
            "deliveryReceipt",
            "verdict",
        }
        if set(value) != expected or value["format"] != "docspec-document-store" or value["formatVersion"] != "1.0":
            raise ValueError("document store has an unknown format or invalid closed shape")
        return cls(
            value["storeId"],
            value["planId"],
            value["logicalPartition"],
            tuple(DocumentEntry.from_dict(item) for item in value["entries"]),
            WorkLimits.from_dict(value["limits"]),
            StoreState(value["state"]),
            value["revision"],
            tuple(value["attempts"]),
            None if value["deliveryReceipt"] is None else ArtifactRef.from_dict(value["deliveryReceipt"]),
            None if value["verdict"] is None else StoreVerdict(value["verdict"]),
        )
