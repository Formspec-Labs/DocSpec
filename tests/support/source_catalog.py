"""Shared source catalog fixtures, extracted from tests.test_source_catalog_snapshot."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from rulespec_artifacts import (
    Producer,
)

from docspec.ports.source_catalog import (
    SourceNativeDescription,
)

_SHA_A = "sha256:" + "a" * 64

_SHA_B = "sha256:" + "b" * 64

_SHA_C = "sha256:" + "c" * 64

_FEDERAL_REGISTER_SOURCE = "https://www.federalregister.gov/api/v1"


def producer() -> Producer:
    implementation = "git+https://example.test/docspec@" + "1" * 40
    return Producer(
        "docspec",
        implementation,
        "urn:docspec:verifier:source-catalog",
        "1.0.0",
        implementation,
    )


def description(*, scope: str = "complete-snapshot") -> SourceNativeDescription:
    return SourceNativeDescription(
        logical_id="urn:spicy:artifact:spicyregs-source-native-release:" + "a" * 64,
        artifact_digest=_SHA_A,
        source_system_id=_FEDERAL_REGISTER_SOURCE,
        source_system_version="v1",
        source_state_scope=scope,
        source_state_digest=_SHA_B,
        source_native_schema_set_digest=_SHA_C,
    )


def record(identity: str, *, malformed_rin: bool = False, agencies: bool = True) -> dict[str, Any]:
    return {
        "sourceRecordId": identity,
        "scopeId": "federal-register-documents",
        "schemaName": "federal-register-document",
        "schemaVersion": "1.0",
        "schemaDigest": _SHA_C,
        "record": {
            "document_number": identity,
            "title": f"Federal Register {identity}",
            "type": "Rule",
            "publication_date": "2026-08-24",
            "agencies": (
                [{"slug": "environmental-protection-agency", "name": "Environmental Protection Agency"}]
                if agencies
                else []
            ),
            "html_url": f"https://www.federalregister.gov/d/{identity}",
            "pdf_url": f"https://example.test/{identity}.pdf",
            "docket_ids": ["EPA-HQ-2026-0001"],
            "regulation_id_numbers": ["not a rin" if malformed_rin else "2060-AV12"],
            "topics": [{"slug": "air-quality", "name": "Air quality"}],
        },
        "fieldDiagnostics": [],
    }


def renditions(identity: str) -> tuple[dict[str, Any], ...]:
    return (
        {
            "sourceRecordId": identity,
            "renditionId": f"{identity}/html",
            "sourceField": "html_url",
            "locator": f"https://www.federalregister.gov/d/{identity}",
            "mediaType": "text/html",
            "expectedSha256": None,
            "expectedByteSize": None,
        },
        {
            "sourceRecordId": identity,
            "renditionId": f"{identity}/pdf",
            "sourceField": "pdf_url",
            "locator": f"https://example.test/{identity}.pdf",
            "mediaType": "application/pdf",
            "expectedSha256": None,
            "expectedByteSize": None,
        },
    )


@dataclass
class FakeSource:
    metadata: SourceNativeDescription
    records: tuple[Mapping[str, Any], ...]
    renditions: tuple[Mapping[str, Any], ...]

    def describe(self) -> SourceNativeDescription:
        return self.metadata

    def iter_records(self) -> Iterator[Mapping[str, Any]]:
        yield from self.records

    def iter_renditions(self) -> Iterator[Mapping[str, Any]]:
        yield from self.renditions
