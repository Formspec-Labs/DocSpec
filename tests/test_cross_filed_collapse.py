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


# --- the retained filing must ARRIVE, not merely be written -------------------
# Written before the carrying code exists and confirmed red, so it constrains
# the implementation rather than describing it. A test that only checked the
# observation was written would pass against a chain that drops it.


class _Source:
    """One source-native input, shaped as the loader consumes it."""

    def __init__(self, description, records):
        self._description = description
        self._records = records

    def describe(self):
        return self._description

    def iter_records(self):
        yield from self._records

    def iter_renditions(self):
        return
        yield


def native_record(document_id: str, docket_id: str, agency_id: str) -> dict[str, Any]:
    return {
        "sourceRecordId": document_id,
        "scopeId": "regulations-gov-documents",
        "schemaName": "regulations-gov-document-raw",
        "schemaVersion": "1.0",
        "schemaDigest": "sha256:" + "0" * 64,
        "fieldDiagnostics": [],
        "record": {
            "data": {
                "id": document_id,
                "type": "documents",
                "attributes": {
                    "docketId": docket_id,
                    "agencyId": agency_id,
                    "title": f"Filed by {agency_id}",
                },
            }
        },
    }


def test_the_discarded_filing_reaches_the_policy_byte_for_byte(tmp_path) -> None:
    """The collapse is only real if the retained filing survives the chain.

    It crosses a closed workspace shape and a frozen dataclass between being
    written and being read. Either one silently omitting it would leave the
    build green and the evidence gone, which is the failure this asserts
    against -- the discarded record and its renditions, byte for byte, not
    merely present.
    """

    from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
    from docspec.adapters.source_catalog_artifact import _CatalogPolicyInputs
    from docspec.ports.source_catalog import SourceNativeDescription

    description = SourceNativeDescription(
        logical_id="urn:test:release",
        artifact_digest="sha256:" + "1" * 64,
        source_system_id="https://api.regulations.gov",
        source_system_version="v4",
        source_state_scope="complete-snapshot",
        source_state_digest="sha256:" + "2" * 64,
        source_native_schema_set_digest="sha256:" + "3" * 64,
    )
    owner = native_record("DHS_FRDOC_0001-2740", "DHS_FRDOC_0001", "DHS")
    cross_file = native_record("DHS_FRDOC_0001-2740", "USCIS-2025-0040", "USCIS")

    with SqliteCatalogPolicyWorkspace(directory=tmp_path) as workspace:
        inputs = _CatalogPolicyInputs(
            [_Source(description, (owner,)), _Source(description, (cross_file,))],
            [description, description],
            [SELECTOR],
            workspace,
            policy(),
        )
        rows = list(inputs.iter_universe_rows())

    assert len(rows) == 1, "the two filings must collapse to one row"
    surviving = rows[0]
    assert surviving.record["record"]["data"]["attributes"]["agencyId"] == "DHS"

    assert len(surviving.discarded_filings) == 1
    retained = surviving.discarded_filings[0]
    assert retained["reasonCode"] == "source.cross-filed-under-another-agency"
    # Byte-for-byte, against the input rather than against a re-derivation.
    assert retained["record"] == cross_file
    assert retained["renditions"] == []
