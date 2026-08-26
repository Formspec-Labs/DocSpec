"""Build and open one complete immutable DocSpec ``SourceCatalog`` snapshot."""

from __future__ import annotations

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
    VerifiedArtifact,
    admit_artifact,
    build_artifact_root,
    canonical_json_bytes,
    describe_member,
    framed_section_digest,
    iter_member_descriptors,
    parse_canonical_json,
    schema_bundle_digest,
    sha256_digest,
)

from docspec.domain.references import SourceCatalogRef
from docspec.domain.identity import require_sha256
from docspec.domain.source_catalog import (
    CatalogDisposition,
    SOURCE_CATALOG_ITEM_SCHEMA_ID,
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
    SourceInputSelector,
    SourceCatalogPolicy,
    SourceCatalogSnapshot,
    SourceCatalogSnapshotSummary,
    SourceCatalogStore,
    SourceNativeDescription,
    SourceNativeRecordSource,
    SourceNativeRow,
)

CATALOG_KIND = "docspec-source-catalog"
CATALOG_POLICY_KEY = "catalog-policy.json"
CATALOG_ITEMS_KEY = "records/source-items.jsonl"
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
_UNIVERSE_ACCOUNTING_NAMESPACE = "docspec-internal/universe"
_OUTPUT_ACCOUNTING_NAMESPACE = "docspec-internal/output"
_SOURCE_ROW_NAMESPACE_PREFIX = "docspec-internal/source-rows/"

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


@dataclass(frozen=True, slots=True)
class SourceCatalogBuildResult:
    reference: SourceCatalogRef
    summary: SourceCatalogSnapshotSummary


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


def _iter_catalog_rows(source: MemberSource, expected_count: int) -> Iterator[SourceCatalogItem]:
    previous: str | None = None
    count = 0
    with source.open(CATALOG_ITEMS_KEY) as stream:
        while raw := stream.readline(MAX_CATALOG_ROW_BYTES + 2):
            if len(raw) > MAX_CATALOG_ROW_BYTES + 1:
                raise LimitExceededError("source-catalog row exceeds its byte limit")
            if not raw.endswith(b"\n"):
                raise IntegrityError("source-catalog rows must end with a newline")
            try:
                value = parse_canonical_json(raw[:-1], path=f"{CATALOG_ITEMS_KEY}/{count}")
            except ArtifactVerificationError as error:
                raise IntegrityError(f"source-catalog row {count} is not canonical: {error}") from error
            _schema_error(_ITEM_VALIDATOR, value, f"source-catalog row {count}")
            try:
                item = SourceCatalogItem.from_dict(value)
            except (TypeError, ValueError) as error:
                raise IntegrityError(f"source-catalog row {count} is invalid: {error}") from error
            if previous is not None and _utf16_key(item.source_item_id) <= _utf16_key(previous):
                raise IntegrityError("source-catalog rows must have strictly increasing sourceItemId values")
            previous = item.source_item_id
            count += 1
            yield item
    if count != expected_count:
        raise IntegrityError("source-catalog row count differs from its member descriptor")


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
        universe_input: SourceInputSelector,
        workspace: CatalogPolicyWorkspace,
    ) -> None:
        self._sources = tuple(sources)
        self._descriptions = tuple(descriptions)
        self._universe_input = universe_input
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
        *,
        account_for_universe: bool,
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
            if account_for_universe:
                self._workspace.put(
                    _UNIVERSE_ACCOUNTING_NAMESPACE,
                    (source_item_id,),
                    {"sourceItemId": source_item_id},
                )
            yield row
        self._completed.add(selector)

    def iter_universe_rows(self) -> Iterator[SourceNativeRow]:
        if self._universe_opened:
            raise IntegrityError("catalog policy attempted to open the universe more than once")
        self._universe_opened = True
        yield from self._rows(self._universe_input, account_for_universe=True)

    def iter_lookup_rows(self, selector: SourceInputSelector) -> Iterator[SourceNativeRow]:
        if selector == self._universe_input:
            raise IntegrityError("catalog policy lookup input must differ from its universe input")
        yield from self._rows(selector, account_for_universe=False)

    def finish(self) -> None:
        if not self._universe_opened or self._universe_input not in self._completed:
            raise IntegrityError("catalog policy did not read its declared universe input")
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
        policy.universe_input,
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


class _CatalogRowWriter:
    def __init__(self, rows: Iterable[SourceCatalogItem]) -> None:
        self._rows = rows
        self.item_count = 0
        self.disposition_counts = {value.value: 0 for value in CatalogDisposition}
        self.selected_count = 0

    def chunks(self) -> Iterator[bytes]:
        previous: str | None = None
        for item in self._rows:
            if previous is not None and _utf16_key(item.source_item_id) <= _utf16_key(previous):
                raise IntegrityError("catalog policy produced duplicate or out-of-order sourceItemId values")
            previous = item.source_item_id
            value = item.to_dict()
            _schema_error(_ITEM_VALIDATOR, value, f"source-catalog row {self.item_count}")
            payload = canonical_json_bytes(value)
            if len(payload) > MAX_CATALOG_ROW_BYTES:
                raise LimitExceededError("source-catalog row exceeds its byte limit")
            self.item_count += 1
            self.disposition_counts[item.disposition.value] += 1
            if item.disposition is CatalogDisposition.SELECTED:
                self.selected_count += 1
            yield payload + b"\n"


def _catalog_state_digests(
    source: MemberSource,
    *,
    item_count: int,
    selected_count: int,
) -> tuple[str, str, str]:
    state = _framed_digest(
        "docspec-source-catalog-state/1",
        "sourceItems",
        item_count,
        (item.to_dict() for item in _iter_catalog_rows(source, item_count)),
    )
    requested = requested_universe_set_digest(
        item_count,
        (item.source_item_id for item in _iter_catalog_rows(source, item_count)),
    )
    selected = selected_source_set_digest(
        selected_count,
        (
            (item.source_item_id, item.document_id)
            for item in _iter_catalog_rows(source, item_count)
            if item.disposition is CatalogDisposition.SELECTED
        ),
    )
    return state, requested, selected


def _catalog_disposition_counts(
    source: MemberSource,
    item_count: int,
) -> dict[str, int]:
    counts = {value.value: 0 for value in CatalogDisposition}
    for item in _iter_catalog_rows(source, item_count):
        counts[item.disposition.value] += 1
    return counts


class SourceCatalogArtifactVerifier:
    """Check DocSpec meaning after Rulespec has checked generic structure."""

    def __init__(self, producer: Producer) -> None:
        self._producer = producer
        self.summary: SourceCatalogSnapshotSummary | None = None

    def __call__(self, artifact: VerifiedArtifact, source: MemberSource) -> None:
        root = artifact.root
        if root["kind"] != CATALOG_KIND:
            raise IntegrityError("source catalog reference names a different product kind")
        spec = _mapping(root["spec"], "source-catalog spec")
        if set(spec) != _CATALOG_SPEC_FIELDS:
            raise IntegrityError("source-catalog spec has an invalid closed shape")
        if not artifact.inputs or {value.role for value in artifact.inputs} != {"source-native"}:
            raise IntegrityError("source catalog must pin one or more source-native inputs")
        members = {value.object_key: value for value in iter_member_descriptors(artifact, source)}
        if set(members) != {CATALOG_POLICY_KEY, CATALOG_ITEMS_KEY, CATALOG_RECEIPT_KEY}:
            raise IntegrityError("source-catalog members differ from the DocSpec product view")
        policy_member = members[CATALOG_POLICY_KEY]
        item_member = members[CATALOG_ITEMS_KEY]
        receipt_member = members[CATALOG_RECEIPT_KEY]
        if (
            policy_member.role != CATALOG_POLICY_ROLE
            or policy_member.media_type != CATALOG_JSON_MEDIA_TYPE
            or policy_member.schema_id != SOURCE_CATALOG_POLICY_SCHEMA_ID
            or policy_member.record_count is not None
            or item_member.role != CATALOG_ITEMS_ROLE
            or item_member.media_type != CATALOG_ITEMS_MEDIA_TYPE
            or item_member.schema_id != SOURCE_CATALOG_ITEM_SCHEMA_ID
            or item_member.record_count is None
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
        if receipt["itemCount"] != item_member.record_count:
            raise IntegrityError("catalog build receipt item count differs from the source-items member")
        counts = receipt["dispositionCounts"]
        if sum(counts.values()) != receipt["itemCount"]:
            raise IntegrityError("catalog build receipt dispositions do not account for every row")
        self.summary = SourceCatalogSnapshotSummary(
            logical_id=artifact.pin.logical_id,
            artifact_digest=artifact.pin.artifact_digest,
            catalog_id=spec["catalogId"],
            catalog_state_digest=spec["catalogStateDigest"],
            requested_universe_set_digest=spec["requestedUniverseSetDigest"],
            selected_source_set_digest=spec["selectedSourceSetDigest"],
            item_count=receipt["itemCount"],
            disposition_counts=dict(counts),
            partitions=("all",),
            item_member_path=CATALOG_ITEMS_KEY,
        )


class SourceCatalogBuildGateVerifier:
    """Add the producer-only full semantic pass to bounded receipt checks."""

    def __init__(self, producer: Producer) -> None:
        self._producer = producer
        self.summary: SourceCatalogSnapshotSummary | None = None

    def __call__(self, artifact: VerifiedArtifact, source: MemberSource) -> None:
        receipt_verifier = SourceCatalogArtifactVerifier(self._producer)
        receipt_verifier(artifact, source)
        summary = receipt_verifier.summary
        if summary is None:
            raise RuntimeError("source catalog receipt verifier produced no summary")

        state, requested, selected = _catalog_state_digests(
            source,
            item_count=summary.item_count,
            selected_count=summary.disposition_counts[CatalogDisposition.SELECTED.value],
        )
        computed = {
            "catalogStateDigest": state,
            "requestedUniverseSetDigest": requested,
            "selectedSourceSetDigest": selected,
        }
        expected = {
            "catalogStateDigest": summary.catalog_state_digest,
            "requestedUniverseSetDigest": summary.requested_universe_set_digest,
            "selectedSourceSetDigest": summary.selected_source_set_digest,
        }
        for name, digest in computed.items():
            if digest != expected[name]:
                raise IntegrityError(f"producer semantic gate recomputed a different {name}")
        if _catalog_disposition_counts(source, summary.item_count) != dict(
            summary.disposition_counts
        ):
            raise IntegrityError("producer semantic gate recomputed different disposition counts")
        self.summary = summary


class SourceCatalogArtifactReader(ImmutableSourceCatalogReader):
    """Open complete snapshots through an injected immutable member resolver."""

    def __init__(self, store: SourceCatalogStore, *, producer: Producer) -> None:
        self._store = store
        self._producer = producer

    def open_snapshot(self, reference: SourceCatalogRef) -> SourceCatalogSnapshot:
        source = self._store.source_for(reference)
        verifier = SourceCatalogArtifactVerifier(self._producer)
        try:
            admit_artifact(
                source,
                expected_pin=ArtifactPin(reference.catalog_id, reference.digest),
                semantic_verifier=verifier,
            )
        except ArtifactVerificationError as error:
            raise IntegrityError(f"source catalog artifact is invalid: {error}") from error
        if verifier.summary is None:
            raise RuntimeError("source catalog verifier produced no summary")
        return SourceCatalogSnapshot(
            verifier.summary,
            _iter_catalog_rows(source, verifier.summary.item_count),
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
            staging.write(CATALOG_POLICY_KEY, (policy_bytes,))
            row_writer = _CatalogRowWriter(
                _policy_rows(
                    sources,
                    descriptions,
                    self._policy,
                    policy_digest,
                    workspace,
                )
            )
            staging.write(CATALOG_ITEMS_KEY, row_writer.chunks())
            state_digest, requested_digest, selected_digest = _catalog_state_digests(
                staging,
                item_count=row_writer.item_count,
                selected_count=row_writer.selected_count,
            )
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
            receipt = {
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
                "itemCount": row_writer.item_count,
                "dispositionCounts": row_writer.disposition_counts,
                "verifierId": self._request.producer.verifier_id,
                "verifierVersion": self._request.producer.verifier_version,
                "verifierImplementationId": self._request.producer.verifier_implementation_id,
                "semanticVerdict": "pass",
            }
            _schema_error(_RECEIPT_VALIDATOR, receipt, "catalog build receipt")
            staging.write(CATALOG_RECEIPT_KEY, (canonical_json_bytes(receipt),))
            members: tuple[MemberDescriptor, ...] = (
                describe_member(
                    staging,
                    object_key=CATALOG_POLICY_KEY,
                    role=CATALOG_POLICY_ROLE,
                    media_type=CATALOG_JSON_MEDIA_TYPE,
                    schema_id=SOURCE_CATALOG_POLICY_SCHEMA_ID,
                ),
                describe_member(
                    staging,
                    object_key=CATALOG_RECEIPT_KEY,
                    role=CATALOG_RECEIPT_ROLE,
                    media_type=CATALOG_JSON_MEDIA_TYPE,
                    schema_id=SOURCE_CATALOG_RECEIPT_SCHEMA_ID,
                ),
                describe_member(
                    staging,
                    object_key=CATALOG_ITEMS_KEY,
                    role=CATALOG_ITEMS_ROLE,
                    media_type=CATALOG_ITEMS_MEDIA_TYPE,
                    record_count=row_writer.item_count,
                    schema_id=SOURCE_CATALOG_ITEM_SCHEMA_ID,
                ),
            )
            manifest, manifest_bytes = MemberManifestReference.for_members(
                scope_kind="global",
                scope_id="catalog",
                object_key=CATALOG_MANIFEST_KEY,
                members=members,
            )
            staging.write(CATALOG_MANIFEST_KEY, (manifest_bytes,))
            root = build_artifact_root(
                kind=CATALOG_KIND,
                spec=spec,
                producer=self._request.producer,
                inputs=inputs,
                manifests=(manifest,),
            )
            staging.write(ROOT_OBJECT_KEY, (canonical_json_bytes(root),))
            reference = SourceCatalogRef(
                root["logicalId"],
                f"{root['artifactDigest'].removeprefix('sha256:')}/{ROOT_OBJECT_KEY}",
                root["artifactDigest"],
            )
            verifier = SourceCatalogBuildGateVerifier(self._request.producer)
            try:
                admit_artifact(
                    staging,
                    expected_pin=ArtifactPin(reference.catalog_id, reference.digest),
                    semantic_verifier=verifier,
                )
            except ArtifactVerificationError as error:
                raise IntegrityError(f"built source catalog is structurally invalid: {error}") from error
            published = staging.commit(reference)
        if verifier.summary is None:
            raise RuntimeError("source catalog verifier produced no summary")
        return SourceCatalogBuildResult(published, verifier.summary)


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
