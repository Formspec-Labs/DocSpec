"""Build, process, publish, and verify one synthetic document without a network.

Run from a checkout: uv run --frozen python -m examples.offline_demo --output PATH
PATH must not exist. The example uses the production Federal Register policy on
synthetic source-shaped metadata, with an explicit adapter for local bytes.
"""

from __future__ import annotations

import argparse
import json
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Any

from rulespec_artifacts import Producer

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.cli import main as cli_main
from docspec.cli.execution import run_local
from docspec.domain.content import CandidateFile
from docspec.domain.identity import canonical_json_file_bytes, sha256_digest
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, DataUsePolicy, RetentionPolicy, RetryPolicy
from docspec.domain.processors import ProcessorSet
from docspec.ports.content_fetcher import FetchStream
from docspec.processing.extraction import DefaultExtractorRegistry
from docspec.processing.segmentation import DefaultSegmenterRegistry
from docspec.profile_registry import ProfileRegistry
from docspec.source_catalog import (
    FederalRegisterCatalogPolicy,
    LocalSourceCatalogStore,
    SourceCatalogArtifactReader,
    SourceCatalogBuilder,
    SourceCatalogBuildRequest,
    SourceNativeDescription,
    SqliteCatalogPolicyWorkspace,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
INPUT_ROOT = Path(__file__).with_name("offline")
SOURCE_SYSTEM = "https://www.federalregister.gov/api/v1"
COMPLETED_AT = "2026-09-11T12:00:00Z"


def write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_file_bytes(value))


class ExampleSource:
    """Admit the checked-in synthetic input at the source-record boundary.

    A real source-native adapter supplies these methods after verifying its
    producer's release. This example pins its own local input bytes instead.
    """

    def __init__(self) -> None:
        raw = (INPUT_ROOT / "source.json").read_bytes()
        self.record = json.loads(raw)
        self.payload = (INPUT_ROOT / "notice.html").read_bytes()
        self.input_digest = sha256_digest(raw + self.payload)
        # The example records the shape it admits; this is not an official
        # Federal Register schema or an upstream qualification assertion.
        self.schema_digest = sha256_digest(
            canonical_json_file_bytes(
                {
                    "type": "object",
                    "required": sorted(self.record),
                }
            )
        )

    def describe(self) -> SourceNativeDescription:
        return SourceNativeDescription(
            logical_id="urn:docspec:example:source:" + self.input_digest,
            artifact_digest=self.input_digest,
            source_system_id=SOURCE_SYSTEM,
            source_system_version="v1",
            source_state_scope="complete-snapshot",
            source_state_digest=self.input_digest,
            source_native_schema_set_digest=self.schema_digest,
        )

    def iter_records(self):
        yield {
            "sourceRecordId": self.record["document_number"],
            "scopeId": "federal-register-documents",
            "schemaName": "federal-register-document",
            "schemaVersion": "1.0",
            "schemaDigest": self.schema_digest,
            "record": self.record,
            "fieldDiagnostics": [],
        }

    def iter_renditions(self):
        yield {
            "sourceRecordId": self.record["document_number"],
            "renditionId": self.record["document_number"] + "/html",
            "sourceField": "html_url",
            "locator": self.record["html_url"],
            "mediaType": "text/html",
            "expectedSha256": sha256_digest(self.payload),
            "expectedByteSize": len(self.payload),
        }


class ExampleFetcher:
    """Map exactly one synthetic URL to the pinned local source file."""

    downloader_id = "docspec.example.local-mapping/v1"

    def __init__(self, source: ExampleSource) -> None:
        self.url = source.record["html_url"]
        self.local = LocalFileContentFetcher(INPUT_ROOT)
        self.configuration_digest = sha256_digest(
            canonical_json_file_bytes(
                {
                    "url": self.url,
                    "inputDigest": source.input_digest,
                }
            )
        )

    def fetch(self, candidate: CandidateFile, **kwargs: Any) -> FetchStream:
        if candidate.locator != self.url:
            raise ValueError("candidate is outside the example's explicit local mapping")
        fetched = self.local.fetch(replace(candidate, locator="notice.html"), **kwargs)
        return FetchStream(
            replace(
                fetched.metadata,
                downloader_id=self.downloader_id,
                downloader_configuration_digest=self.configuration_digest,
                transport_version=candidate.transport_version,
                acquisition_started_at=COMPLETED_AT,
            ),
            fetched.chunks,
            fetched.close,
        )


def implementation_manifest() -> dict[str, str]:
    """Identify the actual local implementation, including uncommitted edits."""
    paths = [path for path in (REPO_ROOT / "src" / "docspec").rglob("*") if path.suffix in {".py", ".json"}]
    paths += [Path(__file__), REPO_ROOT / "uv.lock"]
    return {path.relative_to(REPO_ROOT).as_posix(): sha256_digest(path.read_bytes()) for path in sorted(paths)}


def run_command(command: list[str], result_path: Path) -> dict[str, Any]:
    """Retain the actual CLI result instead of printing a large release root."""
    with result_path.open("w", encoding="utf-8") as stream, redirect_stdout(stream):
        if cli_main(command):
            raise RuntimeError(f"example command failed: {' '.join(command[:2])}")
    return json.loads(result_path.read_text(encoding="utf-8"))


def run_example(output: Path) -> None:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    implementation = implementation_manifest()
    write_json(output / "implementation.json", implementation)
    implementation_id = "urn:docspec:example:implementation:" + sha256_digest(canonical_json_file_bytes(implementation))
    source_producer = Producer(
        "docspec", implementation_id, "urn:docspec:verifier:source-catalog", "1.0.0", implementation_id
    )
    release_producer = replace(source_producer, verifier_id="urn:docspec:verifier:document-release")
    source = ExampleSource()
    store = LocalSourceCatalogStore(output / "source-catalog")
    built = SourceCatalogBuilder(
        store=store,
        policy=FederalRegisterCatalogPolicy(SOURCE_SYSTEM),
        request=SourceCatalogBuildRequest("urn:docspec:example:catalog", source_producer),
        workspace_factory=SqliteCatalogPolicyWorkspace,
    ).build((source,))
    reader = SourceCatalogArtifactReader(store, producer=source_producer)
    reader.verify_snapshot(built.reference)
    write_json(output / "source-catalog-reference.json", built.reference.to_dict())

    profiles = ProfileRegistry.builtin().local_profiles()
    retry, accepted = RetryPolicy(), AcceptedFailurePolicy()
    plan = ProcessingPlan.create(
        source_catalog=built.reference,
        base_release=None,
        profiles=profiles,
        limits=WorkLimits(2, 1024 * 1024, 100, 100, 1000, 1024 * 1024, 60, retry.max_attempts),
        stages=StagePolicy((DefaultExtractorRegistry.extractor_id,), DefaultSegmenterRegistry.segmenter_id),
        processors=ProcessorSet(()),
        partition_count=2,
        selection={},
        retention_policy=RetentionPolicy.retain_all(),
        data_use_policy=DataUsePolicy.local_content(),
        retry_policy_digest=retry.digest,
        accepted_failure_policy_digest=accepted.digest,
    )
    write_json(output / "plan.json", plan.to_dict())
    roots = {
        name: str(output / name)
        for name in (
            "blobStorage",
            "controlRepository",
            "documentCatalog",
            "documentStores",
            "reconciliation",
            "recordStorage",
        )
    }
    roots.update(sourceCatalog=str(store.root), sourceContent=str(INPUT_ROOT))
    request = {
        "format": "docspec-local-run-request",
        "formatVersion": "1.0",
        "plan": str(output / "plan.json"),
        "profileDirectory": str(REPO_ROOT / "src" / "docspec" / "storage_profiles"),
        "roots": roots,
        "resultSinkId": "urn:docspec:example:sink",
        "partitionPolicyId": "source-item-sha256-v1",
        "retryPolicy": retry.to_dict(),
        "acceptedFailurePolicy": accepted.to_dict(),
        "execution": {"maxWorkers": 1, "maxInFlight": 1, "deadlineEpochSeconds": 4_000_000_000},
        "completedAt": COMPLETED_AT,
        "documentReleaseProducer": release_producer.as_dict(),
        "sourceCatalogProducer": source_producer.as_dict(),
    }
    write_json(output / "run-request.json", request)
    run = run_local(output / "run-request.json", content_fetcher=ExampleFetcher(source))
    write_json(output / "run-reference.json", run.to_dict())
    write_json(
        output / "commit-request.json",
        {
            "format": "docspec-local-release-commit-request",
            "formatVersion": "1.0",
            "runRequest": str(output / "run-request.json"),
            "runReceipt": str(output / "run-reference.json"),
            "baseRelease": None,
        },
    )
    command = [
        "document-release",
        "commit",
        "--request",
        str(output / "commit-request.json"),
        "--destination",
        str(output / "release-reference.json"),
        "--receipt",
        str(output / "commit-receipt.json"),
    ]
    run_command(command, output / "commit-result.json")
    command = [
        "document-catalog",
        "open",
        "--reference",
        str(output / "release-reference.json"),
        "--catalog-root",
        roots["documentCatalog"],
        "--record-root",
        roots["recordStorage"],
        "--blob-root",
        roots["blobStorage"],
        "--store-root",
        roots["documentStores"],
        "--control-root",
        roots["controlRepository"],
        "--implementation-id",
        implementation_id,
        "--verifier-implementation-id",
        implementation_id,
    ]
    verified = run_command(command, output / "verification.json")
    print(
        json.dumps(
            {
                "verdict": verified["verdict"],
                "recordCounts": {
                    layer["layerKind"]: layer["recordCount"] for layer in verified["release"]["activeLayers"]
                },
            },
            sort_keys=True,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new directory for all generated artifacts")
    args = parser.parse_args()
    try:
        run_example(args.output)
    except FileExistsError:
        parser.error("--output must name a new directory")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
