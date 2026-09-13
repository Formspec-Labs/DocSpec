"""Real local inputs shared by profile, runtime, and CLI tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.identity import canonical_json_file_bytes, sha256_digest
from docspec.domain.plans import ProcessingPlan, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, DataUsePolicy, RetentionPolicy, RetryPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.profiles import ProfileSet
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.runtime import local_execution_limits, stage_policy
from docspec.workspace import LocalWorkspace
from tests import helpers as _helpers
from tests.support import cli as _cli_helpers

write_shared_source_catalog = _helpers.write_shared_source_catalog


_write_local_run_request = _cli_helpers._write_local_run_request


def _seeded_local_run_arguments(tmp_path: Path, profiles: ProfileSet) -> dict[str, Any]:
    source_content = tmp_path / "source-content"
    source_content.mkdir()
    source_bytes = b"One conformance paragraph."
    (source_content / "document.txt").write_bytes(source_bytes)
    source_catalog_root = tmp_path / "source-catalog"
    source_ref = write_shared_source_catalog(
        source_catalog_root,
        (
            SourceItem(
                "document-a",
                "v1",
                (
                    CandidateFile(
                        "primary",
                        "document.txt",
                        "text/plain",
                        expected_digest=sha256_digest(source_bytes),
                        expected_size=len(source_bytes),
                        transport_version="fixture:v1",
                    ),
                ),
                metadata={"expectedSegments": 1},
            ),
        ),
    )
    retry = RetryPolicy()
    accepted = AcceptedFailurePolicy()
    processor = ContentStatisticsProcessor()
    plan = ProcessingPlan.create(
        source_catalog=source_ref,
        base_release=None,
        profiles=profiles,
        limits=WorkLimits(2, 1024 * 1024, 10, 10, 100, 1024 * 1024, 60, retry.max_attempts),
        stages=stage_policy(processor_ids=(processor.description.processor_id,)),
        processors=ProcessorSet((processor.description,)),
        partition_count=4,
        selection={},
        retention_policy=RetentionPolicy.retain_all(),
        data_use_policy=DataUsePolicy.local_content(),
        retry_policy_digest=retry.digest,
        accepted_failure_policy_digest=accepted.digest,
    )
    roots = {
        "blobStorage": (tmp_path / "blobs").as_posix(),
        "controlRepository": (tmp_path / "controls").as_posix(),
        "documentCatalog": (tmp_path / "catalog").as_posix(),
        "documentStores": (tmp_path / "stores").as_posix(),
        "reconciliation": (tmp_path / "reconciliation").as_posix(),
        "recordStorage": (tmp_path / "records").as_posix(),
        "sourceCatalog": source_catalog_root.as_posix(),
        "sourceContent": source_content.as_posix(),
    }
    return {
        "plan": plan,
        "workspace": LocalWorkspace(tmp_path, roots),
        "retry_policy": retry,
        "accepted_failure_policy": accepted,
        "source_catalog_producer": _helpers.source_catalog_producer(),
        "document_release_producer": _helpers.document_release_producer(),
        "execution_limits": local_execution_limits(),
        "deadline_epoch_seconds": 2_000_000_000,
        "completed_at": "2026-08-05T12:00:00Z",
        "partition_policy_id": "source-item-sha256-v1",
        "result_sink_id": "urn:docspec:test:sink:local-durable",
    }


def _seeded_local_run(tmp_path: Path, profiles: ProfileSet) -> tuple[Path, dict[str, str]]:
    arguments = _seeded_local_run_arguments(tmp_path, profiles)
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(canonical_json_file_bytes(arguments["plan"].to_dict()))
    roots = {name: path.as_posix() for name, path in arguments["workspace"].roots.items()}
    request = _write_local_run_request(
        tmp_path / "run-request.json",
        plan_path=plan_path,
        roots=roots,
        result_sink_id=arguments["result_sink_id"],
        partition_policy_id=arguments["partition_policy_id"],
        retry=arguments["retry_policy"],
        accepted=arguments["accepted_failure_policy"],
        completed_at=arguments["completed_at"],
    )
    return request, roots
