"""Shared cli fixtures, extracted from tests.test_cli."""

from __future__ import annotations

from pathlib import Path

from docspec.domain.identity import canonical_json_file_bytes
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.profiles import ProfileSet
from docspec.profile_registry import ProfileRegistry
from tests.helpers import (
    document_release_producer,
    source_catalog_producer,
)

REPO_ROOT = Path(__file__).parents[2]


def _portable_local_profiles() -> ProfileSet:
    return ProfileRegistry.builtin().local_profiles()


def _write_local_run_request(
    path: Path,
    *,
    plan_path: Path,
    roots: dict[str, str],
    result_sink_id: str,
    retry: RetryPolicy,
    accepted: AcceptedFailurePolicy,
    completed_at: str,
    partition_policy_id: str = "source-item-sha256-v1",
    max_workers: int = 1,
    max_in_flight: int = 1,
) -> Path:
    path.write_bytes(
        canonical_json_file_bytes(
            {
                "format": "docspec-local-run-request",
                "formatVersion": "1.0",
                "documentReleaseProducer": document_release_producer().as_dict(),
                "sourceCatalogProducer": source_catalog_producer().as_dict(),
                "plan": plan_path.as_posix(),
                "profileDirectory": (REPO_ROOT / "src" / "docspec" / "storage_profiles").as_posix(),
                "roots": roots,
                "resultSinkId": result_sink_id,
                "partitionPolicyId": partition_policy_id,
                "retryPolicy": retry.to_dict(),
                "acceptedFailurePolicy": accepted.to_dict(),
                "execution": {
                    "maxWorkers": max_workers,
                    "maxInFlight": max_in_flight,
                    "deadlineEpochSeconds": 2_000_000_000,
                },
                "completedAt": completed_at,
            }
        )
    )
    return path
