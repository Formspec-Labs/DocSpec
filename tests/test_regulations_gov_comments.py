"""Comment observations, dispositions, rendition integrity, and adapter and CLI profile selection."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import docspec.adapters.spicy_docs_source_native as spicy_docs_adapter_module
from docspec.adapters.spicy_docs_source_native import (
    SpicyDocsSourceNativeAdapter,
    spicy_docs_source_profile,
)
from docspec.application.regulations_gov_catalog import (
    RegulationsGovCatalogPolicy,
)
from docspec.domain.source_catalog import CatalogDisposition
from docspec.errors import IntegrityError
from docspec.cli.source_catalog import build_parser as source_catalog_build_parser
from tests.support.regulations_gov import (
    _SHA_A,
    _SHA_E,
    _build_items,
    _comment,
    _docket,
    _document,
    _interpretation,
    _policy,
    _rendition,
)


def test_comments_and_dockets_are_first_class_ordered_universe_members(
    tmp_path: Path,
) -> None:
    comment_id = "EPA-2026-0001-9001"
    comment_renditions = (
        _rendition(
            comment_id,
            "attachment-0000-0000",
            f"https://downloads.regulations.gov/{comment_id}/attachment.pdf",
            source_field="included[0].attributes.fileFormats[0]",
            media_type="application/pdf",
            expected_byte_size=123,
        ),
        _rendition(
            comment_id,
            "comment-0000",
            f"https://downloads.regulations.gov/{comment_id}/comment.txt",
            source_field="data.attributes.fileFormats[0]",
            media_type="text/plain",
            expected_byte_size=42,
        ),
    )
    items = _build_items(
        tmp_path,
        (_document(),),
        policy=_policy(include_comments=True),
        docket_records=(_docket(include_link=True),),
        comment_records=(_comment(comment_id),),
        comment_renditions=comment_renditions,
    )

    assert [item.source_item_id for item in items] == sorted(
        ["EPA-2026-0001", "EPA-2026-0001-0001", comment_id]
    )
    docket = next(item for item in items if item.source_item_id == "EPA-2026-0001")
    comment = next(item for item in items if item.source_item_id == comment_id)
    assert docket.disposition is CatalogDisposition.SELECTED
    assert docket.document_id == docket.source_item_id
    assert docket.source_issued_version == "2026-08-24T05:00:00Z"
    assert docket.normalized_metadata["docketIds"] == ("EPA-2026-0001",)
    assert [value.rendition_id for value in docket.candidate_renditions] == [
        "regulations-gov/source-record"
    ]

    assert comment.disposition is CatalogDisposition.SELECTED
    assert comment.document_id == comment.source_item_id
    assert comment.source_issued_version == "2026-08-25T01:02:03Z"
    assert comment.normalized_metadata["title"] is None
    assert comment.normalized_metadata["docketIds"] == ("EPA-2026-0001",)
    assert comment.normalized_metadata["regulationIdentifierNumbers"] == (
        "2060-AZ99",
    )
    assert [fact["scopeId"] for fact in comment.source_native_facts] == [
        "regulations-gov-comments",
        "regulations-gov-dockets",
        "regulations-gov-documents",
    ]
    assert comment.source_native_facts[0]["fields"]["data"]["attributes"]["comment"] == (
        "Exact public comment body"
    )
    assert comment.source_native_facts[0]["fields"]["included"][0]["attributes"][
        "title"
    ] == "Exact attachment"
    assert [value.rendition_id for value in comment.candidate_renditions] == [
        "regulations-gov/attachment-0000-0000",
        "regulations-gov/comment-0000",
    ]
    assert [
        value["interpretationKind"] for value in comment.interpretations
    ] == [
        "exact-join",
        "normalization",
        "rendition-preference",
        "sampling",
        "selection",
        "topic-recovery",
    ]
    assert [
        value["outcome"] for value in _interpretation(comment, "exact-join")["joins"]
    ] == ["matched", "matched"]


def test_comment_null_modify_date_uses_explicit_exact_posted_date_policy(
    tmp_path: Path,
) -> None:
    comment = _comment(
        "EPA-2026-0001-9002",
        modify_date=None,
        include_body=False,
        title=None,
    )
    items = _build_items(
        tmp_path,
        (_document(),),
        policy=_policy(include_comments=True),
        comment_records=(comment,),
    )
    item = next(value for value in items if value.source_item_id == comment["sourceRecordId"])

    assert item.source_issued_version == "2026-08-24T04:00:00Z"
    own_fact = next(
        fact for fact in item.source_native_facts if fact["scopeId"] == "regulations-gov-comments"
    )
    attributes = own_fact["fields"]["data"]["attributes"]
    assert attributes["modifyDate"] is None
    assert "comment" not in attributes
    assert attributes["title"] is None
    observation = next(
        value
        for value in item.source_observations
        if value["observationKey"] == "comment/source-issued-version-policy"
    )["observationValue"]
    assert observation == {
        "exactSourceValue": "2026-08-24T04:00:00Z",
        "sourcePath": "data.attributes.postedDate",
        "reasonCode": "upstream-selected-comment-has-null-modify-date",
        "upstreamVersionPath": "data.attributes.modifyDate",
        "upstreamVersionValue": None,
    }


@pytest.mark.parametrize(
    ("attributes", "message"),
    [
        (
            {"modifyDate": ""},
            "Regulations.gov comment modifyDate must be nonempty text or null",
        ),
        (
            {"modifyDate": 7},
            "Regulations.gov comment modifyDate must be nonempty text or null",
        ),
        (
            {"modifyDate": None, "postedDate": None},
            "Regulations.gov comment with null modifyDate has no postedDate fallback",
        ),
    ],
)
def test_comment_version_refuses_an_unusable_exact_source_value(
    tmp_path: Path,
    attributes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(IntegrityError) as refused:
        _build_items(
            tmp_path,
            (_document(),),
            policy=_policy(include_comments=True),
            comment_records=(_comment(**attributes),),
        )
    assert str(refused.value) == message


def test_docspec_refuses_to_recollapse_comment_observations(tmp_path: Path) -> None:
    identity = "EPA-2026-0001-9003"
    with pytest.raises(IntegrityError, match="strictly ordered by sourceRecordId"):
        _build_items(
            tmp_path,
            (),
            policy=_policy(include_comments=True),
            docket_records=(),
            comment_records=(
                _comment(identity, modify_date=None),
                _comment(identity, modify_date="2026-08-25T10:00:00Z"),
            ),
            federal_register_records=(),
            federal_register_renditions=(),
        )


def test_comment_and_docket_nonselected_dispositions_are_explicit(
    tmp_path: Path,
) -> None:
    comments = (
        _comment(
            "EPA-2026-0001-9101",
            withdrawn=True,
            reasonWithdrawn="Removed by the source",
        ),
        _comment("EPA-2026-0001-9102", agencyId="UNMAPPED"),
        _comment("EPA-2026-0001-9103", include_link=False),
    )
    items = _build_items(
        tmp_path / "comments",
        (),
        policy=_policy(include_comments=True),
        docket_records=(
            _docket("EPA-2026-1001", include_link=False),
            _docket("EPA-2026-1002", include_link=True, agencyId="UNMAPPED"),
        ),
        comment_records=comments,
        federal_register_records=(),
        federal_register_renditions=(),
    )
    outcomes = {
        item.source_item_id: (item.disposition, item.selection.reason_code)
        for item in items
    }
    assert outcomes == {
        "EPA-2026-0001-9101": (
            CatalogDisposition.DELETED,
            "source.withdrawn-after-publication",
        ),
        "EPA-2026-0001-9102": (
            CatalogDisposition.FAILED,
            "source.normalized-field-missing",
        ),
        "EPA-2026-0001-9103": (
            CatalogDisposition.UNAVAILABLE,
            "source.no-candidate-rendition",
        ),
        "EPA-2026-1001": (
            CatalogDisposition.UNAVAILABLE,
            "source.no-candidate-rendition",
        ),
        "EPA-2026-1002": (
            CatalogDisposition.FAILED,
            "source.normalized-field-missing",
        ),
    }

    base = _policy(include_comments=True)
    budget_policy = RegulationsGovCatalogPolicy(
        document_input=base.document_input,
        docket_input=base.docket_input,
        federal_register_input=base.federal_register_input,
        agency_names=base.agency_names,
        max_selected_items=1,
        comment_input=base.comment_input,
    )
    budget_comments = (
        _comment("EPA-2026-0001-9201"),
        _comment("EPA-2026-0001-9202"),
    )
    budget_items = _build_items(
        tmp_path / "budget",
        (),
        policy=budget_policy,
        docket_records=(),
        comment_records=budget_comments,
        federal_register_records=(),
        federal_register_renditions=(),
    )
    assert [item.disposition for item in budget_items] == [
        CatalogDisposition.SELECTED,
        CatalogDisposition.EXCLUDED,
    ]
    assert budget_items[1].selection.reason_code == "policy.item-budget-exhausted"


def test_content_addressed_candidate_requires_matching_digest_and_size(
    tmp_path: Path,
) -> None:
    comment_id = "EPA-2026-0001-9301"
    candidate = _rendition(
        comment_id,
        "attachment-immutable",
        _SHA_E,
        source_field="included[0].attributes.fileFormats[0]",
        media_type="application/pdf",
        expected_sha256=_SHA_E,
        expected_byte_size=123,
    )
    item = next(
        value
        for value in _build_items(
            tmp_path / "valid",
            (),
            policy=_policy(include_comments=True),
            docket_records=(),
            comment_records=(_comment(comment_id),),
            comment_renditions=(candidate,),
            federal_register_records=(),
            federal_register_renditions=(),
        )
        if value.source_item_id == comment_id
    )
    assert [value.to_dict() for value in item.candidate_renditions] == [
        {
            "renditionId": "regulations-gov/attachment-immutable",
            "mediaType": "application/pdf",
            "locatorKind": "immutable-object",
            "locator": _SHA_E,
            "expectedSha256": _SHA_E,
            "expectedByteSize": 123,
        }
    ]

    mismatch = {**candidate, "expectedSha256": _SHA_A}
    with pytest.raises(IntegrityError, match="differs from its supplied expected SHA-256"):
        _build_items(
            tmp_path / "mismatch",
            (),
            policy=_policy(include_comments=True),
            docket_records=(),
            comment_records=(_comment(comment_id),),
            comment_renditions=(mismatch,),
            federal_register_records=(),
            federal_register_renditions=(),
        )

    missing_size = {**candidate, "expectedByteSize": None}
    with pytest.raises(IntegrityError, match="requires a supplied non-negative byte size"):
        _build_items(
            tmp_path / "missing-size",
            (),
            policy=_policy(include_comments=True),
            docket_records=(),
            comment_records=(_comment(comment_id),),
            comment_renditions=(missing_size,),
            federal_register_records=(),
            federal_register_renditions=(),
        )


def test_installed_adapter_exposes_comment_profile_and_propagates_upstream_tie_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comment_profile = object()
    profiles = SimpleNamespace(REGULATIONS_GOV_COMMENT_PROFILE=comment_profile)
    monkeypatch.setattr(spicy_docs_adapter_module, "import_module", lambda _: profiles)
    assert spicy_docs_source_profile("regulations-gov-comments") is comment_profile

    class RefusingReader:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise ValueError("upstream source-version tie")

    source_native = SimpleNamespace(
        CURRENT_PRODUCER_PRODUCT="spicy-docs",
        SourceNativeReleaseReader=RefusingReader,
    )
    monkeypatch.setattr(spicy_docs_adapter_module, "import_module", lambda _: source_native)
    with pytest.raises(ValueError, match="upstream source-version tie"):
        SpicyDocsSourceNativeAdapter(
            SimpleNamespace(),
            blob_source=SimpleNamespace(),
            profile=comment_profile,
            expected_pin=None,
            accepted_verifier_implementation_ids=frozenset(),
        )


def test_source_catalog_cli_accepts_the_comment_profile_choice() -> None:
    args = source_catalog_build_parser().parse_args(
        [
            "build",
            "--source-native",
            "comments",
            "--source-native-artifact-digest",
            _SHA_E,
            "--source-native-blob-store",
            "blobs",
            "--source-native-profile",
            "regulations-gov-comments",
            "--accepted-source-verifier-implementation-id",
            "urn:test:verifier",
            "--catalog-policy",
            "policy.json",
            "--implementation-id",
            "git+https://example.test/docspec@" + "1" * 40,
            "--verifier-implementation-id",
            "git+https://example.test/docspec@" + "1" * 40,
            "--destination",
            "catalog",
        ]
    )
    assert args.source_native_profile == ["regulations-gov-comments"]
    assert args.source_native_blob_store == [Path("blobs")]
