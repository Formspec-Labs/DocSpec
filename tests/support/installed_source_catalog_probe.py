from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import docspec
import spicy_docs
import httpx
from docspec.adapters.content_fetchers import HttpsContentFetcher, HttpsContentFetcherConfig
from docspec.adapters.catalog_artifact.reader import (
    SourceCatalogArtifactReader,
)
from docspec.adapters.catalog_artifact.rules import (
    source_catalog_producer,
)
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.application.federal_register_catalog import FederalRegisterCatalogPolicy
from docspec.application.regulations_gov_catalog import RegulationsGovCatalogPolicy
from docspec.domain.identity import canonical_json_file_bytes
from docspec.domain.references import SourceCatalogRef
from docspec.ports.source_catalog import SourceInputSelector
from docspec.runtime import (
    build_local_catalog, open_local_catalog, preview_local_catalog,
)
from docspec.application.document_processors import content_statistics_processor, segment_rows
from docspec.domain.core_admission import record_value
from docspec.domain.source_catalog import SourceCatalogItem
from docspec.runtime.core import CoreWorkspace
from contextlib import closing
from docspec.processing.visible_text_runtime import VisibleTextBlockSegmenter, VisibleTextExtractor
from docspec.source_catalog import SpicyDocsSourceNativeAdapter
from rulespec_artifacts import Producer
from spicy_docs.federal_register_source_native import (
    FederalRegisterPage,
    federal_register_documents_url,
)
from spicy_docs.regulations_gov_source_native import (
    COMMENT_COLLECTION,
    DOCUMENT_COLLECTION,
    DOCKET_COLLECTION,
    iter_regulations_gov_comment_pages,
    iter_regulations_gov_document_pages,
    iter_regulations_gov_docket_pages,
)
from spicy_docs.source_native import (
    CURRENT_PRODUCER_PRODUCT,
    FORMAT_VERSION,
    VERIFIER_ID,
    VERIFIER_VERSION,
    SourceNativeReleaseBuild,
    SourceNativeReleasePublisher,
)
from spicy_docs.source_native_profiles import (
    FEDERAL_REGISTER_PROFILE,
    REGULATIONS_GOV_COMMENT_PROFILE,
    REGULATIONS_GOV_DOCUMENT_PROFILE,
    REGULATIONS_GOV_DOCKET_PROFILE,
)
from spicy_docs.source_native_store import LocalSourceNativeBlobStore


RUN_ROOT = Path(sys.argv[1]).resolve(strict=True)
# Only copied example helpers join the isolated interpreter; docspec stays installed.
sys.path.insert(0, str(RUN_ROOT))
from examples.dataset_example_support import document_results, output_value  # noqa: E402
PROOF_PATH = Path(sys.argv[2])
SPICY_DOCS_IMPLEMENTATION = "git+https://example.test/spicy-docs@" + "a" * 40
DOCSPEC_IMPLEMENTATION = "git+https://example.test/docspec@" + "1" * 40
QUERY_SCOPE = {"publishedFrom": "2026-08-25", "publishedThrough": "2026-08-25"}
DOCUMENT_IDS = ("2026-00001", "2026-00002", "2026-00003")
REGULATIONS_DOCUMENT_ID = "EPA-2026-0001-0001"
REGULATIONS_DOCKET_ID = "EPA-2026-0001"
REGULATIONS_COMMENT_ID = "EPA-2026-0001-9001"
REGULATIONS_FR_DOCUMENT_ID = DOCUMENT_IDS[0]

provider_wheel = RUN_ROOT / "wheelhouse" / "spicy_docs-__SPICY_DOCS_VERSION__-py3-none-any.whl"
provider_root = Path(spicy_docs.__file__).resolve(strict=True).parent
assert "site-packages" in provider_root.parts
assert importlib.metadata.version("spicy-docs") == "__SPICY_DOCS_VERSION__"
assert CURRENT_PRODUCER_PRODUCT == "spicy-docs"
assert FORMAT_VERSION == VERIFIER_VERSION == "2.0"
assert hashlib.sha256(provider_wheel.read_bytes()).hexdigest() == "__SPICY_DOCS_WHEEL_SHA256__"
with zipfile.ZipFile(provider_wheel) as archive:
    provider_members = [
        name for name in archive.namelist()
        if name.startswith("spicy_docs/") and not name.endswith("/")
    ]
    assert provider_members
    for name in provider_members:
        assert (provider_root.parent / name).read_bytes() == archive.read(name), name


@dataclass(frozen=True, slots=True)
class SourceFixture:
    release: Any
    profile_name: str
    blob_store: Path


@dataclass(frozen=True, slots=True)
class SourceObject:
    key: str
    etag: str
    version_id: str | None
    content: bytes


class ObjectReader:
    def __init__(self, objects: tuple[SourceObject, ...]) -> None:
        self.objects = objects

    def iter_source_objects(self, *, max_bytes: int) -> Iterator[SourceObject]:
        assert max_bytes == 16 * 1024 * 1024
        yield from self.objects


def document(number: str, *, changed: bool) -> dict[str, object]:
    return {
        "agencies": [],
        "body_html_url": None,
        "full_text_xml_url": None,
        "document_number": number,
        "html_url": f"https://www.federalregister.gov/d/{number}",
        "pdf_url": None,
        "publication_date": "2026-08-25",
        "regulation_id_numbers": ["2060-AV12"],
        "title": "Changed installed-wheel title" if changed else f"Installed-wheel title {number}",
        "topics": [],
        "type": "Rule",
    }


def response(*documents: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "count": len(documents),
            "next_page_url": None,
            "results": list(documents),
            "total_pages": 1,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def pages(payload: bytes) -> tuple[FederalRegisterPage, ...]:
    request = federal_register_documents_url(QUERY_SCOPE)
    return tuple(
        FederalRegisterPage(
            traversal_index=traversal,
            page_index=0,
            request_key=request,
            source_cursor=None,
            response_bytes=payload,
            window_index=0,
            window_page_index=0,
        )
        for traversal in (0, 1)
    )


def completed_at() -> datetime:
    return datetime(2026, 8, 25, 0, 0, 1, tzinfo=UTC)


def producer() -> Producer:
    return Producer(
        "spicy-docs",
        SPICY_DOCS_IMPLEMENTATION,
        VERIFIER_ID,
        VERIFIER_VERSION,
        SPICY_DOCS_IMPLEMENTATION,
    )


def catalog_producer() -> Producer:
    return source_catalog_producer(
        implementation_id=DOCSPEC_IMPLEMENTATION,
        verifier_id="urn:docspec:verifier:source-catalog",
        verifier_version="1.0.0",
        verifier_implementation_id=DOCSPEC_IMPLEMENTATION,
    )


def publish_federal_source(
    destination: Path, *, changed_id: str | None, records: tuple[dict[str, object], ...] | None = None,
) -> SourceFixture:
    payload = response(
        *(tuple(document(value, changed=value == changed_id) for value in DOCUMENT_IDS) if records is None else records)
    )
    blob_store = RUN_ROOT / "source-native-blobs"
    release = SourceNativeReleasePublisher(
        FEDERAL_REGISTER_PROFILE,
        blob_store=LocalSourceNativeBlobStore(blob_store),
        clock=completed_at,
    ).publish(
        pages(payload),
        build=SourceNativeReleaseBuild(
            query_scope=QUERY_SCOPE,
            producer=producer(),
            started_at="2026-08-25T00:00:00Z",
        ),
        destination=destination,
    )
    return SourceFixture(release, "federal-register", blob_store)


def regulations_document() -> dict[str, object]:
    return {
        "data": {
            "id": REGULATIONS_DOCUMENT_ID,
            "type": DOCUMENT_COLLECTION,
            "attributes": {
                "additionalRins": ["2060-AV12"],
                "agencyId": "EPA",
                "commentEndDate": None,
                "docketId": REGULATIONS_DOCKET_ID,
                "documentType": "Notice",
                "fileFormats": [
                    {
                        "fileUrl": (
                            "https://downloads.regulations.gov/"
                            f"{REGULATIONS_DOCUMENT_ID}/content.pdf"
                        ),
                        "format": "pdf",
                        "size": 123,
                    }
                ],
                "frDocNum": REGULATIONS_FR_DOCUMENT_ID,
                "modifyDate": "2026-08-25T01:02:03Z",
                "postedDate": "2026-08-24T04:00:00Z",
                "reasonWithdrawn": None,
                "title": "Installed Regulations.gov document",
                "topics": ["Air quality"],
                "withdrawn": False,
            },
            "links": {
                "self": (
                    "https://api.regulations.gov/v4/documents/"
                    f"{REGULATIONS_DOCUMENT_ID}"
                )
            },
        }
    }


def regulations_docket() -> dict[str, object]:
    return {
        "data": {
            "id": REGULATIONS_DOCKET_ID,
            "type": DOCKET_COLLECTION,
            "attributes": {
                "agencyId": "EPA",
                "dkAbstract": "Installed docket evidence",
                "docketType": "Rulemaking",
                "modifyDate": "2026-08-24T05:00:00Z",
                "rin": "2060-AZ99",
                "title": "Installed Regulations.gov docket",
            },
            "links": {
                "self": (
                    "https://api.regulations.gov/v4/dockets/"
                    f"{REGULATIONS_DOCKET_ID}"
                )
            },
        }
    }


def regulations_comment() -> dict[str, object]:
    return {
        "data": {
            "id": REGULATIONS_COMMENT_ID,
            "type": COMMENT_COLLECTION,
            "attributes": {
                "agencyId": "EPA",
                "comment": "Installed public comment",
                "commentOn": "source-object",
                "commentOnDocumentId": REGULATIONS_DOCUMENT_ID,
                "docketId": REGULATIONS_DOCKET_ID,
                "documentType": "Public Submission",
                "fileFormats": [
                    {
                        "fileUrl": (
                            "https://downloads.regulations.gov/"
                            f"{REGULATIONS_COMMENT_ID}/comment.txt"
                        ),
                        "format": "txt",
                        "size": 42,
                    }
                ],
                "modifyDate": "2026-08-25T06:00:00Z",
                "postedDate": "2026-08-24T04:00:00Z",
                "reasonWithdrawn": None,
                "title": "Installed Regulations.gov comment",
                "withdrawn": False,
            },
            "links": {
                "self": (
                    "https://api.regulations.gov/v4/comments/"
                    f"{REGULATIONS_COMMENT_ID}"
                )
            },
        }
    }


def source_object(collection: str, value: dict[str, object]) -> SourceObject:
    identity = str(value["data"]["id"])
    if collection == DOCUMENT_COLLECTION:
        key = (
            "raw-data/EPA/EPA-2026-0001/text-1/documents/"
            f"{identity}.json"
        )
    elif collection == DOCKET_COLLECTION:
        key = f"raw-data/EPA/{identity}/text-1/docket/{identity}.json"
    else:
        key = (
            "raw-data/EPA/EPA-2026-0001/text-1/comments/"
            f"{identity}.json"
        )
    return SourceObject(
        key,
        f'"{collection}-etag"',
        f"{collection}-version",
        json.dumps(value, indent=2, sort_keys=True).encode("utf-8"),
    )


def publish_regulations_source(
    destination: Path,
    *,
    profile: object,
    profile_name: str,
    collection: str,
    value: dict[str, object],
    query_scope: dict[str, object],
    page_iterator: Any,
) -> SourceFixture:
    blob_store = RUN_ROOT / "source-native-blobs"
    source = source_object(collection, value)

    def read(agency: str) -> ObjectReader:
        assert agency == "EPA"
        return ObjectReader((source,))

    release = SourceNativeReleasePublisher(
        profile,
        blob_store=LocalSourceNativeBlobStore(blob_store),
        clock=completed_at,
    ).publish(
        page_iterator(read, query_scope=query_scope),
        build=SourceNativeReleaseBuild(
            query_scope=query_scope,
            producer=producer(),
            started_at="2026-08-25T00:00:00Z",
        ),
        destination=destination,
    )
    return SourceFixture(release, profile_name, blob_store)


def clean_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}
    }
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def run(command: list[str]) -> dict[str, object]:
    result = subprocess.run(
        command,
        cwd=RUN_ROOT,
        env=clean_environment(),
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {command!r}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return json.loads(result.stdout)


def build_catalog_arguments(
    sources: tuple[SourceFixture, ...],
    policy: object,
    destination: Path,
    blob_store: Path,
) -> list[str]:
    policy_path = destination.parent / f"{destination.name}-policy.json"
    policy_path.write_bytes(
        canonical_json_file_bytes(
            policy.to_member()
        )
    )
    command = [
        str(Path(sys.executable).parent / "docspec"),
        "source-catalog",
        "build",
    ]
    for source in sources:
        command.extend(
            [
                "--source-native",
                str(source.release.root),
                "--source-native-artifact-digest",
                source.release.artifact.pin.artifact_digest,
                "--source-native-profile",
                source.profile_name,
                "--source-native-blob-store",
                str(source.blob_store),
            ]
        )
    command.extend(
        [
            "--accepted-source-verifier-implementation-id",
            SPICY_DOCS_IMPLEMENTATION,
            "--catalog-policy",
            str(policy_path),
            "--implementation-id",
            DOCSPEC_IMPLEMENTATION,
            "--verifier-implementation-id",
            DOCSPEC_IMPLEMENTATION,
            "--destination",
            str(destination),
            "--blob-store",
            str(blob_store),
        ]
    )
    return command


def build_catalog(
    sources: tuple[SourceFixture, ...],
    policy: object,
    destination: Path,
    blob_store: Path,
) -> dict[str, object]:
    return run(build_catalog_arguments(sources, policy, destination, blob_store))


def regulations_policy() -> RegulationsGovCatalogPolicy:
    return RegulationsGovCatalogPolicy(
        SourceInputSelector(
            REGULATIONS_GOV_DOCUMENT_PROFILE.source_system_id,
            REGULATIONS_GOV_DOCUMENT_PROFILE.source_system_version,
            "regulations-gov-documents",
            "regulations-gov-document-raw",
            "1.0",
        ),
        SourceInputSelector(
            REGULATIONS_GOV_DOCKET_PROFILE.source_system_id,
            REGULATIONS_GOV_DOCKET_PROFILE.source_system_version,
            "regulations-gov-dockets",
            "regulations-gov-docket-raw",
            "1.0",
        ),
        SourceInputSelector(
            FEDERAL_REGISTER_PROFILE.source_system_id,
            FEDERAL_REGISTER_PROFILE.source_system_version,
            "federal-register-documents",
            "federal-register-document",
            "1.1",
        ),
        {"EPA": "Environmental Protection Agency"},
        comment_input=SourceInputSelector(
            REGULATIONS_GOV_COMMENT_PROFILE.source_system_id,
            REGULATIONS_GOV_COMMENT_PROFILE.source_system_version,
            "regulations-gov-comments",
            "regulations-gov-comment-raw",
            "1.0",
        ),
    )


def admit_catalog(build_report: dict[str, object], destination: Path, name: str) -> dict[str, object]:
    reference_path = RUN_ROOT / f"{name}-reference.json"
    reference_path.write_bytes(canonical_json_file_bytes(build_report["catalog"]))
    return run(
        [
            str(Path(sys.executable).parent / "docspec"),
            "source-catalog",
            "verify",
            "--root",
            str(destination),
            "--reference",
            str(reference_path),
            "--implementation-id",
            DOCSPEC_IMPLEMENTATION,
            "--verifier-implementation-id",
            DOCSPEC_IMPLEMENTATION,
        ]
    )


assert sys.version_info[:2] == (3, 12)
assert importlib.metadata.version("docspec") == "__DOCSPEC_VERSION__"
assert importlib.metadata.version("rulespec-artifacts") == "1.0.12"
assert importlib.metadata.version("spicy-docs") == "__SPICY_DOCS_VERSION__"
provider_requirements = [
    "".join(requirement.split()).replace('"', "'")
    for requirement in (importlib.metadata.requires("docspec") or ())
    if requirement.lower().startswith("spicy-docs")
]
assert set(provider_requirements) == {
    "spicy-docs==__SPICY_DOCS_VERSION__;extra=='spicy-docs'",
    "spicy-docs[pdf-pypdf]==__SPICY_DOCS_VERSION__;extra=='pdf'",
}
environment_root = Path(sys.prefix).resolve(strict=True)
module_origins = {
    "docspec": str(Path(docspec.__file__).resolve(strict=True)),
    "spicy_docs": str(Path(spicy_docs.__file__).resolve(strict=True)),
}
assert all(Path(value).is_relative_to(environment_root) for value in module_origins.values())

direct_urls: dict[str, object] = {}
for distribution_name in ("docspec", "rulespec-artifacts", "spicy-docs"):
    distribution = importlib.metadata.distribution(distribution_name)
    direct_url = json.loads(distribution.read_text("direct_url.json"))
    assert direct_url.get("dir_info", {}).get("editable") is not True
    direct_urls[distribution_name] = direct_url

source_a = publish_federal_source(RUN_ROOT / "source-native-a", changed_id=None)
source_b = publish_federal_source(
    RUN_ROOT / "source-native-b",
    changed_id=DOCUMENT_IDS[0],
)
public_source = SpicyDocsSourceNativeAdapter.from_local(
    source_a.release.root, blob_root=source_a.blob_store,
    logical_id=source_a.release.artifact.pin.logical_id,
    artifact_digest=source_a.release.artifact.pin.artifact_digest,
    profile=FEDERAL_REGISTER_PROFILE,
    accepted_verifier_implementation_ids=frozenset({SPICY_DOCS_IMPLEMENTATION}),
)
public_workspace = (RUN_ROOT / "public-catalog-workspace")
public_catalog = build_local_catalog(
    (public_source,), public_workspace,
    policy=FederalRegisterCatalogPolicy(FEDERAL_REGISTER_PROFILE.source_system_id),
    catalog_id="urn:docspec:installed-public-catalog", producer=catalog_producer(), max_scratch_bytes=16 * 1024**2,
)
public_rows = tuple(open_local_catalog(public_catalog.reference, public_workspace, producer=catalog_producer()).iter_mappings())
assert {row["documentId"] for row in public_rows} == set(DOCUMENT_IDS)
assert {row["sourceItemId"] for row in public_rows} == {
    row["sourceRecordId"] for row in public_source.iter_records()
}
assert {path.name for path in public_workspace.iterdir()} == {"sourceCatalog"}
assert public_source.describe().collection_outcome["recordOutcome"] == "no-record-rejections"
assert public_source.describe().collection_outcome["failedRecordCount"] == 0
# This older fixture deliberately has empty agency lists: the provider accepts
# those raw records, while DocSpec's required-metadata interpretation refuses.
assert public_catalog.summary.disposition_counts["failed"] == len(DOCUMENT_IDS)

# A successful provider record enters the ordinary public runtime. The real
# HTTPS fetcher reads controlled publisher bytes once; later processing uses
# the retained capture, including its exact source and byte evidence.
body_url = "https://www.federalregister.gov/documents/full_text/2026-00001.xml"
body = b"<RULE><HD>Notice</HD><P>Public access.</P><P>Machine readable documents.</P></RULE>"
body_fixture = publish_federal_source(RUN_ROOT / "source-with-body", changed_id=None, records=(
    document(DOCUMENT_IDS[0], changed=False) | {
        "agencies": [{"slug": "environmental-protection-agency", "name": "Environmental Protection Agency"}],
        "full_text_xml_url": body_url,
        "body_html_url": body_url.replace(".xml", ".html"),
        "pdf_url": body_url.replace(".xml", ".pdf"),
    },
))
body_source = SpicyDocsSourceNativeAdapter.from_local(
    body_fixture.release.root, blob_root=body_fixture.blob_store,
    logical_id=body_fixture.release.artifact.pin.logical_id,
    artifact_digest=body_fixture.release.artifact.pin.artifact_digest,
    profile=FEDERAL_REGISTER_PROFILE,
    accepted_verifier_implementation_ids=frozenset({SPICY_DOCS_IMPLEMENTATION}),
)
body_workspace = (RUN_ROOT / "body-workspace")
body_catalog = build_local_catalog((body_source,), body_workspace,
    policy=FederalRegisterCatalogPolicy(FEDERAL_REGISTER_PROFILE.source_system_id),
    catalog_id="urn:docspec:installed-body-catalog", producer=catalog_producer(), max_scratch_bytes=16 * 1024**2)
assert body_catalog.summary.disposition_counts["selected"] == 1
body_item, = open_local_catalog(body_catalog.reference, body_workspace, producer=catalog_producer()).iter_mappings()
assert body_item["candidateRenditions"][0]["locator"] == body_url
assert body_item["candidateRenditions"][0]["mediaType"] == "application/xml"
assert body_item["sourceNativeFacts"][0]["fields"]["body_html_url"] == body_url.replace(".xml", ".html")
assert body_item["sourceNativeFacts"][0]["fields"]["pdf_url"] == body_url.replace(".xml", ".pdf")
requests = []


def serve_body(request):
    assert request.method == "GET" and str(request.url) == body_url
    requests.append(str(request.url))
    return httpx.Response(200, stream=httpx.ByteStream(body), headers={"Content-Type": "application/xml; charset=utf-8"})


statistics = content_statistics_processor()
with httpx.Client(transport=httpx.MockTransport(serve_body)) as client:
    fetcher = HttpsContentFetcher(client, HttpsContentFetcherConfig(
        allowed_hosts=("www.federalregister.gov",), user_agent="DocSpec installed fixture"))
    with CoreWorkspace(body_workspace) as workspace:
        pipeline = workspace.documents(fetcher=fetcher, extractor=VisibleTextExtractor(),
            segmenter=VisibleTextBlockSegmenter())
        pipeline.import_sources([SourceCatalogItem.from_dict(body_item)], state_id="catalog")
        pipeline.run("catalog", run_id="captured", extract=False, segment=False)
        captured_result = next(document_results(workspace, pipeline, "captured"))[1][0]
    with CoreWorkspace(body_workspace) as workspace:
        pipeline = workspace.documents(fetcher=fetcher, extractor=VisibleTextExtractor(),
            segmenter=VisibleTextBlockSegmenter())
        body_state = pipeline.run("catalog", run_id="processed", processors=(statistics,))
        body_results = next(document_results(workspace, pipeline, "processed"))[1]
        assert len(body_results) == 4 and all(result.outcome.status == "success" for result in body_results)
        assert body_results[0] == captured_result
        assert output_value(workspace, body_results[0], "content") == body
        segments_state = body_results[2].outcome.outputs[0].entity_id
        statistics_state = body_results[3].outcome.outputs[0].entity_id
        statistics_rows = [value for _, _, value in pipeline.rows(statistics_state)]
        with workspace.publisher.session() as session:
            segments = {segment.segment_id: (segment, reference)
                for _, segment, reference in segment_rows(session, segments_state)}
            assert len(segments) == len(statistics_rows) == 3
            for value in statistics_rows:
                segment, reference = segments[value["segmentId"]]
                with closing(session.blobs.read(reference, max_bytes=1024**2)) as chunks:
                    segment_bytes = b"".join(chunks)
                assert value["wordCount"] == len(segment_bytes.decode("utf-8").split())
                assert value["evidence"] == segment.evidence.to_dict()
                assert value["contentDigest"] == "sha256:" + hashlib.sha256(segment_bytes).hexdigest()
        body_proof = {"requests": requests, "segments": len(segments), "derivedRecords": len(statistics_rows),
            "state": record_value(body_state), "selectedResults": [result.result_id for result in body_results]}
assert requests == [body_url]
assert body_catalog.summary.source_native_inputs[0]["collectionOutcome"]["recordOutcome"] == "no-record-rejections"

# The actual installed provider owns classification and evidence admission.
# DocSpec retains its public report and makes acceptance a separate choice.
collection_proof = {}
for outcome_name, records in (
    ("empty", ()),
    ("partial-rejection", (document(DOCUMENT_IDS[0], changed=False) | {
        "agencies": [{"slug": "environmental-protection-agency", "name": "Environmental Protection Agency"}],
    }, {})),
    ("total-rejection", ({},)),
):
    fixture = publish_federal_source(RUN_ROOT / f"source-{outcome_name}", changed_id=None, records=records)
    adapter = SpicyDocsSourceNativeAdapter.from_local(
        fixture.release.root, blob_root=fixture.blob_store,
        logical_id=fixture.release.artifact.pin.logical_id, artifact_digest=fixture.release.artifact.pin.artifact_digest,
        profile=FEDERAL_REGISTER_PROFILE, accepted_verifier_implementation_ids=frozenset({SPICY_DOCS_IMPLEMENTATION}),
    )
    reported = adapter.describe().to_dict()["collectionOutcome"]
    assert reported["recordOutcome"] == outcome_name
    assert reported["discoveredRecordCount"] == len(records)
    assert reported["publishedRecordCount"] == int(outcome_name == "partial-rejection")
    assert reported["failedRecordCount"] == int(outcome_name != "empty")
    assert reported["transientFailureCount"] == reported["unclassedFailureCount"] == 0
    assert list(adapter.iter_failures(limit=0)) == []
    failures = list(adapter.iter_failures(limit=1))
    assert len(failures) == reported["failedRecordCount"]
    if failures:
        raw = response(*records)
        evidence_ref = failures[0]["evidenceBlobRef"]
        assert adapter.read_evidence(evidence_ref, max_bytes=len(raw)) == raw
        assert hashlib.sha256(raw).hexdigest() == evidence_ref.removeprefix("sha256:")
        assert adapter.record_evidence(failures[0]["sourceRecordId"]) is None
        try:
            adapter.read_evidence(evidence_ref, max_bytes=len(raw) - 1)
        except ValueError:
            pass
        else:
            raise AssertionError("provider evidence byte limit was ignored")
    for published in adapter.iter_records():
        assert adapter.record_evidence(published["sourceRecordId"])["failure"] is None
    try:
        adapter.read_evidence("sha256:" + "f" * 64, max_bytes=1024)
    except ValueError:
        pass
    else:
        raise AssertionError("unknown provider evidence was accepted")
    chosen_workspace = (RUN_ROOT / f"catalog-{outcome_name}")
    settings = {
        "policy": FederalRegisterCatalogPolicy(FEDERAL_REGISTER_PROFILE.source_system_id),
        "catalog_id": "urn:docspec:installed-collection-outcome", "producer": catalog_producer(),
        "max_scratch_bytes": 16 * 1024**2,
    }
    if outcome_name != "empty":
        try:
            build_local_catalog((adapter,), chosen_workspace, **settings)
        except ValueError as error:
            assert outcome_name in str(error)
        else:
            raise AssertionError("record rejection was accepted without an explicit choice")
        assert not chosen_workspace.exists()
    if outcome_name == "total-rejection":
        try:
            build_local_catalog((adapter,), chosen_workspace, **settings,
                accepted_record_outcomes=frozenset({"empty", "no-record-rejections", "partial-rejection"}))
        except ValueError as error:
            assert "total-rejection" in str(error)
        else:
            raise AssertionError("partial acceptance authorized total rejection")
        assert not chosen_workspace.exists()
    result = build_local_catalog((adapter,), chosen_workspace, **settings,
        accepted_record_outcomes=frozenset({"empty", "no-record-rejections", outcome_name}))
    preview = preview_local_catalog(result.reference, chosen_workspace, producer=catalog_producer())
    assert preview["catalog"]["sourceNativeInputs"][0]["collectionOutcome"] == reported
    assert preview["catalog"]["catalogSelection"]["counts"]["failed"] == 0
    with CoreWorkspace(chosen_workspace) as workspace:
        pipeline = workspace.documents(fetcher=fetcher)
        admitted = open_local_catalog(result.reference, chosen_workspace, producer=catalog_producer())
        pipeline.import_sources((SourceCatalogItem.from_dict(row) for row in admitted.iter_mappings()), state_id="catalog")
        assert len(list(pipeline.rows("catalog"))) == reported["publishedRecordCount"]
        assert canonical_json_file_bytes(admitted.summary.source_native_inputs[0]["collectionOutcome"]) == canonical_json_file_bytes(reported)
    collection_proof[outcome_name] = reported

unresolved_destination = RUN_ROOT / "source-unresolved"
try:
    SourceNativeReleasePublisher(FEDERAL_REGISTER_PROFILE,
        blob_store=LocalSourceNativeBlobStore(RUN_ROOT / "source-native-blobs"), clock=completed_at,
    ).publish(
        (pages(response(document(DOCUMENT_IDS[0], changed=False)))[0],
         pages(response(document(DOCUMENT_IDS[1], changed=False)))[1]),
        build=SourceNativeReleaseBuild(query_scope=QUERY_SCOPE, producer=producer(), started_at="2026-08-25T00:00:00Z"),
        destination=unresolved_destination,
    )
except ValueError as error:
    assert "stable consecutive traversals" in str(error)
    assert error.failed_acquisition["retainedPageEvidence"]["count"] == 2
else:
    raise AssertionError("unresolved provider collection published a release")
assert not (unresolved_destination / "release.json").exists()
document_source = publish_regulations_source(
    RUN_ROOT / "source-native-regulations-documents",
    profile=REGULATIONS_GOV_DOCUMENT_PROFILE,
    profile_name="regulations-gov-documents",
    collection=DOCUMENT_COLLECTION,
    value=regulations_document(),
    query_scope={
        "agencies": ["EPA"],
        "publishedFrom": "2026-08-24",
        "publishedThrough": "2026-08-24",
    },
    page_iterator=iter_regulations_gov_document_pages,
)
docket_source = publish_regulations_source(
    RUN_ROOT / "source-native-regulations-dockets",
    profile=REGULATIONS_GOV_DOCKET_PROFILE,
    profile_name="regulations-gov-dockets",
    collection=DOCKET_COLLECTION,
    value=regulations_docket(),
    query_scope={
        "agencies": ["EPA"],
        "modifiedFrom": "2026-08-24",
        "modifiedThrough": "2026-08-24",
    },
    page_iterator=iter_regulations_gov_docket_pages,
)
comment_source = publish_regulations_source(
    RUN_ROOT / "source-native-regulations-comments",
    profile=REGULATIONS_GOV_COMMENT_PROFILE,
    profile_name="regulations-gov-comments",
    collection=COMMENT_COLLECTION,
    value=regulations_comment(),
    query_scope={
        "agencies": ["EPA"],
        "postedFrom": "2026-08-24",
        "postedThrough": "2026-08-24",
    },
    page_iterator=iter_regulations_gov_comment_pages,
)
blob_store = RUN_ROOT / "shared-blobs"
destination_a = RUN_ROOT / "catalog-a"
destination_b = RUN_ROOT / "catalog-b"
destination_c = RUN_ROOT / "catalog-physical-rebuild"
destination_regulations = RUN_ROOT / "catalog-regulations"
federal_policy = FederalRegisterCatalogPolicy(
    FEDERAL_REGISTER_PROFILE.source_system_id
)
report_a = build_catalog(
    (source_a,),
    federal_policy,
    destination_a,
    blob_store,
)
report_b = build_catalog(
    (source_b,),
    federal_policy,
    destination_b,
    blob_store,
)
report_c = build_catalog(
    (source_a,),
    federal_policy,
    destination_c,
    blob_store,
)
report_regulations = build_catalog(
    (document_source, docket_source, comment_source, source_a),
    regulations_policy(),
    destination_regulations,
    blob_store,
)
assert {
    report_a["verdict"],
    report_b["verdict"],
    report_c["verdict"],
    report_regulations["verdict"],
} == {"pass"}
for build_report, expected_profiles in (
    (report_a, ["federal-register"]),
    (report_b, ["federal-register"]),
    (report_c, ["federal-register"]),
    (
        report_regulations,
        [
            "regulations-gov-documents",
            "regulations-gov-dockets",
            "regulations-gov-comments",
            "federal-register",
        ],
    ),
):
    assert build_report["acceptedSourceVerifierImplementationIds"] == [
        SPICY_DOCS_IMPLEMENTATION
    ]
    assert [
        value["profile"] for value in build_report["sourceNativeInputs"]
    ] == expected_profiles

artifact_root_a = (
    destination_a / report_a["catalog"]["digest"].removeprefix("sha256:")
)
root_before_refusal = (artifact_root_a / "artifact.json").read_bytes()
refusal = subprocess.run(
    build_catalog_arguments(
        (source_a,),
        federal_policy,
        destination_a,
        blob_store,
    ),
    cwd=RUN_ROOT,
    env=clean_environment(),
    capture_output=True,
    check=False,
    text=True,
)
assert refusal.returncode == 2
assert "refusing to replace existing artifact" in refusal.stderr
assert (artifact_root_a / "artifact.json").read_bytes() == root_before_refusal
assert refusal.stdout == ""

artifact_receipts = []
for build_report, destination in (
    (report_a, destination_a),
    (report_b, destination_b),
    (report_c, destination_c),
    (report_regulations, destination_regulations),
):
    artifact_root = destination / build_report["catalog"]["digest"].removeprefix("sha256:")
    artifact_receipt = json.loads(
        (artifact_root / "catalog-build-receipt.json").read_text(encoding="utf-8")
    )
    assert build_report["byteMeasurements"] == artifact_receipt["byteMeasurements"]
    assert build_report["blobStore"] == {
        "accountingStatus": "complete",
        "path": str(blob_store.resolve(strict=True)),
        "payloadBytesReused": artifact_receipt["byteMeasurements"]["payloadBytesReused"],
        "payloadBytesWritten": artifact_receipt["byteMeasurements"]["payloadBytesWritten"],
        "retention": "verified-content-addressed-blobs-retained-for-reuse",
    }
    artifact_receipts.append(artifact_receipt)

initial_receipt, successor_receipt, rebuilt_receipt, regulations_receipt = (
    artifact_receipts
)
initial_partitions = {
    value["partitionId"]: value for value in initial_receipt["partitions"]
}
successor_partitions = {
    value["partitionId"]: value for value in successor_receipt["partitions"]
}
assert len(initial_partitions) == 3
assert set(successor_partitions) == set(initial_partitions)
changed_partitions = {
    partition_id
    for partition_id in initial_partitions
    if initial_partitions[partition_id]["blobRef"]
    != successor_partitions[partition_id]["blobRef"]
}
assert len(changed_partitions) == 1
changed_partition = changed_partitions.pop()
unchanged_partitions = set(initial_partitions) - {changed_partition}
assert {
    partition_id: successor_partitions[partition_id]["blobRef"]
    for partition_id in unchanged_partitions
} == {
    partition_id: initial_partitions[partition_id]["blobRef"]
    for partition_id in unchanged_partitions
}
assert initial_receipt["byteMeasurements"]["payloadBytesReused"] == 0
assert initial_receipt["byteMeasurements"]["payloadBytesWritten"] == sum(
    value["byteSize"] for value in initial_partitions.values()
)
assert successor_receipt["byteMeasurements"]["payloadBytesWritten"] == (
    successor_partitions[changed_partition]["byteSize"]
)
assert successor_receipt["byteMeasurements"]["payloadBytesReused"] == sum(
    successor_partitions[value]["byteSize"] for value in unchanged_partitions
)
assert rebuilt_receipt["partitions"] == initial_receipt["partitions"]
assert rebuilt_receipt["byteMeasurements"]["payloadBytesWritten"] == 0
assert rebuilt_receipt["byteMeasurements"]["payloadBytesReused"] == sum(
    value["byteSize"] for value in initial_partitions.values()
)
assert report_c["catalog"]["catalogId"] == report_a["catalog"]["catalogId"]
assert report_c["catalog"]["digest"] != report_a["catalog"]["digest"]
assert report_b["catalog"]["catalogId"] != report_a["catalog"]["catalogId"]
assert regulations_receipt["itemCount"] == 3

admission_a = admit_catalog(report_a, destination_a, "catalog-a")
admission_b = admit_catalog(report_b, destination_b, "catalog-b")
admission_c = admit_catalog(report_c, destination_c, "catalog-physical-rebuild")
admission_regulations = admit_catalog(
    report_regulations,
    destination_regulations,
    "catalog-regulations",
)
for admission, build_report, item_count in (
    (admission_a, report_a, len(DOCUMENT_IDS)),
    (admission_b, report_b, len(DOCUMENT_IDS)),
    (admission_c, report_c, len(DOCUMENT_IDS)),
    (admission_regulations, report_regulations, 3),
):
    assert admission["logicalId"] == build_report["catalog"]["catalogId"]
    assert admission["artifactDigest"] == build_report["catalog"]["digest"]
    assert admission["itemCount"] == item_count

regulations_snapshot = SourceCatalogArtifactReader(
    LocalSourceCatalogStore(destination_regulations, create=False),
    producer=catalog_producer(),
).open_snapshot(SourceCatalogRef.from_dict(report_regulations["catalog"]))
regulations_located = tuple(regulations_snapshot.located_items)
regulations_items = tuple(value.item for value in regulations_located)
assert [item.source_item_id for item in regulations_items] == sorted(
    [
        REGULATIONS_DOCUMENT_ID,
        REGULATIONS_DOCKET_ID,
        REGULATIONS_COMMENT_ID,
    ]
)
assert {value.blob_ref for value in regulations_located} == {
    value["blobRef"] for value in regulations_receipt["partitions"]
}
regulations_item_scopes = {
    item.source_item_id: [fact["scopeId"] for fact in item.source_native_facts]
    for item in regulations_items
}
assert regulations_item_scopes == {
    REGULATIONS_DOCUMENT_ID: [
        "regulations-gov-documents",
        "regulations-gov-dockets",
        "federal-register-documents",
    ],
    REGULATIONS_DOCKET_ID: ["regulations-gov-dockets"],
    REGULATIONS_COMMENT_ID: [
        "regulations-gov-comments",
        "regulations-gov-dockets",
        "regulations-gov-documents",
    ],
}

proof = {
    "collectionOutcomes": collection_proof,
    "retainedBodyProcessing": body_proof,
    "publicCatalogReference": public_catalog.reference.to_dict(),
    "pythonVersion": ".".join(str(value) for value in sys.version_info[:3]),
    "sysPath": list(sys.path),
    "moduleOrigins": module_origins,
    "directUrls": direct_urls,
    "provider": {
        "version": "__SPICY_DOCS_VERSION__",
        "sourceRevision": "__SPICY_DOCS_REVISION__",
        "wheelSha256": "__SPICY_DOCS_WHEEL_SHA256__",
        "verifiedPackageMembers": len(provider_members),
        "currentProducerProduct": CURRENT_PRODUCER_PRODUCT,
        "sourceNativeFormatVersion": FORMAT_VERSION,
        "verifierVersion": VERIFIER_VERSION,
        "profiles": {
            name: {
                "sourceSystemId": profile.source_system_id,
                "sourceSystemVersion": profile.source_system_version,
                "sourceStateScope": profile.source_state_scope,
                "acquisitionPolicyVersion": profile.acquisition_policy_version,
                "sourceSchemaKey": profile.source_schema_key,
            }
            for name, profile in (
                ("federal-register", FEDERAL_REGISTER_PROFILE),
                ("regulations-gov-documents", REGULATIONS_GOV_DOCUMENT_PROFILE),
                ("regulations-gov-dockets", REGULATIONS_GOV_DOCKET_PROFILE),
                ("regulations-gov-comments", REGULATIONS_GOV_COMMENT_PROFILE),
            )
        },
    },
    "sourceNativePins": [
        source.release.artifact.pin.as_dict()
        for source in (
            source_a,
            source_b,
            document_source,
            docket_source,
            comment_source,
        )
    ],
    "sourceProfiles": [
        source.profile_name
        for source in (
            source_a,
            document_source,
            docket_source,
            comment_source,
        )
    ],
    "buildReports": [report_a, report_b, report_c, report_regulations],
    "admissions": [
        admission_a,
        admission_b,
        admission_c,
        admission_regulations,
    ],
    "existingDestinationRefusal": {
        "returnCode": refusal.returncode,
        "stderr": refusal.stderr,
    },
    "regulationsItemScopes": regulations_item_scopes,
    "changedPartition": changed_partition,
    "unchangedPartitions": sorted(unchanged_partitions),
}
PROOF_PATH.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
