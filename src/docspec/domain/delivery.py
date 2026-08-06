"""A bounded, idempotent record stream derived from one DocumentStore."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from docspec.domain.content import (
    AcquisitionDisposition,
    CapturedFile,
    DerivedRecord,
    EvidenceMapping,
    Representation,
    Segment,
    SourceItem,
    SourceItemState,
    resolve_evidence_mapping,
)
from docspec.domain.identity import (
    canonical_json_bytes,
    ordered_json_sequence_digest,
    parse_canonical_json,
    stable_urn,
    thaw_json,
)
from docspec.domain.jobs import ChangeKind, DocumentEntry, DocumentStore, FailureRecord, StoreVerdict
from docspec.domain.references import ArtifactRef, BlobRef
from docspec.domain.storage import RecordSchema
from docspec.errors import IntegrityError

_FIELDS = ("recordId", "sourceItemId", "idempotencyKey", "deleted", "payload")
_CORE_LAYER_SCHEMA_IDS = {
    "source-items": "docspec-source-item-record/1.0",
    "files": "docspec-file-record/1.0",
    "representations": "docspec-representation-record/1.0",
    "segments": "docspec-segment-record/1.0",
    "dispositions": "docspec-disposition-record/1.0",
    "failures": "docspec-failure-record/1.0",
    "receipts": "docspec-stage-receipt-record/1.0",
}
_LOGICAL_CONTENT_LAYERS = frozenset({"source-items", "files", "representations", "segments"})


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    layer_kind: str
    schema: RecordSchema
    record_id: str
    source_item_id: str
    idempotency_key: str
    deleted: bool
    payload: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        return {
            "recordId": self.record_id,
            "sourceItemId": self.source_item_id,
            "idempotencyKey": self.idempotency_key,
            "deleted": self.deleted,
            "payload": self.payload,
        }


class DeliveryAccumulator:
    """Summarize one bounded delivery stream and reject repeated idempotency keys."""

    def __init__(self) -> None:
        self.record_count = 0
        self.byte_count = 0
        self._idempotency = hashlib.sha256()
        self._seen: set[str] = set()

    def add(self, item: DeliveryRecord) -> dict[str, Any]:
        if item.idempotency_key in self._seen:
            raise IntegrityError(f"delivery repeats idempotency key {item.idempotency_key}")
        self._seen.add(item.idempotency_key)
        record = item.to_record()
        self.record_count += 1
        self.byte_count += len(canonical_json_bytes(record))
        self._idempotency.update(item.idempotency_key.encode("utf-8"))
        self._idempotency.update(b"\n")
        return record

    @property
    def digest(self) -> str:
        return f"sha256:{self._idempotency.hexdigest()}"


def summarize_delivery_records(records: Iterator[DeliveryRecord]) -> DeliveryAccumulator:
    """Consume a bounded delivery iterator into its independently checkable summary."""

    summary = DeliveryAccumulator()
    for item in records:
        summary.add(item)
    return summary


def delivery_entry_population(store: DocumentStore) -> tuple[int, str]:
    """Return the ordered, identity-bearing entry population for one store."""

    return len(store.entries), ordered_json_sequence_digest(entry.entry_id for entry in store.entries)


def delivery_store_verdict(store: DocumentStore) -> StoreVerdict:
    """Derive one store verdict from the existing terminal entry dispositions."""

    dispositions = {entry.disposition for entry in store.entries}
    if AcquisitionDisposition.REJECTED_RUN in dispositions:
        return StoreVerdict.REJECTED
    if AcquisitionDisposition.ACCEPTED_FAILURE in dispositions:
        return StoreVerdict.ACCEPTED_FAILURE
    return StoreVerdict.COMPLETED


def record_schema(layer_kind: str, schema_id: str) -> RecordSchema:
    return RecordSchema(schema_id, _FIELDS, "recordId", "sourceItemId")


def core_delivery_schemas() -> dict[str, RecordSchema]:
    """Return the required durable layer schemas, including empty layers."""

    return {kind: record_schema(kind, schema_id) for kind, schema_id in _CORE_LAYER_SCHEMA_IDS.items()}


def _record(
    store: DocumentStore,
    entry: DocumentEntry,
    *,
    layer_kind: str,
    schema_id: str,
    record_id: str,
    payload: dict[str, Any],
    deleted: bool = False,
) -> DeliveryRecord:
    idempotency_key = stable_urn(
        "delivery-record",
        {
            "storeId": store.store_id,
            "entryId": entry.entry_id,
            "layerKind": layer_kind,
            "outputId": record_id,
        },
    )
    return DeliveryRecord(
        layer_kind,
        record_schema(layer_kind, schema_id),
        record_id,
        entry.source_item.item_id,
        idempotency_key,
        deleted,
        payload,
    )


def iter_delivery_records(store: DocumentStore) -> Iterator[DeliveryRecord]:
    """Yield the complete, stable, bounded record stream for one store revision."""

    for entry in store.entries:
        deleted = entry.change.value == "deleted"
        yield _record(
            store,
            entry,
            layer_kind="source-items",
            schema_id="docspec-source-item-record/1.0",
            record_id=entry.source_item.item_id,
            payload=entry.source_item.to_dict(),
            deleted=deleted,
        )
        for item in entry.captured_files:
            yield _record(
                store,
                entry,
                layer_kind="files",
                schema_id="docspec-file-record/1.0",
                record_id=item.file_id,
                payload=item.to_dict(),
            )
        for item in entry.representations:
            yield _record(
                store,
                entry,
                layer_kind="representations",
                schema_id="docspec-representation-record/1.0",
                record_id=item.representation_id,
                payload=item.to_dict(),
            )
        for item in entry.segments:
            yield _record(
                store,
                entry,
                layer_kind="segments",
                schema_id="docspec-segment-record/1.0",
                record_id=item.segment_id,
                payload=item.to_dict(),
            )
        for item in entry.derived_records:
            yield _record(
                store,
                entry,
                layer_kind=f"derived:{item.processor_id}",
                schema_id=item.schema_id,
                record_id=item.derived_id,
                payload=item.to_dict(),
            )
        disposition_id = stable_urn(
            "disposition",
            {
                "entryId": entry.entry_id,
                "disposition": None if entry.disposition is None else entry.disposition.value,
            },
        )
        yield _record(
            store,
            entry,
            layer_kind="dispositions",
            schema_id="docspec-disposition-record/1.0",
            record_id=disposition_id,
            payload={
                "entryId": entry.entry_id,
                "change": entry.change.value,
                "disposition": None if entry.disposition is None else entry.disposition.value,
                "warnings": list(entry.warnings),
            },
        )
        for index, failure in enumerate(entry.failures):
            failure_id = stable_urn(
                "failure",
                {"entryId": entry.entry_id, "index": index, "failure": failure.to_dict()},
            )
            yield _record(
                store,
                entry,
                layer_kind="failures",
                schema_id="docspec-failure-record/1.0",
                record_id=failure_id,
                payload={"entryId": entry.entry_id, **failure.to_dict()},
            )
        for receipt in entry.stage_receipts:
            yield _record(
                store,
                entry,
                layer_kind="receipts",
                schema_id="docspec-stage-receipt-record/1.0",
                record_id=receipt.artifact_id,
                payload={"entryId": entry.entry_id, "artifact": receipt.to_dict()},
            )


def verify_logical_release_layers(
    layers: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    verify_artifact: Callable[[ArtifactRef], None] | None = None,
    verify_blob: Callable[[BlobRef], None] | None = None,
) -> None:
    """Verify identity and lineage across one complete active release.

    Layer members are streamed into a temporary SQLite index. This keeps the
    coordinator's memory bounded while still checking relationships whose
    records live in separate immutable files.
    """

    recognized = {
        kind: rows
        for kind, rows in layers.items()
        if kind in _CORE_LAYER_SCHEMA_IDS or kind.startswith("derived:")
    }
    if not recognized:
        return
    if "source-items" not in recognized and any(
        kind in _LOGICAL_CONTENT_LAYERS or kind.startswith("derived:") for kind in recognized
    ):
        raise IntegrityError("logical release content requires a source-items layer")

    with tempfile.TemporaryDirectory(prefix="docspec-release-integrity-") as directory:
        connection = sqlite3.connect(Path(directory) / "lineage.sqlite3")
        try:
            _create_release_integrity_schema(connection)
            ordered_kinds = sorted(recognized, key=lambda kind: (kind != "source-items", kind))
            for kind in ordered_kinds:
                _index_release_layer(
                    connection,
                    kind,
                    recognized[kind],
                    verify_artifact=verify_artifact,
                    verify_blob=verify_blob,
                )
            connection.commit()
            _verify_release_relationships(connection)
        except sqlite3.IntegrityError as error:
            raise IntegrityError(f"logical release repeats an identity or relationship: {error}") from error
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"logical release contains an invalid domain record: {error}") from error
        finally:
            connection.close()


def _create_release_integrity_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        PRAGMA temp_store = FILE;

        CREATE TABLE delivery_keys (
            idempotency_key TEXT PRIMARY KEY
        ) WITHOUT ROWID;
        CREATE TABLE sources (
            source_item_id TEXT PRIMARY KEY,
            version TEXT NOT NULL,
            state TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE candidates (
            source_item_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            media_type TEXT NOT NULL,
            expected_digest TEXT,
            expected_size INTEGER,
            transport_version TEXT,
            PRIMARY KEY (source_item_id, candidate_id)
        ) WITHOUT ROWID;
        CREATE TABLE files (
            file_id TEXT PRIMARY KEY,
            source_item_id TEXT NOT NULL,
            source_version TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            blob_digest TEXT NOT NULL,
            blob_size INTEGER NOT NULL,
            media_type TEXT NOT NULL,
            transport_version TEXT
        ) WITHOUT ROWID;
        CREATE UNIQUE INDEX files_by_candidate ON files(source_item_id, candidate_id);
        CREATE TABLE representations (
            representation_id TEXT PRIMARY KEY,
            source_item_id TEXT NOT NULL,
            file_id TEXT NOT NULL,
            file_digest TEXT NOT NULL,
            blob_size INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE representation_mappings (
            representation_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            representation_start INTEGER NOT NULL,
            representation_end INTEGER NOT NULL,
            payload BLOB NOT NULL,
            PRIMARY KEY (representation_id, ordinal)
        ) WITHOUT ROWID;
        CREATE INDEX representation_mappings_by_range
            ON representation_mappings(representation_id, representation_start, representation_end);
        CREATE TABLE segments (
            segment_id TEXT PRIMARY KEY,
            source_item_id TEXT NOT NULL,
            file_id TEXT NOT NULL,
            representation_id TEXT NOT NULL,
            representation_start INTEGER NOT NULL,
            representation_end INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            evidence_source_digest TEXT NOT NULL,
            evidence_start INTEGER,
            evidence_end INTEGER
        ) WITHOUT ROWID;
        CREATE UNIQUE INDEX segments_by_ordinal ON segments(representation_id, ordinal);
        CREATE TABLE derived_records (
            derived_id TEXT PRIMARY KEY,
            source_item_id TEXT NOT NULL,
            processor_id TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE derived_inputs (
            derived_id TEXT NOT NULL,
            input_id TEXT NOT NULL,
            PRIMARY KEY (derived_id, input_id)
        ) WITHOUT ROWID;
        CREATE TABLE dispositions (
            source_item_id TEXT PRIMARY KEY,
            record_id TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            disposition TEXT
        ) WITHOUT ROWID;
        """
    )


def _delivery_row(row: Mapping[str, Any], layer_kind: str) -> tuple[str, str, bool, dict[str, Any]]:
    if set(row) != set(_FIELDS):
        raise IntegrityError(f"logical release layer {layer_kind!r} contains a non-delivery record")
    record_id = row["recordId"]
    source_item_id = row["sourceItemId"]
    idempotency_key = row["idempotencyKey"]
    deleted = row["deleted"]
    payload = row["payload"]
    if not all(isinstance(value, str) and value for value in (record_id, source_item_id, idempotency_key)):
        raise IntegrityError(f"logical release layer {layer_kind!r} contains an invalid record identity")
    if not isinstance(deleted, bool) or not isinstance(payload, dict):
        raise IntegrityError(f"logical release layer {layer_kind!r} contains an invalid payload wrapper")
    return record_id, source_item_id, deleted, payload


def _index_release_layer(
    connection: sqlite3.Connection,
    layer_kind: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    verify_artifact: Callable[[ArtifactRef], None] | None,
    verify_blob: Callable[[BlobRef], None] | None,
) -> None:
    for row in rows:
        record_id, source_item_id, deleted, payload = _delivery_row(row, layer_kind)
        connection.execute("INSERT INTO delivery_keys VALUES (?)", (row["idempotencyKey"],))
        if layer_kind == "source-items":
            item = SourceItem.from_dict(payload)
            if record_id != item.item_id or source_item_id != item.item_id:
                raise IntegrityError("source-item record wrapper differs from its payload")
            if deleted != (item.state == SourceItemState.DELETED):
                raise IntegrityError("source-item tombstone differs from its declared state")
            connection.execute(
                "INSERT INTO sources VALUES (?, ?, ?)",
                (item.item_id, item.version, item.state.value),
            )
            connection.executemany(
                "INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (
                        item.item_id,
                        candidate.candidate_id,
                        candidate.media_type,
                        candidate.expected_digest,
                        candidate.expected_size,
                        candidate.transport_version,
                    )
                    for candidate in item.candidates
                ),
            )
        elif layer_kind == "files":
            _require_live_record(deleted, layer_kind)
            captured = CapturedFile.from_dict(payload)
            if record_id != captured.file_id or source_item_id != captured.source_item_id:
                raise IntegrityError("file record wrapper differs from its payload")
            if captured.disposition != AcquisitionDisposition.CAPTURED:
                raise IntegrityError("active file layer contains a non-captured file")
            if verify_blob is not None:
                verify_blob(captured.blob)
            connection.execute(
                "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    captured.file_id,
                    captured.source_item_id,
                    captured.source_version,
                    captured.candidate_id,
                    captured.blob.digest,
                    captured.blob.byte_size,
                    captured.media_type,
                    captured.transport_version,
                ),
            )
        elif layer_kind == "representations":
            _require_live_record(deleted, layer_kind)
            representation = Representation.from_dict(payload)
            if record_id != representation.representation_id or source_item_id != representation.source_item_id:
                raise IntegrityError("representation record wrapper differs from its payload")
            if any(boundary.source_digest != representation.file_digest for boundary in representation.boundaries):
                raise IntegrityError("representation boundary names a different exact source file")
            if verify_blob is not None:
                verify_blob(representation.blob)
            captured_row = connection.execute(
                "SELECT blob_size FROM files WHERE file_id = ?",
                (representation.file_id,),
            ).fetchone()
            if captured_row is not None and any(
                mapping.evidence.end is not None and mapping.evidence.end > captured_row[0]
                for mapping in representation.evidence_mappings
            ):
                raise IntegrityError("representation evidence exceeds its captured file")
            connection.execute(
                "INSERT INTO representations VALUES (?, ?, ?, ?, ?)",
                (
                    representation.representation_id,
                    representation.source_item_id,
                    representation.file_id,
                    representation.file_digest,
                    representation.blob.byte_size,
                ),
            )
            connection.executemany(
                "INSERT INTO representation_mappings VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        representation.representation_id,
                        ordinal,
                        mapping.representation_start,
                        mapping.representation_end,
                        canonical_json_bytes(mapping.to_dict()),
                    )
                    for ordinal, mapping in enumerate(representation.evidence_mappings)
                ),
            )
        elif layer_kind == "segments":
            _require_live_record(deleted, layer_kind)
            segment = Segment.from_dict(payload)
            if record_id != segment.segment_id or source_item_id != segment.source_item_id:
                raise IntegrityError("segment record wrapper differs from its payload")
            if not segment.derivation or segment.derivation[0] != f"representation:{segment.representation_id}":
                raise IntegrityError("segment derivation does not begin at its representation")
            if verify_blob is not None:
                verify_blob(segment.content)
            mapping_row = connection.execute(
                """
                SELECT payload
                FROM representation_mappings
                WHERE representation_id = ?
                  AND representation_start <= ?
                  AND representation_end >= ?
                ORDER BY ordinal
                LIMIT 1
                """,
                (segment.representation_id, segment.representation_start, segment.representation_end),
            ).fetchone()
            if mapping_row is None:
                representation_exists = connection.execute(
                    "SELECT 1 FROM representations WHERE representation_id = ?",
                    (segment.representation_id,),
                ).fetchone()
                if representation_exists is not None:
                    raise IntegrityError("segment has no persisted reversible representation mapping")
            else:
                mapping_value = thaw_json(
                    parse_canonical_json(
                        mapping_row[0],
                        label=f"evidence mapping for {segment.segment_id}",
                        file_form=False,
                    )
                )
                if not isinstance(mapping_value, dict):
                    raise IntegrityError("persisted representation mapping is not an object")
                mapping = EvidenceMapping.from_dict(mapping_value)
                if segment.evidence != resolve_evidence_mapping(
                    mapping,
                    segment.representation_start,
                    segment.representation_end,
                ):
                    raise IntegrityError("segment evidence differs from its persisted representation mapping")
            connection.execute(
                "INSERT INTO segments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    segment.segment_id,
                    segment.source_item_id,
                    segment.file_id,
                    segment.representation_id,
                    segment.representation_start,
                    segment.representation_end,
                    segment.ordinal,
                    segment.evidence.source_digest,
                    segment.evidence.start,
                    segment.evidence.end,
                ),
            )
        elif layer_kind.startswith("derived:"):
            _require_live_record(deleted, layer_kind)
            record = DerivedRecord.from_dict(payload)
            expected_processor = layer_kind.removeprefix("derived:")
            if (
                record_id != record.derived_id
                or source_item_id != record.source_item_id
                or record.processor_id != expected_processor
            ):
                raise IntegrityError("derived record wrapper or layer differs from its payload")
            connection.execute(
                "INSERT INTO derived_records VALUES (?, ?, ?)",
                (record.derived_id, record.source_item_id, record.processor_id),
            )
            connection.executemany(
                "INSERT INTO derived_inputs VALUES (?, ?)",
                ((record.derived_id, input_id) for input_id in record.input_ids),
            )
        elif layer_kind == "dispositions":
            _require_live_record(deleted, layer_kind)
            expected = {"entryId", "change", "disposition", "warnings"}
            if set(payload) != expected or not isinstance(payload["warnings"], list):
                raise IntegrityError("disposition record has an invalid closed payload")
            entry_id = payload["entryId"]
            if not isinstance(entry_id, str) or not entry_id:
                raise IntegrityError("disposition record has an invalid entry identity")
            ChangeKind(payload["change"])
            disposition = payload["disposition"]
            if disposition is not None:
                disposition = AcquisitionDisposition(disposition).value
            if any(not isinstance(warning, str) for warning in payload["warnings"]):
                raise IntegrityError("disposition warnings must be strings")
            expected_id = stable_urn(
                "disposition",
                {"entryId": entry_id, "disposition": disposition},
            )
            if record_id != expected_id:
                raise IntegrityError("disposition record identity differs from its payload")
            connection.execute(
                "INSERT INTO dispositions VALUES (?, ?, ?, ?)",
                (source_item_id, record_id, entry_id, disposition),
            )
        elif layer_kind == "failures":
            _require_live_record(deleted, layer_kind)
            if set(payload) != {
                "entryId",
                "failureClass",
                "diagnosticCode",
                "detail",
                "attempt",
                "retryable",
            }:
                raise IntegrityError("failure record has an invalid closed payload")
            FailureRecord.from_dict({key: value for key, value in payload.items() if key != "entryId"})
            _require_source(connection, source_item_id, "failure")
        elif layer_kind == "receipts":
            _require_live_record(deleted, layer_kind)
            if set(payload) != {"entryId", "artifact"}:
                raise IntegrityError("stage-receipt record has an invalid closed payload")
            artifact = ArtifactRef.from_dict(payload["artifact"])
            if artifact.artifact_id != record_id:
                raise IntegrityError("stage-receipt record identity differs from its artifact")
            _require_source(connection, source_item_id, "stage-receipt")
            if verify_artifact is not None:
                verify_artifact(artifact)


def _require_live_record(deleted: bool, layer_kind: str) -> None:
    if deleted:
        raise IntegrityError(f"logical release layer {layer_kind!r} contains a tombstone")


def _require_source(connection: sqlite3.Connection, source_item_id: str, label: str) -> None:
    found = connection.execute(
        "SELECT 1 FROM sources WHERE source_item_id = ?",
        (source_item_id,),
    ).fetchone()
    if found is None:
        raise IntegrityError(f"{label} record names a missing source item")


def _verify_release_relationships(connection: sqlite3.Connection) -> None:
    checks = (
        (
            "file has no matching active source candidate or source version",
            """
            SELECT f.file_id
            FROM files AS f
            LEFT JOIN sources AS s ON s.source_item_id = f.source_item_id
            LEFT JOIN candidates AS c
              ON c.source_item_id = f.source_item_id AND c.candidate_id = f.candidate_id
            WHERE s.source_item_id IS NULL OR s.state != 'active' OR s.version != f.source_version
               OR c.candidate_id IS NULL OR c.media_type != f.media_type
               OR (c.expected_digest IS NOT NULL AND c.expected_digest != f.blob_digest)
               OR (c.expected_size IS NOT NULL AND c.expected_size != f.blob_size)
               OR (c.transport_version IS NOT NULL AND c.transport_version != f.transport_version)
            LIMIT 1
            """,
        ),
        (
            "representation has broken exact-file lineage",
            """
            SELECT r.representation_id
            FROM representations AS r
            LEFT JOIN files AS f ON f.file_id = r.file_id
            WHERE f.file_id IS NULL OR f.source_item_id != r.source_item_id OR f.blob_digest != r.file_digest
            LIMIT 1
            """,
        ),
        (
            "segment has broken representation or evidence lineage",
            """
            SELECT g.segment_id
            FROM segments AS g
            LEFT JOIN representations AS r ON r.representation_id = g.representation_id
            LEFT JOIN files AS f ON f.file_id = g.file_id
            WHERE r.representation_id IS NULL OR f.file_id IS NULL
               OR r.source_item_id != g.source_item_id OR r.file_id != g.file_id
               OR f.source_item_id != g.source_item_id
               OR g.evidence_source_digest != r.file_digest
               OR g.representation_start < 0 OR g.representation_end > r.blob_size
               OR (g.evidence_end IS NOT NULL AND g.evidence_end > f.blob_size)
            LIMIT 1
            """,
        ),
        (
            "derived record names a missing source item",
            """
            SELECT d.derived_id
            FROM derived_records AS d
            LEFT JOIN sources AS s ON s.source_item_id = d.source_item_id
            WHERE s.source_item_id IS NULL OR s.state != 'active'
            LIMIT 1
            """,
        ),
        (
            "derived record names an unavailable or cross-document input",
            """
            SELECT i.derived_id
            FROM derived_inputs AS i
            JOIN derived_records AS owner ON owner.derived_id = i.derived_id
            LEFT JOIN segments AS g ON g.segment_id = i.input_id
            LEFT JOIN derived_records AS dependency ON dependency.derived_id = i.input_id
            WHERE (g.segment_id IS NULL AND dependency.derived_id IS NULL)
               OR COALESCE(g.source_item_id, dependency.source_item_id) != owner.source_item_id
               OR i.input_id = i.derived_id
            LIMIT 1
            """,
        ),
        (
            "disposition names a missing source item",
            """
            SELECT d.record_id
            FROM dispositions AS d
            LEFT JOIN sources AS s ON s.source_item_id = d.source_item_id
            WHERE s.source_item_id IS NULL
            LIMIT 1
            """,
        ),
    )
    for message, query in checks:
        broken = connection.execute(query).fetchone()
        if broken is not None:
            raise IntegrityError(f"{message}: {broken[0]}")
