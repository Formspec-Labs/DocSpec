"""A cross-filed document collapses to its measured owner, keeping the other filing.

DocSpec decision 0004. The owner is chosen by two measured tests rather than by
preference, and the non-owning filing is retained as an observation rather than
dropped, because the two filings of a real cross-filed document were measured to
differ in 8 of 84 and 6 of 90 leaf fields.
"""

from __future__ import annotations

from typing import Any

import pytest

from docspec.application.regulations_gov_catalog import RegulationsGovCatalogPolicy
from docspec.ports.source_catalog import SourceInputSelector

SELECTOR = SourceInputSelector(
    "https://api.regulations.gov", "v4", "regulations-gov-documents",
    "regulations-gov-document-raw", "1.0",
)


def row(document_id: str, docket_id: str, agency_id: str, *, title: str = "A rule") -> dict[str, Any]:
    """One loader row, shaped as `_CatalogPolicyInputs._load` stores it."""

    return {
        "sourceIndex": 0,
        "record": {
            "sourceRecordId": document_id,
            "scopeId": "regulations-gov-documents",
            "schemaName": "regulations-gov-document-raw",
            "schemaVersion": "1.0",
            "schemaDigest": "sha256:" + "0" * 64,
            "record": {
                "data": {
                    "id": document_id,
                    "type": "documents",
                    "attributes": {
                        "docketId": docket_id,
                        "agencyId": agency_id,
                        "title": title,
                    },
                }
            },
        },
        "renditions": [],
    }


def policy() -> RegulationsGovCatalogPolicy:
    return RegulationsGovCatalogPolicy(
        document_input=SELECTOR,
        docket_input=None,
        federal_register_input=None,
        agency_names={"DHS": "Department of Homeland Security"},
    )


# The two records that block the 671-input catalog-A build, and the owner each
# resolves to. Neither is caught by both tests, so both rules carry weight.
REAL_COLLISIONS = [
    pytest.param(
        row("DHS_FRDOC_0001-2737", "DHS_FRDOC_0001", "DHS"),
        row("DHS_FRDOC_0001-2737", "DHS_FRDOC_0001", "CISA"),
        id="2737-cisa-fails-docket-to-agency",
    ),
    pytest.param(
        row("DHS_FRDOC_0001-2740", "DHS_FRDOC_0001", "DHS"),
        row("DHS_FRDOC_0001-2740", "USCIS-2025-0040", "USCIS"),
        id="2740-uscis-fails-document-to-docket",
    ),
]


@pytest.mark.parametrize("owner_row,cross_file", REAL_COLLISIONS)
def test_the_owning_filing_is_chosen_and_the_other_is_kept(owner_row, cross_file) -> None:
    resolution = policy().resolve_source_record_collision(SELECTOR, owner_row, cross_file)
    assert resolution is not None
    assert resolution.owner is owner_row
    assert resolution.discarded is cross_file
    assert resolution.reason_code == "source.cross-filed-under-another-agency"


@pytest.mark.parametrize("owner_row,cross_file", REAL_COLLISIONS)
def test_the_answer_does_not_depend_on_arrival_order(owner_row, cross_file) -> None:
    """Releases are read in whatever order the inputs list gives, so the rule
    must not select whichever filing happened to be stored first."""

    resolution = policy().resolve_source_record_collision(SELECTOR, cross_file, owner_row)
    assert resolution is not None
    assert resolution.owner is owner_row


def test_two_indistinguishable_filings_keep_the_refusal() -> None:
    """Returning None is how the loader's refusal stays in charge.

    Guessing here would convert a repeat nobody has measured into a silent
    collapse, which is the failure 0004 exists to avoid.
    """

    both_valid = policy().resolve_source_record_collision(
        SELECTOR,
        row("DHS_FRDOC_0001-2737", "DHS_FRDOC_0001", "DHS"),
        row("DHS_FRDOC_0001-2737", "DHS_FRDOC_0001", "DHS"),
    )
    assert both_valid is None

    neither_valid = policy().resolve_source_record_collision(
        SELECTOR,
        row("X-1", "UNRELATED-2020-0001", "AAA"),
        row("X-1", "UNRELATED-2020-0002", "BBB"),
    )
    assert neither_valid is None


def test_prefix_containment_admits_a_two_segment_sequence() -> None:
    """40,485 of 1,797,201 real records carry a two-segment sequence.

    Reading the rule as "docket plus one trailing segment" reports every one of
    them as a violation, so this pins containment rather than segment counting.
    """

    owner = row("DOT-OST-1995-125-0050-0001", "DOT-OST-1995-125", "DOT")
    assert policy()._owns_its_filing(owner) is True


def test_a_payload_this_policy_cannot_read_does_not_own_anything() -> None:
    """An unreadable row must not resolve; the loader refuses instead."""

    broken = {"sourceIndex": 0, "record": {"sourceRecordId": "A-1"}, "renditions": []}
    assert policy()._owns_its_filing(broken) is False
    assert policy().resolve_source_record_collision(SELECTOR, broken, broken) is None
