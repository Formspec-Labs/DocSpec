"""Regulations.gov source values, facts, rendition offers and recorded selection rules."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from docspec.application.catalog_policy import (
    http_url as _http_url,
    selection_failure,
    text_value as _text,
    utf16_key as _utf16_key,
)
from docspec.domain.source_catalog import (
    CatalogDisposition,
    CatalogRenditionFamily,
    CatalogSelectionDecision,
    SourceCatalogCandidate,
    SourceCatalogSelection,
)
from docspec.errors import IntegrityError

_IndexedRow = tuple[Mapping[str, Any], tuple[Mapping[str, Any], ...]]

_DOCUMENT_SCOPE = "regulations-gov-documents"

_DOCUMENT_SCHEMA = "regulations-gov-document-raw"

_DOCKET_SCOPE = "regulations-gov-dockets"

_DOCKET_SCHEMA = "regulations-gov-docket-raw"

_COMMENT_SCOPE = "regulations-gov-comments"

_COMMENT_SCHEMA = "regulations-gov-comment-raw"

_FEDERAL_REGISTER_SCOPE = "federal-register-documents"

_FEDERAL_REGISTER_SCHEMA = "federal-register-document"
_FEDERAL_REGISTER_SCHEMA_VERSION = "1.1"

_SCHEMA_VERSION = "1.0"

_RENDITION_ORDER = ("regulations-gov-file", "federal-register")

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

#: Appended to every "no rendition" reason. The disposition is a statement about
#: the records this build acquired, and a reader reasonably hears it as a
#: statement about the document. Measured 2026-09-04: of 13 catalog-A items
#: randomly sampled from the 865,206 carrying this reason, 13 had downloadable
#: files at regulations.gov, in the `attachments` relationship that Mirrulations
#: -- the acquired source -- does not mirror. The disposition was accurate about
#: its input and read as a claim about the world.
_ACQUIRED_SOURCE_SCOPE = (
    " This states what the acquired source contains, not whether the publisher holds content for it."
)

#: The publisher's ``restrictReasonType`` values, each mapped to one reason
#: code, verbatim. Measured on catalog-A 2026-09-04: Copyrighted 53,580,
#: Other 19,965, Confidential Business Information 1,179, Personally
#: Identifiable Information 252 -- 74,976 of the 865,206 unavailable rows
#: carry one, and no sampled row carrying one had a file at the publisher
#: (decision 0005). A value outside this map is never inferred into a bucket:
#: the row fails with :data:`_RESTRICT_REASON_UNREAD` so the receipt shows it.
_PUBLISHER_WITHHOLDING_CODES: Mapping[str, str] = {
    "Copyrighted": "source.publisher-withheld.copyrighted",
    "Confidential Business Information": ("source.publisher-withheld.confidential-business-information"),
    "Personally Identifiable Information": ("source.publisher-withheld.personally-identifiable-information"),
    "Other": "source.publisher-withheld.other",
}

_RESTRICT_REASON_UNREAD = "source.restrict-reason-unread"

#: Regulations.gov publishes its own internal test fixtures through the same
#: public API as real filings, under one of these three source item id
#: prefixes -- anchored at the start, case-sensitive as the publisher writes
#: them. They 404 at both document and docket level on the public API, so
#: they carry no evidence a build could acquire. Measured independently twice
#: over catalog-A 2026-09-04: 41 items total (37 ``failed``, 4 ``deleted``).
#: Left alone they read as real acquisition failures and real withdrawals;
#: neither is true, so they are excluded under their own reason code instead.
_TEST_FIXTURE_ID_PREFIXES: tuple[str, ...] = ("TRAIN-", "ERULE-", "TEST-")

_TEST_FIXTURE_REASON_CODE = "source.publisher-test-fixture"


def _source_fact(record: Mapping[str, Any]) -> dict[str, Any]:
    native = record.get("record")
    if not isinstance(native, Mapping):
        raise IntegrityError("source-native record payload must be an object")
    return {
        "scopeId": record["scopeId"],
        "schemaName": record["schemaName"],
        "schemaVersion": record["schemaVersion"],
        "schemaDigest": record["schemaDigest"],
        "fields": dict(native),
    }


def _record_data(record: Mapping[str, Any], *, expected_type: str) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    native = record.get("record")
    if not isinstance(native, Mapping):
        raise IntegrityError(f"Regulations.gov {expected_type} payload must be an object")
    data = native.get("data")
    if not isinstance(data, Mapping) or data.get("type") != expected_type:
        raise IntegrityError(f"Regulations.gov {expected_type} payload has different data")
    attributes = data.get("attributes")
    if not isinstance(attributes, Mapping):
        raise IntegrityError(f"Regulations.gov {expected_type} attributes must be an object")
    return native, attributes


def _source_identifier(value: object) -> tuple[str | None, tuple[Any, ...]]:
    return _text(value)


def _agency(
    value: object,
    agency_names: Mapping[str, str],
) -> tuple[list[dict[str, str]], tuple[Any, ...]]:
    agency_id, malformed = _text(value)
    if agency_id is None:
        return [], malformed
    agency_name = agency_names.get(agency_id)
    if not isinstance(agency_name, str) or not agency_name:
        return [], (agency_id,)
    return [{"agencyId": agency_id, "agencyName": agency_name}], ()


def _join_result(
    *,
    join_id: str,
    source_field: str,
    source_value: str | None,
    lookup_scope_id: str,
    matched: tuple[Mapping[str, Any], tuple[Mapping[str, Any], ...]] | None,
) -> dict[str, Any]:
    if source_value is None:
        outcome = "not-stated"
    elif matched is None:
        outcome = "no-match"
    else:
        outcome = "matched"
    return {
        "joinId": join_id,
        "sourceField": source_field,
        "sourceValue": source_value,
        "lookupScopeId": lookup_scope_id,
        "outcome": outcome,
        "matchedSourceRecordId": (matched[0]["sourceRecordId"] if matched is not None else None),
    }


def _candidate_from_rendition(
    value: Mapping[str, Any],
    *,
    rendition_id: str,
) -> SourceCatalogCandidate | None:
    locator = value.get("locator")
    if locator is None:
        return None
    if not isinstance(locator, str) or not locator:
        raise IntegrityError("Regulations.gov rendition locator must be nonempty text or null")
    expected_sha256 = value.get("expectedSha256")
    expected_byte_size = value.get("expectedByteSize")
    source_url = _http_url(locator)
    if source_url is not None:
        locator_kind = "source-url"
    elif locator.startswith("sha256:"):
        if expected_sha256 != locator:
            raise IntegrityError("immutable-object locator differs from its supplied expected SHA-256")
        if isinstance(expected_byte_size, bool) or not isinstance(expected_byte_size, int) or expected_byte_size < 0:
            raise IntegrityError("immutable-object candidate requires a supplied non-negative byte size")
        locator_kind = "immutable-object"
    else:
        raise IntegrityError("Regulations.gov rendition locator kind is not supported")
    return SourceCatalogCandidate(
        rendition_id=rendition_id,
        media_type=value["mediaType"],
        locator_kind=locator_kind,
        locator=locator,
        expected_sha256=expected_sha256,
        expected_byte_size=expected_byte_size,
    )


def _no_rendition_selection(attributes: Mapping[str, Any], acquired_reason: str) -> SourceCatalogSelection:
    """Disposition a record with no usable rendition by what the publisher declared.

    ``restrictReasonType`` is the publisher's own statement that it withholds
    the content, so a row carrying one is unavailable for that declared reason.
    A row without one is unavailable for ``acquired_reason``, which says only
    what the acquired source contains. Nothing is inferred in either direction.
    """

    declared = attributes.get("restrictReasonType")
    if declared is None:
        return SourceCatalogSelection(CatalogDisposition.UNAVAILABLE, "source.no-candidate-rendition", acquired_reason)
    code = _PUBLISHER_WITHHOLDING_CODES.get(declared) if isinstance(declared, str) else None
    if code is None:
        return SourceCatalogSelection(
            CatalogDisposition.FAILED,
            _RESTRICT_REASON_UNREAD,
            f"The publisher declares a restrictReasonType this policy does not read: {declared!r}.",
        )
    reason = f"The publisher withholds this record's content; restrictReasonType is {declared!r}"
    subtype = attributes.get("subtype")
    if isinstance(subtype, str) and subtype:
        reason += f" and subtype is {subtype!r}"
    return SourceCatalogSelection(CatalogDisposition.UNAVAILABLE, code, reason + ".")


def _test_fixture_selection(source_item_id: str) -> SourceCatalogSelection | None:
    """Exclude a Regulations.gov internal test fixture, or defer with ``None``.

    Matches only at the start of the source item id, case-sensitive, against
    :data:`_TEST_FIXTURE_ID_PREFIXES`. A real filing merely containing one of
    these letter groups (``EPA-TRAIN-2020-0001``, ``PRETEST-1``) is untouched.
    """

    if not source_item_id.startswith(_TEST_FIXTURE_ID_PREFIXES):
        return None
    return SourceCatalogSelection(
        CatalogDisposition.EXCLUDED,
        _TEST_FIXTURE_REASON_CODE,
        f"The source item id {source_item_id!r} carries a Regulations.gov"
        " internal test-fixture prefix (TRAIN-, ERULE- or TEST-).",
    )


def _selection_result(
    *,
    source_item_id: str,
    attributes: Mapping[str, Any],
    withdrawn: bool,
    withdrawal_reason: str | None,
    missing_fields: Sequence[str],
    candidates: tuple[SourceCatalogCandidate, ...],
    budget_available: bool,
) -> tuple[SourceCatalogSelection, tuple[CatalogSelectionDecision, ...]]:
    decisions: list[CatalogSelectionDecision] = []
    fixture_selection = _test_fixture_selection(source_item_id)
    if fixture_selection is not None:
        return selection_failure(decisions, "publisher-test-fixture", fixture_selection)
    decisions.append(CatalogSelectionDecision("publisher-test-fixture", True))
    if withdrawn:
        reason = "The source marks this item withdrawn."
        if withdrawal_reason is not None:
            reason = f"The source marks this item withdrawn: {withdrawal_reason}"
        selection = SourceCatalogSelection(
            CatalogDisposition.DELETED,
            "source.withdrawn-after-publication",
            reason,
        )
        return selection_failure(decisions, "source-withdrawal", selection)
    decisions.append(CatalogSelectionDecision("source-withdrawal", True))
    if missing_fields:
        reason = "Required normalized fields are absent or unparseable: " + ", ".join(missing_fields)
        selection = SourceCatalogSelection(
            CatalogDisposition.FAILED,
            "source.normalized-field-missing",
            reason,
        )
        return selection_failure(decisions, "required-metadata", selection)
    decisions.append(CatalogSelectionDecision("required-metadata", True))
    if not candidates:
        selection = _no_rendition_selection(
            attributes,
            "The acquired source record offers no usable rendition." + _ACQUIRED_SOURCE_SCOPE,
        )
        return selection_failure(decisions, "candidate-rendition", selection)
    decisions.append(CatalogSelectionDecision("candidate-rendition", True))
    if not budget_available:
        selection = SourceCatalogSelection(
            CatalogDisposition.EXCLUDED,
            "policy.item-budget-exhausted",
            "The catalog selected-item budget is already exhausted.",
        )
        return selection_failure(decisions, "selected-item-budget", selection)
    decisions.append(CatalogSelectionDecision("selected-item-budget", True))
    return SourceCatalogSelection(CatalogDisposition.SELECTED), tuple(decisions)


def _source_observations(rows: Sequence[tuple[str, Mapping[str, Any] | None]]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for prefix, source_record in rows:
        if source_record is None:
            continue
        diagnostics = source_record.get("fieldDiagnostics")
        if isinstance(diagnostics, list):
            observations.extend(
                {
                    "observationKey": f"{prefix}/field-diagnostic/{index}",
                    "observationValue": value,
                }
                for index, value in enumerate(diagnostics)
            )
    return observations


def _source_record_candidate(raw_source_url: object) -> SourceCatalogCandidate | None:
    locator = _http_url(raw_source_url)
    if locator is None:
        return None
    return SourceCatalogCandidate(
        rendition_id="regulations-gov/source-record",
        media_type="application/vnd.api+json",
        locator_kind="source-url",
        locator=locator,
    )


def _source_kind_rendition_preference(
    renditions: tuple[Mapping[str, Any], ...], raw_source_url: object, *, include_files: bool
) -> tuple[tuple[SourceCatalogCandidate, ...], tuple[CatalogRenditionFamily, ...], str | None, tuple[str, ...]]:
    order = ("regulations-gov-file", "regulations-gov-record") if include_files else ("regulations-gov-record",)
    by_family: dict[str, list[SourceCatalogCandidate]] = {family: [] for family in order}
    claimed: set[str] = set()
    if include_files:
        for value in renditions:
            candidate = _candidate_from_rendition(
                value,
                rendition_id=f"regulations-gov/{value['renditionId']}",
            )
            if candidate is None or candidate.locator in claimed:
                continue
            claimed.add(candidate.locator)
            by_family["regulations-gov-file"].append(candidate)
    record_candidate = _source_record_candidate(raw_source_url)
    if record_candidate is not None and record_candidate.locator not in claimed:
        by_family["regulations-gov-record"].append(record_candidate)
    ordered = {
        family: tuple(sorted(by_family[family], key=lambda value: _utf16_key(value.rendition_id))) for family in order
    }
    families = tuple(
        CatalogRenditionFamily(
            family,
            tuple(value.rendition_id for value in ordered[family]),
        )
        for family in order
    )
    selected_family = next((family for family in order if ordered[family]), None)
    return (
        ordered[selected_family] if selected_family is not None else (),
        families,
        selected_family,
        order,
    )
