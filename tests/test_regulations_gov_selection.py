"""Regulations.gov selection precedence, sampling, budgets, refusal accounting, and resume."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
from docspec.application.regulations_gov_catalog import (
    RegulationsGovCatalogPolicy,
    RegulationsGovSamplePolicy,
)
from docspec.domain.source_catalog import CatalogDisposition, SourceCatalogItem
from docspec.errors import IntegrityError
from tests.helpers import CountItems, KillAfter
from tests.support.regulations_gov import (
    _COMMENT_SYSTEM,
    _DOCKET_SYSTEM,
    _DOCUMENT_SYSTEM,
    _FEDERAL_REGISTER_SYSTEM,
    _REGULATIONS_VERSION,
    _build,
    _build_items,
    _build_result,
    _comment,
    _description,
    _docket,
    _document,
    _interpretation,
    _items,
    _policy,
    _rendition,
    _Source,
)


def test_stratified_sample_is_deterministic_and_accounts_for_undrawn_rows(
    tmp_path: Path,
) -> None:
    identities = tuple(f"EPA-2026-0001-{value:04d}" for value in range(1, 5))
    documents = tuple(_document(identity) for identity in identities)
    renditions = tuple(
        _rendition(
            identity,
            "document-0000",
            f"https://downloads.regulations.gov/{identity}/content.pdf",
            source_field="data.attributes.fileFormats[0]",
            media_type="application/pdf",
        )
        for identity in identities
    )
    base = _policy()
    policy = RegulationsGovCatalogPolicy(
        base.document_input,
        base.docket_input,
        base.federal_register_input,
        base.agency_names,
        base.language,
        base.source_url_template,
        RegulationsGovSamplePolicy("stable-seed", 1),
    )

    first = _build_items(
        tmp_path / "first",
        documents,
        policy=policy,
        document_renditions=renditions,
    )
    repeated = _build_items(
        tmp_path / "repeated",
        documents,
        policy=policy,
        document_renditions=renditions,
    )

    first_documents = tuple(item for item in first if item.source_item_id in identities)
    repeated_documents = tuple(
        item for item in repeated if item.source_item_id in identities
    )
    assert [item.source_item_id for item in first_documents] == list(identities)
    assert [item.selection.to_dict() for item in repeated_documents] == [
        item.selection.to_dict() for item in first_documents
    ]
    selected = [
        item.source_item_id
        for item in first_documents
        if item.disposition is CatalogDisposition.SELECTED
    ]
    expected = min(
        identities,
        key=lambda identity: (
            hashlib.md5(
                f"{identity}:stable-seed".encode(),
                usedforsecurity=False,
            ).hexdigest(),
            identity,
        ),
    )
    assert selected == [expected]
    excluded = [
        item for item in first_documents if item.disposition is CatalogDisposition.EXCLUDED
    ]
    assert len(excluded) == 3
    assert {item.selection.reason_code for item in excluded} == {"policy.sample-not-drawn"}
    sampling = [_interpretation(item, "sampling") for item in first_documents]
    assert all(value["frameAdmitted"] is True for value in sampling)
    assert all(value["allocationMethod"] == "rank-over-sqrt-stratum-size" for value in sampling)
    assert all(value["limit"] == 1 for value in sampling)
    assert sorted(value["rank"] for value in sampling) == [1, 2, 3, 4]
    assert {value["stratumSize"] for value in sampling} == {4}
    assert sum(value["drawn"] is True for value in sampling) == 1
    assert all(
        _interpretation(item, "selection")["decisions"][-1]["decisionId"]
        == "sample-draw"
        for item in excluded
    )
    assert RegulationsGovCatalogPolicy.from_member(policy.to_member()).to_member() == (
        policy.to_member()
    )


def test_selected_item_budget_runs_after_source_and_rendition_checks(
    tmp_path: Path,
) -> None:
    identities = ("EPA-2026-0001-0001", "EPA-2026-0001-0002")
    base = _policy()
    policy = RegulationsGovCatalogPolicy(
        base.document_input,
        base.docket_input,
        base.federal_register_input,
        base.agency_names,
        base.language,
        base.source_url_template,
        None,
        1,
    )
    items = _build_items(
        tmp_path,
        tuple(_document(identity) for identity in identities),
        policy=policy,
    )

    document_items = [item for item in items if item.source_item_id in identities]
    assert [item.disposition for item in document_items] == [
        CatalogDisposition.SELECTED,
        CatalogDisposition.EXCLUDED,
    ]
    assert document_items[1].selection.reason_code == "policy.item-budget-exhausted"
    decisions = _interpretation(document_items[1], "selection")["decisions"]
    assert [value["decisionId"] for value in decisions] == [
        "publisher-test-fixture",
        "source-withdrawal",
        "required-metadata",
        "candidate-rendition",
        "selected-item-budget",
    ]


def test_publisher_declared_withholding_is_read_verbatim_and_never_inferred(
    tmp_path: Path,
) -> None:
    """``restrictReasonType`` is the publisher's own statement that it withholds
    the content (decision 0005). Each of its four values maps to exactly one
    reason code; a value the policy does not know fails loudly instead of
    landing in a bucket; a record with no value keeps the acquired-source
    reason; and a record carrying the field alongside a real rendition stays
    selected, because the field labels an absence rather than creating one.
    """

    def without_rendition(name: str, **attributes: object) -> SourceCatalogItem:
        return _build(
            tmp_path / name,
            _document(frDocNum=None, **attributes),
            federal_register_records=(),
            federal_register_renditions=(),
        )

    copyrighted = without_rendition(
        "copyrighted",
        restrictReasonType="Copyrighted",
        subtype="Publication - Copyrighted Materials",
    )
    assert copyrighted.disposition is CatalogDisposition.UNAVAILABLE
    assert copyrighted.selection.reason_code == "source.publisher-withheld.copyrighted"
    assert copyrighted.selection.reason == (
        "The publisher withholds this record's content; restrictReasonType is"
        " 'Copyrighted' and subtype is 'Publication - Copyrighted Materials'."
    )
    for value, slug in (
        ("Confidential Business Information", "confidential-business-information"),
        ("Personally Identifiable Information", "personally-identifiable-information"),
        ("Other", "other"),
    ):
        item = without_rendition(slug, restrictReasonType=value)
        assert item.disposition is CatalogDisposition.UNAVAILABLE
        assert item.selection.reason_code == f"source.publisher-withheld.{slug}"
        assert f"restrictReasonType is {value!r}." in item.selection.reason

    unread = without_rendition("unread", restrictReasonType="Embargoed")
    assert unread.disposition is CatalogDisposition.FAILED
    assert unread.selection.reason_code == "source.restrict-reason-unread"
    assert "'Embargoed'" in unread.selection.reason

    undeclared = without_rendition("undeclared", restrictReasonType=None)
    assert undeclared.disposition is CatalogDisposition.UNAVAILABLE
    assert undeclared.selection.reason_code == "source.no-candidate-rendition"

    offered = _build(tmp_path / "offered", _document(restrictReasonType="Copyrighted"))
    assert offered.disposition is CatalogDisposition.SELECTED

    configuration = _policy().configuration
    declared = {
        (failure["disposition"], failure["reasonCode"])
        for failure in configuration["selectionFailures"]
    }
    assert {
        ("unavailable", "source.publisher-withheld.copyrighted"),
        ("unavailable", "source.publisher-withheld.confidential-business-information"),
        ("unavailable", "source.publisher-withheld.personally-identifiable-information"),
        ("unavailable", "source.publisher-withheld.other"),
        ("failed", "source.restrict-reason-unread"),
    } <= declared
    assert configuration["publisherWithholding"] == {
        "sourceField": "data.attributes.restrictReasonType",
        "reasonCodes": {
            "Copyrighted": "source.publisher-withheld.copyrighted",
            "Confidential Business Information": (
                "source.publisher-withheld.confidential-business-information"
            ),
            "Personally Identifiable Information": (
                "source.publisher-withheld.personally-identifiable-information"
            ),
            "Other": "source.publisher-withheld.other",
        },
        "unreadReasonCode": "source.restrict-reason-unread",
    }


def test_publisher_test_fixture_ids_are_excluded_for_documents_and_dockets(
    tmp_path: Path,
) -> None:
    """Regulations.gov publishes its own internal test fixtures through the
    same public API as real filings, under a ``TRAIN-``/``ERULE-``/``TEST-``
    source item id prefix. They 404 at both document and docket level on the
    public API, so a build must not read them as a real acquisition failure
    or a real withdrawal; they are excluded under their own reason code and
    the fixture decision is the only decision recorded.
    """

    items = _build_items(
        tmp_path,
        (_document("TRAIN-2025-0010-0002", frDocNum=None),),
        docket_records=(_docket("TRAIN-2022-0001"),),
        federal_register_records=(),
        federal_register_renditions=(),
    )
    document = next(item for item in items if item.source_item_id == "TRAIN-2025-0010-0002")
    docket = next(item for item in items if item.source_item_id == "TRAIN-2022-0001")
    for item in (document, docket):
        assert item.disposition is CatalogDisposition.EXCLUDED
        assert item.selection.reason_code == "source.publisher-test-fixture"
        decisions = _interpretation(item, "selection")["decisions"]
        assert [value["decisionId"] for value in decisions] == ["publisher-test-fixture"]


def test_publisher_test_fixture_excludes_rather_than_deletes_when_also_withdrawn(
    tmp_path: Path,
) -> None:
    """The ordering property: measured 2026-09-04, 4 of catalog-A's 41 test
    fixtures are also marked withdrawn. If the fixture check ran after
    ``source-withdrawal`` instead of before it, these would come out
    ``deleted`` at ``source.withdrawn-after-publication`` instead of
    ``excluded`` at ``source.publisher-test-fixture`` -- this test fails if
    the check is moved after withdrawal. Covers both the document path
    (its own inline cascade) and the comment path (the shared
    ``_selection_result`` cascade), which are two different call sites.
    """

    document = _build(
        tmp_path / "document",
        _document(
            "TRAIN-2025-0099-0001",
            frDocNum=None,
            withdrawn=True,
            reasonWithdrawn="Test artifact",
        ),
        federal_register_records=(),
        federal_register_renditions=(),
    )
    assert document.disposition is CatalogDisposition.EXCLUDED
    assert document.selection.reason_code == "source.publisher-test-fixture"

    comment_items = _build_items(
        tmp_path / "comment",
        (),
        policy=_policy(include_comments=True),
        docket_records=(),
        comment_records=(
            _comment(
                "TRAIN-2025-0099-9001",
                withdrawn=True,
                reasonWithdrawn="Test artifact",
            ),
        ),
        federal_register_records=(),
        federal_register_renditions=(),
    )
    comment = next(
        item for item in comment_items if item.source_item_id == "TRAIN-2025-0099-9001"
    )
    assert comment.disposition is CatalogDisposition.EXCLUDED
    assert comment.selection.reason_code == "source.publisher-test-fixture"


def test_ids_merely_containing_fixture_letters_are_unaffected(tmp_path: Path) -> None:
    """The match is anchored at the start of the source item id; a real
    filing that only contains ``TRAIN``/``TEST`` elsewhere in its id must not
    be caught.
    """

    contains = _build(
        tmp_path / "contains",
        _document("EPA-TRAIN-2020-0001", frDocNum=None),
        federal_register_records=(),
        federal_register_renditions=(),
    )
    prefixed_word = _build(
        tmp_path / "prefixed-word",
        _document("PRETEST-1", frDocNum=None),
        federal_register_records=(),
        federal_register_renditions=(),
    )
    for item in (contains, prefixed_word):
        assert item.disposition is CatalogDisposition.UNAVAILABLE
        assert item.selection.reason_code == "source.no-candidate-rendition"


def test_test_fixture_pattern_and_reason_code_are_sealed_and_round_trip(
    tmp_path: Path,
) -> None:
    policy = _policy()
    configuration = policy.configuration
    assert configuration["testFixtures"] == {
        "sourceField": "sourceItemId",
        "idPrefixes": ["TRAIN-", "ERULE-", "TEST-"],
        "reasonCode": "source.publisher-test-fixture",
    }
    assert {
        "decisionId": "publisher-test-fixture",
        "disposition": "excluded",
        "reasonCode": "source.publisher-test-fixture",
    } in configuration["selectionFailures"]
    assert RegulationsGovCatalogPolicy.from_member(policy.to_member()).to_member() == (
        policy.to_member()
    )


def test_receipt_reason_counts_reconcile_to_every_non_selected_bucket(
    tmp_path: Path,
) -> None:
    """The receipt's ``reasonCounts`` rows say why rows were not selected, in
    sealed UTF-16 order, and sum to each ``dispositionCounts`` bucket -- here
    including ``excluded``, a disposition no built catalog had carried before
    this section existed.
    """

    base = _policy(include_comments=True)
    policy = RegulationsGovCatalogPolicy(
        document_input=base.document_input,
        docket_input=base.docket_input,
        federal_register_input=base.federal_register_input,
        agency_names=base.agency_names,
        max_selected_items=1,
        comment_input=base.comment_input,
    )
    _build_items(
        tmp_path,
        (
            _document("EPA-2026-0001-0002", frDocNum=None, restrictReasonType="Copyrighted"),
            _document("EPA-2026-0001-0003", frDocNum=None, restrictReasonType="Copyrighted"),
            _document("EPA-2026-0001-0004", frDocNum=None, restrictReasonType="Other"),
            _document("EPA-2026-0001-0005", frDocNum=None),
            _document("EPA-2026-0001-0006", frDocNum=None, restrictReasonType="Embargoed"),
            _document("EPA-2026-0001-0007", withdrawn=True),
            _document("TRAIN-2026-0001-0001", frDocNum=None),
        ),
        policy=policy,
        docket_records=(),
        comment_records=(_comment("EPA-2026-0001-9201"), _comment("EPA-2026-0001-9202")),
        federal_register_records=(),
        federal_register_renditions=(),
    )
    receipt = json.loads(next(tmp_path.rglob("catalog-build-receipt.json")).read_text())

    assert receipt["dispositionCounts"] == {
        "selected": 1,
        "excluded": 2,
        "deleted": 1,
        "unavailable": 4,
        "failed": 1,
    }
    assert receipt["reasonCounts"] == [
        {"disposition": "deleted", "reasonCode": "source.withdrawn-after-publication", "count": 1},
        {"disposition": "excluded", "reasonCode": "policy.item-budget-exhausted", "count": 1},
        {"disposition": "excluded", "reasonCode": "source.publisher-test-fixture", "count": 1},
        {"disposition": "failed", "reasonCode": "source.restrict-reason-unread", "count": 1},
        {"disposition": "unavailable", "reasonCode": "source.no-candidate-rendition", "count": 1},
        {"disposition": "unavailable", "reasonCode": "source.publisher-withheld.copyrighted", "count": 2},
        {"disposition": "unavailable", "reasonCode": "source.publisher-withheld.other", "count": 1},
    ]


def test_resume_carries_the_selected_item_budget_across_the_kill(tmp_path: Path) -> None:
    """A resumed Regulations.gov build skips the indexing phase it already
    committed, restarts the item stream after its last committed batch, and
    carries the selected-item count forward, so a budget exhausted before the
    kill stays exhausted after it. Losing that count would select two more
    comments and publish a different catalog under the same inputs.
    """

    base = _policy(include_comments=True)

    def policy() -> RegulationsGovCatalogPolicy:
        return RegulationsGovCatalogPolicy(
            document_input=base.document_input,
            docket_input=base.docket_input,
            federal_register_input=base.federal_register_input,
            agency_names=base.agency_names,
            max_selected_items=2,
            comment_input=base.comment_input,
        )

    def sources() -> list[_Source]:
        return [
            _Source(_description("documents", _DOCUMENT_SYSTEM, _REGULATIONS_VERSION), (), ()),
            _Source(_description("dockets", _DOCKET_SYSTEM, _REGULATIONS_VERSION), ()),
            _Source(
                _description("comments", _COMMENT_SYSTEM, _REGULATIONS_VERSION),
                tuple(_comment(f"EPA-2026-0001-92{index:02d}") for index in range(1, 6)),
                (),
            ),
            _Source(
                _description(
                    "federal-register", _FEDERAL_REGISTER_SYSTEM, "v1", state_scope="observed-crawl"
                ),
                (),
                (),
            ),
        ]

    fresh = _build_result(tmp_path / "fresh", sources(), policy())
    assert [item.disposition for item in _items(tmp_path / "fresh", fresh.reference)] == [
        CatalogDisposition.SELECTED,
        CatalogDisposition.SELECTED,
        CatalogDisposition.EXCLUDED,
        CatalogDisposition.EXCLUDED,
        CatalogDisposition.EXCLUDED,
    ]

    workspace_path = tmp_path / "workspace.sqlite3"

    def durable() -> SqliteCatalogPolicyWorkspace:
        return SqliteCatalogPolicyWorkspace(path=workspace_path)

    killed = KillAfter(policy(), yields=3)
    with pytest.raises(IntegrityError, match="injected kill mid-stream"):
        _build_result(
            tmp_path / "resumed", sources(), killed, workspace_factory=durable, resume_batch_items=2
        )
    counted = CountItems(policy())
    resumed = _build_result(
        tmp_path / "resumed", sources(), counted, workspace_factory=durable, resume_batch_items=2
    )

    assert counted.computed == 3
    assert resumed.reference == fresh.reference
