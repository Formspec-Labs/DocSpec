"""Requested stages and the terminal outcome of one retained source item."""

from __future__ import annotations

from typing import Any

from docspec.domain.content import AcquisitionDisposition
from docspec.domain.jobs import ChangeKind, DocumentEntry, FailureRecord
from docspec.domain.plans import StagePolicy
from docspec.errors import IntegrityError

DISPOSITION_SCHEMA_ID = "docspec-disposition-record/3.0"
FAILED_DISPOSITIONS = frozenset({
    AcquisitionDisposition.ACCEPTED_FAILURE.value,
    AcquisitionDisposition.REJECTED_RUN.value,
})


def disposition_payload(entry: DocumentEntry) -> dict[str, Any]:
    """Preserve the final failure independently of unordered historical rows."""
    disposition = None if entry.disposition is None else entry.disposition.value
    terminal = None
    if disposition in FAILED_DISPOSITIONS:
        if not entry.failures:
            raise IntegrityError("a failed disposition requires its terminal failure")
        terminal = entry.failures[-1].to_dict()
    return {
        "entryId": entry.entry_id, "change": entry.change.value,
        "requestedStages": entry.requested_stages.to_dict(), "disposition": disposition,
        "warnings": list(entry.warnings), "terminalFailure": terminal,
    }


def parse_disposition_payload(value: dict[str, Any]) -> tuple[StagePolicy, FailureRecord | None]:
    """Read the same closed outcome shape in planning, inspection, and reuse."""
    if not isinstance(value, dict) or set(value) != {
        "entryId", "change", "requestedStages", "disposition", "warnings", "terminalFailure",
    }:
        raise IntegrityError("disposition record has an invalid closed payload")
    if not isinstance(value["entryId"], str) or not value["entryId"]:
        raise IntegrityError("disposition record has an invalid entry identity")
    if not isinstance(value["warnings"], list) or any(not isinstance(warning, str) for warning in value["warnings"]):
        raise IntegrityError("disposition warnings must be strings")
    try:
        ChangeKind(value["change"])
        if value["disposition"] is not None:
            AcquisitionDisposition(value["disposition"])
        stages = StagePolicy.from_dict(value["requestedStages"])
        terminal = None if value["terminalFailure"] is None else FailureRecord.from_dict(value["terminalFailure"])
    except (TypeError, ValueError, KeyError) as error:
        raise IntegrityError(f"disposition record is invalid: {error}") from error
    if (value["disposition"] in FAILED_DISPOSITIONS) != (terminal is not None):
        raise IntegrityError("terminal failure differs from the final disposition")
    return stages, terminal
