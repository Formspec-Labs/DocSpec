"""Complete DocSpec-owned source-catalog records.

These records preserve the normative catalog row.  The smaller ``SourceItem``
in :mod:`docspec.domain.content` is a processing view derived from this row; it
is not the catalog interchange format.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

from docspec.domain.content import CandidateFile, SourceItem, SourceItemState
from docspec.domain.identity import closed_mapping, freeze_json, require_sha256, require_text, thaw_json

SOURCE_CATALOG_ITEM_SCHEMA_ID = "urn:docspec:schema:source-catalog-item:1.0"
SOURCE_CATALOG_POLICY_SCHEMA_ID = "urn:docspec:schema:source-catalog-policy:1.0"
SOURCE_CATALOG_RECEIPT_SCHEMA_ID = "urn:docspec:schema:source-catalog-build-receipt:1.0"


class CatalogDisposition(StrEnum):
    SELECTED = "selected"
    EXCLUDED = "excluded"
    DELETED = "deleted"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


def _json_object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    frozen = freeze_json(value, label=label)
    assert isinstance(frozen, Mapping)
    return frozen


def _json_objects(values: object, label: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray, memoryview)):
        raise ValueError(f"{label} must be an array")
    return tuple(_json_object(value, f"{label}[{index}]") for index, value in enumerate(values))


@dataclass(frozen=True, slots=True)
class SourceCatalogCandidate:
    rendition_id: str
    media_type: str
    locator_kind: str
    locator: str
    expected_sha256: str | None = None
    expected_byte_size: int | None = None

    def __post_init__(self) -> None:
        require_text(self.rendition_id, "rendition_id")
        require_text(self.media_type, "media_type")
        if self.locator_kind not in {"source-url", "immutable-object"}:
            raise ValueError("locator_kind must be source-url or immutable-object")
        require_text(self.locator, "locator")
        if self.expected_sha256 is not None:
            require_sha256(self.expected_sha256, "expected_sha256")
        if (
            self.expected_byte_size is not None
            and (isinstance(self.expected_byte_size, bool) or self.expected_byte_size < 0)
        ):
            raise ValueError("expected_byte_size must be a non-negative integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "renditionId": self.rendition_id,
            "mediaType": self.media_type,
            "locatorKind": self.locator_kind,
            "locator": self.locator,
            "expectedSha256": self.expected_sha256,
            "expectedByteSize": self.expected_byte_size,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = closed_mapping(
            value,
            {
                "renditionId",
                "mediaType",
                "locatorKind",
                "locator",
                "expectedSha256",
                "expectedByteSize",
            },
            "source-catalog candidate",
            error=ValueError,
        )
        return cls(
            item["renditionId"],
            item["mediaType"],
            item["locatorKind"],
            item["locator"],
            item["expectedSha256"],
            item["expectedByteSize"],
        )


@dataclass(frozen=True, slots=True)
class SourceCatalogSelection:
    disposition: CatalogDisposition
    reason_code: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.disposition is CatalogDisposition.SELECTED:
            if self.reason_code is not None or self.reason is not None:
                raise ValueError("selected catalog rows must not carry a refusal reason")
            return
        require_text(self.reason_code, "selection reason_code")
        require_text(self.reason, "selection reason")

    def to_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "reasonCode": self.reason_code,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = closed_mapping(
            value,
            {"disposition", "reasonCode", "reason"},
            "source-catalog selection",
            error=ValueError,
        )
        return cls(CatalogDisposition(item["disposition"]), item["reasonCode"], item["reason"])


@dataclass(frozen=True, slots=True)
class CatalogNormalizationField:
    """One closed, source-independent normalized-field outcome."""

    normalized_field: str
    source_paths: tuple[str, ...]
    value_source: str
    outcome: str
    value: Any
    unparseable_values: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.normalized_field, "normalized_field")
        for path in self.source_paths:
            require_text(path, "normalization source path")
        if len(self.source_paths) != len(set(self.source_paths)):
            raise ValueError("normalization source paths must be distinct")
        if self.value_source not in {"source", "policy"}:
            raise ValueError("normalization value_source is not recognized")
        if self.outcome not in {"normalized", "absent", "unparseable"}:
            raise ValueError("normalization outcome is not recognized")
        object.__setattr__(self, "value", freeze_json(self.value, label="normalized value"))
        frozen_unparseable = tuple(
            freeze_json(value, label="unparseable normalization value")
            for value in self.unparseable_values
        )
        if any(
            value in frozen_unparseable[:index]
            for index, value in enumerate(frozen_unparseable)
        ):
            raise ValueError("unparseable normalization values must be distinct")
        if (self.outcome == "unparseable") != bool(frozen_unparseable):
            raise ValueError(
                "unparseable normalization outcomes and values must be present together"
            )
        object.__setattr__(
            self,
            "unparseable_values",
            frozen_unparseable,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "normalizedField": self.normalized_field,
            "sourcePaths": list(self.source_paths),
            "valueSource": self.value_source,
            "outcome": self.outcome,
            "value": thaw_json(self.value),
            "unparseableValues": [thaw_json(value) for value in self.unparseable_values],
        }


@dataclass(frozen=True, slots=True)
class CatalogRenditionFamily:
    """One ordered candidate family and the usable renditions it offered."""

    family_id: str
    offered_rendition_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        require_text(self.family_id, "rendition family_id")
        for rendition_id in self.offered_rendition_ids:
            require_text(rendition_id, "offered rendition_id")
        if len(self.offered_rendition_ids) != len(set(self.offered_rendition_ids)):
            raise ValueError("offered rendition identities must be distinct")

    def to_dict(self) -> dict[str, Any]:
        return {
            "familyId": self.family_id,
            "offeredRenditionIds": list(self.offered_rendition_ids),
        }


@dataclass(frozen=True, slots=True)
class CatalogSelectionDecision:
    """One policy decision in evaluation order."""

    decision_id: str
    passed: bool
    failure_disposition: CatalogDisposition | None = None
    reason_code: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        require_text(self.decision_id, "selection decision_id")
        if self.passed:
            if any(
                value is not None
                for value in (self.failure_disposition, self.reason_code, self.reason)
            ):
                raise ValueError("a passed selection decision must not carry failure fields")
            return
        if self.failure_disposition in {None, CatalogDisposition.SELECTED}:
            raise ValueError("a failed selection decision requires a non-selected disposition")
        require_text(self.reason_code, "selection decision reason_code")
        require_text(self.reason, "selection decision reason")

    def to_dict(self) -> dict[str, Any]:
        return {
            "decisionId": self.decision_id,
            "outcome": "pass" if self.passed else "fail",
            "disposition": (
                self.failure_disposition.value if self.failure_disposition is not None else None
            ),
            "reasonCode": self.reason_code,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SourceCatalogItem:
    """One complete normative row from an immutable ``SourceCatalog``."""

    source_item_id: str
    document_id: str
    source_issued_version: str
    source_native_facts: tuple[Mapping[str, Any], ...]
    normalized_metadata: Mapping[str, Any]
    source_observed_topics: tuple[Mapping[str, Any], ...]
    source_observations: tuple[Mapping[str, Any], ...]
    interpretations: tuple[Mapping[str, Any], ...]
    candidate_renditions: tuple[SourceCatalogCandidate, ...]
    selection: SourceCatalogSelection

    def __post_init__(self) -> None:
        require_text(self.source_item_id, "source_item_id")
        require_text(self.document_id, "document_id")
        require_text(self.source_issued_version, "source_issued_version")
        object.__setattr__(
            self,
            "source_native_facts",
            _json_objects(self.source_native_facts, "source_native_facts"),
        )
        object.__setattr__(
            self,
            "normalized_metadata",
            _json_object(self.normalized_metadata, "normalized_metadata"),
        )
        object.__setattr__(
            self,
            "source_observed_topics",
            _json_objects(self.source_observed_topics, "source_observed_topics"),
        )
        object.__setattr__(
            self,
            "source_observations",
            _json_objects(self.source_observations, "source_observations"),
        )
        object.__setattr__(
            self,
            "interpretations",
            _json_objects(self.interpretations, "interpretations"),
        )
        rendition_ids = [candidate.rendition_id for candidate in self.candidate_renditions]
        if len(rendition_ids) != len(set(rendition_ids)):
            raise ValueError("candidate rendition identities must be distinct")
        if self.selection.disposition is CatalogDisposition.SELECTED and not self.candidate_renditions:
            raise ValueError("a selected catalog row must contain a candidate rendition")

    @property
    def disposition(self) -> CatalogDisposition:
        return self.selection.disposition

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourceItemId": self.source_item_id,
            "documentId": self.document_id,
            "sourceIssuedVersion": self.source_issued_version,
            "sourceNativeFacts": [thaw_json(value) for value in self.source_native_facts],
            "normalizedMetadata": thaw_json(self.normalized_metadata),
            "sourceObservedTopics": [thaw_json(value) for value in self.source_observed_topics],
            "sourceObservations": [thaw_json(value) for value in self.source_observations],
            "interpretations": [thaw_json(value) for value in self.interpretations],
            "candidateRenditions": [value.to_dict() for value in self.candidate_renditions],
            "selection": self.selection.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        item = closed_mapping(
            value,
            {
                "sourceItemId",
                "documentId",
                "sourceIssuedVersion",
                "sourceNativeFacts",
                "normalizedMetadata",
                "sourceObservedTopics",
                "sourceObservations",
                "interpretations",
                "candidateRenditions",
                "selection",
            },
            "source-catalog item",
            error=ValueError,
        )
        candidates = item["candidateRenditions"]
        if not isinstance(candidates, list):
            raise ValueError("candidateRenditions must be an array")
        return cls(
            source_item_id=item["sourceItemId"],
            document_id=item["documentId"],
            source_issued_version=item["sourceIssuedVersion"],
            source_native_facts=_json_objects(item["sourceNativeFacts"], "sourceNativeFacts"),
            normalized_metadata=_json_object(item["normalizedMetadata"], "normalizedMetadata"),
            source_observed_topics=_json_objects(item["sourceObservedTopics"], "sourceObservedTopics"),
            source_observations=_json_objects(item["sourceObservations"], "sourceObservations"),
            interpretations=_json_objects(item["interpretations"], "interpretations"),
            candidate_renditions=tuple(SourceCatalogCandidate.from_dict(candidate) for candidate in candidates),
            selection=SourceCatalogSelection.from_dict(item["selection"]),
        )

    def to_processing_item(self) -> SourceItem:
        """Derive the smaller document-processing view without losing this row."""

        state = {
            CatalogDisposition.SELECTED: SourceItemState.ACTIVE,
            CatalogDisposition.DELETED: SourceItemState.DELETED,
            CatalogDisposition.EXCLUDED: SourceItemState.EXCLUDED,
            CatalogDisposition.UNAVAILABLE: SourceItemState.EXCLUDED,
            CatalogDisposition.FAILED: SourceItemState.EXCLUDED,
        }[self.disposition]
        candidates = tuple(
            CandidateFile(
                candidate_id=value.rendition_id,
                locator=value.locator,
                media_type=value.media_type,
                expected_digest=value.expected_sha256,
                expected_size=value.expected_byte_size,
            )
            for value in self.candidate_renditions
        )
        return SourceItem(
            item_id=self.source_item_id,
            version=self.source_issued_version,
            candidates=candidates,
            state=state,
            metadata={
                "documentId": self.document_id,
                "normalizedMetadata": self.normalized_metadata,
                "sourceCatalogRow": self.to_dict(),
            },
        )


def source_catalog_schemas() -> dict[str, dict[str, Any]]:
    """Return the installed closed schema family from the typed row vocabulary."""

    text = {"type": "string", "minLength": 1}
    nullable_text = {"anyOf": [{"type": "null"}, text]}
    digest = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
    text_set = {"type": "array", "items": text, "uniqueItems": True}

    candidate = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "renditionId",
            "mediaType",
            "locatorKind",
            "locator",
            "expectedSha256",
            "expectedByteSize",
        ],
        "properties": {
            "renditionId": {"type": "string", "minLength": 1},
            "mediaType": {"type": "string", "minLength": 1},
            "locatorKind": {"enum": ["source-url", "immutable-object"]},
            "locator": {"type": "string", "minLength": 1},
            "expectedSha256": {
                "anyOf": [
                    {"type": "null"},
                    {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                ]
            },
            "expectedByteSize": {"anyOf": [{"type": "null"}, {"type": "integer", "minimum": 0}]},
        },
    }
    selection = {
        "type": "object",
        "additionalProperties": False,
        "required": ["disposition", "reasonCode", "reason"],
        "properties": {
            "disposition": {"enum": [value.value for value in CatalogDisposition]},
            "reasonCode": {"anyOf": [{"type": "null"}, {"type": "string", "minLength": 1}]},
            "reason": {"anyOf": [{"type": "null"}, {"type": "string", "minLength": 1}]},
        },
        "oneOf": [
            {
                "properties": {
                    "disposition": {"const": CatalogDisposition.SELECTED.value},
                    "reasonCode": {"type": "null"},
                    "reason": {"type": "null"},
                }
            },
            {
                "properties": {
                    "disposition": {
                        "enum": [
                            value.value
                            for value in CatalogDisposition
                            if value is not CatalogDisposition.SELECTED
                        ]
                    },
                    "reasonCode": text,
                    "reason": text,
                }
            },
        ],
    }

    def interpretation(kind: str, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "interpretationKind",
                "policyId",
                "policyVersion",
                "policyDigest",
                "inputScopeIds",
                "result",
            ],
            "properties": {
                "interpretationKind": {"const": kind},
                "policyId": text,
                "policyVersion": text,
                "policyDigest": digest,
                "inputScopeIds": text_set,
                "result": result,
            },
        }

    source_native_fact = {
        "type": "object",
        "additionalProperties": False,
        "required": ["scopeId", "schemaName", "schemaVersion", "schemaDigest", "fields"],
        "properties": {
            "scopeId": text,
            "schemaName": text,
            "schemaVersion": text,
            "schemaDigest": digest,
            # The pinned source-native schema closes this producer-owned object.
            "fields": {"type": "object"},
        },
    }
    agency = {
        "type": "object",
        "additionalProperties": False,
        "required": ["agencyId", "agencyName"],
        "properties": {"agencyId": text, "agencyName": text},
    }
    normalized_metadata = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "title",
            "agencies",
            "documentType",
            "publicationDate",
            "lastUpdatedDate",
            "docketIds",
            "regulationIdentifierNumbers",
            "commentCloseDate",
            "language",
            "sourceUrl",
        ],
        "properties": {
            "title": nullable_text,
            "agencies": {"type": "array", "items": agency, "uniqueItems": True},
            "documentType": nullable_text,
            "publicationDate": nullable_text,
            "lastUpdatedDate": nullable_text,
            "docketIds": text_set,
            "regulationIdentifierNumbers": text_set,
            "commentCloseDate": nullable_text,
            "language": nullable_text,
            "sourceUrl": nullable_text,
        },
    }
    observed_topic = {
        "type": "object",
        "additionalProperties": False,
        "required": ["observedTopicId", "observedTopicScheme", "label"],
        "properties": {
            "observedTopicId": text,
            "observedTopicScheme": text,
            "label": text,
        },
    }
    source_observation = {
        "type": "object",
        "additionalProperties": False,
        "required": ["observationKey", "observationValue"],
        "properties": {"observationKey": text, "observationValue": {}},
    }
    normalization_field = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "normalizedField",
            "sourcePaths",
            "valueSource",
            "outcome",
            "value",
            "unparseableValues",
        ],
        "properties": {
            "normalizedField": text,
            "sourcePaths": text_set,
            "valueSource": {"enum": ["source", "policy"]},
            "outcome": {"enum": ["normalized", "absent", "unparseable"]},
            "value": {},
            "unparseableValues": {"type": "array", "uniqueItems": True},
        },
        "oneOf": [
            {
                "properties": {
                    "outcome": {"enum": ["normalized", "absent"]},
                    "unparseableValues": {"maxItems": 0},
                }
            },
            {
                "properties": {
                    "outcome": {"const": "unparseable"},
                    "unparseableValues": {"minItems": 1},
                }
            },
        ],
    }
    normalization_result = {
        "type": "object",
        "additionalProperties": False,
        "required": ["fields"],
        "properties": {
            "fields": {
                "type": "array",
                "items": normalization_field,
                "minItems": 1,
                "uniqueItems": True,
            }
        },
    }
    rendition_family = {
        "type": "object",
        "additionalProperties": False,
        "required": ["familyId", "offeredRenditionIds"],
        "properties": {"familyId": text, "offeredRenditionIds": text_set},
    }
    rendition_result = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "orderedFamilyIds",
            "families",
            "selectedFamilyId",
            "selectedRenditionIds",
        ],
        "properties": {
            "orderedFamilyIds": text_set,
            "families": {
                "type": "array",
                "items": rendition_family,
                "minItems": 1,
                "uniqueItems": True,
            },
            "selectedFamilyId": nullable_text,
            "selectedRenditionIds": text_set,
        },
    }
    topic_recovery_result = {
        "type": "object",
        "additionalProperties": False,
        "required": ["sourceField", "outcome", "evidenceDigest", "observedTopicIds"],
        "properties": {
            "sourceField": text,
            "outcome": {"enum": ["observed", "publisher-declared-empty", "not-recovered"]},
            "evidenceDigest": {"anyOf": [{"type": "null"}, digest]},
            "observedTopicIds": text_set,
        },
        "oneOf": [
            {
                "properties": {
                    "outcome": {"const": "observed"},
                    "evidenceDigest": {"type": "null"},
                    "observedTopicIds": {"minItems": 1},
                }
            },
            {
                "properties": {
                    "outcome": {"const": "publisher-declared-empty"},
                    "evidenceDigest": digest,
                    "observedTopicIds": {"maxItems": 0},
                }
            },
            {
                "properties": {
                    "outcome": {"const": "not-recovered"},
                    "evidenceDigest": {"type": "null"},
                    "observedTopicIds": {"maxItems": 0},
                }
            },
        ],
    }
    source_join = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "joinId",
            "sourceField",
            "sourceValue",
            "lookupScopeId",
            "outcome",
            "matchedSourceRecordId",
        ],
        "properties": {
            "joinId": text,
            "sourceField": text,
            "sourceValue": nullable_text,
            "lookupScopeId": text,
            "outcome": {"enum": ["matched", "not-stated", "no-match"]},
            "matchedSourceRecordId": nullable_text,
        },
        "oneOf": [
            {
                "properties": {
                    "outcome": {"const": "matched"},
                    "sourceValue": text,
                    "matchedSourceRecordId": text,
                }
            },
            {
                "properties": {
                    "outcome": {"const": "not-stated"},
                    "sourceValue": {"type": "null"},
                    "matchedSourceRecordId": {"type": "null"},
                }
            },
            {
                "properties": {
                    "outcome": {"const": "no-match"},
                    "sourceValue": text,
                    "matchedSourceRecordId": {"type": "null"},
                }
            },
        ],
    }
    source_join_result = {
        "type": "object",
        "additionalProperties": False,
        "required": ["joins"],
        "properties": {
            "joins": {
                "type": "array",
                "items": source_join,
                "minItems": 1,
                "uniqueItems": True,
            }
        },
    }
    selection_decision = {
        "type": "object",
        "additionalProperties": False,
        "required": ["decisionId", "outcome", "disposition", "reasonCode", "reason"],
        "properties": {
            "decisionId": text,
            "outcome": {"enum": ["pass", "fail"]},
            "disposition": {
                "anyOf": [
                    {"type": "null"},
                    {"enum": [
                        CatalogDisposition.EXCLUDED.value,
                        CatalogDisposition.DELETED.value,
                        CatalogDisposition.UNAVAILABLE.value,
                        CatalogDisposition.FAILED.value,
                    ]},
                ]
            },
            "reasonCode": nullable_text,
            "reason": nullable_text,
        },
        "oneOf": [
            {
                "properties": {
                    "outcome": {"const": "pass"},
                    "disposition": {"type": "null"},
                    "reasonCode": {"type": "null"},
                    "reason": {"type": "null"},
                }
            },
            {
                "properties": {
                    "outcome": {"const": "fail"},
                    "disposition": {
                        "enum": [
                            CatalogDisposition.EXCLUDED.value,
                            CatalogDisposition.DELETED.value,
                            CatalogDisposition.UNAVAILABLE.value,
                            CatalogDisposition.FAILED.value,
                        ]
                    },
                    "reasonCode": text,
                    "reason": text,
                }
            },
        ],
    }
    selection_result = {
        "type": "object",
        "additionalProperties": False,
        "required": ["decisions", "finalDisposition", "reasonCode", "reason"],
        "properties": {
            "decisions": {
                "type": "array",
                "items": selection_decision,
                "minItems": 1,
                "uniqueItems": True,
            },
            "finalDisposition": {"enum": [value.value for value in CatalogDisposition]},
            "reasonCode": nullable_text,
            "reason": nullable_text,
        },
    }
    item = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SOURCE_CATALOG_ITEM_SCHEMA_ID,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "sourceItemId",
            "documentId",
            "sourceIssuedVersion",
            "sourceNativeFacts",
            "normalizedMetadata",
            "sourceObservedTopics",
            "sourceObservations",
            "interpretations",
            "candidateRenditions",
            "selection",
        ],
        "properties": {
            "sourceItemId": {"type": "string", "minLength": 1},
            "documentId": {"type": "string", "minLength": 1},
            "sourceIssuedVersion": {"type": "string", "minLength": 1},
            "sourceNativeFacts": {
                "type": "array",
                "items": source_native_fact,
                "minItems": 1,
                "uniqueItems": True,
            },
            "normalizedMetadata": normalized_metadata,
            "sourceObservedTopics": {"type": "array", "items": observed_topic, "uniqueItems": True},
            "sourceObservations": {"type": "array", "items": source_observation, "uniqueItems": True},
            "interpretations": {
                "type": "array",
                "items": {
                    "oneOf": [
                        interpretation("normalization", normalization_result),
                        interpretation("rendition-preference", rendition_result),
                        interpretation("selection", selection_result),
                        interpretation("source-join", source_join_result),
                        interpretation("topic-recovery", topic_recovery_result),
                    ]
                },
                "minItems": 1,
                "uniqueItems": True,
            },
            "candidateRenditions": {"type": "array", "items": candidate},
            "selection": selection,
        },
    }
    policy = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SOURCE_CATALOG_POLICY_SCHEMA_ID,
        "type": "object",
        "additionalProperties": False,
        "required": ["format", "formatVersion", "policyId", "policyVersion", "configuration"],
        "properties": {
            "format": {"const": "docspec-catalog-policy"},
            "formatVersion": {"const": "1.0"},
            "policyId": {"type": "string", "minLength": 1},
            "policyVersion": {"type": "string", "minLength": 1},
            # The wrapper is generic. Each injected policy owns and closes its
            # configuration before the builder receives it.
            "configuration": {"type": "object"},
        },
    }
    receipt = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SOURCE_CATALOG_RECEIPT_SCHEMA_ID,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "format",
            "formatVersion",
            "catalogId",
            "catalogSchemaDigest",
            "sourceSystemSetDigest",
            "sourceNativeSchemaSetDigest",
            "selectionPolicyId",
            "selectionPolicyVersion",
            "selectionPolicyDigest",
            "sourceNativeInputs",
            "catalogStateDigest",
            "requestedUniverseSetDigest",
            "selectedSourceSetDigest",
            "itemCount",
            "dispositionCounts",
            "verifierId",
            "verifierVersion",
            "verifierImplementationId",
            "semanticVerdict",
        ],
        "properties": {
            "format": {"const": "docspec-source-catalog-build-receipt"},
            "formatVersion": {"const": "1.0"},
            "catalogId": {"type": "string", "minLength": 1},
            "catalogSchemaDigest": digest,
            "sourceSystemSetDigest": digest,
            "sourceNativeSchemaSetDigest": digest,
            "selectionPolicyId": text,
            "selectionPolicyVersion": text,
            "selectionPolicyDigest": digest,
            "sourceNativeInputs": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["logicalId", "artifactDigest"],
                    "properties": {"logicalId": text, "artifactDigest": digest},
                },
            },
            "catalogStateDigest": digest,
            "requestedUniverseSetDigest": digest,
            "selectedSourceSetDigest": digest,
            "itemCount": {"type": "integer", "minimum": 0},
            "dispositionCounts": {
                "type": "object",
                "additionalProperties": False,
                "required": [value.value for value in CatalogDisposition],
                "properties": {
                    value.value: {"type": "integer", "minimum": 0} for value in CatalogDisposition
                },
            },
            "verifierId": {"type": "string", "minLength": 1},
            "verifierVersion": {"type": "string", "minLength": 1},
            "verifierImplementationId": {"type": "string", "minLength": 1},
            "semanticVerdict": {"const": "pass"},
        },
    }
    return {
        "source-item.schema.json": item,
        "catalog-policy.schema.json": policy,
        "catalog-build-receipt.schema.json": receipt,
    }


__all__ = [
    "CatalogNormalizationField",
    "CatalogDisposition",
    "CatalogRenditionFamily",
    "CatalogSelectionDecision",
    "SOURCE_CATALOG_ITEM_SCHEMA_ID",
    "SOURCE_CATALOG_POLICY_SCHEMA_ID",
    "SOURCE_CATALOG_RECEIPT_SCHEMA_ID",
    "SourceCatalogCandidate",
    "SourceCatalogItem",
    "SourceCatalogSelection",
    "source_catalog_schemas",
]
