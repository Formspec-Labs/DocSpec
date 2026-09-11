"""Document normalization, exact joins, selection and provenance."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

from docspec.application.catalog_policy import (
    catalog_interpretations,
    observed_topics,
    selection_failure,
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
    CatalogRenditionFamily,
    CatalogSelectionDecision,
    SourceCatalogCandidate,
    SourceCatalogItem,
    SourceCatalogSelection,
)
from docspec.errors import IntegrityError
from docspec.ports.source_catalog import (
    CatalogPolicyWorkspace,
)

from .indexed_rows import (
    _DOCKET_INDEX,
    _FEDERAL_REGISTER_INDEX,
    _FEDERAL_REGISTER_KEY_PATH,
    _indexed_row,
    _lookup_key,
)
from .records import (
    _ACQUIRED_SOURCE_SCOPE,
    _DOCKET_SCOPE,
    _FEDERAL_REGISTER_SCOPE,
    _NORMALIZED_FIELDS,
    _RENDITION_ORDER,
    _REQUIRED_NORMALIZED_FIELDS,
    _agency,
    _candidate_from_rendition,
    _IndexedRow,
    _join_result,
    _no_rendition_selection,
    _record_data,
    _source_fact,
    _source_identifier,
    _source_observations,
    _test_fixture_selection,
)
from .sampling import (
    RegulationsGovSamplePolicy,
    _sampling_result,
)


def _item_from_row(
    record: Mapping[str, Any],
    renditions: tuple[Mapping[str, Any], ...],
    workspace: CatalogPolicyWorkspace,
    *,
    sample_drawn: bool | None,
    budget_available: bool,
    discarded_filings: tuple[Mapping[str, Any], ...] = (),
    agency_names: Mapping[str, str],
    interpretation_pin: Callable[[], Mapping[str, Any]],
    language: str,
    max_selected_items: int | None,
    sample: RegulationsGovSamplePolicy | None,
    source_url_template: str,
) -> SourceCatalogItem:
    native, attributes = _record_data(record, expected_type="documents")
    source_item_id = str(record["sourceRecordId"])
    data = native["data"]
    if not isinstance(data, Mapping) or data.get("id") != source_item_id:
        raise IntegrityError("Regulations.gov document source identity differs")
    docket_id, malformed_docket_id = _source_identifier(attributes.get("docketId"))
    fr_doc_num, malformed_fr_doc_num = _source_identifier(attributes.get("frDocNum"))
    docket, federal_register = _document_joins(workspace, docket_id, fr_doc_num)

    normalized, normalization_fields = _document_normalization(
        source_item_id,
        data,
        attributes,
        docket_id,
        malformed_docket_id,
        docket,
        federal_register,
        agency_names=agency_names,
        language=language,
        source_url_template=source_url_template,
    )

    offers, families, selected_family = _rendition_preference(
        renditions,
        federal_register[1] if federal_register is not None else (),
    )
    withdrawn = attributes.get("withdrawn") is True
    candidates = () if withdrawn else offers
    selected_family_id = None if withdrawn else selected_family
    selection, decisions = _document_selection(
        source_item_id,
        attributes,
        normalized,
        candidates,
        withdrawn=withdrawn,
        sample_drawn=sample_drawn,
        budget_available=budget_available,
        max_selected_items=max_selected_items,
        sample=sample,
    )

    topics = observed_topics(
        attributes.get("topics"),
        scheme="regulations.gov",
        identity_fields=("id", "slug"),
        label_fields=("label", "name"),
    )
    facts, observations = _document_provenance(
        record,
        docket,
        federal_register,
        malformed_fr_doc_num,
        discarded_filings,
    )
    join_rows = (
        _join_result(
            join_id="document-docket",
            source_field="data.attributes.docketId",
            source_value=docket_id,
            lookup_scope_id=_DOCKET_SCOPE,
            matched=docket,
        ),
        _join_result(
            join_id="document-federal-register",
            source_field="data.attributes.frDocNum",
            source_value=fr_doc_num,
            lookup_scope_id=_FEDERAL_REGISTER_SCOPE,
            matched=federal_register,
        ),
    )
    sampling_result = _sampling_result(
        source_item_id, withdrawn=withdrawn, sample_drawn=sample_drawn, workspace=workspace, sample=sample
    )
    interpretations = catalog_interpretations(
        interpretation_pin(),
        joins=join_rows,
        normalization_fields=normalization_fields,
        ordered_family_ids=_RENDITION_ORDER,
        families=families,
        selected_family_id=selected_family_id,
        candidates=candidates,
        sampling_result=sampling_result,
        selection=selection,
        decisions=decisions,
        topic_source_field="data.attributes.topics",
        topics=topics,
    )
    # Neither date leaves required `publicationDate` absent, so `selection`
    # above is already DELETED, EXCLUDED, or FAILED: the placeholder never
    # reaches a SELECTED item, and one bad row cannot abort a long build.
    # `FederalRegisterCatalogPolicy` uses the same `"unknown"` fallback.
    raw_issued_version = attributes.get("modifyDate") or attributes.get("postedDate")
    source_issued_version = (
        raw_issued_version if isinstance(raw_issued_version, str) and raw_issued_version else "unknown"
    )
    return SourceCatalogItem(
        source_item_id=source_item_id,
        document_id=source_item_id,
        source_issued_version=source_issued_version,
        source_native_facts=tuple(facts),
        normalized_metadata=normalized,
        source_observed_topics=topics,
        source_observations=tuple(observations),
        interpretations=interpretations,
        candidate_renditions=candidates,
        selection=selection,
    )


def _document_provenance(
    record: Mapping[str, Any],
    docket: _IndexedRow | None,
    federal_register: _IndexedRow | None,
    malformed_fr_doc_num: tuple[Any, ...],
    discarded_filings: tuple[Mapping[str, Any], ...],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Retain matched facts, source diagnostics, and discarded filing evidence."""
    facts = [_source_fact(record)]
    if docket is not None:
        facts.append(_source_fact(docket[0]))
    if federal_register is not None:
        facts.append(_source_fact(federal_register[0]))
    observations = _source_observations(
        (
            ("document", record),
            ("docket", docket[0] if docket is not None else None),
            ("federal-register", federal_register[0] if federal_register is not None else None),
        )
    )
    if malformed_fr_doc_num:
        observations.append(
            {
                "observationKey": "unparseableFederalRegisterDocumentNumber",
                "observationValue": malformed_fr_doc_num[0],
            }
        )
    # A filing this document was cross-filed under, collapsed by the loader
    # and kept here rather than dropped. Decision 0004: the two filings of a
    # real cross-filed document were measured to differ in 8 of 84 and 6 of
    # 90 leaf fields, so the discarded side carries evidence -- a docket
    # association and a Federal Register volume citation that exist on one
    # side only. sourceObservations already takes a free-form key and an
    # unconstrained value, so this needs no schema version.
    observations.extend(
        {
            "observationKey": f"cross-file-discard/{index}",
            "observationValue": dict(filing),
        }
        for index, filing in enumerate(discarded_filings)
    )
    return facts, observations


def _document_normalization(
    source_item_id: str,
    data: Mapping[str, Any],
    attributes: Mapping[str, Any],
    docket_id: str | None,
    malformed_docket_id: tuple[Any, ...],
    docket: _IndexedRow | None,
    federal_register: _IndexedRow | None,
    *,
    agency_names: Mapping[str, str],
    language: str,
    source_url_template: str,
) -> tuple[dict[str, Any], tuple[CatalogNormalizationField, ...]]:
    """Keep each normalized field beside its source paths and rejected values."""
    title, malformed_title = _text(attributes.get("title"))
    agencies, malformed_agencies = _agency(attributes.get("agencyId"), agency_names)
    document_type, malformed_document_type = _text(attributes.get("documentType"))
    publication_date, malformed_publication_date = _instant_date(attributes.get("postedDate"))
    modified_date, malformed_modified_date = _instant_date(attributes.get("modifyDate"))
    comment_close_date, malformed_comment_close_date = _instant_date(attributes.get("commentEndDate"))
    raw_rins: list[Any] = []
    additional_rins = attributes.get("additionalRins")
    if isinstance(additional_rins, list):
        raw_rins.extend(additional_rins)
    elif additional_rins is not None:
        raw_rins.append(additional_rins)
    if docket is not None:
        _, docket_attributes = _record_data(docket[0], expected_type="dockets")
        if docket_attributes.get("rin") is not None:
            raw_rins.append(docket_attributes["rin"])
    if federal_register is not None:
        fr_native = federal_register[0].get("record")
        if not isinstance(fr_native, Mapping):
            raise IntegrityError("Federal Register join payload must be an object")
        fr_rins = fr_native.get("regulation_id_numbers")
        if isinstance(fr_rins, list):
            raw_rins.extend(fr_rins)
        elif fr_rins is not None:
            raw_rins.append(fr_rins)
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
        source_url = source_url_template.replace("{documentId}", quote(source_item_id, safe=""))
        source_url_from_policy = True

    normalized = {
        "title": title,
        "agencies": agencies,
        "documentType": document_type,
        "publicationDate": publication_date,
        "lastUpdatedDate": modified_date,
        "docketIds": docket_ids,
        "regulationIdentifierNumbers": rins,
        "commentCloseDate": comment_close_date,
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
            (
                "data.attributes.additionalRins",
                "joinedDocket.data.attributes.rin",
                "joinedFederalRegister.regulation_id_numbers",
            ),
            rins,
            unparseable_values=malformed_rins,
        ),
        _field_outcome(
            "commentCloseDate",
            ("data.attributes.commentEndDate",),
            comment_close_date,
            unparseable_values=malformed_comment_close_date,
        ),
        _field_outcome("language", ("policy.configuration.language",), language, value_source="policy"),
        _field_outcome(
            "sourceUrl",
            ("policy.configuration.sourceUrlTemplate" if source_url_from_policy else "data.links.self",),
            source_url,
            value_source="policy" if source_url_from_policy else "source",
            unparseable_values=malformed_source_url,
        ),
    )
    if tuple(value.normalized_field for value in normalization_fields) != _NORMALIZED_FIELDS:
        raise AssertionError("Regulations.gov normalization field order drifted")

    return normalized, normalization_fields


def _document_joins(
    workspace: CatalogPolicyWorkspace, docket_id: str | None, fr_doc_num: str | None
) -> tuple[_IndexedRow | None, _IndexedRow | None]:
    """Resolve declared exact keys and check what the index returned."""
    docket = _indexed_row(workspace, _DOCKET_INDEX, docket_id)
    federal_register = _indexed_row(
        workspace,
        _FEDERAL_REGISTER_INDEX,
        fr_doc_num,
    )
    if docket is not None and docket[0]["sourceRecordId"] != docket_id:
        raise IntegrityError("Regulations.gov docket join returned a different exact key")
    if federal_register is not None and (_lookup_key(federal_register[0], _FEDERAL_REGISTER_KEY_PATH) != fr_doc_num):
        # Compares the field the index was keyed on. Comparing
        # sourceRecordId here was correct only while the two were the same
        # string; once the producer made it composite this guard could
        # never agree, and the reason it never fired is that the lookup was
        # returning None for every document instead.
        raise IntegrityError("Federal Register join returned a different exact key")

    return docket, federal_register


def _document_selection(
    source_item_id: str,
    attributes: Mapping[str, Any],
    normalized: Mapping[str, Any],
    candidates: tuple[SourceCatalogCandidate, ...],
    *,
    withdrawn: bool,
    sample_drawn: bool | None,
    budget_available: bool,
    max_selected_items: int | None,
    sample: RegulationsGovSamplePolicy | None,
) -> tuple[SourceCatalogSelection, tuple[CatalogSelectionDecision, ...]]:
    """Stop at the first document decision, preserving its ordered evidence.

    Documents include sampling and record successful budget checks only
    when configured. Docket/comment selection has different recorded rules.
    """
    decisions: list[CatalogSelectionDecision] = []
    fixture_selection = _test_fixture_selection(source_item_id)
    if fixture_selection is not None:
        return selection_failure(decisions, "publisher-test-fixture", fixture_selection)
    decisions.append(CatalogSelectionDecision("publisher-test-fixture", True))
    if withdrawn:
        reason_withdrawn, _ = _text(attributes.get("reasonWithdrawn"))
        reason = "The source marks this document withdrawn."
        if reason_withdrawn is not None:
            reason = f"The source marks this document withdrawn: {reason_withdrawn}"
        return selection_failure(
            decisions,
            "source-withdrawal",
            SourceCatalogSelection(
                CatalogDisposition.DELETED,
                "source.withdrawn-after-publication",
                reason,
            ),
        )
    decisions.append(CatalogSelectionDecision("source-withdrawal", True))
    if sample_drawn is False:
        limit = sample.per_partition_limit if sample is not None else 0
        reason = (
            "The deterministic stratified sample takes at most "
            f"{limit} items per document type; this item was not drawn."
        )
        return selection_failure(
            decisions,
            "sample-draw",
            SourceCatalogSelection(
                CatalogDisposition.EXCLUDED,
                "policy.sample-not-drawn",
                reason,
            ),
        )
    if sample_drawn is True:
        decisions.append(CatalogSelectionDecision("sample-draw", True))
    missing = [name for name in _REQUIRED_NORMALIZED_FIELDS if not normalized[name]]
    if missing:
        reason = "Required normalized catalog values are unusable: " + ", ".join(missing)
        return selection_failure(
            decisions,
            "required-metadata",
            SourceCatalogSelection(
                CatalogDisposition.FAILED,
                "source.normalized-field-missing",
                reason,
            ),
        )
    decisions.append(CatalogSelectionDecision("required-metadata", True))
    if not candidates:
        reason = (
            "Neither the acquired source record nor its exact "
            "Federal Register match offers a usable rendition." + _ACQUIRED_SOURCE_SCOPE
        )
        return selection_failure(
            decisions,
            "candidate-rendition",
            _no_rendition_selection(attributes, reason),
        )
    decisions.append(CatalogSelectionDecision("candidate-rendition", True))
    if not budget_available:
        return selection_failure(
            decisions,
            "selected-item-budget",
            SourceCatalogSelection(
                CatalogDisposition.EXCLUDED,
                "policy.item-budget-exhausted",
                "The catalog selected-item budget is already exhausted.",
            ),
        )
    if max_selected_items is not None:
        decisions.append(CatalogSelectionDecision("selected-item-budget", True))
    return SourceCatalogSelection(CatalogDisposition.SELECTED), tuple(decisions)


def _rendition_preference(
    regulations_renditions: tuple[Mapping[str, Any], ...], federal_register_renditions: tuple[Mapping[str, Any], ...]
) -> tuple[tuple[SourceCatalogCandidate, ...], tuple[CatalogRenditionFamily, ...], str | None]:
    by_family: dict[str, list[SourceCatalogCandidate]] = {family: [] for family in _RENDITION_ORDER}

    def add(
        family: str,
        values: tuple[Mapping[str, Any], ...],
        *,
        prefix: str,
    ) -> None:
        claimed: set[str] = set()
        for value in values:
            candidate = _candidate_from_rendition(
                value,
                rendition_id=f"{prefix}{value['renditionId']}",
            )
            if candidate is None or candidate.locator in claimed:
                continue
            claimed.add(candidate.locator)
            by_family[family].append(candidate)

    add("regulations-gov-file", regulations_renditions, prefix="regulations-gov/")
    add("federal-register", federal_register_renditions, prefix="federal-register/")
    ordered = {
        family: tuple(sorted(by_family[family], key=lambda value: _utf16_key(value.rendition_id)))
        for family in _RENDITION_ORDER
    }
    families = tuple(
        CatalogRenditionFamily(
            family,
            tuple(value.rendition_id for value in ordered[family]),
        )
        for family in _RENDITION_ORDER
    )
    selected_family = next((family for family in _RENDITION_ORDER if ordered[family]), None)
    return (
        ordered[selected_family] if selected_family is not None else (),
        families,
        selected_family,
    )
