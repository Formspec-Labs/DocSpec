"""Shared Regulations.gov source records and catalog construction."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rulespec_artifacts import Producer

from docspec.adapters.catalog_artifact.builder import (
    SourceCatalogBuilder,
    SourceCatalogBuildRequest,
)
from docspec.adapters.catalog_artifact.reader import (
    SourceCatalogArtifactReader,
)
from docspec.adapters.catalog_policy_workspace import SqliteCatalogPolicyWorkspace
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.application.regulations_gov_catalog import (
    RegulationsGovCatalogPolicy,
)
from docspec.domain.source_catalog import SourceCatalogItem
from docspec.ports.source_catalog import SourceInputSelector, SourceNativeDescription


_DOCUMENT_SYSTEM = "urn:test:regulations-gov:documents"
_DOCKET_SYSTEM = "urn:test:regulations-gov:dockets"
_COMMENT_SYSTEM = "urn:test:regulations-gov:comments"
_FEDERAL_REGISTER_SYSTEM = "https://www.federalregister.gov/api/v1"
_REGULATIONS_VERSION = "regulations.gov-v4-mirrulations-raw-data"
_SHA_A = "sha256:" + "a" * 64
_SHA_B = "sha256:" + "b" * 64
_SHA_C = "sha256:" + "c" * 64
_SHA_D = "sha256:" + "d" * 64
_SHA_E = "sha256:" + "e" * 64


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
        "comments": _SHA_E,
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
        "schemaVersion": "1.1" if schema == "federal-register-document" else "1.0",
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


def _docket(
    identity: str = "EPA-2026-0001",
    *,
    include_link: bool = False,
    **updates: object,
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "agencyId": "EPA",
        "dkAbstract": "Exact docket abstract",
        "docketType": "Rulemaking",
        "modifyDate": "2026-08-24T05:00:00Z",
        "rin": "2060-AZ99",
        "title": "Exact docket title",
    }
    attributes.update(updates)
    data: dict[str, Any] = {
        "id": identity,
        "type": "dockets",
        "attributes": attributes,
    }
    if include_link:
        data["links"] = {
            "self": f"https://api.regulations.gov/v4/dockets/{identity}"
        }
    return _source_record(
        identity,
        scope="regulations-gov-dockets",
        schema="regulations-gov-docket-raw",
        record={
            "data": data
        },
    )


def _comment(
    identity: str = "EPA-2026-0001-9001",
    *,
    modify_date: str | None = "2026-08-25T01:02:03Z",
    include_link: bool = True,
    include_body: bool = True,
    **updates: object,
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "agencyId": "EPA",
        "commentOn": "source-object",
        "commentOnDocumentId": "EPA-2026-0001-0001",
        "docketId": "EPA-2026-0001",
        "documentType": "Public Submission",
        "modifyDate": modify_date,
        "postedDate": "2026-08-24T04:00:00Z",
        "reasonWithdrawn": None,
        "title": None,
        "withdrawn": False,
    }
    if include_body:
        attributes["comment"] = "Exact public comment body"
    attributes.update(updates)
    data: dict[str, Any] = {
        "id": identity,
        "type": "comments",
        "attributes": attributes,
    }
    if include_link:
        data["links"] = {
            "self": f"https://api.regulations.gov/v4/comments/{identity}"
        }
    return _source_record(
        identity,
        scope="regulations-gov-comments",
        schema="regulations-gov-comment-raw",
        record={
            "data": data,
            "included": [
                {
                    "id": f"{identity}-attachment",
                    "type": "attachments",
                    "attributes": {
                        "title": "Exact attachment",
                        "fileFormats": [
                            {
                                "fileUrl": f"https://downloads.regulations.gov/{identity}/attachment.pdf",
                                "format": "pdf",
                                "size": 123,
                            }
                        ],
                    },
                }
            ],
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


def _federal_register_filing(
    document_number: str,
    publication_date: str,
    *,
    title: str = "Exact Federal Register title",
) -> dict[str, Any]:
    """One Federal Register filing under the composite identity of DocSpec 0003.

    ``sourceRecordId`` is ``{number}@{date}`` while ``record.document_number``
    stays bare. Separating them is the whole point: the two were the same string
    until 2026-09-05, and a fixture that keeps them equal cannot catch a join
    keyed on the wrong one.
    """
    return _source_record(
        f"{document_number}@{publication_date}",
        scope="federal-register-documents",
        schema="federal-register-document",
        record={
            "document_number": document_number,
            "publication_date": publication_date,
            "docket_ids": ["EPA-2026-0001"],
            "html_url": f"https://www.federalregister.gov/d/{document_number}",
            "regulation_id_numbers": ["2060-AX01"],
            "title": title,
        },
    )


def _rendition(
    identity: str,
    rendition_id: str,
    locator: str,
    *,
    source_field: str,
    media_type: str,
    expected_sha256: str | None = None,
    expected_byte_size: int | None = None,
) -> dict[str, Any]:
    return {
        "sourceRecordId": identity,
        "renditionId": rendition_id,
        "sourceField": source_field,
        "locator": locator,
        "mediaType": media_type,
        "expectedSha256": expected_sha256,
        "expectedByteSize": expected_byte_size,
    }


def _policy(*, include_comments: bool = False) -> RegulationsGovCatalogPolicy:
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
            "1.1",
        ),
        {"EPA": "Environmental Protection Agency"},
        comment_input=(
            SourceInputSelector(
                _COMMENT_SYSTEM,
                _REGULATIONS_VERSION,
                "regulations-gov-comments",
                "regulations-gov-comment-raw",
                "1.0",
            )
            if include_comments
            else None
        ),
    )


def _build_items(
    root: Path,
    documents: tuple[Mapping[str, Any], ...],
    *,
    policy: RegulationsGovCatalogPolicy | None = None,
    document_renditions: tuple[Mapping[str, Any], ...] = (),
    docket_records: tuple[Mapping[str, Any], ...] = (_docket(),),
    comment_records: tuple[Mapping[str, Any], ...] = (),
    comment_renditions: tuple[Mapping[str, Any], ...] = (),
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
    selected_policy = policy or _policy()
    sources: list[_Source] = [
        _Source(
            _description("documents", _DOCUMENT_SYSTEM, _REGULATIONS_VERSION),
            documents,
            document_renditions,
        ),
        _Source(
            _description("dockets", _DOCKET_SYSTEM, _REGULATIONS_VERSION),
            docket_records,
        ),
    ]
    if selected_policy.comment_input is not None:
        sources.append(
            _Source(
                _description("comments", _COMMENT_SYSTEM, _REGULATIONS_VERSION),
                comment_records,
                comment_renditions,
            )
        )
    sources.append(
        _Source(
            _description(
                "federal-register",
                _FEDERAL_REGISTER_SYSTEM,
                "v1",
                state_scope="observed-crawl",
            ),
            federal_register_records,
            federal_register_renditions,
        )
    )
    result = _build_result(root, sources, selected_policy)
    return _items(root, result.reference)


def _build_result(
    root: Path,
    sources: Sequence[_Source],
    policy: object,
    *,
    workspace_factory: Any = SqliteCatalogPolicyWorkspace,
    resume_batch_items: int | None = None,
) -> Any:
    options = {} if resume_batch_items is None else {"resume_batch_items": resume_batch_items}
    return SourceCatalogBuilder(
        store=LocalSourceCatalogStore(root),
        policy=policy,  # type: ignore[arg-type]
        request=SourceCatalogBuildRequest("urn:test:catalog:regulations-gov", _producer()),
        workspace_factory=workspace_factory,
        **options,
    ).build(tuple(sources))


def _items(root: Path, reference: Any) -> tuple[SourceCatalogItem, ...]:
    snapshot = SourceCatalogArtifactReader(
        LocalSourceCatalogStore(root), producer=_producer()
    ).open_snapshot(reference)
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
    identity = str(document["sourceRecordId"])
    return next(item for item in items if item.source_item_id == identity)


def _interpretation(item: SourceCatalogItem, kind: str) -> Mapping[str, Any]:
    return next(
        value["result"]
        for value in item.interpretations
        if value["interpretationKind"] == kind
    )
