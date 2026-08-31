"""Build and open one complete immutable DocSpec ``SourceCatalog`` snapshot."""

from __future__ import annotations

import hashlib
import heapq
import struct
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any

import jsonschema
from rulespec_artifacts import (
    ROOT_OBJECT_KEY,
    ArtifactInput,
    ArtifactPin,
    ArtifactVerificationError,
    FramedSection,
    MemberDescriptor,
    MemberManifestReference,
    MemberSource,
    Producer,
    Supersedes,
    VerifiedArtifact,
    admit_artifact,
    build_artifact_root,
    canonical_json_bytes,
    describe_member_from_receipt,
    framed_section_digest,
    iter_member_descriptors,
    parse_canonical_json,
    schema_bundle_digest,
    sha256_digest,
)

from docspec.domain.references import SourceCatalogRef
from docspec.domain.identity import require_sha256, require_text
from docspec.domain.storage import partition_bucket
from docspec.domain.source_catalog import (
    CatalogDisposition,
    SOURCE_CATALOG_ITEM_SCHEMA_ID,
    SOURCE_CATALOG_MAX_JOIN_IDS,
    SOURCE_CATALOG_POLICY_SCHEMA_ID,
    SOURCE_CATALOG_RECEIPT_SCHEMA_ID,
    SourceCatalogItem,
    source_catalog_schemas,
)
from docspec.errors import IntegrityError, LimitExceededError
from docspec.ports.source_catalog import (
    CatalogPolicyInputs,
    CatalogPolicyWorkspace,
    ImmutableSourceCatalogReader,
    LocatedSourceCatalogItem,
    SourceInputSelector,
    SourceCatalogPolicy,
    SourceCatalogBlobSource,
    SourceCatalogSnapshot,
    SourceCatalogSnapshotSummary,
    SourceCatalogSuccession,
    SourceCatalogStore,
    SourceNativeDescription,
    SourceNativeRecordSource,
    SourceNativeRow,
)

CATALOG_KIND = "docspec-source-catalog"
CATALOG_POLICY_KEY = "catalog-policy.json"
CATALOG_RECEIPT_KEY = "catalog-build-receipt.json"
CATALOG_MANIFEST_KEY = "manifests/catalog.json"
CATALOG_POLICY_ROLE = "catalog-policy"
CATALOG_ITEMS_ROLE = "source-items"
CATALOG_RECEIPT_ROLE = "catalog-build-receipt"
CATALOG_ITEMS_MEDIA_TYPE = "application/x-ndjson"
CATALOG_JSON_MEDIA_TYPE = "application/json"
CATALOG_POLICY_FORMAT = "docspec-catalog-policy"
CATALOG_RECEIPT_FORMAT = "docspec-source-catalog-build-receipt"
CATALOG_FORMAT_VERSION = "1.0"
MAX_CATALOG_ROW_BYTES = 4 * 1024 * 1024
MAX_SMALL_MEMBER_BYTES = 1024 * 1024
MAX_SOURCE_RENDITIONS_PER_RECORD = 1024
MAX_SOURCE_RENDITION_BYTES_PER_RECORD = 4 * 1024 * 1024
CATALOG_PARTITION_POLICY_ID = "urn:docspec:partition-policy:source-item-sha256:1"
CATALOG_PARTITION_POLICY_VERSION = "1.0.0"
CATALOG_PARTITION_BUCKET_COUNT = 64
_UNIVERSE_ACCOUNTING_NAMESPACE = "docspec-internal/universe"
_OUTPUT_ACCOUNTING_NAMESPACE = "docspec-internal/output"
_OUTPUT_PARTITION_NAMESPACE_PREFIX = "docspec-internal/output-partition/"
_SOURCE_ROW_NAMESPACE_PREFIX = "docspec-internal/source-rows/"
_DIAGNOSTIC_DIGEST_FIELDS = (
    "normalizedFieldsDigest",
    "joinedFieldsDigest",
    "dispositionsDigest",
    "reasonsDigest",
    "interpretationsDigest",
    "renditionChoicesDigest",
)
_INTERPRETATION_KINDS = (
    "exact-join",
    "normalization",
    "rendition-preference",
    "sampling",
    "selection",
    "topic-recovery",
)

_CATALOG_SPEC_FIELDS = {
    "catalogId",
    "catalogSchemaDigest",
    "sourceSystemSetDigest",
    "sourceNativeSchemaSetDigest",
    "selectionPolicyId",
    "selectionPolicyVersion",
    "selectionPolicyDigest",
    "requestedUniverseSetDigest",
    "selectedSourceSetDigest",
    "catalogStateDigest",
}
_SOURCE_RECORD_FIELDS = {
    "sourceRecordId",
    "scopeId",
    "schemaName",
    "schemaVersion",
    "schemaDigest",
    "record",
    "fieldDiagnostics",
}
_SOURCE_RENDITION_REQUIRED_FIELDS = {
    "sourceRecordId",
    "renditionId",
    "sourceField",
    "locator",
    "mediaType",
    "expectedSha256",
    "expectedByteSize",
}
_SCHEMAS = source_catalog_schemas()
_ITEM_VALIDATOR = jsonschema.Draft202012Validator(_SCHEMAS["source-item.schema.json"])
_POLICY_VALIDATOR = jsonschema.Draft202012Validator(_SCHEMAS["catalog-policy.schema.json"])
_RECEIPT_VALIDATOR = jsonschema.Draft202012Validator(_SCHEMAS["catalog-build-receipt.schema.json"])


def source_catalog_producer(
    *,
    implementation_id: str,
    verifier_id: str,
    verifier_version: str,
    verifier_implementation_id: str,
) -> Producer:
    """Validate standard immutable implementation identities at the outer edge."""

    return Producer.from_dict(
        {
            "product": "docspec",
            "implementationId": implementation_id,
            "verifierId": verifier_id,
            "verifierVersion": verifier_version,
            "verifierImplementationId": verifier_implementation_id,
        },
        path="source-catalog/producer",
    )


@dataclass(frozen=True, slots=True)
class SourceCatalogBuildRequest:
    catalog_id: str
    producer: Producer
    supersedes: Supersedes | None = None

    def __post_init__(self) -> None:
        require_text(self.catalog_id, "source catalog series catalog_id")
        if self.supersedes is not None:
            if not isinstance(self.supersedes, Supersedes):
                raise TypeError("source catalog supersedes must use Rulespec Supersedes")
            Supersedes.from_dict(self.supersedes.as_dict(), path="source-catalog/supersedes")
            require_text(self.supersedes.reason, "source catalog supersedes reason")


@dataclass(frozen=True, slots=True)
class SourceCatalogBuildResult:
    reference: SourceCatalogRef
    summary: SourceCatalogSnapshotSummary
    byte_measurements: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class _CatalogPartition:
    partition_id: str
    member: MemberDescriptor

    def to_receipt(self) -> dict[str, object]:
        if self.member.blob_ref is None or self.member.record_count is None:
            raise ValueError("source-item partitions require blobRef and recordCount")
        return {
            "partitionId": self.partition_id,
            "blobRef": self.member.blob_ref,
            "byteSize": self.member.byte_size,
            "recordCount": self.member.record_count,
        }


def _partition_policy() -> dict[str, object]:
    identity = {
        "policyId": CATALOG_PARTITION_POLICY_ID,
        "policyVersion": CATALOG_PARTITION_POLICY_VERSION,
        "bucketCount": CATALOG_PARTITION_BUCKET_COUNT,
    }
    return {**identity, "policyDigest": sha256_digest(canonical_json_bytes(identity))}


def _partition_id(source_item_id: str) -> str:
    bucket = partition_bucket(source_item_id, CATALOG_PARTITION_BUCKET_COUNT)
    return f"{bucket:04d}"


def _partition_namespace(partition_id: str) -> str:
    return f"{_OUTPUT_PARTITION_NAMESPACE_PREFIX}{partition_id}"


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IntegrityError(f"{label} must be an object")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise IntegrityError(f"{label} must be nonempty text")
    return value


def _utf16_key(value: str) -> bytes:
    """Use the shared artifact ordering rule for DocSpec-owned row keys."""

    try:
        return value.encode("utf-16-be")
    except UnicodeEncodeError as error:
        raise IntegrityError("catalog identity contains a lone Unicode surrogate") from error


def _require_interpretation_order(item: SourceCatalogItem) -> None:
    kinds = tuple(value["interpretationKind"] for value in item.to_dict()["interpretations"])
    if kinds != _INTERPRETATION_KINDS:
        raise IntegrityError("source-catalog interpretations differ from the closed ordered kind set")


def _read_small(source: MemberSource, key: str) -> bytes:
    with source.open(key) as stream:
        payload = stream.read(MAX_SMALL_MEMBER_BYTES + 1)
    if len(payload) > MAX_SMALL_MEMBER_BYTES:
        raise LimitExceededError(f"{key} exceeds its {MAX_SMALL_MEMBER_BYTES}-byte limit")
    return payload


def _schema_error(validator: jsonschema.Draft202012Validator, value: object, label: str) -> None:
    try:
        validator.validate(value)
    except jsonschema.ValidationError as error:
        path = "/".join(str(part) for part in error.absolute_path) or "$"
        raise IntegrityError(f"{label} schema failure at {path}: {error.message}") from error


class _FramedSectionHasher:
    """Incrementally reproduce ``framed_section_digest`` for one known-count section.

    Byte-for-byte the same protocol Rulespec's batch function seals -- domain,
    NUL, u64 name length, name, u64 count, then u64 payload length + payload per
    record -- so many digests can share one pass over the rows instead of each
    demanding its own. Equality with the batch function is pinned by test.
    """

    __slots__ = ("_digest", "_domain", "_name", "_count", "_observed")

    def __init__(self, domain: str, name: str, count: int) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise IntegrityError(f"cannot compute {domain}: section count must be a non-negative integer")
        self._digest = hashlib.sha256(domain.encode("utf-8") + b"\0")
        name_bytes = name.encode("utf-8")
        self._digest.update(struct.pack(">Q", len(name_bytes)))
        self._digest.update(name_bytes)
        self._digest.update(struct.pack(">Q", count))
        self._domain = domain
        self._name = name
        self._count = count
        self._observed = 0

    def add_payload(self, payload: bytes) -> None:
        self._observed += 1
        if self._observed > self._count:
            raise IntegrityError(
                f"cannot compute {self._domain}: section {self._name!r} exceeds its declared count"
            )
        self._digest.update(struct.pack(">Q", len(payload)))
        self._digest.update(payload)

    def add(self, record: Mapping[str, object]) -> None:
        self.add_payload(canonical_json_bytes(record))

    def digest(self) -> str:
        if self._observed != self._count:
            raise IntegrityError(
                f"cannot compute {self._domain}: section {self._name!r} declared "
                f"{self._count} records but yielded {self._observed}"
            )
        return "sha256:" + self._digest.hexdigest()


def _iter_partition_rows(
    blob_source: SourceCatalogBlobSource,
    partition: _CatalogPartition,
    *,
    validate: bool = True,
    with_raw: bool = False,
) -> Iterator[Any]:
    member = partition.member
    if member.blob_ref is None or member.record_count is None:
        raise IntegrityError("source-item partition descriptor requires blobRef and recordCount")
    previous: str | None = None
    count = 0
    with blob_source.open(member.blob_ref) as stream:
        while raw := stream.readline(MAX_CATALOG_ROW_BYTES + 2):
            if len(raw) > MAX_CATALOG_ROW_BYTES + 1:
                raise LimitExceededError("source-catalog row exceeds its byte limit")
            if not raw.endswith(b"\n"):
                raise IntegrityError("source-catalog rows must end with a newline")
            try:
                value = parse_canonical_json(
                    raw[:-1],
                    path=f"source-items/{partition.partition_id}/{count}",
                )
            except ArtifactVerificationError as error:
                raise IntegrityError(f"source-catalog row {count} is not canonical: {error}") from error
            if validate:
                _schema_error(_ITEM_VALIDATOR, value, f"source-catalog row {count}")
            try:
                item = SourceCatalogItem.from_dict(value)
            except (TypeError, ValueError) as error:
                raise IntegrityError(f"source-catalog row {count} is invalid: {error}") from error
            if validate:
                _require_interpretation_order(item)
            if _partition_id(item.source_item_id) != partition.partition_id:
                raise IntegrityError("source-catalog row is stored in the wrong logical partition")
            if previous is not None and _utf16_key(item.source_item_id) <= _utf16_key(previous):
                raise IntegrityError("source-catalog partition rows must be strictly ordered and distinct")
            previous = item.source_item_id
            count += 1
            yield (item, raw[:-1]) if with_raw else item
    if count != member.record_count:
        raise IntegrityError("source-catalog row count differs from its partition descriptor")


def _iter_catalog_rows(
    blob_source: SourceCatalogBlobSource,
    partitions: Sequence[_CatalogPartition],
    expected_count: int,
    *,
    validate: bool = True,
    with_raw: bool = False,
) -> Iterator[Any]:
    rows = _iter_located_catalog_rows(
        blob_source, partitions, expected_count, validate=validate, with_raw=with_raw
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
) -> Iterator[Any]:
    """Attach each parsed row to its supplying partition without reparsing it."""

    streams = [
        iter(_iter_partition_rows(blob_source, partition, validate=validate, with_raw=with_raw))
        for partition in partitions
    ]
    heap: list[tuple[bytes, int, Any]] = []
    previous: str | None = None
    count = 0
    try:
        for index, stream in enumerate(streams):
            entry = next(stream, None)
            if entry is not None:
                item = entry[0] if with_raw else entry
                heapq.heappush(heap, (_utf16_key(item.source_item_id), index, entry))
        while heap:
            _, index, entry = heapq.heappop(heap)
            item = entry[0] if with_raw else entry
            if previous is not None and _utf16_key(item.source_item_id) <= _utf16_key(previous):
                raise IntegrityError("source-catalog rows must be globally ordered and distinct")
            previous = item.source_item_id
            blob_ref = partitions[index].member.blob_ref
            if blob_ref is None:
                raise IntegrityError("source-item partition descriptor requires blobRef")
            count += 1
            if with_raw:
                yield LocatedSourceCatalogItem(item, blob_ref), entry[1]
            else:
                yield LocatedSourceCatalogItem(item, blob_ref)
            following = next(streams[index], None)
            if following is not None:
                following_item = following[0] if with_raw else following
                heapq.heappush(heap, (_utf16_key(following_item.source_item_id), index, following))
    finally:
        for stream in streams:
            stream.close()
    if count != expected_count:
        raise IntegrityError("source-catalog row count differs from its partition descriptors")


def _framed_digest(domain: str, name: str, count: int, records: Iterable[object]) -> str:
    try:
        return framed_section_digest(domain, (FramedSection(name, count, records),))
    except (TypeError, ValueError) as error:
        raise IntegrityError(f"cannot compute {domain}: {error}") from error


def requested_universe_set_digest(
    count: int,
    sorted_source_item_ids: Iterable[str],
) -> str:
    """Digest one bounded, UTF-16-ordered requested-universe identity stream."""

    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("requested-universe count must be a non-negative integer")

    def records() -> Iterator[Mapping[str, str]]:
        previous: str | None = None
        for raw_identity in sorted_source_item_ids:
            identity = _text(raw_identity, "requested-universe sourceItemId")
            if previous is not None and _utf16_key(identity) <= _utf16_key(previous):
                raise IntegrityError("requested-universe identities must be sorted and distinct")
            previous = identity
            yield {"sourceItemId": identity}

    return _framed_digest("docspec-requested-universe-set/1", "members", count, records())


def selected_source_set_digest(
    count: int,
    sorted_members: Iterable[tuple[str, str]],
) -> str:
    """Digest one bounded, UTF-16-ordered selected source/document stream."""

    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("selected-source count must be a non-negative integer")

    def records() -> Iterator[Mapping[str, str]]:
        previous: tuple[bytes, bytes] | None = None
        for raw_source_item_id, raw_document_id in sorted_members:
            source_item_id = _text(raw_source_item_id, "selected-source sourceItemId")
            document_id = _text(raw_document_id, "selected-source documentId")
            key = (_utf16_key(source_item_id), _utf16_key(document_id))
            if previous is not None and key <= previous:
                raise IntegrityError("selected-source members must be sorted and distinct")
            previous = key
            yield {"sourceItemId": source_item_id, "documentId": document_id}

    return _framed_digest("docspec-selected-source-set/1", "members", count, records())


def _source_system_set_digest(descriptions: Sequence[SourceNativeDescription]) -> str:
    rows = tuple(
        sorted(
            (
                {
                    "sourceSystemId": value.source_system_id,
                    "sourceSystemVersion": value.source_system_version,
                    "logicalDigest": value.logical_id.rsplit(":", 1)[-1],
                    "sourceStateScope": value.source_state_scope,
                    "sourceStateDigest": value.source_state_digest,
                    "sourceNativeSchemaSetDigest": value.source_native_schema_set_digest,
                }
                for value in descriptions
            ),
            key=lambda value: (
                _utf16_key(value["sourceSystemId"]),
                _utf16_key(value["sourceSystemVersion"]),
                _utf16_key(value["logicalDigest"]),
            ),
        )
    )
    keys = [
        (value["sourceSystemId"], value["sourceSystemVersion"], value["logicalDigest"])
        for value in rows
    ]
    if len(keys) != len(set(keys)):
        raise IntegrityError("source-native inputs contain a duplicate logical source system")
    return _framed_digest("docspec-source-system-set/1", "sources", len(rows), rows)


def _source_schema_set_digest(descriptions: Sequence[SourceNativeDescription]) -> str:
    rows = tuple(
        sorted(
            (
                {
                    "sourceSystemId": value.source_system_id,
                    "sourceSystemVersion": value.source_system_version,
                    "sourceNativeSchemaSetDigest": value.source_native_schema_set_digest,
                }
                for value in descriptions
            ),
            key=lambda value: (
                _utf16_key(value["sourceSystemId"]),
                _utf16_key(value["sourceSystemVersion"]),
                _utf16_key(value["sourceNativeSchemaSetDigest"]),
            ),
        )
    )
    return _framed_digest("docspec-source-native-schema-set/1", "schemas", len(rows), rows)


def _source_rows(source: SourceNativeRecordSource) -> Iterator[tuple[Mapping[str, Any], tuple[Mapping[str, Any], ...]]]:
    renditions = iter(source.iter_renditions())
    next_rendition = next(renditions, None)
    previous_record_id: str | None = None
    previous_rendition_key: tuple[str, str] | None = None

    def checked_rendition(value: object) -> tuple[Mapping[str, Any], tuple[str, str]]:
        item = _mapping(value, "source-native rendition")
        fields = set(item)
        if fields != _SOURCE_RENDITION_REQUIRED_FIELDS:
            raise IntegrityError("source-native rendition has an invalid closed shape")
        key = (
            _text(item["sourceRecordId"], "source-native rendition sourceRecordId"),
            _text(item["renditionId"], "source-native rendition renditionId"),
        )
        _text(item["sourceField"], "source-native rendition sourceField")
        _text(item["mediaType"], "source-native rendition mediaType")
        locator = item["locator"]
        if locator is not None:
            _text(locator, "source-native rendition locator")
        expected_digest = item["expectedSha256"]
        if expected_digest is not None:
            try:
                require_sha256(expected_digest, "source-native rendition expectedSha256")
            except ValueError as error:
                raise IntegrityError(str(error)) from error
        expected_size = item["expectedByteSize"]
        if expected_size is not None and (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
        ):
            raise IntegrityError("source-native rendition expectedByteSize must be null or non-negative")
        return item, key

    for raw_record in source.iter_records():
        record = _mapping(raw_record, "source-native record")
        if set(record) != _SOURCE_RECORD_FIELDS:
            raise IntegrityError("source-native record has an invalid closed shape")
        record_id = _text(record["sourceRecordId"], "source-native sourceRecordId")
        _text(record["scopeId"], "source-native scopeId")
        _text(record["schemaName"], "source-native schemaName")
        _text(record["schemaVersion"], "source-native schemaVersion")
        try:
            require_sha256(record["schemaDigest"], "source-native schemaDigest")
        except ValueError as error:
            raise IntegrityError(str(error)) from error
        _mapping(record["record"], "source-native record payload")
        if not isinstance(record["fieldDiagnostics"], list):
            raise IntegrityError("source-native fieldDiagnostics must be an array")
        if previous_record_id is not None and _utf16_key(record_id) <= _utf16_key(previous_record_id):
            raise IntegrityError("source-native records must be strictly ordered by sourceRecordId")
        previous_record_id = record_id
        selected: list[Mapping[str, Any]] = []
        selected_bytes = 0
        while next_rendition is not None:
            rendition, key = checked_rendition(next_rendition)
            key_order = tuple(_utf16_key(part) for part in key)
            previous_order = (
                tuple(_utf16_key(part) for part in previous_rendition_key)
                if previous_rendition_key is not None
                else None
            )
            if previous_order is not None and key_order <= previous_order:
                raise IntegrityError("source-native renditions must be strictly ordered")
            if _utf16_key(key[0]) < _utf16_key(record_id):
                raise IntegrityError("source-native rendition has no matching record")
            if _utf16_key(key[0]) > _utf16_key(record_id):
                break
            previous_rendition_key = key
            if len(selected) >= MAX_SOURCE_RENDITIONS_PER_RECORD:
                raise LimitExceededError(
                    "source-native rendition count exceeds its per-record limit"
                )
            rendition_bytes = len(canonical_json_bytes(rendition))
            if selected_bytes + rendition_bytes > MAX_SOURCE_RENDITION_BYTES_PER_RECORD:
                raise LimitExceededError(
                    "source-native rendition bytes exceed their per-record limit"
                )
            selected_bytes += rendition_bytes
            selected.append(rendition)
            next_rendition = next(renditions, None)
        yield record, tuple(selected)
    if next_rendition is not None:
        raise IntegrityError("source-native rendition has no matching record")


class _CatalogPolicyInputs:
    """Validate each selected source once and account for the complete universe."""

    def __init__(
        self,
        sources: Sequence[SourceNativeRecordSource],
        descriptions: Sequence[SourceNativeDescription],
        universe_inputs: Sequence[SourceInputSelector],
        workspace: CatalogPolicyWorkspace,
    ) -> None:
        self._sources = tuple(sources)
        self._descriptions = tuple(descriptions)
        self._universe_inputs = tuple(universe_inputs)
        if not self._universe_inputs:
            raise ValueError("catalog policy must declare at least one universe input")
        if len(self._universe_inputs) != len(set(self._universe_inputs)):
            raise ValueError("catalog policy universe inputs must be distinct")
        self._workspace = workspace
        self._loaded = False
        self._opened: set[SourceInputSelector] = set()
        self._completed: set[SourceInputSelector] = set()
        self._universe_opened = False

    @property
    def descriptions(self) -> tuple[SourceNativeDescription, ...]:
        return self._descriptions

    @staticmethod
    def _namespace(selector: SourceInputSelector) -> str:
        digest = sha256_digest(canonical_json_bytes(selector.to_dict()))
        return f"{_SOURCE_ROW_NAMESPACE_PREFIX}{digest}"

    def _load(self) -> None:
        if self._loaded:
            return
        for source_index, (source, description) in enumerate(
            zip(self._sources, self._descriptions, strict=True)
        ):
            for record, renditions in _source_rows(source):
                selector = SourceInputSelector(
                    description.source_system_id,
                    description.source_system_version,
                    record["scopeId"],
                    record["schemaName"],
                    record["schemaVersion"],
                )
                try:
                    self._workspace.put(
                        self._namespace(selector),
                        (record["sourceRecordId"],),
                        {
                            "sourceIndex": source_index,
                            "record": dict(record),
                            "renditions": [dict(value) for value in renditions],
                        },
                    )
                except IntegrityError as error:
                    raise IntegrityError(
                        "source-native inputs repeat a sourceRecordId for one policy selector"
                    ) from error
        self._loaded = True

    def _ensure_available(self, selector: SourceInputSelector) -> None:
        if not any(
            description.source_system_id == selector.source_system_id
            and description.source_system_version == selector.source_system_version
            for description in self._descriptions
        ):
            raise IntegrityError("catalog policy source input selector matched no source-native input")

    def _row(self, value: Mapping[str, Any]) -> SourceNativeRow:
        if set(value) != {"sourceIndex", "record", "renditions"}:
            raise IntegrityError("catalog policy workspace source row has an invalid closed shape")
        source_index = value["sourceIndex"]
        if (
            isinstance(source_index, bool)
            or not isinstance(source_index, int)
            or source_index < 0
            or source_index >= len(self._descriptions)
        ):
            raise IntegrityError("catalog policy workspace source index is invalid")
        record = _mapping(value["record"], "catalog policy workspace source record")
        raw_renditions = value["renditions"]
        if not isinstance(raw_renditions, list):
            raise IntegrityError("catalog policy workspace renditions must be an array")
        renditions = tuple(
            _mapping(raw, "catalog policy workspace rendition") for raw in raw_renditions
        )
        return SourceNativeRow(self._descriptions[source_index], record, renditions)

    def _rows(
        self,
        selector: SourceInputSelector,
    ) -> Iterator[SourceNativeRow]:
        self._ensure_available(selector)
        if selector in self._opened:
            raise IntegrityError("catalog policy attempted to open one selected input more than once")
        self._opened.add(selector)
        self._load()
        previous: str | None = None
        for value in self._workspace.iter_ordered(self._namespace(selector)):
            row = self._row(value)
            if (
                row.description.source_system_id != selector.source_system_id
                or row.description.source_system_version != selector.source_system_version
                or row.record["scopeId"] != selector.scope_id
                or row.record["schemaName"] != selector.schema_name
                or row.record["schemaVersion"] != selector.schema_version
            ):
                raise IntegrityError("catalog policy workspace returned a row for another selector")
            source_item_id = row.record["sourceRecordId"]
            if previous is not None and _utf16_key(source_item_id) <= _utf16_key(previous):
                raise IntegrityError("catalog policy workspace source rows are not sorted and distinct")
            previous = source_item_id
            yield row
        self._completed.add(selector)

    def iter_universe_rows(self) -> Iterator[SourceNativeRow]:
        if self._universe_opened:
            raise IntegrityError("catalog policy attempted to open the universe more than once")
        self._universe_opened = True
        streams = [
            iter(self._rows(selector))
            for selector in self._universe_inputs
        ]
        heap: list[tuple[bytes, int, SourceNativeRow]] = []
        try:
            for index, stream in enumerate(streams):
                row = next(stream, None)
                if row is not None:
                    heapq.heappush(
                        heap,
                        (_utf16_key(str(row.record["sourceRecordId"])), index, row),
                    )
            previous: str | None = None
            while heap:
                _, index, row = heapq.heappop(heap)
                source_item_id = str(row.record["sourceRecordId"])
                if previous is not None and _utf16_key(source_item_id) <= _utf16_key(previous):
                    raise IntegrityError(
                        "catalog policy universe sourceItemId values are not globally distinct"
                    )
                previous = source_item_id
                self._workspace.put(
                    _UNIVERSE_ACCOUNTING_NAMESPACE,
                    (source_item_id,),
                    {"sourceItemId": source_item_id},
                )
                yield row
                following = next(streams[index], None)
                if following is not None:
                    heapq.heappush(
                        heap,
                        (
                            _utf16_key(str(following.record["sourceRecordId"])),
                            index,
                            following,
                        ),
                    )
        finally:
            for stream in streams:
                stream.close()

    def iter_lookup_rows(self, selector: SourceInputSelector) -> Iterator[SourceNativeRow]:
        if selector in self._universe_inputs:
            raise IntegrityError("catalog policy lookup input must differ from its universe input")
        yield from self._rows(selector)

    def finish(self) -> None:
        if not self._universe_opened or not set(self._universe_inputs).issubset(self._completed):
            raise IntegrityError("catalog policy did not read every declared universe input")
        if self._opened != self._completed:
            raise IntegrityError("catalog policy did not fully consume every selected source input")


def _policy_rows(
    sources: Sequence[SourceNativeRecordSource],
    descriptions: Sequence[SourceNativeDescription],
    policy: SourceCatalogPolicy,
    policy_digest: str,
    workspace: CatalogPolicyWorkspace,
) -> Iterator[SourceCatalogItem]:
    inputs: CatalogPolicyInputs = _CatalogPolicyInputs(
        sources,
        descriptions,
        policy.universe_inputs,
        workspace,
    )
    for item in policy.iter_items(inputs, workspace):
        for interpretation in item.interpretations:
            if (
                interpretation.get("policyId") != policy.policy_id
                or interpretation.get("policyVersion") != policy.policy_version
                or interpretation.get("policyDigest") != policy_digest
            ):
                raise IntegrityError("catalog interpretation differs from the installed policy pin")
        workspace.put(
            _OUTPUT_ACCOUNTING_NAMESPACE,
            (item.source_item_id,),
            {"sourceItemId": item.source_item_id},
        )
        yield item
    inputs.finish()
    universe = workspace.iter_ordered(_UNIVERSE_ACCOUNTING_NAMESPACE)
    output = workspace.iter_ordered(_OUTPUT_ACCOUNTING_NAMESPACE)
    for expected, actual in zip_longest(universe, output):
        if expected != actual:
            raise IntegrityError("catalog policy output does not account for its complete universe")


class _CatalogRowPartitioner:
    def __init__(self, rows: Iterable[SourceCatalogItem]) -> None:
        self._rows = rows
        self.item_count = 0
        self.disposition_counts = {value.value: 0 for value in CatalogDisposition}
        self.selected_count = 0
        self.partition_counts: dict[str, int] = {}

    def stage(self, workspace: CatalogPolicyWorkspace) -> None:
        previous: str | None = None
        for item in self._rows:
            if previous is not None and _utf16_key(item.source_item_id) <= _utf16_key(previous):
                raise IntegrityError("catalog policy produced duplicate or out-of-order sourceItemId values")
            previous = item.source_item_id
            value = item.to_dict()
            _schema_error(_ITEM_VALIDATOR, value, f"source-catalog row {self.item_count}")
            _require_interpretation_order(item)
            payload = canonical_json_bytes(value)
            if len(payload) > MAX_CATALOG_ROW_BYTES:
                raise LimitExceededError("source-catalog row exceeds its byte limit")
            self.item_count += 1
            self.disposition_counts[item.disposition.value] += 1
            if item.disposition is CatalogDisposition.SELECTED:
                self.selected_count += 1
            selected_partition = _partition_id(item.source_item_id)
            workspace.put(
                _partition_namespace(selected_partition),
                (item.source_item_id,),
                value,
            )
            self.partition_counts[selected_partition] = self.partition_counts.get(selected_partition, 0) + 1

    @staticmethod
    def chunks(workspace: CatalogPolicyWorkspace, partition_id: str) -> Iterator[bytes]:
        for value in workspace.iter_ordered(_partition_namespace(partition_id)):
            yield canonical_json_bytes(value) + b"\n"


def _measure_blob(chunks: Iterable[bytes]) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_size = 0
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise TypeError("source-catalog blob measurements require bytes")
        digest.update(chunk)
        byte_size += len(chunk)
    return "sha256:" + digest.hexdigest(), byte_size


@dataclass(frozen=True, slots=True)
class _DerivedCatalog:
    """Every digest and diagnostic one derivation pass proves about the rows."""

    catalog_state_digest: str
    requested_universe_set_digest: str
    selected_source_set_digest: str
    disposition_counts: dict[str, int]
    diagnostics: dict[str, object]


def _derive_catalog(
    blob_source: SourceCatalogBlobSource,
    partitions: Sequence[_CatalogPartition],
    *,
    item_count: int,
    selected_count: int,
) -> _DerivedCatalog:
    """Derive the catalog's digests and diagnostics in two streamed passes.

    Pass one validates every row exactly once and feeds each fixed-count framed
    digest incrementally; the staged row bytes are proven canonical by the
    parse, and item round-tripping is byte-exact (pinned by test), so the state
    digest frames the raw row bytes instead of re-serializing. The three
    diagnostics whose framed sections declare data-dependent counts are counted
    in pass one and hashed in pass two, which re-reads rows without repeating
    the schema validation pass one already performed. The per-row ordering the
    old per-digest generators re-checked is enforced once, globally, by
    ``_iter_located_catalog_rows``.
    """

    state = _FramedSectionHasher("docspec-source-catalog-state/1", "sourceItems", item_count)
    requested = _FramedSectionHasher("docspec-requested-universe-set/1", "members", item_count)
    selected = _FramedSectionHasher("docspec-selected-source-set/1", "members", selected_count)
    dispositions = _FramedSectionHasher("docspec-catalog-dispositions/1", "records", item_count)
    reasons = _FramedSectionHasher("docspec-catalog-reasons/1", "records", item_count)
    rendition_choices = _FramedSectionHasher(
        "docspec-catalog-rendition-choices/1", "records", item_count
    )
    disposition_counts = {value.value: 0 for value in CatalogDisposition}
    join_counts: dict[str, dict[str, int]] = {}
    normalized_count = 0
    joined_count = 0
    interpretation_count = 0
    for item, raw in _iter_catalog_rows(blob_source, partitions, item_count, with_raw=True):
        state.add_payload(raw)
        requested.add({"sourceItemId": item.source_item_id})
        disposition_counts[item.disposition.value] += 1
        if item.disposition is CatalogDisposition.SELECTED:
            selected.add({"sourceItemId": item.source_item_id, "documentId": item.document_id})
        dispositions.add(
            {"sourceItemId": item.source_item_id, "disposition": item.disposition.value}
        )
        reasons.add({"sourceItemId": item.source_item_id, "reason": item.selection.reason})
        rendition_choices.add(_rendition_choice_record(item))
        for _record in _normalized_field_records_for(item):
            normalized_count += 1
        for record in _joined_field_records_for(item):
            joined_count += 1
            _accumulate_join_coverage(join_counts, record)
        for _record in _interpretation_records_for(item):
            interpretation_count += 1
    normalized = _FramedSectionHasher(
        "docspec-catalog-normalized-fields/1", "records", normalized_count
    )
    joined = _FramedSectionHasher("docspec-catalog-joined-fields/1", "records", joined_count)
    interpretations = _FramedSectionHasher(
        "docspec-catalog-interpretations/1", "records", interpretation_count
    )
    for item in _iter_catalog_rows(blob_source, partitions, item_count, validate=False):
        for record in _normalized_field_records_for(item):
            normalized.add(record)
        for record in _joined_field_records_for(item):
            joined.add(record)
        for record in _interpretation_records_for(item):
            interpretations.add(record)
    join_coverage = [
        {"joinId": join_id, **join_counts[join_id]}
        for join_id in sorted(join_counts, key=_utf16_key)
    ]
    return _DerivedCatalog(
        state.digest(),
        requested.digest(),
        selected.digest(),
        disposition_counts,
        {
            "joinCoverage": join_coverage,
            "normalizedFieldsDigest": normalized.digest(),
            "joinedFieldsDigest": joined.digest(),
            "dispositionsDigest": dispositions.digest(),
            "reasonsDigest": reasons.digest(),
            "interpretationsDigest": interpretations.digest(),
            "renditionChoicesDigest": rendition_choices.digest(),
        },
    )


def _item_interpretations(item: SourceCatalogItem, kind: str) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        value
        for value in item.to_dict()["interpretations"]
        if value["interpretationKind"] == kind
    )


def _normalized_field_records_for(item: SourceCatalogItem) -> Iterator[Mapping[str, Any]]:
    fields: list[Mapping[str, Any]] = []
    for interpretation in _item_interpretations(item, "normalization"):
        fields.extend(interpretation["result"]["fields"])
    previous_key: tuple[bytes, int] | None = None
    for field in sorted(fields, key=lambda value: _utf16_key(value["normalizedField"])):
        field_path = field["normalizedField"]
        for value_index, value in _indexed_values(field["value"]):
            key = (_utf16_key(field_path), value_index)
            if previous_key is not None and key <= previous_key:
                raise IntegrityError("normalized-field diagnostic keys must be ordered and distinct")
            previous_key = key
            yield {
                "sourceItemId": item.source_item_id,
                "fieldPath": field_path,
                "valueIndex": value_index,
                "value": value,
                "diagnostics": {
                    "outcome": field["outcome"],
                    "sourcePaths": field["sourcePaths"],
                    "unparseableValues": field["unparseableValues"],
                    "valueSource": field["valueSource"],
                },
            }


def _indexed_values(value: object) -> Iterator[tuple[int, object]]:
    if isinstance(value, list) and value:
        yield from enumerate(value)
        return
    yield 0, value


def _joined_field_records_for(item: SourceCatalogItem) -> Iterator[Mapping[str, Any]]:
    joins: list[Mapping[str, Any]] = []
    for interpretation in _item_interpretations(item, "exact-join"):
        joins.extend(interpretation["result"]["joins"])
    previous_key: tuple[bytes, bytes, int] | None = None
    for join in sorted(joins, key=lambda value: _utf16_key(value["joinId"])):
        key = (_utf16_key(join["joinId"]), _utf16_key("matchedSourceRecordId"), 0)
        if previous_key is not None and key <= previous_key:
            raise IntegrityError("joined-field diagnostic keys must be ordered and distinct")
        previous_key = key
        yield {
            "sourceItemId": item.source_item_id,
            "joinId": join["joinId"],
            "outputPath": "matchedSourceRecordId",
            "valueIndex": 0,
            "value": join["matchedSourceRecordId"],
            "outcome": join["outcome"],
            "evidence": {
                "lookupScopeId": join["lookupScopeId"],
                "sourceField": join["sourceField"],
                "sourceValue": join["sourceValue"],
            },
        }


def _interpretation_records_for(item: SourceCatalogItem) -> Iterator[Mapping[str, Any]]:
    by_kind: dict[str, list[Mapping[str, Any]]] = {}
    for interpretation in item.to_dict()["interpretations"]:
        by_kind.setdefault(interpretation["interpretationKind"], []).append(interpretation)
    for kind in sorted(by_kind, key=_utf16_key):
        for index, interpretation in enumerate(by_kind[kind]):
            yield {
                "sourceItemId": item.source_item_id,
                "interpretationKind": kind,
                "interpretationId": f"{index:04d}",
                "value": interpretation["result"],
                "diagnostics": {
                    "inputScopeIds": interpretation["inputScopeIds"],
                    "policyDigest": interpretation["policyDigest"],
                    "policyId": interpretation["policyId"],
                    "policyVersion": interpretation["policyVersion"],
                },
            }


def _rendition_choice_record(item: SourceCatalogItem) -> Mapping[str, Any]:
    choices = _item_interpretations(item, "rendition-preference")
    if len(choices) != 1:
        raise IntegrityError("source-catalog row requires one rendition-preference interpretation")
    return {
        "sourceItemId": item.source_item_id,
        "selectedFamilyId": choices[0]["result"]["selectedFamilyId"],
        "candidateIds": [candidate.rendition_id for candidate in item.candidate_renditions],
    }


def _accumulate_join_coverage(
    counts: dict[str, dict[str, int]],
    record: Mapping[str, Any],
) -> None:
    join_id = record["joinId"]
    if not isinstance(join_id, str):
        raise IntegrityError("catalog join identity must be text")
    if join_id not in counts and len(counts) >= SOURCE_CATALOG_MAX_JOIN_IDS:
        raise LimitExceededError("catalog join coverage exceeds its distinct-identity limit")
    selected = counts.setdefault(
        join_id,
        {"eligible": 0, "matched": 0, "unmatched": 0, "nullResult": 0},
    )
    outcome = record["outcome"]
    if outcome == "matched":
        selected["eligible"] += 1
        selected["matched"] += 1
    elif outcome == "no-match":
        selected["eligible"] += 1
        selected["unmatched"] += 1
    elif outcome == "not-stated":
        selected["nullResult"] += 1
    else:
        raise IntegrityError("catalog join outcome is not recognized")


def _source_catalog_succession(value: object) -> SourceCatalogSuccession:
    supersedes = Supersedes.from_dict(value, path="source-catalog/supersedes")
    return SourceCatalogSuccession(
        supersedes.logical_id,
        supersedes.artifact_digest,
        supersedes.reason,
    )


class SourceCatalogArtifactVerifier:
    """Check DocSpec meaning after Rulespec has checked generic structure."""

    def __init__(self, producer: Producer, blob_source: SourceCatalogBlobSource) -> None:
        self._producer = producer
        self._blob_source = blob_source
        self.summary: SourceCatalogSnapshotSummary | None = None
        self.partitions: tuple[_CatalogPartition, ...] = ()
        self.receipt: Mapping[str, Any] | None = None

    def __call__(self, artifact: VerifiedArtifact, source: MemberSource) -> None:
        root = artifact.root
        if root["kind"] != CATALOG_KIND:
            raise IntegrityError("source catalog reference names a different product kind")
        spec = _mapping(root["spec"], "source-catalog spec")
        if set(spec) != _CATALOG_SPEC_FIELDS:
            raise IntegrityError("source-catalog spec has an invalid closed shape")
        if not artifact.inputs or {value.role for value in artifact.inputs} != {"source-native"}:
            raise IntegrityError("source catalog must pin one or more source-native inputs")
        declared_members = tuple(iter_member_descriptors(artifact, source))
        local_members = {
            value.object_key: value for value in declared_members if value.object_key is not None
        }
        item_members = tuple(value for value in declared_members if value.role == CATALOG_ITEMS_ROLE)
        if set(local_members) != {CATALOG_POLICY_KEY, CATALOG_RECEIPT_KEY}:
            raise IntegrityError("source-catalog members differ from the DocSpec product view")
        if len(declared_members) != len(local_members) + len(item_members):
            raise IntegrityError("source-catalog member roles differ from the closed product role set")
        policy_member = local_members[CATALOG_POLICY_KEY]
        receipt_member = local_members[CATALOG_RECEIPT_KEY]
        if (
            policy_member.role != CATALOG_POLICY_ROLE
            or policy_member.media_type != CATALOG_JSON_MEDIA_TYPE
            or policy_member.schema_id != SOURCE_CATALOG_POLICY_SCHEMA_ID
            or policy_member.record_count is not None
            or receipt_member.role != CATALOG_RECEIPT_ROLE
            or receipt_member.media_type != CATALOG_JSON_MEDIA_TYPE
            or receipt_member.schema_id != SOURCE_CATALOG_RECEIPT_SCHEMA_ID
            or receipt_member.record_count is not None
        ):
            raise IntegrityError("source-catalog member descriptions are invalid")
        policy = parse_canonical_json(_read_small(source, CATALOG_POLICY_KEY), path=CATALOG_POLICY_KEY)
        receipt = parse_canonical_json(_read_small(source, CATALOG_RECEIPT_KEY), path=CATALOG_RECEIPT_KEY)
        _schema_error(_POLICY_VALIDATOR, policy, "catalog policy")
        _schema_error(_RECEIPT_VALIDATOR, receipt, "catalog build receipt")
        policy = _mapping(policy, "catalog policy")
        receipt = _mapping(receipt, "catalog build receipt")
        producer = _mapping(root["producer"], "source-catalog producer")
        expected_producer = self._producer.as_dict()
        if producer != expected_producer:
            raise IntegrityError("source-catalog producer differs from the installed implementation")
        comparisons = {
            "catalogId": "catalogId",
            "catalogSchemaDigest": "catalogSchemaDigest",
            "sourceSystemSetDigest": "sourceSystemSetDigest",
            "sourceNativeSchemaSetDigest": "sourceNativeSchemaSetDigest",
            "selectionPolicyId": "selectionPolicyId",
            "selectionPolicyVersion": "selectionPolicyVersion",
            "selectionPolicyDigest": "selectionPolicyDigest",
            "catalogStateDigest": "catalogStateDigest",
            "requestedUniverseSetDigest": "requestedUniverseSetDigest",
            "selectedSourceSetDigest": "selectedSourceSetDigest",
        }
        for receipt_field, spec_field in comparisons.items():
            if receipt[receipt_field] != spec[spec_field]:
                raise IntegrityError(f"catalog build receipt {receipt_field} differs from the root")
        if spec["catalogSchemaDigest"] != schema_bundle_digest(_SCHEMAS):
            raise IntegrityError("source catalog schema digest differs from the installed schema family")
        expected_inputs = [
            {"logicalId": value.logical_id, "artifactDigest": value.artifact_digest}
            for value in artifact.inputs
        ]
        if receipt["sourceNativeInputs"] != expected_inputs:
            raise IntegrityError("catalog build receipt source-native inputs differ from the root")
        if (
            policy["policyId"] != spec["selectionPolicyId"]
            or policy["policyVersion"] != spec["selectionPolicyVersion"]
            or sha256_digest(canonical_json_bytes(policy)) != spec["selectionPolicyDigest"]
        ):
            raise IntegrityError("catalog policy identity differs from the root")
        if (
            receipt["verifierId"] != producer["verifierId"]
            or receipt["verifierVersion"] != producer["verifierVersion"]
            or receipt["verifierImplementationId"] != producer["verifierImplementationId"]
        ):
            raise IntegrityError("catalog build receipt verifier differs from the root producer")
        if receipt["partitionPolicy"] != _partition_policy():
            raise IntegrityError("catalog build receipt partition policy differs from the installed policy")
        partition_rows = receipt["partitions"]
        partitions: list[_CatalogPartition] = []
        previous_partition: str | None = None
        members_by_ref = {value.blob_ref: value for value in item_members}
        if None in members_by_ref or len(members_by_ref) != len(item_members):
            raise IntegrityError("source-item members require distinct blobRef values")
        for raw_partition in partition_rows:
            partition = _mapping(raw_partition, "catalog receipt partition")
            partition_id = _text(partition["partitionId"], "catalog partitionId")
            if (
                previous_partition is not None
                and _utf16_key(partition_id) <= _utf16_key(previous_partition)
            ):
                raise IntegrityError("catalog receipt partitions must be ordered and distinct")
            previous_partition = partition_id
            if (
                len(partition_id) != 4
                or not partition_id.isascii()
                or not partition_id.isdigit()
                or not 0 <= int(partition_id) < CATALOG_PARTITION_BUCKET_COUNT
            ):
                raise IntegrityError("catalog receipt partitionId is outside the installed policy")
            member = members_by_ref.get(partition["blobRef"])
            if member is None:
                raise IntegrityError("catalog receipt partition has no matching source-items member")
            if (
                member.media_type != CATALOG_ITEMS_MEDIA_TYPE
                or member.schema_id != SOURCE_CATALOG_ITEM_SCHEMA_ID
                or member.record_count != partition["recordCount"]
                or member.byte_size != partition["byteSize"]
                or member.object_key is not None
                or member.sha256 is not None
            ):
                raise IntegrityError("source-item partition descriptor differs from its build receipt")
            partitions.append(_CatalogPartition(partition_id, member))
        if {value.member.blob_ref for value in partitions} != set(members_by_ref):
            raise IntegrityError("catalog receipt does not account for every source-items member")
        if receipt["itemCount"] != sum(value.member.record_count or 0 for value in partitions):
            raise IntegrityError("catalog build receipt item count differs from its source-item partitions")
        counts = receipt["dispositionCounts"]
        if sum(counts.values()) != receipt["itemCount"]:
            raise IntegrityError("catalog build receipt dispositions do not account for every row")
        measurements = receipt["byteMeasurements"]
        payload_bytes = sum(value.member.byte_size for value in partitions)
        if (
            measurements["payloadBytesRead"] != payload_bytes
            or measurements["payloadBytesReused"] + measurements["payloadBytesWritten"]
            != payload_bytes
        ):
            raise IntegrityError("catalog build receipt payload byte measurements do not reconcile")
        publication_bytes = (
            policy_member.byte_size
            + receipt_member.byte_size
            + sum(value.byte_size for value in artifact.manifests)
            + len(_read_small(source, ROOT_OBJECT_KEY))
        )
        if measurements["publicationBytesWritten"] != publication_bytes:
            raise IntegrityError("catalog build receipt publication byte measurements do not reconcile")
        previous_join: str | None = None
        for coverage in receipt["joinCoverage"]:
            join_id = coverage["joinId"]
            if previous_join is not None and _utf16_key(join_id) <= _utf16_key(previous_join):
                raise IntegrityError("catalog join coverage must be ordered and distinct")
            previous_join = join_id
            if coverage["eligible"] != coverage["matched"] + coverage["unmatched"]:
                raise IntegrityError("catalog join coverage eligible count does not reconcile")
            if coverage["eligible"] + coverage["nullResult"] > receipt["itemCount"]:
                raise IntegrityError("catalog join coverage exceeds the catalog population")
        self.partitions = tuple(partitions)
        self.receipt = receipt
        self.summary = SourceCatalogSnapshotSummary(
            logical_id=artifact.pin.logical_id,
            artifact_digest=artifact.pin.artifact_digest,
            catalog_id=spec["catalogId"],
            catalog_state_digest=spec["catalogStateDigest"],
            requested_universe_set_digest=spec["requestedUniverseSetDigest"],
            selected_source_set_digest=spec["selectedSourceSetDigest"],
            item_count=receipt["itemCount"],
            disposition_counts=dict(counts),
            partitions=tuple(value.partition_id for value in partitions),
            selection_policy={
                "policyId": spec["selectionPolicyId"],
                "policyVersion": spec["selectionPolicyVersion"],
                "policyDigest": spec["selectionPolicyDigest"],
            },
            partition_policy=dict(receipt["partitionPolicy"]),
            join_coverage=tuple(dict(value) for value in receipt["joinCoverage"]),
            diagnostic_digests={name: receipt[name] for name in _DIAGNOSTIC_DIGEST_FIELDS},
            source_native_inputs=tuple(dict(value) for value in receipt["sourceNativeInputs"]),
            byte_measurements=dict(receipt["byteMeasurements"]),
            succession=(
                None
                if "supersedes" not in root
                else _source_catalog_succession(root["supersedes"])
            ),
        )


class SourceCatalogBuildGateVerifier:
    """Add the producer-only full semantic pass to bounded receipt checks."""

    def __init__(self, producer: Producer, blob_source: SourceCatalogBlobSource) -> None:
        self._producer = producer
        self._blob_source = blob_source
        self.summary: SourceCatalogSnapshotSummary | None = None

    def __call__(self, artifact: VerifiedArtifact, source: MemberSource) -> None:
        receipt_verifier = SourceCatalogArtifactVerifier(self._producer, self._blob_source)
        receipt_verifier(artifact, source)
        summary = receipt_verifier.summary
        receipt = receipt_verifier.receipt
        if summary is None or receipt is None:
            raise RuntimeError("source catalog receipt verifier produced no summary")

        derived = _derive_catalog(
            self._blob_source,
            receipt_verifier.partitions,
            item_count=summary.item_count,
            selected_count=summary.disposition_counts[CatalogDisposition.SELECTED.value],
        )
        computed = {
            "catalogStateDigest": derived.catalog_state_digest,
            "requestedUniverseSetDigest": derived.requested_universe_set_digest,
            "selectedSourceSetDigest": derived.selected_source_set_digest,
        }
        expected = {
            "catalogStateDigest": summary.catalog_state_digest,
            "requestedUniverseSetDigest": summary.requested_universe_set_digest,
            "selectedSourceSetDigest": summary.selected_source_set_digest,
        }
        for name, digest in computed.items():
            if digest != expected[name]:
                raise IntegrityError(f"producer semantic gate recomputed a different {name}")
        if derived.disposition_counts != dict(summary.disposition_counts):
            raise IntegrityError("producer semantic gate recomputed different disposition counts")
        for name, value in derived.diagnostics.items():
            if receipt[name] != value:
                raise IntegrityError(f"producer semantic gate recomputed a different {name}")
        self.summary = summary


class SourceCatalogArtifactReader(ImmutableSourceCatalogReader):
    """Open complete snapshots through an injected immutable member resolver."""

    def __init__(self, store: SourceCatalogStore, *, producer: Producer) -> None:
        self._store = store
        self._producer = producer

    def open_snapshot(self, reference: SourceCatalogRef) -> SourceCatalogSnapshot:
        try:
            source = self._store.source_for(reference)
            blob_source = self._store.blob_source()
            verifier = SourceCatalogArtifactVerifier(self._producer, blob_source)
            admit_artifact(
                source,
                blob_source=blob_source,
                expected_pin=ArtifactPin(reference.catalog_id, reference.digest),
                semantic_verifier=verifier,
            )
        except ArtifactVerificationError as error:
            raise IntegrityError(f"source catalog artifact is invalid: {error}") from error
        if verifier.summary is None:
            raise RuntimeError("source catalog verifier produced no summary")
        return SourceCatalogSnapshot(
            verifier.summary,
            _iter_located_catalog_rows(
                blob_source,
                verifier.partitions,
                verifier.summary.item_count,
            ),
        )

    def verify_snapshot(self, reference: SourceCatalogRef) -> SourceCatalogSnapshotSummary:
        snapshot = self.open_snapshot(reference)
        for _ in snapshot.items:
            pass
        return snapshot.summary


class SourceCatalogBuilder:
    """Create one complete snapshot from injected source, policy, and storage ports."""

    def __init__(
        self,
        *,
        store: SourceCatalogStore,
        policy: SourceCatalogPolicy,
        request: SourceCatalogBuildRequest,
        workspace_factory: Callable[
            [],
            AbstractContextManager[CatalogPolicyWorkspace],
        ],
    ) -> None:
        self._store = store
        self._policy = policy
        self._request = request
        self._workspace_factory = workspace_factory

    def build(self, sources: Sequence[SourceNativeRecordSource]) -> SourceCatalogBuildResult:
        if not sources:
            raise ValueError("a source catalog requires at least one source-native input")
        descriptions = tuple(source.describe() for source in sources)
        policy = {
            "format": CATALOG_POLICY_FORMAT,
            "formatVersion": CATALOG_FORMAT_VERSION,
            "policyId": self._policy.policy_id,
            "policyVersion": self._policy.policy_version,
            "configuration": dict(self._policy.configuration),
        }
        _schema_error(_POLICY_VALIDATOR, policy, "catalog policy")
        policy_bytes = canonical_json_bytes(policy)
        policy_digest = sha256_digest(policy_bytes)
        catalog_schema_digest = schema_bundle_digest(_SCHEMAS)

        with self._workspace_factory() as workspace, self._store.stage() as staging:
            row_partitioner = _CatalogRowPartitioner(
                _policy_rows(
                    sources,
                    descriptions,
                    self._policy,
                    policy_digest,
                    workspace,
                )
            )
            row_partitioner.stage(workspace)
            partitions: list[_CatalogPartition] = []
            payload_bytes_reused = 0
            payload_bytes_written = 0
            for partition_id in sorted(row_partitioner.partition_counts, key=_utf16_key):
                blob_ref, byte_size = _measure_blob(
                    _CatalogRowPartitioner.chunks(workspace, partition_id)
                )
                write = staging.put_blob(
                    blob_ref,
                    byte_size,
                    _CatalogRowPartitioner.chunks(workspace, partition_id),
                )
                if write.reused:
                    payload_bytes_reused += write.byte_size
                else:
                    payload_bytes_written += write.byte_size
                partitions.append(
                    _CatalogPartition(
                        partition_id,
                        describe_member_from_receipt(
                            blob_ref=write.blob_ref,
                            role=CATALOG_ITEMS_ROLE,
                            media_type=CATALOG_ITEMS_MEDIA_TYPE,
                            byte_size=write.byte_size,
                            record_count=row_partitioner.partition_counts[partition_id],
                            schema_id=SOURCE_CATALOG_ITEM_SCHEMA_ID,
                        ),
                    )
                )
            selected_blob_source = staging.blob_source()
            derived = _derive_catalog(
                selected_blob_source,
                partitions,
                item_count=row_partitioner.item_count,
                selected_count=row_partitioner.selected_count,
            )
            state_digest = derived.catalog_state_digest
            requested_digest = derived.requested_universe_set_digest
            selected_digest = derived.selected_source_set_digest
            diagnostics = derived.diagnostics
            spec = {
                "catalogId": self._request.catalog_id,
                "catalogSchemaDigest": catalog_schema_digest,
                "sourceSystemSetDigest": _source_system_set_digest(descriptions),
                "sourceNativeSchemaSetDigest": _source_schema_set_digest(descriptions),
                "selectionPolicyId": self._policy.policy_id,
                "selectionPolicyVersion": self._policy.policy_version,
                "selectionPolicyDigest": policy_digest,
                "requestedUniverseSetDigest": requested_digest,
                "selectedSourceSetDigest": selected_digest,
                "catalogStateDigest": state_digest,
            }
            inputs = tuple(
                ArtifactInput("source-native", value.logical_id, value.artifact_digest)
                for value in descriptions
            )
            ordered_inputs = tuple(
                sorted(
                    inputs,
                    key=lambda value: _utf16_key(value.logical_id.rsplit(":", 1)[-1]),
                )
            )
            payload_bytes_read = payload_bytes_reused + payload_bytes_written
            receipt: dict[str, Any] = {
                "format": CATALOG_RECEIPT_FORMAT,
                "formatVersion": CATALOG_FORMAT_VERSION,
                "catalogId": self._request.catalog_id,
                "catalogSchemaDigest": catalog_schema_digest,
                "sourceSystemSetDigest": spec["sourceSystemSetDigest"],
                "sourceNativeSchemaSetDigest": spec["sourceNativeSchemaSetDigest"],
                "selectionPolicyId": self._policy.policy_id,
                "selectionPolicyVersion": self._policy.policy_version,
                "selectionPolicyDigest": policy_digest,
                "sourceNativeInputs": [
                    {
                        "logicalId": value.logical_id,
                        "artifactDigest": value.artifact_digest,
                    }
                    for value in ordered_inputs
                ],
                "catalogStateDigest": state_digest,
                "requestedUniverseSetDigest": requested_digest,
                "selectedSourceSetDigest": selected_digest,
                "itemCount": row_partitioner.item_count,
                "dispositionCounts": row_partitioner.disposition_counts,
                "partitionPolicy": _partition_policy(),
                "partitions": [value.to_receipt() for value in partitions],
                **diagnostics,
                "byteMeasurements": {
                    "payloadBytesRead": payload_bytes_read,
                    "payloadBytesReused": payload_bytes_reused,
                    "payloadBytesWritten": payload_bytes_written,
                    "publicationBytesWritten": 0,
                },
                "verifierId": self._request.producer.verifier_id,
                "verifierVersion": self._request.producer.verifier_version,
                "verifierImplementationId": self._request.producer.verifier_implementation_id,
                "semanticVerdict": "pass",
            }
            publication_bytes = -1
            for _ in range(8):
                receipt["byteMeasurements"]["publicationBytesWritten"] = publication_bytes
                receipt_bytes = canonical_json_bytes(receipt)
                local_members = (
                    describe_member_from_receipt(
                        object_key=CATALOG_POLICY_KEY,
                        sha256=sha256_digest(policy_bytes),
                        role=CATALOG_POLICY_ROLE,
                        media_type=CATALOG_JSON_MEDIA_TYPE,
                        byte_size=len(policy_bytes),
                        schema_id=SOURCE_CATALOG_POLICY_SCHEMA_ID,
                    ),
                    describe_member_from_receipt(
                        object_key=CATALOG_RECEIPT_KEY,
                        sha256=sha256_digest(receipt_bytes),
                        role=CATALOG_RECEIPT_ROLE,
                        media_type=CATALOG_JSON_MEDIA_TYPE,
                        byte_size=len(receipt_bytes),
                        schema_id=SOURCE_CATALOG_RECEIPT_SCHEMA_ID,
                    ),
                )
                members = (*local_members, *(value.member for value in partitions))
                manifest, manifest_bytes = MemberManifestReference.for_members(
                    scope_kind="global",
                    scope_id="catalog",
                    object_key=CATALOG_MANIFEST_KEY,
                    members=members,
                )
                root = build_artifact_root(
                    kind=CATALOG_KIND,
                    spec=spec,
                    producer=self._request.producer,
                    inputs=ordered_inputs,
                    manifests=(manifest,),
                    supersedes=self._request.supersedes,
                )
                root_bytes = canonical_json_bytes(root)
                measured_publication_bytes = (
                    len(policy_bytes) + len(receipt_bytes) + len(manifest_bytes) + len(root_bytes)
                )
                if measured_publication_bytes == publication_bytes:
                    break
                publication_bytes = measured_publication_bytes
            else:
                raise IntegrityError("catalog publication byte accounting did not stabilize")
            _schema_error(_RECEIPT_VALIDATOR, receipt, "catalog build receipt")
            staging.write(CATALOG_POLICY_KEY, (policy_bytes,))
            staging.write(CATALOG_RECEIPT_KEY, (receipt_bytes,))
            staging.write(CATALOG_MANIFEST_KEY, (manifest_bytes,))
            staging.write(ROOT_OBJECT_KEY, (root_bytes,))
            reference = SourceCatalogRef(
                root["logicalId"],
                f"{root['artifactDigest'].removeprefix('sha256:')}/{ROOT_OBJECT_KEY}",
                root["artifactDigest"],
            )
            verifier = SourceCatalogBuildGateVerifier(self._request.producer, selected_blob_source)
            try:
                admit_artifact(
                    staging,
                    blob_source=selected_blob_source,
                    expected_pin=ArtifactPin(reference.catalog_id, reference.digest),
                    semantic_verifier=verifier,
                )
            except ArtifactVerificationError as error:
                raise IntegrityError(f"built source catalog is structurally invalid: {error}") from error
            published = staging.commit(reference)
        if verifier.summary is None:
            raise RuntimeError("source catalog verifier produced no summary")
        return SourceCatalogBuildResult(
            published,
            verifier.summary,
            dict(receipt["byteMeasurements"]),
        )


__all__ = [
    "CATALOG_KIND",
    "SourceCatalogArtifactReader",
    "SourceCatalogArtifactVerifier",
    "SourceCatalogBuildGateVerifier",
    "SourceCatalogBuildRequest",
    "SourceCatalogBuildResult",
    "SourceCatalogBuilder",
    "requested_universe_set_digest",
    "selected_source_set_digest",
    "source_catalog_producer",
]
