"""Snapshot bounded caller records through the existing source-native port."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
import json
from typing import Any

from docspec.application.catalog_policy import utf16_key
from docspec.application.supplied_records_catalog import (
    SUPPLIED_RECORD_SCHEMA_DIGEST,
    SUPPLIED_RECORD_SCHEMA_NAME,
    SUPPLIED_RECORD_SCHEMA_VERSION,
    SUPPLIED_RECORD_SCOPE,
    supplied_item_id,
    supplied_record,
    supplied_renditions,
)
from docspec.domain.identity import canonical_json_bytes, identity_digest, require_text, stable_urn
from docspec.errors import LimitExceededError
from docspec.ports.source_catalog import SourceNativeDescription

from .framing import FramedSectionHasher


@dataclass(frozen=True, slots=True, init=False)
class SuppliedRecordSource:
    """A repeatable snapshot of exactly the records the caller supplied.

    Bounds cover record count and retained canonical record bytes, not arbitrary
    caller object overhead. No document is opened and no collection outcome is
    inferred. ``complete-snapshot`` describes the caller's submitted universe;
    ``observed-crawl`` preserves an explicitly incomplete observed population.
    """

    _description: SourceNativeDescription
    _records: tuple[tuple[str, bytes], ...]

    def __init__(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        source_system_id: str,
        source_system_version: str,
        source_state_scope: str,
        max_records: int,
        max_bytes: int,
    ) -> None:
        """Snapshot and sort the supplied records, refusing duplicate recordIds and limit overruns."""

        require_text(source_system_id, "supplied source system")
        require_text(source_system_version, "supplied source system version")
        if source_state_scope not in {"complete-snapshot", "observed-crawl"}:
            raise ValueError("source_state_scope must be complete-snapshot or observed-crawl")
        if type(max_records) is not int or max_records < 1 or type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("supplied record and byte limits must be positive integers")
        snapshots: list[tuple[str, bytes]] = []
        byte_count = 0
        iterator = iter(records)
        try:
            for value in iterator:
                if len(snapshots) >= max_records:
                    raise LimitExceededError("supplied records exceed their record limit")
                raw = supplied_record(value)
                payload = canonical_json_bytes(raw)
                byte_count += len(payload)
                if byte_count > max_bytes:
                    raise LimitExceededError("supplied records exceed their canonical-byte limit")
                snapshots.append((supplied_item_id(source_system_id, raw["recordId"]), payload))
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()
        snapshots.sort(key=lambda value: utf16_key(value[0]))
        if any(left[0] == right[0] for left, right in zip(snapshots, snapshots[1:])):
            raise ValueError("supplied records repeat a recordId in the same source namespace")
        rows_digest = FramedSectionHasher("docspec-supplied-records/1", "records", len(snapshots))
        for _identifier, payload in snapshots:
            rows_digest.add_payload(payload)
        state_digest = rows_digest.digest()
        schema_set_digest = identity_digest({"schemas": [{
            "schemaName": SUPPLIED_RECORD_SCHEMA_NAME, "schemaVersion": SUPPLIED_RECORD_SCHEMA_VERSION,
            "schemaDigest": SUPPLIED_RECORD_SCHEMA_DIGEST,
        }]})
        identity = {
            "format": "docspec-supplied-source", "formatVersion": "1.0",
            "sourceSystemId": source_system_id, "sourceSystemVersion": source_system_version,
            "sourceStateScope": source_state_scope, "sourceStateDigest": state_digest,
            "sourceNativeSchemaSetDigest": schema_set_digest,
        }
        object.__setattr__(self, "_description", SourceNativeDescription(
            stable_urn("supplied-source", identity), identity_digest(identity),
            source_system_id, source_system_version, source_state_scope, state_digest, schema_set_digest,
        ))
        object.__setattr__(self, "_records", tuple(snapshots))

    def describe(self) -> SourceNativeDescription:
        """Return the snapshot's source-native description."""

        return self._description

    def iter_records(self) -> Iterator[Mapping[str, Any]]:
        """Yield each supplied record in the source-native row shape."""

        for identifier, payload in self._records:
            yield {
                "sourceRecordId": identifier, "scopeId": SUPPLIED_RECORD_SCOPE,
                "schemaName": SUPPLIED_RECORD_SCHEMA_NAME, "schemaVersion": SUPPLIED_RECORD_SCHEMA_VERSION,
                "schemaDigest": SUPPLIED_RECORD_SCHEMA_DIGEST, "record": json.loads(payload), "fieldDiagnostics": [],
            }

    def iter_renditions(self) -> Iterator[Mapping[str, Any]]:
        """Yield the supplied records' renditions."""

        for identifier, payload in self._records:
            yield from supplied_renditions(identifier, json.loads(payload))
