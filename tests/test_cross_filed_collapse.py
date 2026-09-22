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
from docspec.application.regulations_gov_catalog.documents import _item_from_row
from docspec.domain.identity import canonical_json_bytes
from docspec.errors import IntegrityError
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
    """Build the regulations.gov catalog policy with the document selector and DHS agency names."""
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

    def __init__(self, description, records, renditions=()):
        self._description = description
        self._records = records
        self._renditions = renditions

    def describe(self):
        return self._description

    def iter_records(self):
        yield from self._records

    def iter_renditions(self):
        yield from self._renditions


def native_record(document_id: str, docket_id: str, agency_id: str) -> dict[str, Any]:
    """Build a source-native regulations.gov document record with the given ids."""
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
    """The collapse is only real if the retained filing survives the loader and workspace to reach the
    policy byte for byte, not merely present.

    The discarded record and its renditions cross a closed workspace shape and a frozen dataclass, either
    of which silently omitting them would leave the build green and the evidence gone.
    """

    from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
    from docspec.adapters.catalog_artifact.inputs import _CatalogPolicyInputs
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
    cross_file_rendition = {
        "sourceRecordId": "DHS_FRDOC_0001-2740",
        "renditionId": "uscis-content",
        "sourceField": "fileFormats",
        "mediaType": "application/pdf",
        "locator": "https://example.test/uscis.pdf",
        "expectedSha256": "sha256:" + "4" * 64,
        "expectedByteSize": 1024,
    }

    with SqliteCatalogPolicyWorkspace(directory=tmp_path) as workspace:
        inputs = _CatalogPolicyInputs(
            [
                _Source(description, (owner,)),
                _Source(description, (cross_file,), (cross_file_rendition,)),
            ],
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
    # Byte-for-byte in the sense the platform means it. Dict equality would hold
    # across 1 vs 1.0 and across key reordering, which are precisely what a
    # canonical-JSON round trip through the workspace could introduce, so the
    # comparison is over encoded bytes rather than structure.
    assert canonical_json_bytes(retained["record"]) == canonical_json_bytes(cross_file)
    assert canonical_json_bytes(retained["renditions"]) == canonical_json_bytes(
        [cross_file_rendition]
    )


def test_the_retained_filing_is_emitted_as_an_item_observation(tmp_path) -> None:
    """Arrival at the policy row is not arrival at the item: the policy must write the retained filing
    where a consumer can read it, byte for byte, under a schema-accepted key."""

    from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace

    owner = native_record("DHS_FRDOC_0001-2740", "DHS_FRDOC_0001", "DHS")
    owner["record"]["data"]["attributes"].update(
        {"documentType": "Rule", "postedDate": "2026-08-25T00:00:00Z"}
    )
    cross_file = native_record("DHS_FRDOC_0001-2740", "USCIS-2025-0040", "USCIS")
    # Given a rendition so both tests exercise the half that carries file
    # evidence. The workspace test already proves renditions survive the round
    # trip; leaving this one empty made the two inconsistent for no reason.
    rendition = {
        "sourceRecordId": "DHS_FRDOC_0001-2740",
        "renditionId": "uscis-content",
        "sourceField": "fileFormats",
        "mediaType": "application/pdf",
        "locator": "https://example.test/uscis.pdf",
        "expectedSha256": "sha256:" + "4" * 64,
        "expectedByteSize": 1024,
    }
    carried = {
        "reasonCode": "source.cross-filed-under-another-agency",
        "reason": "the same document is mirrored under another agency",
        "record": cross_file,
        "renditions": [rendition],
    }
    owner_rendition = {
        **rendition,
        "renditionId": "dhs-content",
        "locator": "https://example.test/dhs.pdf",
    }
    selected_policy = policy()
    with SqliteCatalogPolicyWorkspace(directory=tmp_path) as workspace:
        item = _item_from_row(
            owner,
            (owner_rendition,),
            workspace,
            sample_drawn=None,
            budget_available=True,
            discarded_filings=(carried,),
            agency_names=selected_policy.agency_names,
            language=selected_policy.language,
            source_url_template=selected_policy.source_url_template,
            sample=selected_policy.sample,
            max_selected_items=selected_policy.max_selected_items,
            interpretation_pin=selected_policy._interpretation_pin,
        )
    observations = item.to_dict()["sourceObservations"]

    assert [o["observationKey"] for o in observations] == ["cross-file-discard/0"]
    emitted = observations[0]["observationValue"]
    assert canonical_json_bytes(emitted["record"]) == canonical_json_bytes(cross_file)
    assert canonical_json_bytes(emitted["renditions"]) == canonical_json_bytes([rendition])
    assert emitted["reasonCode"] == "source.cross-filed-under-another-agency"
    assert item.source_item_id == owner["sourceRecordId"]
    assert item.normalized_metadata["title"] == "Filed by DHS"
    assert canonical_json_bytes(item.source_native_facts[0]["fields"]) == canonical_json_bytes(
        owner["record"]
    )
    assert item.selection.disposition.value == "selected"
    assert [candidate.locator for candidate in item.candidate_renditions] == [owner_rendition["locator"]]


def test_the_observation_shape_satisfies_the_installed_item_schema() -> None:
    """No schema version moves, so the emitted shape must fit the installed sourceObservations schema:
    additionalProperties false over exactly observationKey and observationValue, with an unconstrained value."""

    from docspec.adapters.catalog_artifact.schemas import _SCHEMAS

    schema = _SCHEMAS["source-item.schema.json"]
    observation = schema["properties"]["sourceObservations"]["items"]
    assert observation["additionalProperties"] is False
    assert set(observation["properties"]) == {"observationKey", "observationValue"}
    # An unconstrained value is what lets a whole discarded filing live here.
    assert observation["properties"]["observationValue"] == {}


def test_a_row_carrying_a_discard_survives_the_join_index_round_trip() -> None:
    """The join index must round-trip a row carrying a discard; a real build found that `_carried_discards`
    wrote a third key its reader still refused, which no fixture without a collision could expose."""

    from docspec.application.regulations_gov_catalog.indexed_rows import (
        _carried_discards,
        _stored_discards,
        _stored_row,
    )

    class _Row:
        record = {"sourceRecordId": "DHS_FRDOC_0001-2740"}
        renditions = ()
        discarded_filings = ({"reasonCode": "source.cross-filed-under-another-agency"},)

    stored = {
        "record": dict(_Row.record),
        "renditions": [],
        **_carried_discards(_Row()),
    }
    assert set(stored) == {"record", "renditions", "discardedFilings"}

    record, renditions = _stored_row(stored)
    assert record == _Row.record
    assert renditions == ()
    assert _stored_discards(stored) == _Row.discarded_filings


def test_a_row_without_discards_still_refuses_an_unknown_key() -> None:
    """Widening for one optional key must not open the shape generally."""

    from docspec.application.regulations_gov_catalog.indexed_rows import _stored_row

    with pytest.raises(IntegrityError):
        _stored_row({"record": {}, "renditions": [], "somethingElse": 1})
