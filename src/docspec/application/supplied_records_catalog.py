"""Interpret explicitly supplied records without inventing collection outcomes."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from docspec.domain.identity import canonical_json_bytes, closed_mapping, identity_digest, require_text, stable_urn
from docspec.domain.source_catalog import (
    CatalogDisposition,
    CatalogRenditionFamily,
    CatalogSelectionDecision,
    SourceCatalogCandidate,
    SourceCatalogItem,
    SourceCatalogSelection,
    source_catalog_schemas,
)
from docspec.errors import IntegrityError
from docspec.ports.source_catalog import CatalogPolicyInputs, CatalogPolicyWorkspace, SourceInputSelector

from .catalog_policy import catalog_interpretations, normalization_field, utf16_key

SUPPLIED_RECORD_SCHEMA_NAME = "docspec-supplied-record"
SUPPLIED_RECORD_SCHEMA_VERSION = "1.0"
SUPPLIED_RECORD_SCOPE = "supplied-records"
_FIELDS = {"recordId", "sourceIssuedVersion", "title", "metadata", "candidateRenditions"}
_CANDIDATE_SCHEMA = source_catalog_schemas()["source-item.schema.json"]["properties"]["candidateRenditions"]
SUPPLIED_RECORD_SCHEMA_DIGEST = identity_digest({
    "type": "object", "additionalProperties": False, "required": sorted(_FIELDS),
    "properties": {
        "recordId": {"type": "string", "minLength": 1},
        "sourceIssuedVersion": {"type": "string", "minLength": 1},
        "title": {"type": ["string", "null"], "minLength": 1},
        "metadata": {"type": "object"},
        "candidateRenditions": _CANDIDATE_SCHEMA,
    },
})


def supplied_item_id(source_system_id: str, record_id: str) -> str:
    return stable_urn("supplied-source-item", {"sourceSystemId": source_system_id, "recordId": record_id})


def supplied_record(value: object) -> Mapping[str, Any]:
    """Validate the one supported supplied-input shape, preserving raw fields."""

    raw = closed_mapping(value, _FIELDS, "supplied record", error=ValueError)
    require_text(raw["recordId"], "supplied recordId")
    require_text(raw["sourceIssuedVersion"], "supplied sourceIssuedVersion")
    if raw["title"] is not None:
        require_text(raw["title"], "supplied title")
    if not isinstance(raw["metadata"], Mapping):
        raise ValueError("supplied metadata must be an object")
    if not isinstance(raw["candidateRenditions"], list):
        raise ValueError("supplied candidateRenditions must be an array")
    candidates = tuple(SourceCatalogCandidate.from_dict(candidate) for candidate in raw["candidateRenditions"])
    if len({candidate.rendition_id for candidate in candidates}) != len(candidates):
        raise ValueError("supplied candidate rendition identities must be distinct")
    canonical_json_bytes(raw)
    return raw


def supplied_renditions(source_item_id: str, record: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    return tuple({
        "sourceRecordId": source_item_id, "renditionId": candidate["renditionId"],
        "sourceField": "candidateRenditions", "locator": candidate["locator"],
        "mediaType": candidate["mediaType"], "expectedSha256": candidate["expectedSha256"],
        "expectedByteSize": candidate["expectedByteSize"],
    } for candidate in sorted(record["candidateRenditions"], key=lambda value: utf16_key(value["renditionId"])))


@dataclass(frozen=True)
class SuppliedRecordCatalogPolicy:
    """Select supplied candidates and retain absent publisher metadata as absent."""

    source_system_id: str
    source_system_version: str
    policy_id = "urn:docspec:catalog-policy:supplied-records:1"
    policy_version = "1.0.0"

    def __post_init__(self) -> None:
        require_text(self.source_system_id, "supplied source system")
        require_text(self.source_system_version, "supplied source system version")

    @property
    def universe_inputs(self) -> tuple[SourceInputSelector, ...]:
        return (SourceInputSelector(self.source_system_id, self.source_system_version,
            SUPPLIED_RECORD_SCOPE, SUPPLIED_RECORD_SCHEMA_NAME, SUPPLIED_RECORD_SCHEMA_VERSION),)

    @property
    def configuration(self) -> Mapping[str, Any]:
        return {
            "universeInputs": [value.to_dict() for value in self.universe_inputs],
            "sourceSchemaDigest": SUPPLIED_RECORD_SCHEMA_DIGEST,
            "identityRule": "source-system-and-record-id/1",
            "metadataRule": "supplied-title-only/1", "candidateRule": "all-supplied-renditions/1",
            "evidenceScope": "caller-supplied-records",
        }

    def iter_items(self, inputs: CatalogPolicyInputs, workspace: CatalogPolicyWorkspace) -> Iterator[SourceCatalogItem]:
        del workspace
        policy_digest = identity_digest({"format": "docspec-catalog-policy", "formatVersion": "1.0",
            "policyId": self.policy_id, "policyVersion": self.policy_version, "configuration": dict(self.configuration)})
        for row in inputs.iter_universe_rows():
            record = supplied_record(row.record["record"])
            source_item_id = supplied_item_id(self.source_system_id, record["recordId"])
            if (
                row.description.source_system_id != self.source_system_id
                or row.description.source_system_version != self.source_system_version
                or row.record["sourceRecordId"] != source_item_id
                or row.record["schemaDigest"] != SUPPLIED_RECORD_SCHEMA_DIGEST
                or tuple(row.renditions) != supplied_renditions(source_item_id, record)
            ):
                raise IntegrityError("supplied record differs from its declared source identity, schema, or renditions")
            candidates = tuple(SourceCatalogCandidate.from_dict(value) for value in record["candidateRenditions"])
            selection = SourceCatalogSelection(CatalogDisposition.SELECTED) if candidates else SourceCatalogSelection(
                CatalogDisposition.UNAVAILABLE, "supplied.no-candidate", "The supplied record has no document candidate.",
            )
            decision = CatalogSelectionDecision("supplied-candidates", True) if candidates else CatalogSelectionDecision(
                "supplied-candidates", False, selection.disposition, selection.reason_code, selection.reason,
            )
            normalized = {
                "title": record["title"], "agencies": [], "documentType": None,
                "publicationDate": None, "lastUpdatedDate": None, "docketIds": [],
                "regulationIdentifierNumbers": [], "commentCloseDate": None, "language": None, "sourceUrl": None,
            }
            interpretations = catalog_interpretations(
                {"policyId": self.policy_id, "policyVersion": self.policy_version,
                 "policyDigest": policy_digest, "inputScopeIds": [SUPPLIED_RECORD_SCOPE]},
                joins=(), normalization_fields=tuple(normalization_field(
                    name, ("record.title",) if name == "title" else (), value,
                ) for name, value in normalized.items()),
                ordered_family_ids=("supplied",),
                families=(CatalogRenditionFamily("supplied", tuple(value.rendition_id for value in candidates)),),
                selected_family_id="supplied" if candidates else None, candidates=candidates,
                sampling_result={"frameAdmitted": True, "partition": "all", "stratum": ["all"],
                    "orderHash": None, "rank": None, "stratumSize": None, "allocationMethod": "all", "limit": None, "drawn": True},
                selection=selection, decisions=(decision,), topic_source_field="record.metadata", topics=(),
            )
            yield SourceCatalogItem(
                source_item_id, record["recordId"], record["sourceIssuedVersion"],
                ({"scopeId": SUPPLIED_RECORD_SCOPE, "schemaName": SUPPLIED_RECORD_SCHEMA_NAME,
                  "schemaVersion": SUPPLIED_RECORD_SCHEMA_VERSION, "schemaDigest": SUPPLIED_RECORD_SCHEMA_DIGEST,
                  "fields": dict(record)},), normalized, (),
                ({"observationKey": "docspec.input-origin", "observationValue": "caller-supplied-records"},),
                interpretations, candidates, selection,
            )
