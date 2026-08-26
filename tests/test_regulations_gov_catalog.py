from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rulespec_artifacts import Producer

from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
from docspec.adapters.source_catalog_artifact import (
    SourceCatalogArtifactReader,
    SourceCatalogBuildRequest,
    SourceCatalogBuilder,
)
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.application.regulations_gov_catalog import (
    RegulationsGovCatalogPolicy,
    RegulationsGovSamplePolicy,
)
from docspec.domain.source_catalog import CatalogDisposition, SourceCatalogItem
from docspec.ports.source_catalog import SourceInputSelector, SourceNativeDescription

_DOCUMENT_SYSTEM = "urn:test:regulations-gov:documents"
_DOCKET_SYSTEM = "urn:test:regulations-gov:dockets"
_FEDERAL_REGISTER_SYSTEM = "https://www.federalregister.gov/api/v1"
_REGULATIONS_VERSION = "regulations.gov-v4-mirrulations-raw-data"
_SHA_A = "sha256:" + "a" * 64
_SHA_B = "sha256:" + "b" * 64
_SHA_C = "sha256:" + "c" * 64
_SHA_D = "sha256:" + "d" * 64


def _producer() -> Producer:
    implementation = "git+https://example.test/docspec@" + "1" * 40
    return Producer(
        "docspec",
        implementation,
        "urn:docspec:verifier:source-catalog",
        "1.0.0",
        implementation,
    )


def _description(
    identity: str,
    source_system_id: str,
    source_system_version: str,
    *,
    state_scope: str = "complete-snapshot",
) -> SourceNativeDescription:
    artifact_digest = {
        "documents": _SHA_A,
        "dockets": _SHA_B,
        "federal-register": _SHA_C,
    }[identity]
    return SourceNativeDescription(
        logical_id=f"urn:test:source-native:{artifact_digest.removeprefix('sha256:')}",
        artifact_digest=artifact_digest,
        source_system_id=source_system_id,
        source_system_version=source_system_version,
        source_state_scope=state_scope,
        source_state_digest=_SHA_D,
        source_native_schema_set_digest=_SHA_A,
    )


@dataclass
class _Source:
    description_value: SourceNativeDescription
    records: tuple[Mapping[str, Any], ...]
    renditions: tuple[Mapping[str, Any], ...] = ()

    def describe(self) -> SourceNativeDescription:
        return self.description_value

    def iter_records(self) -> Iterator[Mapping[str, Any]]:
        yield from self.records

    def iter_renditions(self) -> Iterator[Mapping[str, Any]]:
        yield from self.renditions


def _source_record(
    identity: str,
    *,
    scope: str,
    schema: str,
    record: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "fieldDiagnostics": [],
        "record": dict(record),
        "schemaDigest": _SHA_A,
        "schemaName": schema,
        "schemaVersion": "1.0",
        "scopeId": scope,
        "sourceRecordId": identity,
    }


def _document(
    identity: str = "EPA-2026-0001-0001",
    **updates: object,
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "additionalRins": ["2060-AV12", "not-a-rin"],
        "agencyId": "EPA",
        "commentEndDate": "2026-09-01T00:00:00Z",
        "docketId": "EPA-2026-0001",
        "documentType": "Notice",
        "frDocNum": "2026-10001",
        "modifyDate": "2026-08-25T01:02:03Z",
        "postedDate": "2026-08-24T04:00:00Z",
        "reasonWithdrawn": None,
        "title": "Exact source title",
        "topics": ["Air quality", {"id": "source-topic", "label": "Source topic"}],
        "withdrawn": False,
    }
    attributes.update(updates)
    return _source_record(
        identity,
        scope="regulations-gov-documents",
        schema="regulations-gov-document-raw",
        record={
            "data": {
                "id": identity,
                "type": "documents",
                "attributes": attributes,
                "links": {"self": f"https://api.regulations.gov/v4/documents/{identity}"},
            }
        },
    )


def _docket(identity: str = "EPA-2026-0001") -> dict[str, Any]:
    return _source_record(
        identity,
        scope="regulations-gov-dockets",
        schema="regulations-gov-docket-raw",
        record={
            "data": {
                "id": identity,
                "type": "dockets",
                "attributes": {
                    "agencyId": "EPA",
                    "dkAbstract": "Exact docket abstract",
                    "modifyDate": "2026-08-24T05:00:00Z",
                    "rin": "2060-AZ99",
                    "title": "Exact docket title",
                },
            }
        },
    )


def _federal_register(identity: str = "2026-10001") -> dict[str, Any]:
    return _source_record(
        identity,
        scope="federal-register-documents",
        schema="federal-register-document",
        record={
            "document_number": identity,
            "docket_ids": ["EPA-2026-0001"],
            "html_url": f"https://www.federalregister.gov/d/{identity}",
            "regulation_id_numbers": ["2060-AX01"],
            "title": "Exact Federal Register title",
        },
    )


def _rendition(
    identity: str,
    rendition_id: str,
    locator: str,
    *,
    source_field: str,
    media_type: str,
) -> dict[str, Any]:
    return {
        "sourceRecordId": identity,
        "renditionId": rendition_id,
        "sourceField": source_field,
        "locator": locator,
        "mediaType": media_type,
        "expectedSha256": None,
        "expectedByteSize": None,
    }


def _policy() -> RegulationsGovCatalogPolicy:
    return RegulationsGovCatalogPolicy(
        SourceInputSelector(
            _DOCUMENT_SYSTEM,
            _REGULATIONS_VERSION,
            "regulations-gov-documents",
            "regulations-gov-document-raw",
            "1.0",
        ),
        SourceInputSelector(
            _DOCKET_SYSTEM,
            _REGULATIONS_VERSION,
            "regulations-gov-dockets",
            "regulations-gov-docket-raw",
            "1.0",
        ),
        SourceInputSelector(
            _FEDERAL_REGISTER_SYSTEM,
            "v1",
            "federal-register-documents",
            "federal-register-document",
            "1.0",
        ),
        {"EPA": "Environmental Protection Agency"},
    )


def _build_items(
    root: Path,
    documents: tuple[Mapping[str, Any], ...],
    *,
    policy: RegulationsGovCatalogPolicy | None = None,
    document_renditions: tuple[Mapping[str, Any], ...] = (),
    docket_records: tuple[Mapping[str, Any], ...] = (_docket(),),
    federal_register_records: tuple[Mapping[str, Any], ...] = (_federal_register(),),
    federal_register_renditions: tuple[Mapping[str, Any], ...] | None = None,
) -> tuple[SourceCatalogItem, ...]:
    if federal_register_renditions is None:
        federal_register_renditions = (
            _rendition(
                "2026-10001",
                "2026-10001/html",
                "https://www.federalregister.gov/d/2026-10001",
                source_field="html_url",
                media_type="text/html",
            ),
        )
    sources = (
        _Source(
            _description("documents", _DOCUMENT_SYSTEM, _REGULATIONS_VERSION),
            documents,
            document_renditions,
        ),
        _Source(
            _description("dockets", _DOCKET_SYSTEM, _REGULATIONS_VERSION),
            docket_records,
        ),
        _Source(
            _description(
                "federal-register",
                _FEDERAL_REGISTER_SYSTEM,
                "v1",
                state_scope="observed-crawl",
            ),
            federal_register_records,
            federal_register_renditions,
        ),
    )
    store = LocalSourceCatalogStore(root)
    result = SourceCatalogBuilder(
        store=store,
        policy=policy or _policy(),
        request=SourceCatalogBuildRequest("urn:test:catalog:regulations-gov", _producer()),
        workspace_factory=SqliteCatalogPolicyWorkspace,
    ).build(sources)
    snapshot = SourceCatalogArtifactReader(store, producer=_producer()).open_snapshot(
        result.reference
    )
    return tuple(snapshot.items)


def _build(
    root: Path,
    document: Mapping[str, Any],
    *,
    policy: RegulationsGovCatalogPolicy | None = None,
    document_renditions: tuple[Mapping[str, Any], ...] = (),
    docket_records: tuple[Mapping[str, Any], ...] = (_docket(),),
    federal_register_records: tuple[Mapping[str, Any], ...] = (_federal_register(),),
    federal_register_renditions: tuple[Mapping[str, Any], ...] | None = None,
) -> SourceCatalogItem:
    items = _build_items(
        root,
        (document,),
        policy=policy,
        document_renditions=document_renditions,
        docket_records=docket_records,
        federal_register_records=federal_register_records,
        federal_register_renditions=federal_register_renditions,
    )
    assert len(items) == 1
    return items[0]


def _interpretation(item: SourceCatalogItem, kind: str) -> Mapping[str, Any]:
    return next(
        value["result"]
        for value in item.interpretations
        if value["interpretationKind"] == kind
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
    assert item.normalized_metadata["agencies"] == (
        {
            "agencyId": "EPA",
            "agencyName": "Environmental Protection Agency",
        },
    )
    assert [value["outcome"] for value in _interpretation(item, "source-join")["joins"]] == [
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


def test_join_uses_only_document_exact_keys_and_records_no_match(tmp_path: Path) -> None:
    item = _build(
        tmp_path,
        _document(docketId="EPA-DOES-NOT-EXIST", frDocNum="2026-DOES-NOT-EXIST"),
        federal_register_renditions=(),
    )

    joins = _interpretation(item, "source-join")["joins"]
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

    assert [item.source_item_id for item in first] == list(identities)
    assert [item.selection.to_dict() for item in repeated] == [
        item.selection.to_dict() for item in first
    ]
    selected = [item.source_item_id for item in first if item.disposition is CatalogDisposition.SELECTED]
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
    excluded = [item for item in first if item.disposition is CatalogDisposition.EXCLUDED]
    assert len(excluded) == 3
    assert {item.selection.reason_code for item in excluded} == {"policy.sample-not-drawn"}
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

    assert [item.disposition for item in items] == [
        CatalogDisposition.SELECTED,
        CatalogDisposition.EXCLUDED,
    ]
    assert items[1].selection.reason_code == "policy.item-budget-exhausted"
    decisions = _interpretation(items[1], "selection")["decisions"]
    assert [value["decisionId"] for value in decisions] == [
        "source-withdrawal",
        "required-metadata",
        "candidate-rendition",
        "selected-item-budget",
    ]
