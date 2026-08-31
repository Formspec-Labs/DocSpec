from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RULESPEC_WHEEL = ROOT / "vendor" / "rulespec_artifacts-1.0.9-py3-none-any.whl"
RULESPEC_WHEEL_SHA256 = "67cb33bf63c11bc6812ad0e8f0a8b73e89501fa6d4242acf75a7cc6612f5d6c6"
SPICY_REGS_WHEEL = (
    ROOT
    / "tests"
    / "fixtures"
    / "installed_wheels"
    / "spicy_regs-0.1.7-py3-none-any.whl"
)
SPICY_REGS_WHEEL_SHA256 = "b8c2f9ea3a7f44dbd1c373f4ba3902371ff1664ac55776d5f01372091f97f3ec"


# SpicyRegs 0.1.7 is not published to a package index. This is the exact real
# producer wheel used to publish and verify the bounded source-native fixtures.
# It is test input, not a DocSpec dependency. Different wheel bytes require a
# new SpicyRegs version and a new explicit digest pin; this test never rebuilds it.
_INSTALLED_PROBE = r'''
from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import docspec
import spicy_regs
from docspec.adapters.source_catalog_artifact import (
    SourceCatalogArtifactReader,
    source_catalog_producer,
)
from docspec.adapters.source_catalog_store import LocalSourceCatalogStore
from docspec.application.federal_register_catalog import FederalRegisterCatalogPolicy
from docspec.application.regulations_gov_catalog import RegulationsGovCatalogPolicy
from docspec.domain.identity import canonical_json_file_bytes
from docspec.domain.references import SourceCatalogRef
from docspec.ports.source_catalog import SourceInputSelector
from rulespec_artifacts import Producer
from spicy_regs.federal_register_source_native import (
    FederalRegisterPage,
    federal_register_documents_url,
)
from spicy_regs.regulations_gov_source_native import (
    COMMENT_COLLECTION,
    DOCUMENT_COLLECTION,
    DOCKET_COLLECTION,
    iter_regulations_gov_comment_pages,
    iter_regulations_gov_document_pages,
    iter_regulations_gov_docket_pages,
)
from spicy_regs.source_native import SourceNativeReleaseBuild, SourceNativeReleasePublisher
from spicy_regs.source_native_profiles import (
    FEDERAL_REGISTER_PROFILE,
    REGULATIONS_GOV_COMMENT_PROFILE,
    REGULATIONS_GOV_DOCUMENT_PROFILE,
    REGULATIONS_GOV_DOCKET_PROFILE,
)
from spicy_regs.source_native_store import LocalSourceNativeBlobStore


RUN_ROOT = Path(sys.argv[1]).resolve(strict=True)
PROOF_PATH = Path(sys.argv[2])
SPICY_IMPLEMENTATION = "git+https://example.test/spicy-regs@" + "a" * 40
DOCSPEC_IMPLEMENTATION = "git+https://example.test/docspec@" + "1" * 40
QUERY_SCOPE = {"publishedFrom": "2026-08-25", "publishedThrough": "2026-08-25"}
DOCUMENT_IDS = ("2026-00001", "2026-00002", "2026-00003")
REGULATIONS_DOCUMENT_ID = "EPA-2026-0001-0001"
REGULATIONS_DOCKET_ID = "EPA-2026-0001"
REGULATIONS_COMMENT_ID = "EPA-2026-0001-9001"
REGULATIONS_FR_DOCUMENT_ID = DOCUMENT_IDS[0]


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
        "spicy-regs",
        SPICY_IMPLEMENTATION,
        "urn:spicy-regs:source-native-release-verifier",
        "1.0",
        SPICY_IMPLEMENTATION,
    )


def catalog_producer() -> Producer:
    return source_catalog_producer(
        implementation_id=DOCSPEC_IMPLEMENTATION,
        verifier_id="urn:docspec:verifier:source-catalog",
        verifier_version="1.0.0",
        verifier_implementation_id=DOCSPEC_IMPLEMENTATION,
    )


def publish_federal_source(destination: Path, *, changed_id: str | None) -> SourceFixture:
    payload = response(
        *(document(value, changed=value == changed_id) for value in DOCUMENT_IDS)
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
    receipt: Path,
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
            SPICY_IMPLEMENTATION,
            "--catalog-policy",
            str(policy_path),
            "--implementation-id",
            DOCSPEC_IMPLEMENTATION,
            "--verifier-implementation-id",
            DOCSPEC_IMPLEMENTATION,
            "--destination",
            str(destination),
            "--receipt",
            str(receipt),
            "--blob-store",
            str(blob_store),
        ]
    )
    return command


def build_catalog(
    sources: tuple[SourceFixture, ...],
    policy: object,
    destination: Path,
    receipt: Path,
    blob_store: Path,
) -> dict[str, object]:
    return run(build_catalog_arguments(sources, policy, destination, receipt, blob_store))


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
            "1.0",
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


def admit_catalog(command_receipt: dict[str, object], destination: Path, name: str) -> dict[str, object]:
    reference_path = RUN_ROOT / f"{name}-reference.json"
    reference_path.write_bytes(canonical_json_file_bytes(command_receipt["catalog"]))
    return run(
        [
            str(Path(sys.executable).parent / "docspec"),
            "source-catalog",
            "verify",
            "--root",
            str(destination),
            "--reference",
            str(reference_path),
            "--expected-command-receipt-id",
            command_receipt["receiptId"],
            "--implementation-id",
            DOCSPEC_IMPLEMENTATION,
            "--verifier-implementation-id",
            DOCSPEC_IMPLEMENTATION,
        ]
    )


assert sys.version_info[:2] == (3, 12)
assert importlib.metadata.version("docspec") == "0.2.7"
assert importlib.metadata.version("rulespec-artifacts") == "1.0.9"
assert importlib.metadata.version("spicy-regs") == "0.1.7"
assert not any(
    requirement.lower().startswith("spicy-regs")
    for requirement in (importlib.metadata.requires("docspec") or ())
)
environment_root = Path(sys.prefix).resolve(strict=True)
module_origins = {
    "docspec": str(Path(docspec.__file__).resolve(strict=True)),
    "spicy_regs": str(Path(spicy_regs.__file__).resolve(strict=True)),
}
assert all(Path(value).is_relative_to(environment_root) for value in module_origins.values())

direct_urls: dict[str, object] = {}
for distribution_name in ("docspec", "rulespec-artifacts", "spicy-regs"):
    distribution = importlib.metadata.distribution(distribution_name)
    direct_url = json.loads(distribution.read_text("direct_url.json"))
    assert direct_url.get("dir_info", {}).get("editable") is not True
    direct_urls[distribution_name] = direct_url

source_a = publish_federal_source(RUN_ROOT / "source-native-a", changed_id=None)
source_b = publish_federal_source(
    RUN_ROOT / "source-native-b",
    changed_id=DOCUMENT_IDS[0],
)
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
receipt_path_a = destination_a / "source-catalog-build-command-receipt.json"
receipt_path_b = destination_b / "source-catalog-build-command-receipt.json"
receipt_path_c = destination_c / "source-catalog-build-command-receipt.json"
receipt_path_regulations = (
    destination_regulations / "source-catalog-build-command-receipt.json"
)
federal_policy = FederalRegisterCatalogPolicy(
    FEDERAL_REGISTER_PROFILE.source_system_id
)
command_a = build_catalog(
    (source_a,),
    federal_policy,
    destination_a,
    receipt_path_a,
    blob_store,
)
command_b = build_catalog(
    (source_b,),
    federal_policy,
    destination_b,
    receipt_path_b,
    blob_store,
)
command_c = build_catalog(
    (source_a,),
    federal_policy,
    destination_c,
    receipt_path_c,
    blob_store,
)
command_regulations = build_catalog(
    (document_source, docket_source, comment_source, source_a),
    regulations_policy(),
    destination_regulations,
    receipt_path_regulations,
    blob_store,
)
assert command_a == json.loads(receipt_path_a.read_text(encoding="utf-8"))
assert command_b == json.loads(receipt_path_b.read_text(encoding="utf-8"))
assert command_c == json.loads(receipt_path_c.read_text(encoding="utf-8"))
assert command_regulations == json.loads(
    receipt_path_regulations.read_text(encoding="utf-8")
)
assert {
    command_a["verdict"],
    command_b["verdict"],
    command_c["verdict"],
    command_regulations["verdict"],
} == {"pass"}
for command_receipt, expected_profiles in (
    (command_a, ["federal-register"]),
    (command_b, ["federal-register"]),
    (command_c, ["federal-register"]),
    (
        command_regulations,
        [
            "regulations-gov-documents",
            "regulations-gov-dockets",
            "regulations-gov-comments",
            "federal-register",
        ],
    ),
):
    assert command_receipt["acceptedSourceVerifierImplementationIds"] == [
        SPICY_IMPLEMENTATION
    ]
    assert [
        value["profile"] for value in command_receipt["sourceNativeInputs"]
    ] == expected_profiles

artifact_root_a = (
    destination_a / command_a["catalog"]["digest"].removeprefix("sha256:")
)
root_before_refusal = (artifact_root_a / "artifact.json").read_bytes()
refusal_receipt = destination_a / "source-catalog-build-command-receipt.json"
refusal = subprocess.run(
    build_catalog_arguments(
        (source_a,),
        federal_policy,
        destination_a,
        refusal_receipt,
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
assert json.loads(refusal_receipt.read_text(encoding="utf-8")) == command_a

artifact_receipts = []
for command_receipt, destination in (
    (command_a, destination_a),
    (command_b, destination_b),
    (command_c, destination_c),
    (command_regulations, destination_regulations),
):
    artifact_root = destination / command_receipt["catalog"]["digest"].removeprefix("sha256:")
    artifact_receipt = json.loads(
        (artifact_root / "catalog-build-receipt.json").read_text(encoding="utf-8")
    )
    assert command_receipt["byteMeasurements"] == artifact_receipt["byteMeasurements"]
    assert command_receipt["blobStore"] == {
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
assert command_c["catalog"]["catalogId"] == command_a["catalog"]["catalogId"]
assert command_c["catalog"]["digest"] != command_a["catalog"]["digest"]
assert command_b["catalog"]["catalogId"] != command_a["catalog"]["catalogId"]
assert regulations_receipt["itemCount"] == 3

admission_a = admit_catalog(command_a, destination_a, "catalog-a")
admission_b = admit_catalog(command_b, destination_b, "catalog-b")
admission_c = admit_catalog(command_c, destination_c, "catalog-physical-rebuild")
admission_regulations = admit_catalog(
    command_regulations,
    destination_regulations,
    "catalog-regulations",
)
for admission, command_receipt, item_count in (
    (admission_a, command_a, len(DOCUMENT_IDS)),
    (admission_b, command_b, len(DOCUMENT_IDS)),
    (admission_c, command_c, len(DOCUMENT_IDS)),
    (admission_regulations, command_regulations, 3),
):
    assert admission["commandReceiptId"] == command_receipt["receiptId"]
    assert admission["logicalId"] == command_receipt["catalog"]["catalogId"]
    assert admission["artifactDigest"] == command_receipt["catalog"]["digest"]
    assert admission["itemCount"] == item_count

regulations_snapshot = SourceCatalogArtifactReader(
    LocalSourceCatalogStore(destination_regulations, create=False),
    producer=catalog_producer(),
).open_snapshot(SourceCatalogRef.from_dict(command_regulations["catalog"]))
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
    "pythonVersion": ".".join(str(value) for value in sys.version_info[:3]),
    "sysPath": list(sys.path),
    "moduleOrigins": module_origins,
    "directUrls": direct_urls,
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
    "commandReceipts": [command_a, command_b, command_c, command_regulations],
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
'''


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def test_installed_wheels_cover_source_kinds_reuse_and_independent_admission(
    tmp_path: Path,
) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "the installed-wheel SourceCatalog proof requires uv"
    assert _sha256(RULESPEC_WHEEL) == RULESPEC_WHEEL_SHA256
    assert _sha256(SPICY_REGS_WHEEL) == SPICY_REGS_WHEEL_SHA256

    build_root = tmp_path / "build"
    build_root.mkdir()
    build = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(build_root)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    docspec_wheel = next(build_root.glob("docspec-0.2.7-*.whl"))
    with zipfile.ZipFile(docspec_wheel) as archive:
        assert not any(
            name.endswith(".whl") or name.startswith("spicy_" + "regs/")
            for name in archive.namelist()
        )

    runtime_root = tmp_path / "installed-runtime"
    wheelhouse = runtime_root / "wheelhouse"
    wheelhouse.mkdir(parents=True)
    runtime_rulespec = shutil.copy2(RULESPEC_WHEEL, wheelhouse / RULESPEC_WHEEL.name)
    runtime_spicy_regs = shutil.copy2(SPICY_REGS_WHEEL, wheelhouse / SPICY_REGS_WHEEL.name)
    runtime_docspec = shutil.copy2(docspec_wheel, wheelhouse / docspec_wheel.name)
    environment = runtime_root / "environment"
    create = subprocess.run(
        [uv, "venv", "--python", sys.executable, str(environment)],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert create.returncode == 0, create.stderr
    environment_python = environment / "bin" / "python"
    install_core = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(environment_python),
            str(runtime_rulespec),
            str(runtime_docspec),
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert install_core.returncode == 0, install_core.stderr
    install_producer = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(environment_python),
            "--no-deps",
            str(runtime_spicy_regs),
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert install_producer.returncode == 0, install_producer.stderr

    probe_path = runtime_root / "installed_source_catalog_probe.py"
    proof_path = runtime_root / "installed_source_catalog_proof.json"
    probe_path.write_text(_INSTALLED_PROBE, encoding="utf-8")
    probe = subprocess.run(
        [
            environment_python,
            "-I",
            str(probe_path),
            str(runtime_root),
            str(proof_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
    proof = json.loads(proof_path.read_text(encoding="utf-8"))

    verify_environment = runtime_root / "verify-environment"
    create_verify = subprocess.run(
        [uv, "venv", "--python", sys.executable, str(verify_environment)],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert create_verify.returncode == 0, create_verify.stderr
    verify_python = verify_environment / "bin" / "python"
    install_verify = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(verify_python),
            str(runtime_rulespec),
            str(runtime_docspec),
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )
    assert install_verify.returncode == 0, install_verify.stderr
    isolated_environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}
    }
    isolated_environment["PYTHONNOUSERSITE"] = "1"
    producer_absence = subprocess.run(
        [
            verify_python,
            "-I",
            "-c",
            (
                "import importlib.util; "
                "assert importlib.util.find_spec('spicy_regs') is None"
            ),
        ],
        cwd=tmp_path,
        env=isolated_environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert producer_absence.returncode == 0, producer_absence.stderr
    verify_references = runtime_root / "verify-references"
    verify_references.mkdir()
    docspec_implementation = "git+https://example.test/docspec@" + "1" * 40
    for index, command_receipt in enumerate(proof["commandReceipts"]):
        reference_path = verify_references / f"catalog-{index}.json"
        reference_path.write_text(
            json.dumps(command_receipt["catalog"], sort_keys=True),
            encoding="utf-8",
        )
        verification = subprocess.run(
            [
                verify_environment / "bin" / "docspec",
                "source-catalog",
                "verify",
                "--root",
                command_receipt["destination"],
                "--reference",
                str(reference_path),
                "--expected-command-receipt-id",
                command_receipt["receiptId"],
                "--implementation-id",
                docspec_implementation,
                "--verifier-implementation-id",
                docspec_implementation,
            ],
            cwd=tmp_path,
            env=isolated_environment,
            capture_output=True,
            check=False,
            text=True,
        )
        assert verification.returncode == 0, verification.stderr

    workspace = ROOT.parent.resolve(strict=True).as_posix()
    assert workspace not in json.dumps(proof, sort_keys=True)
    assert proof["pythonVersion"].startswith("3.12.")
    assert proof["sourceProfiles"] == [
        "federal-register",
        "regulations-gov-documents",
        "regulations-gov-dockets",
        "regulations-gov-comments",
    ]
    assert len(proof["sourceNativePins"]) == 5
    assert len(proof["commandReceipts"]) == 4
    assert len(proof["admissions"]) == 4
    assert proof["existingDestinationRefusal"]["returnCode"] == 2
    initial, successor, physical_rebuild, regulations = proof["commandReceipts"]
    assert initial["catalog"]["catalogId"] == physical_rebuild["catalog"]["catalogId"]
    assert initial["catalog"]["digest"] != physical_rebuild["catalog"]["digest"]
    assert initial["catalog"]["catalogId"] != successor["catalog"]["catalogId"]
    assert regulations["itemCount"] == 3
    assert set(proof["regulationsItemScopes"]) == {
        "EPA-2026-0001",
        "EPA-2026-0001-0001",
        "EPA-2026-0001-9001",
    }
    assert len(proof["unchangedPartitions"]) == 2
    for path in runtime_root.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".jsonl", ".txt"}:
            assert workspace.encode() not in path.read_bytes()
