"""Exercise local workspace defaults through the command request boundary."""

import json
from pathlib import Path

import pytest

from docspec.cli.requests import _local_run_arguments, _local_run_request
from docspec.cli_io import CliError
from docspec.domain.identity import canonical_json_file_bytes
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.profile_registry import BUILTIN_PROFILE_DIRECTORY
from docspec.workspace import LocalWorkspace
from tests.helpers import document_release_producer, source_catalog_producer


def request_value(tmp_path: Path) -> dict:
    return {
        "format": "docspec-local-run-request",
        "formatVersion": "3.0",
        "workspace": str(tmp_path / "dataset"),
        "plan": str(tmp_path / "plan.json"),
        "retryPolicy": RetryPolicy().to_dict(),
        "acceptedFailurePolicy": AcceptedFailurePolicy().to_dict(),
        "execution": {"deadlineEpochSeconds": 4_000_000_000},
        "completedAt": "2026-09-11T12:00:00Z",
        "documentReleaseProducer": document_release_producer().as_dict(),
        "sourceCatalogProducer": source_catalog_producer().as_dict(),
    }


def read_request(tmp_path: Path, value: dict) -> dict:
    path = tmp_path / "request.json"
    path.write_bytes(canonical_json_file_bytes(value))
    return _local_run_request(path)


def test_request_derives_storage_and_execution_without_creating_dataset(tmp_path: Path) -> None:
    value = request_value(tmp_path)
    request = read_request(tmp_path, value)
    assert request["roots"] == {
        name: tmp_path / "dataset" / name
        for name in (
            "blobStorage", "controlRepository", "documentCatalog", "documentStores",
            "reconciliation", "recordStorage", "sourceCatalog", "sourceContent",
        )
    }
    assert request["profileDirectory"] == BUILTIN_PROFILE_DIRECTORY
    assert request["execution"]["maxWorkers"] == request["execution"]["maxInFlight"] == 1
    assert request["execution"]["deadlineEpochSeconds"] == 4_000_000_000
    assert request["sourceCatalogProducer"].as_dict() == value["sourceCatalogProducer"]
    assert request["documentReleaseProducer"].as_dict() == value["documentReleaseProducer"]
    assert request["acceptedFailurePolicy"].to_dict() == value["acceptedFailurePolicy"]
    assert not (tmp_path / "dataset").exists()


def test_overrides_and_worker_limit_are_explicit_reproducible_choices(tmp_path: Path) -> None:
    value = request_value(tmp_path)
    value.update(roots={"sourceContent": str(tmp_path / "inputs")}, profileDirectory=str(tmp_path / "profiles"))
    value["execution"]["maxWorkers"] = 3
    request = read_request(tmp_path, value)
    assert request["roots"]["sourceContent"] == tmp_path / "inputs"
    assert request["roots"]["blobStorage"] == tmp_path / "dataset" / "blobStorage"
    assert request["profileDirectory"] == tmp_path / "profiles"
    assert request["execution"]["maxInFlight"] == 3
    value["execution"]["maxInFlight"] = 2
    assert read_request(tmp_path, value)["execution"]["maxInFlight"] == 2


@pytest.mark.parametrize(("change", "message"), [
    ({"workspace": "relative"}, "absolute path"),
    ({"workspace": None}, "absolute path"),
    ({"roots": {"typo": "/somewhere"}}, "unknown storage names"),
    ({"roots": {"sourceContent": "relative"}}, "absolute path"),
    ({"profileDirectory": None}, "absolute path"),
    ({"execution": {"deadlineEpochSeconds": 10, "maxWorkers": True}}, "integer"),
    ({"execution": {"deadlineEpochSeconds": 10, "unknown": 1}}, "closed shape"),
    ({"execution": {"maxWorkers": 1}}, "closed shape"),
    ({"formatVersion": "1.0"}, "unknown format"),
])
def test_bad_configuration_refuses_before_state_creation(tmp_path: Path, change: dict, message: str) -> None:
    with pytest.raises(CliError, match=message):
        read_request(tmp_path, request_value(tmp_path) | change)
    assert not (tmp_path / "dataset").exists()


def test_verifier_acceptance_cannot_be_omitted_for_defaults(tmp_path: Path) -> None:
    value = request_value(tmp_path)
    del value["sourceCatalogProducer"]
    with pytest.raises(CliError, match="closed shape"):
        read_request(tmp_path, value)


def test_workspace_copies_override_configuration(tmp_path: Path) -> None:
    overrides = {"sourceContent": tmp_path / "original"}
    workspace = LocalWorkspace(tmp_path, overrides)
    overrides["sourceContent"] = tmp_path / "changed"
    roots = workspace.roots
    roots["sourceContent"] = tmp_path / "changed-again"
    assert workspace.roots["sourceContent"] == tmp_path / "original"


def test_implicit_and_explicit_defaults_resume_the_same_prepared_work(tmp_path: Path) -> None:
    from docspec.runtime import prepare_local_run
    from docspec.profile_registry import ProfileRegistry
    from tests.support.profiles import _seeded_local_run

    request_path, roots = _seeded_local_run(tmp_path, ProfileRegistry.builtin().local_profiles())
    value = json.loads(request_path.read_text())
    # The fixture supplies source inputs separately; local state takes defaults.
    value["roots"] = {name: roots[name] for name in ("sourceCatalog", "sourceContent")}
    for name in ("resultSinkId", "partitionPolicyId"):
        value.pop(name)
    for name in ("maxWorkers", "maxInFlight"):
        value["execution"].pop(name)
    implicit = read_request(tmp_path, value)
    prepared = prepare_local_run(**_local_run_arguments(implicit), resume=False)

    value.update(
        roots={name: str(path) for name, path in implicit["roots"].items()},
        profileDirectory=str(implicit["profileDirectory"]),
        resultSinkId=implicit["resultSinkId"],
        partitionPolicyId=implicit["partitionPolicyId"],
        execution=implicit["execution"],
    )
    explicit = _local_run_arguments(read_request(tmp_path, value))
    rebuilt = prepare_local_run(**explicit, resume=True)
    assert rebuilt.handoff_ref == prepared.handoff_ref
    assert rebuilt.execution_profile_ref == prepared.execution_profile_ref
    resumed = prepare_local_run(**explicit, handoff_ref=prepared.handoff_ref)
    assert resumed.handoff_ref == prepared.handoff_ref
    assert resumed.execution_profile_ref == prepared.execution_profile_ref
    assert resumed.handoff.planned_store_ledger == prepared.handoff.planned_store_ledger
