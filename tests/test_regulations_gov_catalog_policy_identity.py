"""Joined Federal Register format choices are part of the saved policy identity: a v1.3.0 member round-trips
unchanged, while any changed rendition preference order, max-candidates or failure-fallback value refuses.

The refusal is ValueError from from_member, so a saved policy whose configuration no longer matches the
installed version cannot silently change joined-FR behavior; max-candidates must be an exact int (a bool or
float refuses).
"""

import pytest

from docspec.application.regulations_gov_catalog import RegulationsGovCatalogPolicy
from tests.support.regulations_gov import _policy


@pytest.mark.parametrize("field,changed", [
    ("federalRegisterRenditionPreference", ["body_html_url", "full_text_xml_url", "html_url", "pdf_url"]),
    ("federalRegisterMaxCandidates", 2),
    ("federalRegisterMaxCandidates", True),
    ("federalRegisterMaxCandidates", 1.0),
    ("federalRegisterAcquisitionFailureFallback", True),
    ("federalRegisterAcquisitionFailureFallback", 0),
    ("federalRegisterAcquisitionFailureFallback", 0.0),
])
def test_joined_fr_preference_round_trips_and_refuses_changed_behavior(field, changed):
    policy = _policy()
    member = policy.to_member()
    assert policy.policy_version == "1.3.0"
    assert member["configuration"]["federalRegisterRenditionPreference"] == [
        "full_text_xml_url", "body_html_url", "html_url", "pdf_url",
    ]
    assert member["configuration"]["federalRegisterMaxCandidates"] == 1
    assert member["configuration"]["federalRegisterAcquisitionFailureFallback"] is False
    assert RegulationsGovCatalogPolicy.from_member(member).policy_digest == policy.policy_digest
    member["configuration"][field] = changed
    with pytest.raises(ValueError, match="installed policy version"):
        RegulationsGovCatalogPolicy.from_member(member)
