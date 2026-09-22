"""Joined Federal Register rendition policy: a matched filing contributes exactly one best body in preference
order (full text XML, body HTML, HTML, PDF) while every usable offer is retained as evidence but only the
selected one becomes a processing candidate.

Covers Regulations.gov files outranking all FR offers, a shared XML/HTML locator not erasing the XML offer, no
usable offer or an ambiguous filing yielding UNAVAILABLE with no fetch task, and the hashed policy declaring the
single-body rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docspec.domain.source_catalog import CatalogDisposition
from tests.support.regulations_gov import (
    _build,
    _document,
    _federal_register,
    _federal_register_filing,
    _interpretation,
    _policy,
    _rendition,
)

_FORMATS = (
    ("full_text_xml_url", "body-xml", "application/xml"),
    ("body_html_url", "body-html", "text/html"),
    ("html_url", "html", "text/html"),
    ("pdf_url", "pdf", "application/pdf"),
)


def _offers(identity: str = "2026-10001") -> tuple[dict, ...]:
    return tuple(
        _rendition(
            identity,
            f"{identity}/{rendition}",
            f"https://www.federalregister.gov/{identity}/{rendition}",
            source_field=source_field,
            media_type=media_type,
        )
        for source_field, rendition, media_type in _FORMATS
    )


def _source_order(offers):
    """Sort offers into source-record order, by sourceRecordId then renditionId."""
    return tuple(sorted(offers, key=lambda value: (value["sourceRecordId"], value["renditionId"])))


def _offered_ids(item) -> tuple[str, ...]:
    """The federal-register family's offered rendition ids from the rendition-preference interpretation."""
    preference = _interpretation(item, "rendition-preference")
    return next(
        family["offeredRenditionIds"] for family in preference["families"] if family["familyId"] == "federal-register"
    )


@pytest.mark.parametrize("first_usable", range(4))
def test_join_selects_one_best_body_and_preserves_every_usable_offer(tmp_path: Path, first_usable: int):
    offers = tuple(
        dict(value, locator=None) if index < first_usable else value for index, value in enumerate(_offers())
    )
    native = _federal_register()
    native["record"].update({value["sourceField"]: value["locator"] for value in offers})
    item = _build(
        tmp_path, _document(), federal_register_records=(native,), federal_register_renditions=_source_order(offers)
    )

    assert item.disposition is CatalogDisposition.SELECTED
    (candidate,) = item.candidate_renditions
    expected = offers[first_usable]
    assert (candidate.rendition_id, candidate.locator, candidate.media_type) == (
        f"federal-register/{expected['renditionId']}",
        expected["locator"],
        expected["mediaType"],
    )
    assert _offered_ids(item) == tuple(f"federal-register/{value['renditionId']}" for value in offers[first_usable:])
    assert item.to_dict()["sourceNativeFacts"][-1]["fields"] == native["record"]
    # This is the runtime's fetch list; evidence offers do not become extra tasks.
    (processing_candidate,) = item.to_processing_item().candidates
    assert processing_candidate.locator == expected["locator"]


def test_regulations_files_still_win_over_all_federal_register_offers(tmp_path: Path):
    document = _document()
    regulations = tuple(
        _rendition(
            document["sourceRecordId"],
            f"file-{index}",
            f"https://downloads.regulations.gov/document-{index}.pdf",
            source_field=f"data.attributes.fileFormats[{index}]",
            media_type="application/pdf",
        )
        for index in range(2)
    )
    offers = _offers()
    item = _build(
        tmp_path, document, document_renditions=regulations, federal_register_renditions=_source_order(offers)
    )

    assert [candidate.rendition_id for candidate in item.candidate_renditions] == [
        "regulations-gov/file-0",
        "regulations-gov/file-1",
    ]
    assert _interpretation(item, "rendition-preference")["selectedFamilyId"] == "regulations-gov-file"
    assert _offered_ids(item) == tuple(f"federal-register/{value['renditionId']}" for value in offers)


def test_shared_xml_and_html_locator_does_not_erase_the_xml_offer(tmp_path: Path):
    offers = list(_offers())
    offers[1] = dict(offers[1], locator=offers[0]["locator"])
    item = _build(tmp_path, _document(), federal_register_renditions=_source_order(offers))

    (candidate,) = item.candidate_renditions
    assert candidate.media_type == "application/xml"
    assert candidate.rendition_id.endswith("/body-xml")
    assert set(_offered_ids(item)) == {f"federal-register/{value['renditionId']}" for value in offers}


def test_no_usable_federal_register_offer_produces_no_fetch_task(tmp_path: Path):
    offers = tuple(dict(value, locator=None) for value in _offers())
    item = _build(tmp_path, _document(), federal_register_renditions=_source_order(offers))
    assert item.disposition is CatalogDisposition.UNAVAILABLE
    assert item.to_processing_item().candidates == ()
    assert _offered_ids(item) == ()


def test_representation_selection_cannot_choose_between_ambiguous_filings(tmp_path: Path):
    records = tuple(_federal_register_filing("2026-10001", day) for day in ("2026-03-04", "2026-03-08"))
    offers = tuple(offer for record in records for offer in _offers(record["sourceRecordId"]))
    item = _build(
        tmp_path, _document(), federal_register_records=records, federal_register_renditions=_source_order(offers)
    )

    assert item.disposition is CatalogDisposition.UNAVAILABLE
    assert item.to_processing_item().candidates == ()
    join = next(
        value
        for value in _interpretation(item, "exact-join")["joins"]
        if value["joinId"] == "document-federal-register"
    )
    assert join["outcome"] == "no-match"
    assert join["matchedSourceRecordId"] is None


def test_hashed_policy_declares_the_single_federal_register_body_rule():
    configuration = _policy().to_member()["configuration"]
    assert configuration["renditionPreference"] == ["regulations-gov-file", "federal-register"]
    assert configuration["federalRegisterRenditionPreference"] == [
        "full_text_xml_url",
        "body_html_url",
        "html_url",
        "pdf_url",
    ]
    assert configuration["federalRegisterMaxCandidates"] == 1
