"""Exact source joins, field provenance, date policy, and ambiguous Federal Register filings."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
from docspec.application.regulations_gov_catalog import (
    RegulationsGovCatalogPolicy,
)
from docspec.domain.source_catalog import CatalogDisposition
from docspec.errors import IntegrityError
from docspec.ports.source_catalog import CatalogResumePoint, SourceNativeRow
from tests.support.regulations_gov import (
    _build,
    _build_items,
    _description,
    _docket,
    _document,
    _federal_register_filing,
    _interpretation,
    _policy,
    _rendition,
)


def test_exact_joins_preserve_all_three_source_facts_and_normalized_value(
    tmp_path: Path,
) -> None:
    document_id = "EPA-2026-0001-0001"
    item = _build(
        tmp_path,
        _document(document_id),
        document_renditions=(
            _rendition(
                document_id,
                "document-0000",
                f"https://downloads.regulations.gov/{document_id}/content.pdf",
                source_field="data.attributes.fileFormats[0]",
                media_type="application/pdf",
            ),
        ),
    )

    assert item.disposition is CatalogDisposition.SELECTED
    assert [fact["scopeId"] for fact in item.source_native_facts] == [
        "regulations-gov-documents",
        "regulations-gov-dockets",
        "federal-register-documents",
    ]
    assert item.normalized_metadata["regulationIdentifierNumbers"] == (
        "2060-AV12",
        "2060-AX01",
        "2060-AZ99",
    )
    artifact_root = next(
        path for path in tmp_path.iterdir() if path.is_dir() and not path.name.startswith(".")
    )
    receipt = json.loads((artifact_root / "catalog-build-receipt.json").read_text())
    assert receipt["joinCoverage"] == [
        {
            "joinId": "document-docket",
            "eligible": 1,
            "matched": 1,
            "unmatched": 0,
            "nullResult": 0,
        },
        {
            "joinId": "document-federal-register",
            "eligible": 1,
            "matched": 1,
            "unmatched": 0,
            "nullResult": 0,
        },
    ]
    assert item.normalized_metadata["agencies"] == (
        {
            "agencyId": "EPA",
            "agencyName": "Environmental Protection Agency",
        },
    )
    assert [value["outcome"] for value in _interpretation(item, "exact-join")["joins"]] == [
        "matched",
        "matched",
    ]
    assert [value.rendition_id for value in item.candidate_renditions] == [
        "regulations-gov/document-0000"
    ]
    assert _interpretation(item, "rendition-preference")["selectedFamilyId"] == (
        "regulations-gov-file"
    )
    assert {topic["observedTopicId"] for topic in item.source_observed_topics} == {
        "Air quality",
        "source-topic",
    }


def test_document_with_unmatched_docket_stays_selected_with_no_docket_fact(
    tmp_path: Path,
) -> None:
    """Gate B.2 evidence: a document whose docketId has no docket row (a real
    Mirrulations shape for whole agencies, e.g. SEC ships zero docket JSONs)
    must not be excluded from the catalog. It stays SELECTED, its
    document-docket join records outcome "no-match", it carries no docket
    source-native fact and no docket-sourced RIN, and the miss is visible as
    an "unmatched" build-receipt joinCoverage count rather than a per-item
    disposition.
    """

    document_id = "EPA-2026-0001-0001"
    item = _build(
        tmp_path,
        _document(document_id, docketId="EPA-DOES-NOT-EXIST"),
        document_renditions=(
            _rendition(
                document_id,
                "document-0000",
                f"https://downloads.regulations.gov/{document_id}/content.pdf",
                source_field="data.attributes.fileFormats[0]",
                media_type="application/pdf",
            ),
        ),
    )

    assert item.disposition is CatalogDisposition.SELECTED
    joins = _interpretation(item, "exact-join")["joins"]
    docket_join = next(value for value in joins if value["joinId"] == "document-docket")
    assert docket_join["outcome"] == "no-match"
    assert docket_join["sourceValue"] == "EPA-DOES-NOT-EXIST"
    assert docket_join["matchedSourceRecordId"] is None
    assert [fact["scopeId"] for fact in item.source_native_facts] == [
        "regulations-gov-documents",
        "federal-register-documents",
    ]
    assert item.normalized_metadata["docketIds"] == ("EPA-DOES-NOT-EXIST",)
    assert item.normalized_metadata["regulationIdentifierNumbers"] == (
        "2060-AV12",
        "2060-AX01",
    )

    artifact_root = next(
        path for path in tmp_path.iterdir() if path.is_dir() and not path.name.startswith(".")
    )
    receipt = json.loads((artifact_root / "catalog-build-receipt.json").read_text())
    document_docket_coverage = next(
        value for value in receipt["joinCoverage"] if value["joinId"] == "document-docket"
    )
    assert document_docket_coverage["unmatched"] >= 1
    assert document_docket_coverage["matched"] == 0


def test_join_uses_only_document_exact_keys_and_records_no_match(tmp_path: Path) -> None:
    item = _build(
        tmp_path,
        _document(docketId="EPA-DOES-NOT-EXIST", frDocNum="2026-DOES-NOT-EXIST"),
        federal_register_renditions=(),
    )

    joins = _interpretation(item, "exact-join")["joins"]
    assert [value["outcome"] for value in joins] == ["no-match", "no-match"]
    assert len(item.source_native_facts) == 1
    assert item.disposition is CatalogDisposition.UNAVAILABLE


def test_federal_register_rendition_is_only_a_fallback_for_an_exact_match(
    tmp_path: Path,
) -> None:
    item = _build(tmp_path, _document())

    assert item.disposition is CatalogDisposition.SELECTED
    assert [value.rendition_id for value in item.candidate_renditions] == [
        "federal-register/2026-10001/html"
    ]
    assert _interpretation(item, "rendition-preference")["selectedFamilyId"] == (
        "federal-register"
    )


def test_withdrawn_missing_and_unavailable_rows_have_distinct_dispositions(
    tmp_path: Path,
) -> None:
    withdrawn = _build(
        tmp_path / "withdrawn",
        _document(withdrawn=True, reasonWithdrawn="Issued in error"),
    )
    missing = _build(
        tmp_path / "missing",
        _document(title=None),
    )
    unavailable = _build(
        tmp_path / "unavailable",
        _document(frDocNum=None),
        federal_register_records=(),
        federal_register_renditions=(),
    )

    assert withdrawn.disposition is CatalogDisposition.DELETED
    assert withdrawn.selection.reason_code == "source.withdrawn-after-publication"
    assert withdrawn.candidate_renditions == ()
    assert missing.disposition is CatalogDisposition.FAILED
    assert missing.selection.reason_code == "source.normalized-field-missing"
    assert unavailable.disposition is CatalogDisposition.UNAVAILABLE
    assert unavailable.selection.reason_code == "source.no-candidate-rendition"


def test_document_with_no_modify_or_posted_date_gets_an_explicit_disposition(
    tmp_path: Path,
) -> None:
    """A document with neither modifyDate nor postedDate used to hard-abort
    the whole build (IntegrityError); it must instead disposition explicitly
    and let the build continue, the way the superseded passthrough minter
    excluded such rows gracefully.
    """

    item = _build(
        tmp_path,
        _document(modifyDate=None, postedDate=None),
    )

    assert item.disposition is CatalogDisposition.FAILED
    assert item.selection.reason_code == "source.normalized-field-missing"
    assert "publicationDate" in (item.selection.reason or "")
    assert item.source_issued_version == "unknown"


def test_docket_with_no_modify_date_gets_an_explicit_disposition(tmp_path: Path) -> None:
    """The same proof on the docket row: a missing modifyDate leaves required
    `lastUpdatedDate` absent, so the row is never SELECTED and the version
    placeholder is never served -- it must not abort the whole build.
    """

    items = _build_items(
        tmp_path,
        (),
        docket_records=(_docket(include_link=True, modifyDate=None),),
        federal_register_records=(),
        federal_register_renditions=(),
    )

    item = next(value for value in items if value.source_item_id == "EPA-2026-0001")
    assert item.disposition is CatalogDisposition.FAILED
    assert item.selection.reason_code == "source.normalized-field-missing"
    assert "lastUpdatedDate" in (item.selection.reason or "")
    assert item.source_issued_version == "unknown"


def test_dates_are_strict_and_policy_member_round_trips(tmp_path: Path) -> None:
    item = _build(
        tmp_path,
        _document(postedDate="2026-08-24T04:00:00+00:00"),
    )
    policy = _policy()

    assert item.disposition is CatalogDisposition.FAILED
    normalization = _interpretation(item, "normalization")["fields"]
    publication = next(
        value for value in normalization if value["normalizedField"] == "publicationDate"
    )
    assert publication["outcome"] == "unparseable"
    assert RegulationsGovCatalogPolicy.from_member(policy.to_member()).to_member() == (
        policy.to_member()
    )


def test_a_composite_source_record_id_still_joins_on_the_bare_number(
    tmp_path: Path,
) -> None:
    """The regression this whole change exists for.

    DocSpec 0003 made the Federal Register sourceRecordId composite so a reused
    number stops discarding the older filing. The index was keyed on that field
    and the document side looks up by a bare frDocNum, so every one of 499,238
    lookups missed and the build still reported pass.
    """
    item = _build(
        tmp_path,
        _document(frDocNum="2026-10001"),
        federal_register_records=(
            _federal_register_filing("2026-10001", "2026-03-04"),
        ),
        federal_register_renditions=(),
    )

    joins = {value["joinId"]: value for value in _interpretation(item, "exact-join")["joins"]}
    federal_register = joins["document-federal-register"]
    assert federal_register["outcome"] == "matched"
    assert federal_register["matchedSourceRecordId"] == "2026-10001@2026-03-04"


def test_a_reused_federal_register_number_matches_neither_filing(tmp_path: Path) -> None:
    """Abstention, not an arbitrary winner.

    Under composite identity 474 numbers carry more than one filing. Keeping the
    latest publication_date would reproduce the pre-fix coverage exactly, which
    is why it is tempting and why it is wrong -- it re-asserts the collapse 0003
    removed, and would attach 00-111's BLM plat notice to a document that may
    have meant the IRS rule filed under the same number four days earlier.
    Measured population for this refusal: 30 documents of 430,323 matches.
    """
    item = _build(
        tmp_path,
        _document(frDocNum="2026-10001"),
        federal_register_records=(
            _federal_register_filing("2026-10001", "2026-03-04", title="First filing"),
            _federal_register_filing("2026-10001", "2026-03-08", title="Second filing"),
        ),
        federal_register_renditions=(),
    )

    federal_register = {
        value["joinId"]: value for value in _interpretation(item, "exact-join")["joins"]
    }["document-federal-register"]
    assert federal_register["outcome"] == "no-match"
    assert federal_register["matchedSourceRecordId"] is None
    assert federal_register["sourceValue"] == "2026-10001"


def test_one_ambiguous_number_does_not_suppress_an_unambiguous_one(
    tmp_path: Path,
) -> None:
    """The refusal is per key. A global abstention would be the same outage again."""
    item = _build(
        tmp_path,
        _document(frDocNum="2026-10002"),
        federal_register_records=(
            _federal_register_filing("2026-10001", "2026-03-04"),
            _federal_register_filing("2026-10001", "2026-03-08"),
            _federal_register_filing("2026-10002", "2026-03-09"),
        ),
        federal_register_renditions=(),
    )

    federal_register = {
        value["joinId"]: value for value in _interpretation(item, "exact-join")["joins"]
    }["document-federal-register"]
    assert federal_register["outcome"] == "matched"
    assert federal_register["matchedSourceRecordId"] == "2026-10002@2026-03-09"


@pytest.mark.parametrize("kind", ["documents", "dockets", "comments"])
@pytest.mark.parametrize("language", ["en", "\ud800"], ids=["valid-policy", "unencodable-policy"])
def test_direct_policy_refuses_bad_source_before_reading_its_identity(
    tmp_path: Path, kind: str, language: str,
) -> None:
    """An early source refusal precedes policy serialization, even outside the builder."""
    selected_policy = replace(_policy(include_comments=True), language=language)
    selector = selected_policy.document_input
    source_row = SourceNativeRow(
        description=_description("documents", selector.source_system_id, selector.source_system_version),
        record={
            "scopeId": f"regulations-gov-{kind}",
            "sourceRecordId": "EPA-2026-0001",
            "record": None,
        },
        renditions=(),
    )

    class Inputs:
        resume = CatalogResumePoint(indexed=True, after=None, selected_count=0)

        def iter_universe_rows(self):
            yield source_row

    with SqliteCatalogPolicyWorkspace(directory=tmp_path) as workspace:
        with pytest.raises(IntegrityError, match=f"^Regulations.gov {kind} payload must be an object$"):
            next(selected_policy.iter_items(Inputs(), workspace))

    assert selected_policy._policy_digest is None
    if language != "en":
        with pytest.raises(UnicodeEncodeError):
            _ = selected_policy.policy_digest
