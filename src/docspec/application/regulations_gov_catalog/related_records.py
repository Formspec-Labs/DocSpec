"""Regulations.gov comment and docket normalization, selection and source provenance."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

from docspec.application.catalog_policy import (
    catalog_interpretations,
    http_url as _http_url,
    normalization_field as _field_outcome,
    normalized_rins as _normalized_rins,
    text_value as _text,
    utc_instant_date_value as _instant_date,
    utf16_key as _utf16_key,
)
from docspec.domain.source_catalog import (
    CatalogDisposition,
    CatalogNormalizationField,
    SourceCatalogItem,
)
from docspec.errors import IntegrityError
from docspec.ports.source_catalog import (
    CatalogPolicyWorkspace,
)

from .indexed_rows import (
    _DOCKET_INDEX,
    _DOCUMENT_INDEX,
    _indexed_row,
)
from .records import (
    _DOCKET_SCOPE,
    _DOCUMENT_SCOPE,
    _agency,
    _IndexedRow,
    _join_result,
    _record_data,
    _selection_result,
    _source_fact,
    _source_identifier,
    _source_kind_rendition_preference,
    _source_observations,
)


def _docket_item_from_row(
    record: Mapping[str, Any],
    renditions: tuple[Mapping[str, Any], ...],
    *,
    budget_available: bool,
    agency_names: Mapping[str, str],
    interpretation_pin: Callable[[], Mapping[str, Any]],
    language: str,
) -> SourceCatalogItem:
    """Interpret one Regulations.gov docket row into a catalog item."""
    native, attributes = _record_data(record, expected_type="dockets")
    source_item_id = str(record["sourceRecordId"])
    data = native["data"]
    if not isinstance(data, Mapping) or data.get("id") != source_item_id:
        raise IntegrityError("Regulations.gov docket source identity differs")
    title, malformed_title = _text(attributes.get("title"))
    agencies, malformed_agencies = _agency(attributes.get("agencyId"), agency_names)
    document_type, malformed_document_type = _text(attributes.get("docketType"))
    modified_date, malformed_modified_date = _instant_date(attributes.get("modifyDate"))
    rin_value = attributes.get("rin")
    rins, malformed_rins = _normalized_rins([] if rin_value is None else [rin_value])
    source_link = data.get("links")
    raw_source_url = source_link.get("self") if isinstance(source_link, Mapping) else None
    source_url = _http_url(raw_source_url)
    malformed_source_url: tuple[Any, ...] = ()
    source_url_from_policy = False
    if raw_source_url is not None and source_url is None:
        malformed_source_url = (raw_source_url,)
    elif source_url is None:
        source_url = "https://www.regulations.gov/docket/" + quote(source_item_id, safe="")
        source_url_from_policy = True
    normalized = {
        "title": title,
        "agencies": agencies,
        "documentType": document_type,
        "publicationDate": None,
        "lastUpdatedDate": modified_date,
        "docketIds": [source_item_id],
        "regulationIdentifierNumbers": rins,
        "commentCloseDate": None,
        "language": language,
        "sourceUrl": source_url,
    }
    normalization_fields = (
        _field_outcome("title", ("data.attributes.title",), title, unparseable_values=malformed_title),
        _field_outcome("agencies", ("data.attributes.agencyId",), agencies, unparseable_values=malformed_agencies),
        _field_outcome(
            "documentType", ("data.attributes.docketType",), document_type, unparseable_values=malformed_document_type
        ),
        _field_outcome("publicationDate", (), None),
        _field_outcome(
            "lastUpdatedDate",
            ("data.attributes.modifyDate",),
            modified_date,
            unparseable_values=malformed_modified_date,
        ),
        _field_outcome("docketIds", ("data.id",), [source_item_id]),
        _field_outcome(
            "regulationIdentifierNumbers", ("data.attributes.rin",), rins, unparseable_values=malformed_rins
        ),
        _field_outcome("commentCloseDate", (), None),
        _field_outcome("language", ("policy.configuration.language",), language, value_source="policy"),
        _field_outcome(
            "sourceUrl",
            ("policy.configuration.sourceUrlTemplates.dockets" if source_url_from_policy else "data.links.self",),
            source_url,
            value_source="policy" if source_url_from_policy else "source",
            unparseable_values=malformed_source_url,
        ),
    )
    offers, families, selected_family, family_order = _source_kind_rendition_preference(
        renditions,
        raw_source_url,
        include_files=False,
    )
    missing = [name for name in ("title", "agencies", "lastUpdatedDate", "sourceUrl") if not normalized[name]]
    selection, decisions = _selection_result(
        source_item_id=source_item_id,
        attributes=attributes,
        withdrawn=False,
        withdrawal_reason=None,
        missing_fields=missing,
        candidates=offers,
        budget_available=budget_available,
    )
    # A missing modifyDate leaves required `lastUpdatedDate` absent, so
    # `selection` above is never SELECTED and the placeholder never reaches
    # a served item; the same fallback the document row applies.
    raw_issued_version = attributes.get("modifyDate")
    source_issued_version = (
        raw_issued_version if isinstance(raw_issued_version, str) and raw_issued_version else "unknown"
    )
    sampling_result = {
        "frameAdmitted": True,
        "partition": "all",
        "stratum": ["all"],
        "orderHash": None,
        "rank": None,
        "stratumSize": None,
        "allocationMethod": "all",
        "limit": None,
        "drawn": True,
    }
    return SourceCatalogItem(
        source_item_id=source_item_id,
        document_id=source_item_id,
        source_issued_version=source_issued_version,
        source_native_facts=(_source_fact(record),),
        normalized_metadata=normalized,
        source_observed_topics=(),
        source_observations=tuple(_source_observations((("docket", record),))),
        interpretations=catalog_interpretations(
            interpretation_pin(),
            joins=(),
            normalization_fields=normalization_fields,
            ordered_family_ids=family_order,
            families=families,
            selected_family_id=selected_family,
            candidates=offers,
            sampling_result=sampling_result,
            selection=selection,
            decisions=decisions,
            topic_source_field="data.attributes.topics",
        ),
        candidate_renditions=() if selection.disposition is CatalogDisposition.DELETED else offers,
        selection=selection,
    )


def _comment_item_from_row(
    record: Mapping[str, Any],
    renditions: tuple[Mapping[str, Any], ...],
    workspace: CatalogPolicyWorkspace,
    *,
    budget_available: bool,
    agency_names: Mapping[str, str],
    interpretation_pin: Callable[[], Mapping[str, Any]],
    language: str,
) -> SourceCatalogItem:
    """Interpret one Regulations.gov comment row, joining its declared docket and document."""
    native, attributes = _record_data(record, expected_type="comments")
    source_item_id = str(record["sourceRecordId"])
    data = native["data"]
    if not isinstance(data, Mapping) or data.get("id") != source_item_id:
        raise IntegrityError("Regulations.gov comment source identity differs")
    docket_id, malformed_docket_id = _source_identifier(attributes.get("docketId"))
    comment_on_document_id, malformed_comment_on_document_id = _source_identifier(attributes.get("commentOnDocumentId"))
    docket, document = _comment_joins(workspace, docket_id, comment_on_document_id)

    normalized, normalization_fields, raw_source_url = _comment_normalization(
        source_item_id,
        data,
        attributes,
        docket_id,
        malformed_docket_id,
        docket,
        agency_names=agency_names,
        language=language,
    )
    offers, families, selected_family, family_order = _source_kind_rendition_preference(
        renditions,
        raw_source_url,
        include_files=True,
    )
    withdrawn = attributes.get("withdrawn") is True
    candidates = () if withdrawn else offers
    selected_family_id = None if withdrawn else selected_family
    withdrawal_reason, _ = _text(attributes.get("reasonWithdrawn"))
    missing = [name for name in ("agencies", "documentType", "publicationDate", "sourceUrl") if not normalized[name]]
    selection, decisions = _selection_result(
        source_item_id=source_item_id,
        attributes=attributes,
        withdrawn=withdrawn,
        withdrawal_reason=withdrawal_reason,
        missing_fields=missing,
        candidates=candidates,
        budget_available=budget_available,
    )

    source_issued_version, source_facts, observations = _comment_provenance(
        record,
        attributes,
        docket,
        document,
        malformed_comment_on_document_id,
    )
    joins = (
        _join_result(
            join_id="comment-docket",
            source_field="data.attributes.docketId",
            source_value=docket_id,
            lookup_scope_id=_DOCKET_SCOPE,
            matched=docket,
        ),
        _join_result(
            join_id="comment-document",
            source_field="data.attributes.commentOnDocumentId",
            source_value=comment_on_document_id,
            lookup_scope_id=_DOCUMENT_SCOPE,
            matched=document,
        ),
    )
    sampling_result = {
        "frameAdmitted": not withdrawn,
        "partition": None if withdrawn else "all",
        "stratum": [] if withdrawn else ["all"],
        "orderHash": None,
        "rank": None,
        "stratumSize": None,
        "allocationMethod": "all",
        "limit": None,
        "drawn": not withdrawn,
    }
    return SourceCatalogItem(
        source_item_id=source_item_id,
        document_id=source_item_id,
        source_issued_version=source_issued_version,
        source_native_facts=source_facts,
        normalized_metadata=normalized,
        source_observed_topics=(),
        source_observations=tuple(observations),
        interpretations=catalog_interpretations(
            interpretation_pin(),
            joins=joins,
            normalization_fields=normalization_fields,
            ordered_family_ids=family_order,
            families=families,
            selected_family_id=selected_family_id,
            candidates=candidates,
            sampling_result=sampling_result,
            selection=selection,
            decisions=decisions,
            topic_source_field="data.attributes.topics",
        ),
        candidate_renditions=candidates,
        selection=selection,
    )


def _comment_provenance(
    record: Mapping[str, Any],
    attributes: Mapping[str, Any],
    docket: _IndexedRow | None,
    document: _IndexedRow | None,
    malformed_comment_on_document_id: tuple[Any, ...],
) -> tuple[str, tuple[dict[str, Any], ...], list[dict[str, Any]]]:
    """Retain the upstream comment version choice and ordered source evidence.

    Comments require an exact version; the document 'unknown' fallback
    would discard the upstream-selected-version signal here.
    """
    observations = _source_observations(
        (
            ("comment", record),
            ("docket", docket[0] if docket is not None else None),
            ("document", document[0] if document is not None else None),
        )
    )
    modify_date = attributes.get("modifyDate")
    if isinstance(modify_date, str) and modify_date:
        source_issued_version = modify_date
        source_issued_version_field = "data.attributes.modifyDate"
        version_reason = "upstream-selected-newest-comment-version"
    elif modify_date is None:
        posted_date = attributes.get("postedDate")
        if not isinstance(posted_date, str) or not posted_date:
            raise IntegrityError("Regulations.gov comment with null modifyDate has no postedDate fallback")
        source_issued_version = posted_date
        source_issued_version_field = "data.attributes.postedDate"
        version_reason = "upstream-selected-comment-has-null-modify-date"
    else:
        raise IntegrityError("Regulations.gov comment modifyDate must be nonempty text or null")
    observations.append(
        {
            "observationKey": "comment/source-issued-version-policy",
            "observationValue": {
                "exactSourceValue": source_issued_version,
                "sourcePath": source_issued_version_field,
                "reasonCode": version_reason,
                "upstreamVersionPath": "data.attributes.modifyDate",
                "upstreamVersionValue": modify_date,
            },
        }
    )
    if malformed_comment_on_document_id:
        observations.append(
            {
                "observationKey": "comment/unparseable-comment-on-document-id",
                "observationValue": malformed_comment_on_document_id[0],
            }
        )
    observations.sort(key=lambda value: _utf16_key(value["observationKey"]))
    facts = [record]
    if docket is not None:
        facts.append(docket[0])
    if document is not None:
        facts.append(document[0])
    source_facts = tuple(
        _source_fact(value) for value in sorted(facts, key=lambda value: _utf16_key(str(value["scopeId"])))
    )
    return source_issued_version, source_facts, observations


def _comment_normalization(
    source_item_id: str,
    data: Mapping[str, Any],
    attributes: Mapping[str, Any],
    docket_id: str | None,
    malformed_docket_id: tuple[Any, ...],
    docket: _IndexedRow | None,
    *,
    agency_names: Mapping[str, str],
    language: str,
) -> tuple[dict[str, Any], tuple[CatalogNormalizationField, ...], object]:
    """Normalize comment fields, preserving their distinct docket-only RIN source."""
    title, malformed_title = _text(attributes.get("title"))
    agencies, malformed_agencies = _agency(attributes.get("agencyId"), agency_names)
    document_type, malformed_document_type = _text(attributes.get("documentType"))
    publication_date, malformed_publication_date = _instant_date(attributes.get("postedDate"))
    modified_date, malformed_modified_date = _instant_date(attributes.get("modifyDate"))
    raw_rins: list[Any] = []
    if docket is not None:
        _, docket_attributes = _record_data(docket[0], expected_type="dockets")
        if docket_attributes.get("rin") is not None:
            raw_rins.append(docket_attributes["rin"])
    rins, malformed_rins = _normalized_rins(raw_rins)
    docket_ids = [docket_id] if docket_id is not None else []
    source_link = data.get("links")
    raw_source_url = source_link.get("self") if isinstance(source_link, Mapping) else None
    source_url = _http_url(raw_source_url)
    malformed_source_url: tuple[Any, ...] = ()
    source_url_from_policy = False
    if raw_source_url is not None and source_url is None:
        malformed_source_url = (raw_source_url,)
    elif source_url is None:
        source_url = "https://www.regulations.gov/comment/" + quote(source_item_id, safe="")
        source_url_from_policy = True
    normalized = {
        "title": title,
        "agencies": agencies,
        "documentType": document_type,
        "publicationDate": publication_date,
        "lastUpdatedDate": modified_date,
        "docketIds": docket_ids,
        "regulationIdentifierNumbers": rins,
        "commentCloseDate": None,
        "language": language,
        "sourceUrl": source_url,
    }
    normalization_fields = (
        _field_outcome("title", ("data.attributes.title",), title, unparseable_values=malformed_title),
        _field_outcome("agencies", ("data.attributes.agencyId",), agencies, unparseable_values=malformed_agencies),
        _field_outcome(
            "documentType", ("data.attributes.documentType",), document_type, unparseable_values=malformed_document_type
        ),
        _field_outcome(
            "publicationDate",
            ("data.attributes.postedDate",),
            publication_date,
            unparseable_values=malformed_publication_date,
        ),
        _field_outcome(
            "lastUpdatedDate",
            ("data.attributes.modifyDate",),
            modified_date,
            unparseable_values=malformed_modified_date,
        ),
        _field_outcome("docketIds", ("data.attributes.docketId",), docket_ids, unparseable_values=malformed_docket_id),
        _field_outcome(
            "regulationIdentifierNumbers",
            ("joinedDocket.data.attributes.rin",),
            rins,
            unparseable_values=malformed_rins,
        ),
        _field_outcome("commentCloseDate", (), None),
        _field_outcome("language", ("policy.configuration.language",), language, value_source="policy"),
        _field_outcome(
            "sourceUrl",
            ("policy.configuration.sourceUrlTemplates.comments" if source_url_from_policy else "data.links.self",),
            source_url,
            value_source="policy" if source_url_from_policy else "source",
            unparseable_values=malformed_source_url,
        ),
    )
    return normalized, normalization_fields, raw_source_url


def _comment_joins(
    workspace: CatalogPolicyWorkspace, docket_id: str | None, comment_on_document_id: str | None
) -> tuple[_IndexedRow | None, _IndexedRow | None]:
    """Resolve a comment's declared docket and parent document independently."""
    docket = _indexed_row(workspace, _DOCKET_INDEX, docket_id)
    document = _indexed_row(
        workspace,
        _DOCUMENT_INDEX,
        comment_on_document_id,
    )
    if docket is not None and docket[0]["sourceRecordId"] != docket_id:
        raise IntegrityError("Regulations.gov comment docket join returned a different key")
    if document is not None and document[0]["sourceRecordId"] != comment_on_document_id:
        raise IntegrityError("Regulations.gov comment document join returned a different key")

    return docket, document
