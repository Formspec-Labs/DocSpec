"""Bounded catalog row readers and their ordering and placement checks."""

from __future__ import annotations

import heapq
import json
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from rulespec_artifacts import (
    ArtifactVerificationError,
    MemberSource,
    parse_canonical_json,
)

from docspec.adapters.catalog_artifact.rules import (
    _INTERPRETATION_KINDS,
    MAX_CATALOG_ROW_BYTES,
    MAX_SMALL_MEMBER_BYTES,
    _CatalogPartition,
    _partition_id,
    _utf16_key,
)
from docspec.adapters.catalog_artifact.schemas import _ITEM_VALIDATOR
from docspec.domain.identity import require_text, trusted_json_input
from docspec.domain.source_catalog import (
    SourceCatalogCandidate,
    SourceCatalogItem,
    SourceCatalogSelection,
    _require_candidate_renditions,
)
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.source_catalog import (
    LocatedSourceCatalogItem,
    LocatedSourceCatalogMapping,
    SourceCatalogBlobSource,
)


def _require_interpretation_order(row: Mapping[str, Any]) -> None:
    kinds = tuple(value["interpretationKind"] for value in row["interpretations"])
    if kinds != _INTERPRETATION_KINDS:
        raise IntegrityError("source-catalog interpretations differ from the closed ordered kind set")


def _read_small(source: MemberSource, key: str) -> bytes:
    with source.open(key) as stream:
        payload = stream.read(MAX_SMALL_MEMBER_BYTES + 1)
    if len(payload) > MAX_SMALL_MEMBER_BYTES:
        raise LimitExceededError(f"{key} exceeds its {MAX_SMALL_MEMBER_BYTES}-byte limit")
    return payload


def _iter_partition_rows(
    blob_source: SourceCatalogBlobSource,
    partition: _CatalogPartition,
    *,
    validate: bool = True,
    with_raw: bool = False,
    as_dict: bool = False,
) -> Iterator[Any]:
    member = partition.member
    if member.blob_ref is None or member.record_count is None:
        raise IntegrityError("source-item partition descriptor requires blobRef and recordCount")
    with blob_source.open(member.blob_ref) as stream:
        yield from _iter_partition_stream(
            stream,
            partition_id=partition.partition_id,
            record_count=member.record_count,
            validate=validate,
            with_raw=with_raw,
            as_dict=as_dict,
        )


def _iter_partition_stream(
    stream: Any,
    *,
    partition_id: str,
    record_count: int,
    validate: bool,
    with_raw: bool,
    as_dict: bool = False,
) -> Iterator[Any]:
    previous: bytes | None = None
    count = 0
    while raw := stream.readline(MAX_CATALOG_ROW_BYTES + 2):
        if len(raw) > MAX_CATALOG_ROW_BYTES + 1:
            raise LimitExceededError("source-catalog row exceeds its byte limit")
        if not raw.endswith(b"\n"):
            raise IntegrityError("source-catalog rows must end with a newline")
        if validate:
            try:
                value = parse_canonical_json(
                    raw[:-1],
                    path=f"source-items/{partition_id}/{count}",
                )
            except ArtifactVerificationError as error:
                raise IntegrityError(f"source-catalog row {count} is not canonical: {error}") from error
            _ITEM_VALIDATOR.error(value, f"source-catalog row {count}")
        else:
            # An unvalidated pass only re-reads bytes that a validated pass of
            # the same derivation (or the producer gate) proves canonical and
            # schema-conformant; plain parsing avoids re-serializing every row.
            value = json.loads(raw[:-1])
        if validate:
            _require_interpretation_order(value)
        if as_dict:
            source_item_id = value["sourceItemId"]
            if not isinstance(source_item_id, str):
                raise IntegrityError(f"source-catalog row {count} is invalid: sourceItemId must be text")
            if validate:
                try:
                    for field in ("sourceItemId", "documentId", "sourceIssuedVersion"):
                        require_text(value[field], field)
                    _require_candidate_renditions(
                        tuple(SourceCatalogCandidate.from_dict(candidate) for candidate in value["candidateRenditions"]),
                        SourceCatalogSelection.from_dict(value["selection"]),
                    )
                except (TypeError, ValueError) as error:
                    raise IntegrityError(f"source-catalog row {count} is invalid: {error}") from error
            item: Any = value
        else:
            try:
                if validate:
                    item = SourceCatalogItem.from_dict(value)
                else:
                    # The bytes were admitted by a verifier already (this is
                    # the unvalidated re-stream); construct by wrapping alone.
                    with trusted_json_input():
                        item = SourceCatalogItem.from_dict(value)
            except (TypeError, ValueError) as error:
                raise IntegrityError(f"source-catalog row {count} is invalid: {error}") from error
            source_item_id = item.source_item_id
        if _partition_id(source_item_id) != partition_id:
            raise IntegrityError("source-catalog row is stored in the wrong logical partition")
        key = _utf16_key(source_item_id)
        if previous is not None and key <= previous:
            raise IntegrityError("source-catalog partition rows must be strictly ordered and distinct")
        previous = key
        count += 1
        yield (item, raw[:-1]) if with_raw else item
    if count != record_count:
        raise IntegrityError("source-catalog row count differs from its partition descriptor")


def _iter_catalog_rows(
    blob_source: SourceCatalogBlobSource,
    partitions: Sequence[_CatalogPartition],
    expected_count: int,
    *,
    validate: bool = True,
    with_raw: bool = False,
    as_dict: bool = False,
) -> Iterator[Any]:
    rows = _iter_located_catalog_rows(
        blob_source,
        partitions,
        expected_count,
        validate=validate,
        with_raw=with_raw,
        as_dict=as_dict,
    )
    if with_raw:
        for located, raw in rows:
            yield located.item, raw
    else:
        for located in rows:
            yield located.item


def _iter_located_catalog_rows(
    blob_source: SourceCatalogBlobSource,
    partitions: Sequence[_CatalogPartition],
    expected_count: int,
    *,
    validate: bool = True,
    with_raw: bool = False,
    as_dict: bool = False,
) -> Iterator[Any]:
    """Attach each parsed row to its supplying partition without reparsing it."""

    streams = [
        iter(_iter_partition_rows(blob_source, partition, validate=validate, with_raw=with_raw, as_dict=as_dict))
        for partition in partitions
    ]
    heap: list[tuple[bytes, int, Any]] = []
    previous: bytes | None = None
    count = 0
    try:

        def entry_id(entry: Any) -> str:
            row = entry[0] if with_raw else entry
            return row["sourceItemId"] if as_dict else row.source_item_id

        for index, stream in enumerate(streams):
            entry = next(stream, None)
            if entry is not None:
                heapq.heappush(heap, (_utf16_key(entry_id(entry)), index, entry))
        while heap:
            key, index, entry = heapq.heappop(heap)
            item = entry[0] if with_raw else entry
            if previous is not None and key <= previous:
                raise IntegrityError("source-catalog rows must be globally ordered and distinct")
            previous = key
            blob_ref = partitions[index].member.blob_ref
            if blob_ref is None:
                raise IntegrityError("source-item partition descriptor requires blobRef")
            count += 1
            located = (
                LocatedSourceCatalogMapping(item, blob_ref)
                if as_dict else LocatedSourceCatalogItem(item, blob_ref)
            )
            yield (located, entry[1]) if with_raw else located
            following = next(streams[index], None)
            if following is not None:
                heapq.heappush(heap, (_utf16_key(entry_id(following)), index, following))
    finally:
        for stream in streams:
            stream.close()
    if count != expected_count:
        raise IntegrityError("source-catalog row count differs from its partition descriptors")
