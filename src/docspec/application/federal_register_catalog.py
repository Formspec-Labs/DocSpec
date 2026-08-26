"""DocSpec's first source interpretation policy: Federal Register records."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from docspec.application.catalog_policy import (
    array_with_unparseable as _array_with_unparseable,
    date_value as _date_value,
    http_url as _http_url,
    http_url_value as _http_url_value,
    normalization_field as _field_outcome,
    normalized_rins as _normalized_rins,
    observed_topics,
    strings as _strings,
    text_value as _text,
    utf16_key as _utf16_key,
)

from docspec.domain.source_catalog import (
    CatalogDisposition,
    CatalogRenditionFamily,
    CatalogSelectionDecision,
    SourceCatalogCandidate,
    SourceCatalogItem,
    SourceCatalogSelection,
)
from docspec.domain.identity import canonical_json_bytes, closed_mapping, sha256_digest
from docspec.errors import IntegrityError
from docspec.ports.source_catalog import (
    CatalogPolicyInputs,
    CatalogPolicyWorkspace,
    SourceInputSelector,
    SourceNativeDescription,
)

_RENDITION_ORDER = ("body_html_url", "html_url", "pdf_url")
_NORMALIZED_FIELDS = (
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
)
_REQUIRED_NORMALIZED_FIELDS = (
    "title",
    "agencies",
    "documentType",
    "publicationDate",
    "sourceUrl",
)
_SELECTION_DECISION_ORDER = ("required-metadata", "candidate-rendition")
_SELECTION_FAILURES = (
    (
        _SELECTION_DECISION_ORDER[0],
        CatalogDisposition.FAILED,
        "source.normalized-field-missing",
    ),
    (
        _SELECTION_DECISION_ORDER[1],
        CatalogDisposition.UNAVAILABLE,
        "source.no-candidate-rendition",
    ),
)


def _agencies(value: object) -> tuple[list[dict[str, str]], tuple[Any, ...]]:
    result: dict[tuple[str, str], dict[str, str]] = {}
    values, rejected = _array_with_unparseable(value)
    unparseable = list(rejected)
    for raw in values:
        if not isinstance(raw, Mapping):
            unparseable.append(raw)
            continue
        name = raw.get("name") or raw.get("raw_name")
        identity = raw.get("slug") or name
        if isinstance(identity, str) and identity and isinstance(name, str) and name:
            result[(identity, name)] = {"agencyId": identity, "agencyName": name}
        else:
            unparseable.append(dict(raw))
    return (
        [
            result[key]
            for key in sorted(result, key=lambda value: tuple(_utf16_key(part) for part in value))
        ],
        tuple(unparseable),
    )


def _topics(value: object) -> tuple[dict[str, str], ...]:
    return observed_topics(
        value,
        scheme="federalregister.gov",
        identity_fields=("slug", "id"),
        label_fields=("name", "label"),
    )


@dataclass(frozen=True, slots=True)
class FederalRegisterCatalogPolicy:
    """Map faithful Federal Register rows into complete DocSpec catalog rows."""

    expected_source_system_id: str

    policy_id = "urn:docspec:catalog-policy:federal-register:1"
    policy_version = "1.0.0"

    @property
    def universe_input(self) -> SourceInputSelector:
        return SourceInputSelector(
            self.expected_source_system_id,
            "v1",
            "federal-register-documents",
            "federal-register-document",
            "1.0",
        )

    @property
    def configuration(self) -> Mapping[str, Any]:
        return {
            "sourceProfile": "federal-register",
            "universeInput": self.universe_input.to_dict(),
            "language": "en",
            "normalizationFields": list(_NORMALIZED_FIELDS),
            "rinNormalization": "federal-register-rin-syntax/1",
            "requiredNormalizedFields": list(_REQUIRED_NORMALIZED_FIELDS),
            "renditionPreference": list(_RENDITION_ORDER),
            "topicRecovery": {
                "sourceField": "record.topics",
                "emptyOutcome": "not-recovered",
                "publisherDeclaredEmptyEvidenceDigest": None,
            },
            "selectionFailures": [
                {
                    "decisionId": decision_id,
                    "disposition": disposition.value,
                    "reasonCode": reason_code,
                }
                for decision_id, disposition, reason_code in _SELECTION_FAILURES
            ],
        }

    def to_member(self) -> dict[str, Any]:
        return {
            "format": "docspec-catalog-policy",
            "formatVersion": "1.0",
            "policyId": self.policy_id,
            "policyVersion": self.policy_version,
            "configuration": dict(self.configuration),
        }

    @property
    def policy_digest(self) -> str:
        return sha256_digest(canonical_json_bytes(self.to_member()))

    @classmethod
    def from_member(cls, value: object) -> FederalRegisterCatalogPolicy:
        member = closed_mapping(
            value,
            {"format", "formatVersion", "policyId", "policyVersion", "configuration"},
            "Federal Register catalog policy",
            error=ValueError,
        )
        configuration = closed_mapping(
            member["configuration"],
            {
                "language",
                "normalizationFields",
                "requiredNormalizedFields",
                "sourceProfile",
                "universeInput",
                "rinNormalization",
                "renditionPreference",
                "topicRecovery",
                "selectionFailures",
            },
            "Federal Register catalog policy configuration",
            error=ValueError,
        )
        selector = SourceInputSelector.from_dict(configuration["universeInput"])
        policy = cls(selector.source_system_id)
        if member != policy.to_member():
            raise ValueError("Federal Register catalog policy differs from the installed policy version")
        return policy

    def iter_items(
        self,
        inputs: CatalogPolicyInputs,
        workspace: CatalogPolicyWorkspace,
    ) -> Iterator[SourceCatalogItem]:
        del workspace
        for row in inputs.iter_universe_rows():
            yield self._item_from_row(row.description, row.record, row.renditions)

    def _item_from_row(
        self,
        source: SourceNativeDescription,
        record: Mapping[str, Any],
        renditions: tuple[Mapping[str, Any], ...],
    ) -> SourceCatalogItem:
        if source.source_system_id != self.expected_source_system_id:
            raise IntegrityError("Federal Register policy received a different source system")
        native = record.get("record")
        if not isinstance(native, Mapping):
            raise IntegrityError("Federal Register source-native record payload must be an object")
        source_item_id = record["sourceRecordId"]
        document_id = native.get("document_number")
        if not isinstance(document_id, str) or not document_id:
            document_id = source_item_id
        title, malformed_title = _text(native.get("title"))
        agencies, malformed_agencies = _agencies(native.get("agencies"))
        document_type, malformed_document_type = _text(native.get("type"))
        publication_date, malformed_publication_date = _date_value(
            native.get("publication_date")
        )
        issued_version = publication_date or str(native.get("publication_date") or "unknown")
        docket_ids, malformed_docket_ids = _strings(native.get("docket_ids"))
        rins, malformed_rins = _normalized_rins(native.get("regulation_id_numbers"))
        comment_close_date, malformed_comment_close_date = _date_value(
            native.get("comments_close_on")
        )
        source_url, malformed_source_url = _http_url_value(native.get("html_url"))
        candidates, rendition_families, selected_family_id = self._rendition_preference(
            renditions
        )
        normalized = {
            "title": title,
            "agencies": agencies,
            "documentType": document_type,
            "publicationDate": publication_date,
            "lastUpdatedDate": None,
            "docketIds": docket_ids,
            "regulationIdentifierNumbers": rins,
            "commentCloseDate": comment_close_date,
            "language": "en",
            "sourceUrl": source_url,
        }
        normalization_fields = (
            _field_outcome(
                "title",
                ("record.title",),
                title,
                unparseable_values=malformed_title,
            ),
            _field_outcome(
                "agencies",
                ("record.agencies",),
                agencies,
                unparseable_values=malformed_agencies,
            ),
            _field_outcome(
                "documentType",
                ("record.type",),
                document_type,
                unparseable_values=malformed_document_type,
            ),
            _field_outcome(
                "publicationDate",
                ("record.publication_date",),
                publication_date,
                unparseable_values=malformed_publication_date,
            ),
            _field_outcome("lastUpdatedDate", (), None),
            _field_outcome(
                "docketIds",
                ("record.docket_ids",),
                docket_ids,
                unparseable_values=malformed_docket_ids,
            ),
            _field_outcome(
                "regulationIdentifierNumbers",
                ("record.regulation_id_numbers",),
                rins,
                unparseable_values=malformed_rins,
            ),
            _field_outcome(
                "commentCloseDate",
                ("record.comments_close_on",),
                comment_close_date,
                unparseable_values=malformed_comment_close_date,
            ),
            _field_outcome(
                "language",
                ("policy.configuration.language",),
                "en",
                value_source="policy",
            ),
            _field_outcome(
                "sourceUrl",
                ("record.html_url",),
                source_url,
                unparseable_values=malformed_source_url,
            ),
        )
        if tuple(field.normalized_field for field in normalization_fields) != _NORMALIZED_FIELDS:
            raise AssertionError("Federal Register normalization field order drifted")
        missing = [name for name in _REQUIRED_NORMALIZED_FIELDS if not normalized[name]]
        decisions: list[CatalogSelectionDecision] = []
        metadata_decision_id, metadata_disposition, metadata_reason_code = _SELECTION_FAILURES[0]
        if missing:
            reason = "Required normalized catalog values are unusable: " + ", ".join(missing)
            decisions.append(
                CatalogSelectionDecision(
                    metadata_decision_id,
                    False,
                    metadata_disposition,
                    metadata_reason_code,
                    reason,
                )
            )
            selection = SourceCatalogSelection(
                metadata_disposition,
                metadata_reason_code,
                reason,
            )
        else:
            decisions.append(CatalogSelectionDecision(metadata_decision_id, True))
            rendition_decision_id, rendition_disposition, rendition_reason_code = (
                _SELECTION_FAILURES[1]
            )
            if candidates:
                decisions.append(CatalogSelectionDecision(rendition_decision_id, True))
                selection = SourceCatalogSelection(CatalogDisposition.SELECTED)
            else:
                reason = "The source offers no usable rendition to capture."
                decisions.append(
                    CatalogSelectionDecision(
                        rendition_decision_id,
                        False,
                        rendition_disposition,
                        rendition_reason_code,
                        reason,
                    )
                )
                selection = SourceCatalogSelection(
                    rendition_disposition,
                    rendition_reason_code,
                    reason,
                )
        field_diagnostics = record.get("fieldDiagnostics")
        diagnostics = (
            tuple(field_diagnostics)
            if isinstance(field_diagnostics, Sequence)
            and not isinstance(field_diagnostics, (str, bytes, bytearray, memoryview))
            else ()
        )
        observations = tuple(
            {"observationKey": f"field-diagnostic/{index}", "observationValue": value}
            for index, value in enumerate(diagnostics)
        )
        observed_topics = _topics(native.get("topics"))
        topic_outcome = "observed" if observed_topics else "not-recovered"
        interpretation_pin = {
            "policyId": self.policy_id,
            "policyVersion": self.policy_version,
            "policyDigest": self.policy_digest,
            "inputScopeIds": [record["scopeId"]],
        }
        interpretations = (
            {
                "interpretationKind": "normalization",
                **interpretation_pin,
                "result": {"fields": [field.to_dict() for field in normalization_fields]},
            },
            {
                "interpretationKind": "rendition-preference",
                **interpretation_pin,
                "result": {
                    "orderedFamilyIds": list(_RENDITION_ORDER),
                    "families": [family.to_dict() for family in rendition_families],
                    "selectedFamilyId": selected_family_id,
                    "selectedRenditionIds": [candidate.rendition_id for candidate in candidates],
                },
            },
            {
                "interpretationKind": "selection",
                **interpretation_pin,
                "result": {
                    "decisions": [decision.to_dict() for decision in decisions],
                    "finalDisposition": selection.disposition.value,
                    "reasonCode": selection.reason_code,
                    "reason": selection.reason,
                },
            },
            {
                "interpretationKind": "topic-recovery",
                **interpretation_pin,
                "result": {
                    "sourceField": "record.topics",
                    "outcome": topic_outcome,
                    "evidenceDigest": None,
                    "observedTopicIds": [topic["observedTopicId"] for topic in observed_topics],
                },
            },
        )
        return SourceCatalogItem(
            source_item_id=source_item_id,
            document_id=document_id,
            source_issued_version=issued_version,
            source_native_facts=(
                {
                    "scopeId": record["scopeId"],
                    "schemaName": record["schemaName"],
                    "schemaVersion": record["schemaVersion"],
                    "schemaDigest": record["schemaDigest"],
                    "fields": dict(native),
                },
            ),
            normalized_metadata=normalized,
            source_observed_topics=observed_topics,
            source_observations=observations,
            interpretations=interpretations,
            candidate_renditions=candidates,
            selection=selection,
        )

    @staticmethod
    def _rendition_preference(
        renditions: tuple[Mapping[str, Any], ...],
    ) -> tuple[
        tuple[SourceCatalogCandidate, ...],
        tuple[CatalogRenditionFamily, ...],
        str | None,
    ]:
        by_family: dict[str, list[SourceCatalogCandidate]] = {
            family: [] for family in _RENDITION_ORDER
        }
        for value in renditions:
            source_field = value.get("sourceField")
            if source_field not in by_family:
                continue
            locator = _http_url(value.get("locator"))
            if locator is None:
                continue
            by_family[source_field].append(
                SourceCatalogCandidate(
                    rendition_id=value["renditionId"],
                    media_type=value["mediaType"],
                    locator_kind="source-url",
                    locator=locator,
                    expected_sha256=value.get("expectedSha256"),
                    expected_byte_size=value.get("expectedByteSize"),
                )
            )
        ordered_offers = {
            family: tuple(
                sorted(by_family[family], key=lambda value: _utf16_key(value.rendition_id))
            )
            for family in _RENDITION_ORDER
        }
        families = tuple(
            CatalogRenditionFamily(
                family,
                tuple(candidate.rendition_id for candidate in ordered_offers[family]),
            )
            for family in _RENDITION_ORDER
        )
        selected_family_id = next(
            (family for family in _RENDITION_ORDER if ordered_offers[family]),
            None,
        )
        selected = ordered_offers[selected_family_id] if selected_family_id is not None else ()
        return selected, families, selected_family_id


__all__ = ["FederalRegisterCatalogPolicy"]
